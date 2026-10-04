"""Real third-party libraries must work *inside* the hardened sandbox, and the hardening must stay
on while they do.

Security that breaks the code people actually run gets switched off, so this is a first-class
requirement, not an afterthought: every library here runs under `IsolatedRuntime` with its default
protections (jitless V8, the OS sandbox, `SharedArrayBuffer`/`Atomics`/`WeakRef` stripped), and its
result must equal what the plain in-process `Runtime` produces.

The bytes are vendored and pinned (`vendor/libs/README.md`); nothing is fetched.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import pathlib
import zipfile

import pytest

from pydeno import (
    WEB_POLYFILLS,
    IsolatedRuntime,
    JavaScriptError,
    Runtime,
    RuntimeConfig,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent / "vendor"
LIBS = ROOT / "libs"

PINS = {
    "d3-force-3.0.0-delaunay-6.0.4.bundle.js": "67e190242161066fea190c201ea2f97ea4d3d97fb1ba9f2f577f33d5dc5b97f7",
    "dagre.bundle.js": "ca109f634a32870d6865e6cb01702a3c8cca68eeb3dccde871aa031ef4b2dbd0",
    "echarts-6.1.0.min.js": "b66b25aeb4df84e33199dc21694014d336d222cbd9deb0e5a7c14bd6aa0d0fd0",
    "three-0.180.0-gltf.bundle.js": "b3faa3da4cf40d0fad9883002324ed35bfb0a57cbc4fdb1584f1df8065ba061a",
    "turf-7.4.0.bundle.js": "ab93309f52566b6cd998200485d4825be1c434c5c8f81a3e18dde0a92dd63940",
    "vega-6.4.0.min.js": "8f6a3587cf8d4f42c7e08120e3eb05d067e746d554e39d2dcf52acc0bd5ba28f",
    "vega-interpreter-2.3.2.bundle.js": "54d2c534de8f0b35e29db6170a4776e666847c9b88c5fd15d56489575c89abdb",
    "vega-lite-6.4.3.min.js": "35a9821df838825b05a6a73e9414b58747a1b18321583858ed903c66393a5c7e",
}

# name -> (files to evaluate in order, an async-friendly expression, what it must equal/contain)
CASES: dict[str, tuple[list[str], str, object]] = {
    "d3": (
        ["d3-force-3.0.0-delaunay-6.0.4.bundle.js"],
        """(() => {
          const nodes = [{id: 'a'}, {id: 'b'}, {id: 'c'}];
          const sim = d3.forceSimulation(nodes)
            .force('link', d3.forceLink([{source: 'a', target: 'b'}, {source: 'b', target: 'c'}])
              .id(d => d.id))
            .force('charge', d3.forceManyBody()).stop();
          for (let i = 0; i < 50; i++) sim.tick();
          const tri = d3.Delaunay.from([[0, 0], [1, 0], [0, 1], [1, 1]]).triangles.length;
          return [d3.scaleLinear().domain([0, 10]).range([0, 100])(5), tri,
                  nodes.every(n => Number.isFinite(n.x) && Number.isFinite(n.y))];
        })()""",
        [50, 6, True],
    ),
    "echarts-ssr": (
        ["echarts-6.1.0.min.js"],
        """(() => {
          const chart = echarts.init(null, null, {renderer: 'svg', ssr: true, width: 400, height: 300});
          chart.setOption({animation: false, xAxis: {type: 'category', data: ['a', 'b', 'c']},
                           yAxis: {type: 'value'}, series: [{type: 'bar', data: [1, 3, 2]}]});
          const svg = chart.renderToSVGString();
          chart.dispose();
          return [svg.startsWith('<svg'), svg.includes('<path'), svg.length > 1000];
        })()""",
        [True, True, True],
    ),
    "turf": (
        ["turf-7.4.0.bundle.js"],
        """(() => {
          const km = turf.distance(turf.point([0, 0]), turf.point([0, 1]), {units: 'kilometers'});
          const area = turf.area(turf.bboxPolygon([0, 0, 1, 1]));
          return [Math.round(km), Math.round(area / 1e9)];
        })()""",
        [111, 12],
    ),
    "dagre": (
        ["dagre.bundle.js"],
        """(() => {
          const g = new dagre.graphlib.Graph(); g.setGraph({}); g.setDefaultEdgeLabel(() => ({}));
          for (const n of ['a', 'b', 'c']) g.setNode(n, {width: 10, height: 10});
          g.setEdge('a', 'b'); g.setEdge('b', 'c');
          dagre.layout(g);
          return [g.node('a').y < g.node('b').y, g.node('b').y < g.node('c').y];
        })()""",
        [True, True],
    ),
    "three-gltf": (
        ["three-0.180.0-gltf.bundle.js"],
        """new Promise((resolve, reject) => {
          const scene = new THREE.Scene();
          scene.add(new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1),
                                   new THREE.MeshStandardMaterial({color: 0x336699})));
          new GLTFExporter().parse(scene, (glb) => {
            const head = new Uint8Array(glb, 0, 4);
            resolve(String.fromCharCode(...head) + ':' + (glb.byteLength > 500));
          }, reject, {binary: true});
        })""",
        "glTF:true",
    ),
    "vega-interpreter": (
        # Vega's expression interpreter, which never compiles strings: what `strict_eval=True`
        # needs. Parse with `{ast: true}` and pass `expr: vega.expressionInterpreter`.
        [
            "vega-6.4.0.min.js",
            "vega-lite-6.4.3.min.js",
            "vega-interpreter-2.3.2.bundle.js",
        ],
        """(async () => {
          const view = new vega.View(vega.parse({
            data: [{name: 'table', values: [{x: 1, y: 2}, {x: 2, y: 5}, {x: 3, y: 3}],
                    transform: [{type: 'filter', expr: 'datum.y > 2 && isNumber(datum.x)'},
                                {type: 'formula', as: 'z', expr: 'datum.x * 2 + PI - PI'}]}],
            signals: [{name: 'k', value: 4}, {name: 'k2', update: 'k * k + length("ab")'}],
          }, null, {ast: true}), {renderer: 'none', expr: vega.expressionInterpreter});
          await view.runAsync();
          const spec = {
            data: {values: [{x: 1, y: 2}, {x: 2, y: 5}, {x: 3, y: 3}]},
            transform: [{calculate: 'datum.y * 10', as: 'y10'}],
            mark: 'line',
            encoding: {x: {field: 'x', type: 'quantitative'}, y: {field: 'y10', type: 'quantitative'}},
          };
          const lite = new vega.View(vega.parse(vegaLite.compile(spec).spec, null, {ast: true}),
                                     {renderer: 'none', expr: vega.expressionInterpreter});
          const svg = await lite.toSVG();
          return [view.data('table').map(d => d.z), view.signal('k2'),
                  svg.startsWith('<svg'), svg.includes('<path')];
        })()""",
        [[4, 6], 18, True, True],
    ),
    "vega-lite": (
        ["vega-6.4.0.min.js", "vega-lite-6.4.3.min.js"],
        """(async () => {
          const spec = {
            $schema: 'https://vega.github.io/schema/vega-lite/v5.json',
            data: {values: [{x: 1, y: 2}, {x: 2, y: 5}, {x: 3, y: 3}]},
            mark: 'line',
            encoding: {x: {field: 'x', type: 'quantitative'}, y: {field: 'y', type: 'quantitative'}},
          };
          const view = new vega.View(vega.parse(vegaLite.compile(spec).spec), {renderer: 'none'});
          const svg = await view.toSVG();
          return [svg.startsWith('<svg'), svg.includes('<path'), svg.length > 2000];
        })()""",
        [True, True, True],
    ),
}


def _sources(files: list[str]) -> str:
    return "\n;\n".join((LIBS / name).read_text() for name in files)


# The libraries that work under `strict_eval=True` exactly as they are. Vega and Vega-Lite compile
# their expressions with `Function` and need the interpreter ("vega-interpreter") instead.
STRICT_CASES = sorted(set(CASES) - {"vega-lite"})


def _run(runtime: object, files: list[str], expr: str) -> object:
    async def go() -> object:
        rt = runtime
        await rt.eval_async(_sources(files), timeout=60)  # type: ignore[attr-defined]
        return await rt.eval_async(f"(async () => ({expr}))()", timeout=60)  # type: ignore[attr-defined]

    return asyncio.run(go())


def _config() -> RuntimeConfig:
    return RuntimeConfig(bootstrap=WEB_POLYFILLS)


@pytest.mark.parametrize("name", sorted(PINS))
def test_vendored_bytes_are_the_pinned_bytes(name: str) -> None:
    digest = hashlib.sha256((LIBS / name).read_bytes()).hexdigest()
    assert digest == PINS[name], f"{name} changed: sha256 {digest}"


@pytest.mark.parametrize("name", sorted(CASES))
def test_library_works_inside_the_hardened_sandbox(name: str) -> None:
    files, expr, expected = CASES[name]
    with IsolatedRuntime(_config()) as rt:
        assert _run(rt, files, expr) == expected


@pytest.mark.parametrize("name", sorted(CASES))
def test_library_result_matches_the_in_process_runtime(name: str) -> None:
    """Hardening must not change what a library computes."""
    files, expr, _ = CASES[name]
    with Runtime(_config()) as plain:
        in_process = _run(plain, files, expr)
    with IsolatedRuntime(_config()) as rt:
        assert _run(rt, files, expr) == in_process


@pytest.mark.parametrize("name", STRICT_CASES)
def test_library_works_with_strict_eval(name: str) -> None:
    """No code generation from strings in the guest, and the library computes the same."""
    files, expr, expected = CASES[name]
    with IsolatedRuntime(_config(), strict_eval=True) as rt:
        assert rt.strict_eval
        assert _run(rt, files, expr) == expected
        with pytest.raises(JavaScriptError, match="EvalError"):
            rt.eval("new Function('return 1')()")


def test_vega_without_the_interpreter_needs_code_generation() -> None:
    """Why `strict_eval=True` needs `vega-interpreter`: Vega's default expression path calls
    `Function`, which strict mode refuses."""
    files, expr, _ = CASES["vega-lite"]
    with IsolatedRuntime(_config(), strict_eval=True) as rt:
        with pytest.raises(JavaScriptError, match="Code generation from strings"):
            _run(rt, files, expr)


def test_the_libraries_run_with_jitless_and_with_the_jit_alike() -> None:
    files, expr, expected = CASES["dagre"]
    with IsolatedRuntime(_config(), jitless=False) as rt:
        assert _run(rt, files, expr) == expected


@pytest.mark.parametrize("strict_eval", [False, True])
def test_pptxgenjs_builds_a_valid_deck_inside_the_sandbox(strict_eval: bool) -> None:
    bundle = (ROOT / "pptxgenjs" / "pptxgen.bundle.js").read_text()
    code = """(async () => {
      const pptx = new PptxGenJS();
      for (let i = 0; i < 3; i++) {
        const slide = pptx.addSlide();
        slide.addText('Slide ' + i, {x: 1, y: 1, w: 5, h: 1, fontSize: 24});
        slide.addTable([[{text: 'a'}, {text: 'b'}], ['1', '2']], {x: 1, y: 2, w: 5});
      }
      return await pptx.write({outputType: 'base64'});
    })()"""

    async def go() -> str:
        with IsolatedRuntime(_config(), strict_eval=strict_eval) as rt:
            await rt.eval_async(
                bundle + "\n;0", timeout=60
            )  # a UMD bundle ends in a function
            return await rt.eval_async(code, timeout=60)

    raw = base64.b64decode(asyncio.run(go()))
    assert raw[:2] == b"PK"
    names = zipfile.ZipFile(io.BytesIO(raw)).namelist()
    assert "[Content_Types].xml" in names
    assert sum(n.startswith("ppt/slides/slide") for n in names) == 3


def test_hardening_is_still_on_after_every_library_has_loaded() -> None:
    probe = (
        "[typeof SharedArrayBuffer, typeof Atomics, typeof WeakRef, typeof FinalizationRegistry,"
        " typeof WebAssembly, typeof fetch, typeof process, typeof require, typeof Deno,"
        " typeof window, typeof document]"
    )
    with IsolatedRuntime(_config()) as rt:
        for files, _, _ in CASES.values():
            asyncio.run(rt.eval_async(_sources(files), timeout=60))
        assert rt.eval(probe) == ["undefined"] * 11
        assert "jitless" in " ".join(rt.v8_flags)
        assert rt.sandbox != "none" or not _expects_sandbox()


def _expects_sandbox() -> bool:
    import os
    import sys

    expected = os.environ.get("PYDENO_EXPECT_SANDBOX")
    if expected is not None:
        return expected != "none"
    return sys.platform == "darwin" or sys.platform.startswith("linux")


def test_a_library_cannot_use_its_cpu_budget_to_outlive_the_deadline() -> None:
    """A heavy library call is still bounded by the hard deadline."""
    from pydeno import RuntimeTimeout

    files, _, _ = CASES["dagre"]
    with IsolatedRuntime(_config(), request_timeout=3.0) as rt:
        asyncio.run(rt.eval_async(_sources(files), timeout=60))
        with pytest.raises(RuntimeTimeout):
            # Numeric on purpose: appending to a string here would grow without bound and trip the
            # memory ceiling before the deadline, which is a different (also contained) failure.
            rt.eval(
                "let s = 0; for (;;) { s += new dagre.graphlib.Graph().nodeCount() + 1 }"
            )


# --- the polyfills themselves (pure JS, so they are checked directly) ----------------------


@pytest.fixture
def poly():
    with Runtime(_config()) as rt:
        yield rt


def test_text_encoder_and_decoder_round_trip_multibyte_text(poly: Runtime) -> None:
    text = "héllo wörld ✓ 日本語 😀"
    assert (
        poly.eval(f"new TextDecoder().decode(new TextEncoder().encode({text!r}))")
        == text
    )
    assert poly.eval("Array.from(new TextEncoder().encode('é😀'))") == [
        195,
        169,
        240,
        159,
        152,
        128,
    ]
    assert poly.eval("new TextDecoder().decode(new Uint8Array([0xff, 0x41]))") == "�A"


def test_btoa_and_atob_match_the_standard(poly: Runtime) -> None:
    for plain in ("", "f", "fo", "foo", "foob", "fooba", "foobar"):
        assert (
            poly.eval(f"btoa({plain!r})") == base64.b64encode(plain.encode()).decode()
        )
        assert poly.eval(f"atob(btoa({plain!r}))") == plain


def test_timers_run_in_virtual_time_order_without_waiting(poly: Runtime) -> None:
    async def go() -> list[int]:
        return await poly.eval_async(
            "new Promise(r => { const o = []; setTimeout(() => o.push(3), 30);"
            " setTimeout(() => o.push(1), 10); setTimeout(() => o.push(2), 20);"
            " setTimeout(() => r(o), 100000); })",
            timeout=10,
        )

    assert asyncio.run(go()) == [1, 2, 3]  # and it did not take 100 seconds


def test_an_endless_interval_ends_instead_of_spinning_forever(poly: Runtime) -> None:
    async def go() -> int:
        return await poly.eval_async(
            "new Promise(r => { let n = 0; setInterval(() => { n++; }, 1);"
            " setTimeout(() => r(n), 5); })",
            timeout=10,
        )

    assert asyncio.run(go()) >= 4


def test_clear_timeout_cancels(poly: Runtime) -> None:
    async def go() -> list[str]:
        return await poly.eval_async(
            "new Promise(r => { const o = []; const t = setTimeout(() => o.push('no'), 1);"
            " clearTimeout(t); setTimeout(() => r(o), 5); })",
            timeout=10,
        )

    assert asyncio.run(go()) == []


def test_performance_now_is_a_virtual_counter_not_a_clock(poly: Runtime) -> None:
    """A real high-resolution timer is what side-channel attacks are built from."""
    assert poly.eval("performance.now()") == 0
    assert poly.eval("performance.now() === performance.now()") is True


def test_event_target_dispatches_to_listeners(poly: Runtime) -> None:
    assert (
        poly.eval(
            "(() => { const t = new EventTarget(); let got = null;"
            " t.addEventListener('x', e => { got = e.type }); t.dispatchEvent(new Event('x'));"
            " return got; })()"
        )
        == "x"
    )


def test_blob_text_round_trips(poly: Runtime) -> None:
    async def blob_text() -> str:
        return await poly.eval_async("new Blob(['héllo']).text()", timeout=10)

    assert asyncio.run(blob_text()) == "héllo"


def test_the_polyfills_do_not_define_a_dom(poly: Runtime) -> None:
    """Defining `window` or `document` makes libraries take their browser code paths, which
    then fail in far less obvious ways than a missing global does."""
    assert (
        poly.eval("[typeof window, typeof document, typeof navigator]")
        == ["undefined"] * 3
    )
    assert poly.eval("[self === globalThis, global === globalThis]") == [True, True]


def test_the_polyfills_do_not_replace_what_is_already_there(poly: Runtime) -> None:
    with Runtime(
        RuntimeConfig(bootstrap="globalThis.btoa = () => 'mine';" + WEB_POLYFILLS)
    ) as rt:
        assert rt.eval("btoa('x')") == "mine"
