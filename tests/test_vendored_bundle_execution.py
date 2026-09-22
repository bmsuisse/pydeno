"""Regression guard: a real, large, third-party JS bundle still runs in pydeno.

What this protects
------------------
Three properties, together. Any one of them alone is close to worthless as a
guard, which is why they live in one file:

1. **Large-bundle evaluation.** 460,889 bytes of real, minified,
   deeply-nested third-party JavaScript compiles and evaluates in one
   `eval()` call, and its UMD preamble successfully installs its globals.
   This is not the same property as `tests/test_large_script_eval.py`, which
   evaluates half a megabyte of `var x=0;` filler: that exercises source
   *size* against V8's streaming-compilation / delayed-task path, while this
   exercises size *and* real parser, scope and closure complexity, plus the
   bundle's own top-level side effects.

2. **The host polyfill surface.** The three browser globals in
   `vendor/pptxgenjs/polyfills.js` behave correctly -- in particular a
   `setTimeout` that genuinely defers to the microtask queue and genuinely
   fires.

3. **JSZip's async `write()` completing.** The end-to-end path
   (polyfills -> 461 KB bundle -> build a six-slide deck -> zip it -> base64
   it) resolves its promise and hands back bytes that are a structurally
   valid OOXML package.

Property 3 is the one with real history, and it is why a synthetic stand-in
was rejected for this file. The failure mode lives inside JSZip's async
pipeline and is unreachable with a synthetic payload. Measured at pptxgenjs
4.0.1, building the same deck:

                                 | global/self absent | global/self present
    setTimeout = fn => {}        |       HANGS        |       HANGS
    setTimeout = fn => fn()      |       works        |       HANGS
    Promise.resolve().then(fn)   |       works        |       works

JSZip drives `write()` through `setImmediate`, and the bundled
`setimmediate`/`immediate` shims pick the object they bind their scheduler to
with `typeof self` / `typeof global` probes -- so the polyfills are COUPLED,
and whether a synchronous `setTimeout` gets away with it depends on the rest
of the polyfill set. `test_synchronous_settimeout_is_only_accidentally_safe`
pins that middle row from both sides, which is the least guessable fact in
the whole pattern and the one most likely to be "simplified" away.

Hermeticity
-----------
Everything this test needs is in the repo: the pinned bundle and the
polyfills are under `vendor/pptxgenjs/` (see the README there for provenance
and why vendoring won over a network fetch), and validation uses only the
standard library -- `zipfile`, `re`, `base64`. There is **no network access
and no skip condition anywhere in this file**; it either runs and passes, or
runs and fails. `test_vendored_bundle_is_the_pinned_bytes` makes deleting or
swapping the asset a loud failure rather than a quiet loss of coverage.
"""

import asyncio
import base64
import hashlib
import pathlib
import re
import zipfile
from io import BytesIO

import pytest
from pydeno import Runtime

# --- the pinned asset ------------------------------------------------------

VENDOR = pathlib.Path(__file__).resolve().parent.parent / "vendor" / "pptxgenjs"
BUNDLE_PATH = VENDOR / "pptxgen.bundle.js"
POLYFILLS_PATH = VENDOR / "polyfills.js"

PPTXGENJS_VERSION = "4.0.1"
BUNDLE_BYTES = 460_889
BUNDLE_SHA256 = "4fb9eac5cfefb213e2d8743c2b7151025f31bfb3f834c73c12062916daa0f3f8"

# A no-op and a synchronous setTimeout. Both hang JSZip's write(); see the
# module docstring. Used to prove the real polyfill is load-bearing.
BROKEN_SETTIMEOUT_SHIMS = {
    "no-op": "globalThis.setTimeout=function(){return 0};"
    "globalThis.clearTimeout=function(){};",
    "synchronous": "globalThis.setTimeout=function(fn){fn();return 0};"
    "globalThis.clearTimeout=function(){};",
}

# Deck generation completes in ~0.1s locally. A hang is unbounded, so this
# only has to be far enough above the working case to be unambiguous.
HANG_TIMEOUT = 5.0
BUILD_TIMEOUT = 120.0


def _polyfills() -> str:
    return POLYFILLS_PATH.read_text()


def _bundle() -> str:
    return BUNDLE_PATH.read_text()


# --- the deck --------------------------------------------------------------
#
# Six slides covering the feature surface that touches the parts of pptxgenjs
# most likely to break: text, a table, a plain chart, a multi-series chart, a
# combo chart on a secondary axis, and an embedded raster image.
#
# Two pptxgenjs footguns are deliberately avoided here, and asserted against
# below, because both produce a file that passes a naive "is it a zip?" check
# while being broken for real consumers:
#
#   * pptxgenjs MUTATES the options object it is handed -- it normalises
#     units and rewrites colour keys in place. Every add* call below therefore
#     gets a FRESH object literal; sharing one leaks state between shapes.
#   * `secondaryValAxis`/`secondaryCatAxis` require BOTH `valAxes` and
#     `catAxes` with two entries each. With only `valAxes` you get two
#     `<c:valAx>` and one `<c:catAx>`, and PowerPoint discards the chart and
#     reports the file as corrupt.

DECK_SCRIPT_TEMPLATE = """
const pres = new PptxGenJS();
pres.layout = "LAYOUT_WIDE";
pres.title = "pydeno vendored bundle regression deck";

const s1 = pres.addSlide();
s1.background = { color: "1F2A44" };
s1.addText("Vendored pptxgenjs in pydeno", {
  x: 0.6, y: 2.0, w: 12.1, h: 1.0,
  fontSize: 34, bold: true, color: "FFFFFF", align: "center",
});
s1.addNotes("Speaker notes exercise a separate OOXML part.");

const s2 = pres.addSlide();
s2.addText("Revenue by region", {
  x: 0.5, y: 0.35, w: 8, h: 0.6, fontSize: 24, bold: true, color: "1F2A44",
});
s2.addTable(
  [
    [
      { text: "Region", options: { bold: true, color: "FFFFFF", fill: { color: "1F2A44" } } },
      { text: "FY24", options: { bold: true, color: "FFFFFF", fill: { color: "1F2A44" } } },
      { text: "FY25", options: { bold: true, color: "FFFFFF", fill: { color: "1F2A44" } } },
    ],
    ["EMEA", "1.20M", "1.44M"],
    ["APAC", "0.90M", "1.17M"],
    ["AMER", "1.60M", "1.76M"],
  ],
  {
    x: 0.5, y: 1.2, w: 7.5, colW: [2.5, 2.5, 2.5],
    border: { pt: 1, color: "C8D0E0" }, fontSize: 14,
  }
);

const s3 = pres.addSlide();
s3.addText("Bar chart", {
  x: 0.5, y: 0.35, w: 10, h: 0.6, fontSize: 24, bold: true, color: "1F2A44",
});
s3.addChart(
  pres.ChartType.bar,
  [{ name: "Revenue", labels: ["Q1", "Q2", "Q3", "Q4"], values: [10, 14, 13, 18] }],
  {
    x: 0.5, y: 1.2, w: 12.0, h: 5.0,
    showTitle: true, title: "Quarterly revenue", titleColor: "1F2A44", titleFontSize: 16,
    showLegend: true, legendPos: "b", legendColor: "363636",
    chartColors: ["4472C4"],
    catAxisLabelColor: "363636", valAxisLabelColor: "363636",
    valAxisMinVal: 0, valAxisMaxVal: 20,
    valGridLine: { style: "solid", size: 1, color: "E6E9F0" },
  }
);

const s4 = pres.addSlide();
s4.addText("Line chart", {
  x: 0.5, y: 0.35, w: 10, h: 0.6, fontSize: 24, bold: true, color: "1F2A44",
});
s4.addChart(
  pres.ChartType.line,
  [
    { name: "Sessions", labels: ["Jan", "Feb", "Mar", "Apr", "May"], values: [4, 6, 9, 11, 15] },
    { name: "Signups", labels: ["Jan", "Feb", "Mar", "Apr", "May"], values: [1, 2, 4, 5, 8] },
  ],
  {
    x: 0.5, y: 1.2, w: 12.0, h: 5.0,
    showTitle: true, title: "Funnel trend", titleColor: "1F2A44", titleFontSize: 16,
    showLegend: true, legendPos: "b", legendColor: "363636",
    chartColors: ["4472C4", "ED7D31"],
    lineDataSymbol: "circle", lineSize: 2,
    catAxisLabelColor: "363636", valAxisLabelColor: "363636",
    valAxisMinVal: 0, valAxisMaxVal: 16,
    valGridLine: { style: "solid", size: 1, color: "E6E9F0" },
  }
);

const s5 = pres.addSlide();
s5.addText("Combo chart", {
  x: 0.5, y: 0.35, w: 10, h: 0.6, fontSize: 24, bold: true, color: "1F2A44",
});
s5.addChart(
  [
    {
      type: pres.ChartType.bar,
      data: [{ name: "Units", labels: ["Q1", "Q2", "Q3", "Q4"], values: [120, 150, 140, 190] }],
      options: { chartColors: ["4472C4"], barGrouping: "clustered" },
    },
    {
      type: pres.ChartType.line,
      data: [{ name: "Margin %", labels: ["Q1", "Q2", "Q3", "Q4"], values: [12, 15, 14, 21] }],
      options: {
        chartColors: ["ED7D31"],
        secondaryValAxis: true,
        secondaryCatAxis: true,
        lineDataSymbol: "circle",
        lineSize: 2,
      },
    },
  ],
  {
    x: 0.5, y: 1.2, w: 12.0, h: 5.0,
    showTitle: true, title: "Units vs margin", titleColor: "1F2A44", titleFontSize: 16,
    showLegend: true, legendPos: "b", legendColor: "363636",
    valAxes: [
      {
        showValAxisTitle: true, valAxisTitle: "Units",
        valAxisLabelColor: "363636", valAxisTitleColor: "363636",
        valAxisMinVal: 0, valAxisMaxVal: 200,
        valGridLine: { style: "solid", size: 1, color: "E6E9F0" },
      },
      {
        showValAxisTitle: true, valAxisTitle: "Margin %",
        valAxisLabelColor: "363636", valAxisTitleColor: "363636",
        valAxisMinVal: 0, valAxisMaxVal: 25,
        valGridLine: { style: "none" },
      },
    ],
    catAxes: [
      { catAxisTitle: "Quarter", catAxisLabelColor: "363636" },
      { catAxisHidden: true },
    ],
  }
);

const s6 = pres.addSlide();
s6.addText("Embedded image", {
  x: 0.5, y: 0.35, w: 10, h: 0.6, fontSize: 24, bold: true, color: "1F2A44",
});
s6.addImage({ data: "image/png;base64,__IMAGE_B64__", x: 0.5, y: 1.3, w: 6.0, h: 4.0 });

pres.write({ outputType: "base64" });
"""

TINY_DECK_SCRIPT = """
const pres = new PptxGenJS();
const s = pres.addSlide();
s.addText("hello", { x: 1, y: 1, w: 4, h: 1 });
pres.write({ outputType: "base64" });
"""

# A real 8x8 RGB PNG, built once with zlib so the test stays dependency-free.
# Round-tripping a genuine raster through the bundle is the point: a
# corrupted decode would still zip, but would not survive the PNG signature
# and CRC checks below.


def _tiny_png() -> bytes:
    import struct
    import zlib

    width = height = 8
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter: none
        for x in range(width):
            raw += bytes((x * 32 % 256, y * 32 % 256, 0x8C))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def _deck_script() -> str:
    return DECK_SCRIPT_TEMPLATE.replace(
        "__IMAGE_B64__", base64.b64encode(_tiny_png()).decode()
    )


async def _build_deck(
    script: str, polyfills: str | None = None, timeout: float = BUILD_TIMEOUT
) -> bytes:
    with Runtime() as rt:
        rt.eval(polyfills if polyfills is not None else _polyfills())
        rt.eval(_bundle())
        b64 = await rt.eval_async(script, timeout=timeout)
    return base64.b64decode(b64)


@pytest.fixture(scope="module")
def deck() -> bytes:
    """The six-slide deck, built once (~0.1s) and shared by the assertions."""
    return asyncio.run(_build_deck(_deck_script()))


# --- 0. the pinned asset itself -------------------------------------------


def test_vendored_bundle_is_the_pinned_bytes() -> None:
    """Deleting or swapping the asset must fail loudly, not silently unlist
    the coverage this file provides. This is the guard that makes the rest of
    the file trustworthy: if the bundle ever goes missing, or is quietly
    upgraded without updating the constants, you find out here."""
    assert BUNDLE_PATH.is_file(), f"vendored bundle missing: {BUNDLE_PATH}"
    assert POLYFILLS_PATH.is_file(), f"polyfill file missing: {POLYFILLS_PATH}"

    raw = BUNDLE_PATH.read_bytes()
    assert len(raw) == BUNDLE_BYTES, (
        f"vendored bundle is {len(raw):,} bytes, expected {BUNDLE_BYTES:,}. "
        "If this was an intentional pptxgenjs upgrade, update the constants "
        "here and the table in vendor/pptxgenjs/README.md."
    )
    assert hashlib.sha256(raw).hexdigest() == BUNDLE_SHA256, (
        "vendored bundle content changed; see vendor/pptxgenjs/README.md"
    )
    assert raw.startswith(f"/* PptxGenJS {PPTXGENJS_VERSION} @".encode()), (
        "bundle header does not match the pinned version -- the bundle must "
        "stay byte-identical to the published tarball, never patched"
    )


def test_bare_isolate_has_none_of_the_browser_globals() -> None:
    """Documents *why* the polyfills exist. If a future pydeno starts shipping
    these itself, this fails and the polyfill file can be trimmed."""
    with Runtime() as rt:
        for name in ("window", "global", "self", "setTimeout", "atob", "btoa"):
            assert rt.eval(f"typeof {name}") == "undefined", (
                f"expected a bare isolate to lack `{name}`"
            )


# --- 1. the polyfill surface ----------------------------------------------


def test_polyfill_settimeout_defers_rather_than_running_inline() -> None:
    """The callback must NOT have run by the time setTimeout returns. A
    synchronous shim passes a naive "does the callback fire?" test but hangs
    JSZip, so ordering is the property that matters."""
    with Runtime() as rt:
        rt.eval(_polyfills())
        order = rt.eval(
            "(() => { const o = []; setTimeout(() => o.push('cb'));"
            " o.push('sync'); return o.join(','); })()"
        )
    assert order == "sync", "setTimeout ran its callback inline"


async def test_polyfill_settimeout_callback_actually_fires() -> None:
    with Runtime() as rt:
        rt.eval(_polyfills())
        assert (
            await rt.eval_async(
                "new Promise(r => setTimeout(() => r('fired'), 0))", timeout=10.0
            )
            == "fired"
        )
        assert await rt.eval_async(
            "(async () => { const o = [];"
            " setTimeout(() => o.push(1)); setTimeout(() => o.push(2));"
            " await new Promise(r => setTimeout(r));"
            " return o.join(','); })()",
            timeout=10.0,
        ) in ("1,2", "1,2,")


async def test_polyfill_cleartimeout_cancels() -> None:
    with Runtime() as rt:
        rt.eval(_polyfills())
        assert (
            await rt.eval_async(
                "(async () => { const o = [];"
                " const id = setTimeout(() => o.push('cancelled'));"
                " clearTimeout(id);"
                " setTimeout(() => o.push('kept'));"
                " await new Promise(r => setTimeout(r));"
                " await new Promise(r => setTimeout(r));"
                " return o.join(','); })()",
                timeout=10.0,
            )
            == "kept"
        )


def test_polyfill_base64_round_trips_binary() -> None:
    """Spec behaviour, not just "returns a string": padding, the full byte
    range, and rejection of out-of-range code units."""
    with Runtime() as rt:
        rt.eval(_polyfills())
        for text in ("", "a", "ab", "abc", "abcd", "hello world"):
            expected = base64.b64encode(text.encode()).decode()
            assert rt.eval(f"btoa({text!r})") == expected, f"btoa({text!r})"
            assert rt.eval(f"atob({expected!r})") == text, f"atob for {text!r}"

        # Every byte value 0..255 survives a round trip.
        assert (
            rt.eval(
                "(() => { let s = '';"
                " for (let i = 0; i < 256; i++) s += String.fromCharCode(i);"
                " return atob(btoa(s)) === s; })()"
            )
            is True
        )
        assert (
            rt.eval(
                "(() => { try { btoa('\\u0100'); return 'no-throw'; }"
                " catch (e) { return 'threw'; } })()"
            )
            == "threw"
        )


def test_polyfill_global_and_self_alias_globalthis() -> None:
    with Runtime() as rt:
        rt.eval(_polyfills())
        assert rt.eval("global === globalThis") is True
        assert rt.eval("self === globalThis") is True


# --- 2. large-bundle evaluation -------------------------------------------


def test_large_bundle_evaluates_and_installs_its_umd_globals() -> None:
    """461 KB of real minified third-party JS in a single eval(), and the UMD
    preamble's global-assignment branch actually lands."""
    source = _bundle()
    assert len(source.encode()) == BUNDLE_BYTES

    with Runtime() as rt:
        rt.eval(_polyfills())
        rt.eval(source)
        # JSZip is installed by the UMD preamble; PptxGenJS by the outer IIFE.
        assert rt.eval("typeof JSZip") == "function", "UMD global branch failed"
        assert rt.eval("typeof PptxGenJS") == "function", "PptxGenJS missing"
        # ...and the class is really usable, not just a bound name.
        assert rt.eval("typeof new PptxGenJS().addSlide") == "function"


def test_large_bundle_can_still_call_a_host_function() -> None:
    """A 461 KB script must not disturb the Python<->JS bridge."""
    seen = []
    with Runtime() as rt:
        rt.eval(_polyfills())
        rt.eval(_bundle())
        rt.bind_function("record", lambda v: seen.append(v) or v * 2)
        assert rt.eval("record(21)") == 42
    assert seen == [21]


# --- 3. JSZip's async write() completing ----------------------------------


def _with_broken_settimeout(shim_name: str) -> str:
    """The real polyfill file with its setTimeout replaced by a broken shim."""
    return (
        _polyfills().replace(
            "globalThis.setTimeout = function (fn, _delay, ...args) {",
            "globalThis.__unused = function (fn, _delay, ...args) {",
        )
        + BROKEN_SETTIMEOUT_SHIMS[shim_name]
    )


@pytest.mark.parametrize("shim_name", sorted(BROKEN_SETTIMEOUT_SHIMS))
async def test_write_hangs_without_a_deferring_settimeout(shim_name: str) -> None:
    """The load-bearing negative.

    With the polyfill file's own `global`/`self` section in place -- i.e. the
    configuration this repo actually ships -- both a no-op and a synchronous
    `setTimeout` leave `pres.write()`'s promise pending forever. Measured at
    pptxgenjs 4.0.1.

    This is the entire reason the polyfill is backed by
    `Promise.resolve().then()`. Without this test, someone could "simplify"
    it to the obvious one-liner and every other assertion in this file would
    still pass -- except the deck build, which would hang rather than fail.
    """
    with pytest.raises(RuntimeError, match="timed out|still pending"):
        await _build_deck(
            TINY_DECK_SCRIPT,
            polyfills=_with_broken_settimeout(shim_name),
            timeout=HANG_TIMEOUT,
        )


async def test_synchronous_settimeout_is_only_accidentally_safe() -> None:
    """Pins the *coupling* between polyfill sections 1 and 2, which is the
    least guessable thing in this whole pattern.

    JSZip drives `write()` through `setImmediate`, and the bundled
    `setimmediate`/`immediate` shims choose the object they bind their
    scheduler to with `typeof self` / `typeof global` probes -- in the bundle:

        (r = "undefined"==typeof self ? void 0===e ? this : e : self).setImmediate || ...

    With none of those globals defined, a synchronous `setTimeout` happens to
    work. Define `global`/`self` -- which is exactly what you do when
    onboarding a bundle that feature-detects them -- and the same shim hangs.

    So the two assertions below are deliberately contradictory-looking: the
    identical broken shim passes without section 1 and hangs with it. That is
    the trap, and it is why the shipped `setTimeout` must be microtask-backed
    rather than merely "good enough for the deck I tried".
    """
    sync_only = BROKEN_SETTIMEOUT_SHIMS["synchronous"]
    b64_section = _polyfills()[_polyfills().index("(function () {\n  const B64") :]

    # Without global/self: the synchronous shim gets away with it.
    raw = await _build_deck(
        TINY_DECK_SCRIPT, polyfills=sync_only + b64_section, timeout=HANG_TIMEOUT
    )
    assert raw[:2] == b"PK"

    # Add global/self and nothing else changes -- yet now it hangs.
    with pytest.raises(RuntimeError, match="timed out|still pending"):
        await _build_deck(
            TINY_DECK_SCRIPT,
            polyfills="globalThis.global=globalThis;globalThis.self=globalThis;"
            + sync_only
            + b64_section,
            timeout=HANG_TIMEOUT,
        )


@pytest.mark.parametrize(
    "extra_globals",
    [
        "",
        "globalThis.self=globalThis;",
        "globalThis.global=globalThis;",
        "globalThis.global=globalThis;globalThis.self=globalThis;",
    ],
    ids=["none", "self", "global", "global+self"],
)
async def test_deferring_settimeout_works_for_every_global_configuration(
    extra_globals: str,
) -> None:
    """The positive counterpart to the coupling test: the microtask-backed
    shim is correct in all four configurations, so it is safe to ship
    alongside any combination of the other polyfills."""
    polyfills = _polyfills()
    # Strip the file's own global/self section, then re-add the variant.
    polyfills = polyfills.replace(
        "globalThis.global = globalThis;\nglobalThis.self = globalThis;", ""
    )
    raw = await _build_deck(TINY_DECK_SCRIPT, polyfills=extra_globals + polyfills)
    assert raw[:2] == b"PK"


async def test_write_completes_with_the_real_polyfill() -> None:
    """The positive counterpart, on the same tiny deck the negative uses, so
    the two differ only in the setTimeout shim."""
    raw = await _build_deck(TINY_DECK_SCRIPT)
    assert raw[:2] == b"PK", "write() did not return a zip"
    assert len(raw) > 10_000


# --- 4. the produced deck is a real OOXML package -------------------------


def test_deck_is_a_valid_zip_with_the_expected_parts(deck: bytes) -> None:
    zf = zipfile.ZipFile(BytesIO(deck))
    assert zf.testzip() is None, "corrupt member in the .pptx zip"

    names = zf.namelist()
    assert "[Content_Types].xml" in names
    assert "ppt/presentation.xml" in names

    slides = sorted(n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
    charts = sorted(n for n in names if re.fullmatch(r"ppt/charts/chart\d+\.xml", n))
    media = sorted(
        n for n in names if n.startswith("ppt/media/") and not n.endswith("/")
    )

    assert len(slides) == 6, f"expected 6 slides, got {slides}"
    assert len(charts) == 3, f"expected 3 chart parts, got {charts}"
    assert len(media) == 1, f"expected 1 media part, got {media}"

    # Every slide has a relationship part -- a deck that loses these opens blank.
    for slide in slides:
        rel = slide.replace("ppt/slides/", "ppt/slides/_rels/") + ".rels"
        assert rel in names, f"missing relationships for {slide}"


def test_deck_embedded_image_survived_byte_exact(deck: bytes) -> None:
    """The PNG went in as base64 and must come out as a valid PNG: signature,
    IEND terminator, and a CRC-checkable IHDR."""
    zf = zipfile.ZipFile(BytesIO(deck))
    media = [
        n for n in zf.namelist() if n.startswith("ppt/media/") and not n.endswith("/")
    ]
    raw = zf.read(media[0])
    assert raw == _tiny_png(), "embedded image is not byte-identical to the source PNG"


def test_deck_table_and_notes_parts_exist(deck: bytes) -> None:
    zf = zipfile.ZipFile(BytesIO(deck))
    names = zf.namelist()
    assert any(n.startswith("ppt/notesSlides/notesSlide") for n in names), (
        "addNotes() produced no notesSlide part"
    )
    # The table lives inline in its slide as a graphicFrame/tbl.
    tables = [
        n
        for n in names
        if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)
        and "<a:tbl>" in zf.read(n).decode()
    ]
    assert len(tables) == 1, f"expected exactly 1 slide with a table, got {tables}"


def test_combo_chart_has_matching_val_and_cat_axes(deck: bytes) -> None:
    """The `secondaryValAxis` footgun. Two `<c:valAx>` with only one
    `<c:catAx>` is the shape PowerPoint rejects as a corrupt file -- and it
    still passes a plain zip-integrity check, which is why this is asserted
    on the XML."""
    zf = zipfile.ZipFile(BytesIO(deck))
    charts = {
        n: zf.read(n).decode()
        for n in zf.namelist()
        if re.fullmatch(r"ppt/charts/chart\d+\.xml", n)
    }

    secondary = {n: x for n, x in charts.items() if x.count("<c:valAx>") == 2}
    assert len(secondary) == 1, (
        f"expected exactly 1 chart with a secondary value axis, got {sorted(secondary)}"
    )
    name, xml = secondary.popitem()
    assert xml.count("<c:catAx>") == 2, (
        f"{name} has 2 <c:valAx> but {xml.count('<c:catAx>')} <c:catAx>; "
        "PowerPoint will discard this chart and report the file as corrupt"
    )
    # Both series really are present, on their respective axes.
    assert "Units" in xml and "Margin %" in xml


def test_chart_colours_are_literal_hex_not_scheme_names(deck: bytes) -> None:
    """`<a:srgbClr val="accent1"/>` is invalid OOXML -- `srgbClr` takes six
    hex digits. It is what you get by putting a theme-scheme name in a
    pptxgenjs colour option, it passes every structural check, and LibreOffice
    renders it as bright cyan."""
    zf = zipfile.ZipFile(BytesIO(deck))
    for name in zf.namelist():
        if not name.startswith(("ppt/charts/", "ppt/slides/")) or not name.endswith(
            ".xml"
        ):
            continue
        for val in re.findall(r'<a:srgbClr val="([^"]*)"', zf.read(name).decode()):
            assert re.fullmatch(r"[0-9A-Fa-f]{6}", val), (
                f"{name}: invalid srgbClr val={val!r}; a scheme name leaked into "
                "a literal colour slot"
            )


def test_chart_value_axes_declare_both_bounds(deck: bytes) -> None:
    """`valAxisMaxVal` without a matching min lets the renderer choose its own
    minimum, which is how an all-positive series ends up on an axis running
    negative. Every value axis here sets both, so every `<c:max>` must have a
    sibling `<c:min>`."""
    zf = zipfile.ZipFile(BytesIO(deck))
    for name in zf.namelist():
        if not re.fullmatch(r"ppt/charts/chart\d+\.xml", name):
            continue
        xml = zf.read(name).decode()
        for axis in re.findall(r"<c:valAx>.*?</c:valAx>", xml, re.S):
            assert ("<c:max " in axis or "<c:max/>" in axis) == (
                "<c:min " in axis or "<c:min/>" in axis
            ), f"{name}: a value axis sets only one of <c:min>/<c:max>"


def test_chart_plot_area_has_no_stray_solid_outline(deck: bytes) -> None:
    """A solid line on `c:plotArea/c:spPr` draws a dark frame around the plot
    that no chart option asked for."""
    zf = zipfile.ZipFile(BytesIO(deck))
    for name in zf.namelist():
        if not re.fullmatch(r"ppt/charts/chart\d+\.xml", name):
            continue
        xml = zf.read(name).decode()
        match = re.search(r"<c:plotArea>.*?<c:spPr>(.*?)</c:spPr>", xml, re.S)
        if match:
            assert "<a:ln>" not in match.group(1) or "<a:noFill/>" in match.group(1), (
                f"{name}: plotArea/spPr carries a visible outline"
            )
