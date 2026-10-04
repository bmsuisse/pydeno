"""Resource-containment tests modelled on pydantic/monty's security suite.

Monty (https://github.com/pydantic/monty, `docs/security.md`,
`crates/monty/tests/{security,resource_limits,source_nesting}.rs`) promises:

1. a memory budget that covers what the guest can allocate,
2. guest code cannot take the host process down (workers are subprocesses),
3. a wedged guest can always be stopped.

pydeno runs V8 in-process, so (2) and (3) hold only for allocations and loops
that V8 can interrupt. Every probe below runs in a *child* process so a host
abort is observed as a result instead of killing pytest.

Status classes:

- `TestOffHeapMemoryIsBudgeted` -- fixed; must keep passing.
- `TestKnownUncontainedNativeSinks` -- strict xfail. Single native builtins
  that allocate or loop without an interrupt check. V8 honours
  `TerminateExecution` only at interrupt points, so no in-process guard closes
  the class; only process isolation does. A strict xfail flips to a failure
  the day one of these starts passing, which is the signal to move it above.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from pydeno import JavaScriptError, Runtime, RuntimeConfig

MIB = 1024 * 1024

_CHILD = textwrap.dedent(
    """
    import sys
    from pydeno import Runtime, RuntimeConfig
    rt = Runtime(RuntimeConfig(max_heap_size=64 * 1024 * 1024, timeout=1.5))
    try:
        rt.eval(sys.stdin.read())
        print("VALUE")
    except BaseException as e:
        print("ERR", type(e).__name__)
    """
)


def _probe(js: str, wall: float = 12.0) -> str:
    """Run `js` in a child runtime. Returns 'VALUE', 'ERR <Type>', 'CRASH', or 'HANG'."""
    try:
        p = subprocess.run(
            [sys.executable, "-c", _CHILD],
            input=js,
            text=True,
            capture_output=True,
            timeout=wall,
        )
    except subprocess.TimeoutExpired:
        return "HANG"
    if p.returncode != 0:
        return "CRASH"
    return p.stdout.strip().splitlines()[-1]


def _contained(outcome: str) -> bool:
    return outcome.startswith("ERR ")


def _capped(cap: int = 64 * MIB) -> Runtime:
    return Runtime(RuntimeConfig(max_buffer_bytes=cap))


class TestOffHeapMemoryIsBudgeted:
    """`max_buffer_bytes` bounds ArrayBuffer-backed memory.

    Root cause class: V8 charges `ArrayBuffer` / `SharedArrayBuffer` storage to
    the embedder's allocator, not the JS heap, so `max_heap_size` alone leaves
    gigabytes of host RAM reachable. Monty's `max_memory` budgets every byte
    the guest requests from the allocator.
    """

    @pytest.mark.parametrize(
        "js",
        [
            "new ArrayBuffer(4 * 1024 ** 3)",
            "new Uint8Array(2 * 1024 ** 3)",
            "new SharedArrayBuffer(2 * 1024 ** 3)",
            "new ArrayBuffer(2 ** 20).transfer(2 ** 32)",
        ],
    )
    def test_single_oversized_buffer_is_refused(self, js: str) -> None:
        with pytest.raises(
            JavaScriptError, match="(?i)allocation failed|invalid|range"
        ):
            _capped().eval(js)

    def test_many_buffers_are_budgeted_in_aggregate(self) -> None:
        with pytest.raises(JavaScriptError):
            _capped().eval(
                "const a = []; for (let i = 0; i < 64; i++) a.push(new ArrayBuffer(16 * 1024 ** 2));"
            )

    def test_unreachable_buffers_return_their_budget(self) -> None:
        """The cap is a live budget: V8 collects and retries on a refused allocation."""
        rt = _capped()
        for _ in range(8):
            assert rt.eval("new ArrayBuffer(32 * 1024 ** 2).byteLength") == 32 * MIB

    def test_a_normal_sized_buffer_still_works(self) -> None:
        assert _capped().eval("new Uint8Array(1024 * 1024).fill(7)[1023]") == 7

    def test_a_guest_that_swallows_the_refusal_is_stopped_by_the_timeout(self) -> None:
        """The refusal is catchable, so exhaustion alone does not end the guest;
        `timeout=` does."""
        rt = Runtime(RuntimeConfig(max_buffer_bytes=8 * MIB, timeout=1.0))
        with pytest.raises(RuntimeError, match="(?i)timed out"):
            rt.eval(
                "const a = []; for (;;) { try { a.push(new Uint8Array(1 << 20)) } catch (e) {} }"
            )

    def test_op_results_are_not_charged_to_an_exhausted_budget(self) -> None:
        """Host bytes handed to the guest use their own backing store, so a full
        guest budget must not abort the process when an op returns bytes."""
        rt = _capped(8 * MIB)
        rt.bind_function("blob", lambda: b"x" * 1024)
        rt.eval(
            "const a = []; try { for (;;) a.push(new Uint8Array(1 << 20)) } catch (e) {}"
        )
        assert rt.eval("blob().length") == 1024

    def test_heap_limit_alone_does_not_cap_buffers(self) -> None:
        """`max_heap_size` keeps its old meaning: a 10 MB heap can still hold a
        larger typed array. Buffers have their own knob."""
        rt = Runtime(RuntimeConfig(max_heap_size=10 * MIB))
        assert rt.eval("new Uint8Array(16 * 1024 ** 2).byteLength") == 16 * MIB

    def test_no_cap_by_default(self) -> None:
        assert (
            Runtime(RuntimeConfig()).eval("new ArrayBuffer(8 * 1024 ** 2).byteLength")
            == 8 * MIB
        )

    def test_the_option_is_validated_and_readable(self) -> None:
        assert RuntimeConfig().max_buffer_bytes is None
        assert RuntimeConfig(max_buffer_bytes=MIB).max_buffer_bytes == MIB
        with pytest.raises(ValueError):
            RuntimeConfig(max_buffer_bytes=0)


class TestKnownOffHeapResiduals:
    """Memory V8 reserves through its page allocator, bypassing the `ArrayBuffer` allocator.

    The only V8 knobs are process-global flags (`--wasm-max-mem-pages`, ...),
    which would cap every runtime in the process and break legitimate WASM use,
    so they are deliberately not set. Pages are reserved, then committed as the
    guest touches them.
    """

    # strict=False: the reservation itself depends on OS overcommit / ulimit -v.
    @pytest.mark.xfail(reason="WebAssembly.Memory bypasses the ArrayBuffer allocator")
    def test_wasm_memory_is_budgeted(self) -> None:
        rt = _capped()
        with pytest.raises(JavaScriptError):
            rt.eval("new WebAssembly.Memory({initial: 65536})")

    def test_resizable_buffer_growth_is_budgeted(self) -> None:
        """No longer a residual: the bridge charges resizable buffers to the same budget."""
        rt = _capped()
        with pytest.raises(JavaScriptError):
            rt.eval("new ArrayBuffer(8, {maxByteLength: 2 ** 33}).resize(2 ** 33)")


# A refused buffer allocation must leave nothing behind for a later, genuine heap overflow.
# V8 retries a refused allocation (GC, retry, GC, retry, last-resort GC, which calls the
# near-heap-limit callback, then a final attempt), so the final attempt used to re-flag the
# refusal after the callback had consumed it. The next real heap overflow then took that stale
# flag for a refusal, handed V8 its limit back unchanged, and V8 aborted the process
# (FatalProcessOutOfMemory) in a few percent of runs. Many rounds, in a subprocess, so an abort
# fails the test instead of the suite.
_REFUSAL_THEN_OVERFLOW = textwrap.dedent(
    """
    from pydeno import Runtime, RuntimeConfig
    MIB = 1024 * 1024
    outcomes = {{}}
    for i in range({rounds}):
        rt = Runtime(RuntimeConfig(max_heap_size=48 * MIB, max_buffer_bytes=8 * MIB))
        # Twice: a second refusal must still be a catchable RangeError, not a termination.
        refused = rt.eval(
            "(() => {{ const out = []; for (let k = 0; k < 2; k++) {{"
            " try {{ new ArrayBuffer(16 * 2 ** 20); out.push('allocated') }}"
            " catch (e) {{ out.push(e.name) }} }} return out.join('+') }})()"
        )
        try:
            rt.eval("const a = []; for (;;) a.push(new Array(1e5).fill(1.5))")
            overflow = "returned"
        except Exception as exc:
            overflow = "heap" if "Heap limit exceeded" in str(exc) else type(exc).__name__
        key = (refused, overflow)
        outcomes[key] = outcomes.get(key, 0) + 1
        try:
            rt.close()
        except Exception:
            pass
    print(sorted(outcomes.items()))
    """
)


def test_a_refused_buffer_does_not_disarm_a_later_heap_overflow() -> None:
    done = subprocess.run(
        [sys.executable, "-c", _REFUSAL_THEN_OVERFLOW.format(rounds=300)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert done.returncode == 0, (done.returncode, done.stderr[-600:])
    assert done.stdout.strip() == "[(('RangeError+RangeError', 'heap'), 300)]", (
        done.stdout
    )


_SINKS = {
    "array_fill_max_length": "new Array(2 ** 32 - 1).fill(0).length",
    "array_from_max_length": "Array.from({length: 2 ** 32 - 1}).length",
    "length_set_then_fill": "const a = []; a.length = 2 ** 32 - 1; a.fill(1).length",
    "regex_match_flood": "'a'.repeat(2 ** 28).match(/a/g).length",
    "split_flood": "'a'.repeat(2 ** 28).split('').length",
    "sparse_sort": "const a = []; a[2 ** 32 - 2] = 1; a.sort().length",
    "sparse_reverse": "const a = []; a[2 ** 32 - 2] = 1; a.reverse().length",
    "sparse_map": "const a = []; a[2 ** 32 - 2] = 1; a.map(x => x).length",
}


class TestKnownUncontainedNativeSinks:
    """Host abort (V8 fatal OOM) or uninterruptible hang from one native builtin.

    Root cause class: the near-heap-limit callback and `timeout=` both rely on
    `TerminateExecution`, which V8 checks only at stack-guard interrupts. A
    single C++ builtin that loops over, or allocates for, billions of elements
    never reaches one. Monty is immune because its worker is a subprocess.
    """

    @pytest.mark.xfail(
        strict=True,
        reason="in-process V8 cannot interrupt a single native builtin; needs subprocess isolation",
    )
    @pytest.mark.parametrize("name", list(_SINKS))
    def test_sink_is_contained(self, name: str) -> None:
        outcome = _probe(_SINKS[name])
        assert _contained(outcome), f"{name}: {outcome}"
