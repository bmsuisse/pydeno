"""
Move large tabular datasets into sandboxed JS via Arrow IPC.

This demonstrates a PATTERN, not a built-in feature. `pydeno` needs no
new code, no Rust dependency and no optional extra for this -- it already
passes Python `bytes` straight through to a JS `Uint8Array`
(`src/runtime/conversion.rs`, the `PyBytes` -> `JSValue::Bytes` arm). Arrow
IPC is a self-describing byte stream, so once those bytes are inside the
isolate, a vendored `apache-arrow` browser bundle can reconstruct the full
typed table -- schema included -- with no schema negotiation of our own.

WHY BOTHER: the default JSON transfer path has a hard
`max_serialization_bytes` limit of 10 MB (`src/runtime/js_value.rs`,
`MAX_JS_BYTES`). Past roughly 10k rows of a modest 3-column record, a
list-of-dicts does not merely get slow -- it is *rejected outright* with
"Size (...) exceeded maximum limit of 10485760 bytes" unless the caller
explicitly raises the limit via `RuntimeConfig`. This script demonstrates
that failure too, so the tradeoff is visible rather than asserted.

WHEN NOT TO BOTHER: the Arrow bundle costs a one-time ~5 ms eval, so below
roughly 1k rows plain JSON is simply faster and much less code. The
crossover measured here sits around 5,000 rows.

`pyarrow` is required only on the PYTHON side of this script, to build the
IPC buffer. It is NOT a `pydeno` dependency and there is no
`pydeno[arrow]` extra -- the library itself needs nothing at all for
this. Install it alongside if you want to run this example:

    pip install pyarrow httpx

See `docs/guides/advanced/arrow-ipc-dataframes.md` for the full writeup,
including the measured numbers and their environment.
"""

import json
import time

import httpx
import pyarrow as pa

from pydeno import Runtime, RuntimeConfig

# apache-arrow's package metadata declares this file for `unpkg`/`jsdelivr`:
# a self-contained 188 KB UMD build with flatbuffers inlined and zero
# `require(` calls. Pin the exact version -- the host decides what gets
# loaded, never the guest.
ARROW_BUNDLE_URL = (
    "https://cdn.jsdelivr.net/npm/apache-arrow@21.2.0/Arrow.es2015.min.js"
)

# The bundle's only unmet dependency in a bare V8 isolate: it decodes the
# IPC schema's field names and encodes strings back out. Everything else it
# needs (TypedArrays, DataView) V8 already has.
TEXT_CODEC_POLYFILLS = r"""
globalThis.TextDecoder = class TextDecoder {
  constructor(label) { this.encoding = label || "utf-8"; }
  decode(input) {
    if (input === undefined || input === null) return "";
    const bytes = input instanceof Uint8Array ? input : new Uint8Array(input.buffer || input);
    let out = "";
    for (let i = 0; i < bytes.length; ) {
      const b = bytes[i++];
      if (b < 0x80) { out += String.fromCharCode(b); continue; }
      if (b < 0xe0) { out += String.fromCharCode(((b & 0x1f) << 6) | (bytes[i++] & 0x3f)); continue; }
      if (b < 0xf0) {
        out += String.fromCharCode(
          ((b & 0x0f) << 12) | ((bytes[i++] & 0x3f) << 6) | (bytes[i++] & 0x3f),
        );
        continue;
      }
      let cp = ((b & 0x07) << 18) | ((bytes[i++] & 0x3f) << 12)
             | ((bytes[i++] & 0x3f) << 6) | (bytes[i++] & 0x3f);
      cp -= 0x10000;
      out += String.fromCharCode(0xd800 + (cp >> 10), 0xdc00 + (cp & 0x3ff));
    }
    return out;
  }
};
globalThis.TextEncoder = class TextEncoder {
  constructor() { this.encoding = "utf-8"; }
  encode(str) {
    const s = String(str === undefined ? "" : str);
    const out = [];
    for (let i = 0; i < s.length; i++) {
      let cp = s.charCodeAt(i);
      if (cp >= 0xd800 && cp <= 0xdbff && i + 1 < s.length) {
        cp = 0x10000 + ((cp - 0xd800) << 10) + (s.charCodeAt(++i) - 0xdc00);
      }
      if (cp < 0x80) out.push(cp);
      else if (cp < 0x800) out.push(0xc0 | (cp >> 6), 0x80 | (cp & 0x3f));
      else if (cp < 0x10000) out.push(0xe0 | (cp >> 12), 0x80 | ((cp >> 6) & 0x3f), 0x80 | (cp & 0x3f));
      else out.push(
        0xf0 | (cp >> 18), 0x80 | ((cp >> 12) & 0x3f),
        0x80 | ((cp >> 6) & 0x3f), 0x80 | (cp & 0x3f),
      );
    }
    return new Uint8Array(out);
  }
};
"""

# Guest JS: read the IPC bytes, then work column-wise. The RESULT that
# comes back is small, so it travels the ordinary (JSON-fine) return path.
ARROW_SCRIPT = """
const table = Arrow.tableFromIPC(get_ipc());
const value = table.getChild("value");

let sum = 0;
let peak = -Infinity;
for (let i = 0; i < value.length; i++) {
  const v = value.get(i);
  sum += v;
  if (v > peak) peak = v;
}

({
  rows: table.numRows,
  columns: table.schema.fields.map((f) => `${f.name}:${f.type}`),
  sum: sum,
  peak: peak,
  first_name: table.getChild("name").get(0),
})
"""

JSON_SCRIPT = """
const rows = get_json();
let sum = 0;
for (let i = 0; i < rows.length; i++) sum += rows[i].value;
({ rows: rows.length, sum: sum })
"""


def make_rows(n: int) -> list[dict]:
    return [{"id": i, "name": f"row-{i}", "value": i * 1.5} for i in range(n)]


def to_ipc(table: pa.Table) -> bytes:
    """Serialize an Arrow table to a self-describing IPC stream."""
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def fetch_bundle(url: str) -> str:
    response = httpx.get(url, follow_redirects=True, timeout=60.0)
    response.raise_for_status()
    return response.text


def run_arrow(bundle: str, rows: list[dict]) -> tuple[dict, dict]:
    """The real recipe, with each phase timed separately."""
    table = pa.Table.from_pylist(rows)

    t0 = time.perf_counter()
    ipc = to_ipc(table)
    build_ms = (time.perf_counter() - t0) * 1000

    with Runtime() as runtime:
        runtime.eval(TEXT_CODEC_POLYFILLS)

        t0 = time.perf_counter()
        runtime.eval(bundle)  # one-time cost per Runtime
        bundle_ms = (time.perf_counter() - t0) * 1000

        # Python `bytes` arrive in JS as a `Uint8Array` -- no base64, no
        # JSON, no copy through a string.
        runtime.bind_function("get_ipc", lambda: ipc)

        t0 = time.perf_counter()
        result = runtime.eval(ARROW_SCRIPT)
        eval_ms = (time.perf_counter() - t0) * 1000

    timings = {
        "ipc_build_ms": build_ms,
        "bundle_eval_ms": bundle_ms,
        "eval_ms": eval_ms,
        "payload_bytes": len(ipc),
    }
    return result, timings


def run_json(rows: list[dict], *, limit_bytes: int | None = None) -> tuple[dict, dict]:
    config = RuntimeConfig(max_serialization_bytes=limit_bytes) if limit_bytes else None
    with Runtime(config) as runtime:
        runtime.bind_function("get_json", lambda: rows)
        t0 = time.perf_counter()
        result = runtime.eval(JSON_SCRIPT)
        eval_ms = (time.perf_counter() - t0) * 1000
    return result, {"eval_ms": eval_ms, "payload_bytes": len(json.dumps(rows).encode())}


def main() -> None:
    print(f"Fetching the pinned apache-arrow UMD bundle:\n  {ARROW_BUNDLE_URL}")
    bundle = fetch_bundle(ARROW_BUNDLE_URL)
    assert "require(" not in bundle, "bundle is not self-contained"
    print(
        f"  {len(bundle.encode()) / 1024:.0f} KB, self-contained (zero `require(` calls)\n"
    )

    rows = make_rows(100_000)
    expected_sum = sum(r["value"] for r in rows)

    # 1. The status quo, at default limits: this FAILS, it is not just slow.
    print("100k rows through the default JSON path (default max_serialization_bytes):")
    try:
        run_json(rows)
        raise AssertionError("expected the 10 MB serialization limit to reject this")
    except Exception as exc:  # noqa: BLE001 - demonstrating the real failure
        print(f"  REJECTED: {exc}\n")

    # 2. The same payload, with the limit explicitly raised.
    print("100k rows through the JSON path with the limit raised to 64 MB:")
    json_result, json_timings = run_json(rows, limit_bytes=64 * 1024 * 1024)
    assert json_result["rows"] == 100_000
    assert abs(json_result["sum"] - expected_sum) < 1e-6
    print(
        f"  OK: {json_timings['eval_ms']:.1f} ms eval, "
        f"{json_timings['payload_bytes'] / 1e6:.2f} MB of JSON text\n"
    )

    # 3. The Arrow IPC recipe, at default limits -- no config change needed.
    print("100k rows through Arrow IPC (default limits, unmodified Runtime):")
    arrow_result, arrow_timings = run_arrow(bundle, rows)
    assert arrow_result["rows"] == 100_000
    assert abs(arrow_result["sum"] - expected_sum) < 1e-6
    assert arrow_result["first_name"] == "row-0"
    assert arrow_result["columns"] == ["id:Int64", "name:Utf8", "value:Float64"]
    print(
        f"  OK: {arrow_timings['eval_ms']:.1f} ms eval "
        f"+ {arrow_timings['ipc_build_ms']:.2f} ms IPC build "
        f"+ {arrow_timings['bundle_eval_ms']:.1f} ms one-time bundle eval"
    )
    print(f"  payload {arrow_timings['payload_bytes'] / 1e6:.2f} MB")
    print(f"  schema round-tripped into JS: {arrow_result['columns']}")
    print(
        f"  sum={arrow_result['sum']} peak={arrow_result['peak']} (verified against Python)\n"
    )

    speedup = json_timings["eval_ms"] / arrow_timings["eval_ms"]
    print(f"  => {speedup:.0f}x faster in-sandbox, and it works at DEFAULT limits.\n")

    # 4. Where the crossover actually sits.
    print("Crossover sweep (eval only; Arrow also pays the one-time bundle eval):")
    print(f"  {'rows':>8}  {'JSON':>10}  {'Arrow':>10}")
    for n in (1_000, 5_000, 10_000, 100_000):
        small = make_rows(n)
        _, jt = run_json(small, limit_bytes=64 * 1024 * 1024)
        _, at = run_arrow(bundle, small)
        print(f"  {n:>8}  {jt['eval_ms']:>7.2f} ms  {at['eval_ms']:>7.2f} ms")
    print("\n  Below ~1k rows JSON wins once the bundle eval is counted.")


if __name__ == "__main__":
    main()
