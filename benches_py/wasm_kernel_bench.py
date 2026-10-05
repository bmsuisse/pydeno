"""One numeric kernel as JavaScript and as WebAssembly (`load_wasm`), in the same runtime.

    python benches_py/wasm_kernel_bench.py [N]

The kernel (i32 arithmetic, wrapping): ``acc = ((acc + i * i) | 0) ^ (i >>> 3)`` for ``i < N``.
Written once in JavaScript and once as hand-assembled WebAssembly, so no toolchain is needed. Each
is called from Python, with the call overhead included, on `Runtime` and on
`IsolatedRuntime(jitless=False)`, and on the default jitless worker for the JavaScript (the one
setting that has no WebAssembly). Prints the median of 15 runs in milliseconds.
"""

from __future__ import annotations

import statistics
import sys
import time

from pydeno import IsolatedRuntime, Runtime, RuntimeConfig

JS = """(n) => {
  let acc = 0;
  for (let i = 0; i < n; i++) acc = ((acc + Math.imul(i, i)) | 0) ^ (i >>> 3);
  return acc;
}"""

# (func (export "kernel") (param $n i32) (result i32) (local $i i32) (local $acc i32) ...)
WASM = bytes.fromhex(
    "0061736d01000000"
    "01060160017f017f"  # type: (i32) -> i32
    "03020100"  # function 0: type 0
    "070a01066b65726e656c0000"  # export "kernel"
    "0a2e012c"  # code section, one body of 0x2c bytes
    "01027f"  # locals: 2 x i32 ($i, $acc)
    "0240"  # block
    "0340"  # loop
    "200120004f0d01"  # br_if 1 (i >= n, unsigned)
    "2002200120016c6a"  # acc + i * i
    "20014103767321"  # ^ (i >>> 3)
    "02"  # local.set $acc
    "20014101"  # i + 1
    "6a2101"  # local.set $i
    "0c00"  # br 0
    "0b0b"  # end loop, end block
    "20020b"  # local.get $acc; end
)


def median_ms(fn, runs: int = 15) -> float:  # type: ignore[no-untyped-def]
    fn()  # warm up (and JIT)
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000_000
    config = RuntimeConfig(timeout=60.0)
    rows = []
    with Runtime(config) as rt:
        js = rt.eval(JS)
        wasm = rt.load_wasm(WASM)
        assert js(n) == wasm.call("kernel", n)
        rows.append(("Runtime", "JavaScript", median_ms(lambda: js(n))))
        rows.append(
            ("Runtime", "WebAssembly", median_ms(lambda: wasm.call("kernel", n)))
        )
    with IsolatedRuntime(config, jitless=False) as rt:
        rt.eval(f"globalThis.kernel = {JS}; 0")
        wasm = rt.load_wasm(WASM)
        rows.append(
            (
                "IsolatedRuntime(jitless=False)",
                "JavaScript",
                median_ms(lambda: rt.eval(f"kernel({n})")),
            )
        )
        rows.append(
            (
                "IsolatedRuntime(jitless=False)",
                "WebAssembly",
                median_ms(lambda: wasm.call("kernel", n)),
            )
        )
    with IsolatedRuntime(config) as rt:
        rt.eval(f"globalThis.kernel = {JS}; 0")
        rows.append(
            (
                "IsolatedRuntime() (jitless)",
                "JavaScript",
                median_ms(lambda: rt.eval(f"kernel({n})"), runs=5),
            )
        )
    print(f"kernel over N = {n:,}; median ms")
    for runtime, kind, ms in rows:
        print(f"  {runtime:32} {kind:12} {ms:9.1f}")


if __name__ == "__main__":
    main()
