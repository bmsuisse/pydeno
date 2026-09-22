# Moving large tabular datasets in with Arrow IPC

The ordinary way to hand data to sandboxed JS is to pass a Python object and
let `pydeno` convert it -- a list of dicts becomes an array of objects,
and that is the right answer most of the time. It stops being the right
answer once the table gets big: the conversion is per-value, it materializes
every row as a JS object, and it runs into a hard size limit.

This page covers a pattern for the big case: serialize the table **once**,
Python-side, into an [Arrow IPC](https://arrow.apache.org/docs/format/Columnar.html#serialization-and-interprocess-communication-ipc)
byte stream, pass those bytes straight through, and let a vendored
`apache-arrow` browser bundle reconstruct the typed table inside the
isolate. Arrow IPC is self-describing -- the schema travels in the stream --
so there is no schema negotiation to write on either side.

**`pydeno` needs no new code for this.** The `PyBytes` arm of
`src/runtime/conversion.rs` already maps Python `bytes` to `JSValue::Bytes`,
which surfaces in JS as a `Uint8Array`. There is no Rust dependency to add,
no optional extra to install, and nothing to enable -- the numbers below
were measured against an unmodified release build of `main` (the
development state between v0.2.1 and v0.3.0).

## When to use this -- and when not to

Read this section first; most callers do not need this pattern.

**Below roughly 1,000 rows, don't.** The Arrow bundle costs a one-time
~5 ms `eval()` per `Runtime`, which is more than the entire JSON transfer
of a small table. Plain Python objects are faster *and* far less code.

**The crossover measured here sits around 5,000 rows.** At 5k, the Arrow
path wins on `eval` time by roughly 4x, which is just enough to pay back
the bundle eval within a single call. Below that it doesn't.

**Past roughly 10,000 rows, the JSON path does not merely get slow -- it
fails.** `max_serialization_bytes` defaults to 10 MB (`MAX_JS_BYTES` in
`src/runtime/js_value.rs`), and that limit is accounted against the
transfer, not against the JSON text you'd get from `json.dumps`. A 100k-row
table of `{id, name, value}` is only 5.4 MB of JSON text but is rejected
outright:

```
RuntimeError: Serialization size (10485790 bytes) exceeded the configured limit
of 10485760 bytes (RuntimeConfig(max_serialization_bytes=...)); see
docs/guides/advanced/arrow-ipc-dataframes.md for transferring large payloads
```

The caller's only options are to raise the limit explicitly via
`RuntimeConfig(max_serialization_bytes=...)` -- which also means accepting
the memory cost in the table below -- or to move the bytes some other way.
That failure, more than the speed, is why this recipe is worth writing down.

The limit is symmetric: it caps what one call transfers in **either**
direction, aggregated across all of a call's arguments, so a host tool
receiving several large arguments from guest JS is bounded by the same
number. (Through v0.2.0 it was enforced outbound only, which meant it
inconvenienced the trusted side and did not constrain the untrusted one.)

## The recipe

### 1. Pin the bundle

`apache-arrow`'s package metadata declares `Arrow.es2015.min.js` for both
`unpkg` and `jsdelivr`. It is a self-contained 188 KB UMD build with
flatbuffers inlined and zero `require(` calls -- exactly the shape the
[vendored npm libraries](vendored-npm-libraries.md) pattern wants. Pin the
version; the host decides what gets loaded, never the guest.

```python
ARROW_BUNDLE_URL = "https://cdn.jsdelivr.net/npm/apache-arrow@21.2.0/Arrow.es2015.min.js"
```

### 2. Polyfill `TextDecoder` / `TextEncoder`

That is the bundle's only unmet dependency in a bare V8 isolate -- it
decodes the IPC schema's field names and encodes strings back out.
Everything else it needs (`TypedArray`, `DataView`) V8 already has. The
polyfill is about 40 lines of straightforward UTF-8 work; see
`examples/arrow_ipc_dataframes.py` for the full text.

```python
TEXT_CODEC_POLYFILLS = r"""
globalThis.TextDecoder = class TextDecoder { /* ~20 lines of UTF-8 decode */ };
globalThis.TextEncoder = class TextEncoder { /* ~20 lines of UTF-8 encode */ };
"""
```

### 3. Build the IPC buffer Python-side

```python
import pyarrow as pa

def to_ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()
```

If you already have an Arrow table (from Parquet, a database driver, Polars
via `.to_arrow()`), this is essentially free. If you are starting from a
list of dicts, `pa.Table.from_pylist` adds a real cost -- about 21 ms for
100k rows here -- and it is still an order of magnitude ahead overall.

### 4. Pass the bytes and read them JS-side

```python
from pydeno import Runtime

with Runtime() as runtime:
    runtime.eval(TEXT_CODEC_POLYFILLS)
    runtime.eval(bundle_source)               # one-time, ~5 ms
    runtime.bind_function("get_ipc", lambda: ipc_bytes)

    result = runtime.eval("""
      const table = Arrow.tableFromIPC(get_ipc());
      const value = table.getChild("value");

      let sum = 0;
      for (let i = 0; i < value.length; i++) sum += value.get(i);

      ({ rows: table.numRows, sum: sum })   // small -- JSON is fine here
    """)
```

`get_ipc()` returns a `Uint8Array`: no base64, no JSON, no round trip
through a string. `tableFromIPC` reconstructs the full typed table,
including the schema -- in the example, `id:Int64, name:Utf8, value:Float64`
arrive in JS without either side declaring them.

The **return** value goes back over the ordinary conversion path, and that
is deliberate. This recipe is for getting a lot of data *in*; the answer
coming back out is normally an aggregate, a filtered handful of rows, or a
chart spec, all of which are comfortably within JSON's limits. If your
result is also large, build an IPC stream in JS with `Arrow.tableToIPC` and
return that `Uint8Array` -- it arrives in Python as `bytes`.

## Measured numbers

Single machine, one process per measurement, Arrow path at **default**
limits and the JSON path with `max_serialization_bytes` raised to 64 MB so
it can run at all above 10k rows. Rows are `{id: int, name: str, value:
float}`; the JS work is a full column scan summing `value`.

| Rows | JSON eval | JSON payload | JSON peak RSS | Arrow eval | Arrow payload | Arrow peak RSS |
|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 3.6 ms | 0.05 MB | 64 MB | 2.4 ms | 0.03 MB | 69 MB |
| 5,000 | 12.7 ms | 0.25 MB | 74 MB | 2.8 ms | 0.14 MB | 72 MB |
| 10,000 | 25.6 ms | 0.51 MB | 86 MB | 2.9 ms | 0.28 MB | 74 MB |
| 100,000 | 253.5 ms | 5.40 MB | 264 MB | 5.0 ms | 2.89 MB | 126 MB |
| 500,000 | 1386.7 ms | 28.20 MB | 791 MB | 11.0 ms | 14.89 MB | 339 MB |

Costs not in the table: building the IPC buffer takes 0.1 ms at 1k rows and
4.7 ms at 500k, and the bundle `eval` is a one-time 5.1-5.8 ms per
`Runtime`. Starting from a list of dicts rather than an existing Arrow
table adds `pa.Table.from_pylist` -- 21.5 ms at 100k rows, so about 31 ms
end to end against 253 ms.

!!! note "These numbers are machine-dependent"
    Measured on macOS 15 / Apple Silicon, CPython 3.14, a `--release` build
    of `pydeno` 0.3.0, `pyarrow` 25.0.1 and `apache-arrow` 21.2.0.
    Treat them as one data point about the *shape* of the curve -- roughly
    flat for Arrow, roughly linear for JSON -- not as universal figures.
    Re-run `examples/arrow_ipc_dataframes.py` to get your own; it prints
    the same sweep. Run-to-run variance within a single process was
    noticeable (up to 2x on the JSON path), which is another reason to
    trust the shape over the digits.

At the two ends of that table: 100k rows is 253 ms and 264 MB versus 5 ms
and 126 MB, and 500k rows is 1.4 s versus 11 ms. Both of the large JSON
rows also required raising the serialization limit by hand.

## `pyarrow` is a *your-side* dependency

`pyarrow` appears in this recipe only to build the IPC buffer, on the
Python side, in your own code. It is **not** a `pydeno` dependency and
`pydeno` never imports it. The library's side of this pattern is the
`bytes` -> `Uint8Array` conversion it already does.

There is no `pydeno[arrow]` extra, and none is planned. Anything that can
produce Arrow IPC bytes works just as well -- Polars, DuckDB's
`.arrow()`/`fetch_record_batch()`, a Parquet reader, or a byte stream that
arrived over the network already in IPC format. If you already hold IPC
bytes, you can skip `pyarrow` entirely and pass them through.

## What this is NOT

This is a **manual recipe, not a polished API**. There is no
`Runtime(dataframe=...)`, no `runtime.send_table(...)`, and no automatic
Arrow bundle management. The host writes its own polyfill, fetches or
vendors its own pinned bundle, serializes its own table, and binds its own
accessor function -- all four steps, every time. Everything on this page is
assembled from pieces that already exist.

Shipping a real helper (a bundled, version-pinned Arrow runtime plus a
`Table`-shaped convenience API) is plausible future work, but it does not
exist today, and this page does not describe a feature that is on its way.
As with [vendored npm libraries](vendored-npm-libraries.md), the sandbox
guarantees are unchanged: the guest never chooses what gets loaded, and the
bytes it receives are whatever the host decided to serialize.
