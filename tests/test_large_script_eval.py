"""Regression test for a real SIGABRT crash on large JS source text.

Root cause: `deno_core` registers each `v8::Isolate` with a
`tokio::runtime::Handle::try_current()` snapshot at `JsRuntime::new` time
(see `deno_core::runtime::setup::register_isolate`). Once a script is large
enough that V8 schedules a background task (streaming compilation, or a GC
memory-reducer task under heap pressure), `deno_core` posts that task onto
the isolate's registered handle. `spawn_runtime_thread`
(src/runtime/runner.rs) used to build its per-isolate `JsRuntime` *before*
entering the dedicated tokio runtime on that thread, so the isolate was
always registered with no handle at all. The very next delayed task then
hit `deno_core`'s `spawn_delayed_task`, found no handle, and called
`std::process::abort()` -- an uncatchable process-wide SIGABRT, not a
catchable Rust panic or JS exception.

Measured threshold on this machine: ~147.3KB of plain filler JS (`var
x=0;` repeated) evaluates fine, ~147.7KB reliably aborts the whole Python
process. The exact byte count is V8-version- and platform-dependent, so
these tests use sizes an order of magnitude past any plausible threshold
(500KB+) to stay robust across environments, plus a couple of sizes
straddling the specific number found during triage for extra confidence.

The fix (src/runtime/runner.rs, `spawn_runtime_thread`) enters the
thread's own single-threaded tokio runtime with `tokio_rt.enter()` before
constructing `RuntimeCoreState` (which owns the `JsRuntime`/isolate), so
`Handle::try_current()` succeeds at registration time and delayed tasks
have somewhere real to land.
"""

import asyncio

import pydeno
import pytest
from pydeno import Runtime, RuntimeConfig, SnapshotBuilder


def _filler(byte_size: int) -> str:
    """~`byte_size` bytes of inert JS, one statement per line."""
    line = "var x=0;\n"
    return line * (byte_size // len(line))


@pytest.mark.parametrize("size_bytes", [200_000, 500_000, 1_000_000, 5_000_000])
def test_large_script_eval_sync_does_not_crash(size_bytes):
    with Runtime() as rt:
        result = rt.eval(_filler(size_bytes) + "\n1 + 1")
    assert result == 2


@pytest.mark.parametrize("size_bytes", [147_000, 148_000, 500_000])
def test_large_script_eval_async_does_not_crash(size_bytes):
    async def run():
        with Runtime() as rt:
            return await rt.eval_async(_filler(size_bytes) + "\n1 + 1")

    assert asyncio.run(run()) == 2


def test_large_script_module_level_eval_async_does_not_crash():
    async def run():
        return await pydeno.eval_async(_filler(1_000_000) + "\n1 + 1")

    assert asyncio.run(run()) == 2


def test_large_bootstrap_script_does_not_crash():
    config = RuntimeConfig(bootstrap=_filler(500_000))
    with Runtime(config) as rt:
        assert rt.eval("1 + 1") == 2


def test_large_script_can_still_call_a_host_function():
    """Not just "doesn't crash" -- large scripts must still work correctly:
    the host-callback bridge (op_pydeno_call_python_sync) must still
    function after a large script has been compiled/evaluated."""
    calls = []

    with Runtime() as rt:
        rt.bind_function("record", lambda value: calls.append(value) or value * 2)
        src = _filler(600_000) + "\nrecord(21)"
        result = rt.eval(src)

    assert result == 42
    assert calls == [21]


# --- SnapshotBuilder: same latent bug class, different isolate creation path ---
#
# `SnapshotBuilder` (src/runtime/snapshot.rs) creates its `JsRuntimeForSnapshot`
# directly on whatever thread calls it from Python, with no tokio runtime ever
# entered there either -- the same `register_isolate(..., Handle::try_current())`
# call site as `spawn_runtime_thread` above, so in principle the same class of
# uncatchable abort applies.
#
# In practice, extensive reproduction attempts against this code path (plain
# filler scripts up to 20MB, heap-building scripts producing 300MB+ of
# snapshotted data, GC-churn scripts creating/discarding ~500MB of garbage
# across many rounds, and 60,000+ function declarations in one script) did
# NOT crash, pre-fix. `SnapshotBuilder` always builds with `will_snapshot =
# true`, which routes isolate creation through V8's `SnapshotCreator`
# (deno_core's `runtime::setup::create_isolate`) instead of a plain
# `v8::Isolate::new` -- and that path appears to force fully synchronous
# compilation with no background/delayed V8 tasks ever posted, so the
# `spawn_delayed_task` abort path this bug depends on is never reached today.
#
# The fix (entering a minimal current-thread tokio runtime around
# `JsRuntimeForSnapshot::try_new` in `create_runtime()`) is applied anyway as
# defense-in-depth: it costs nothing, mirrors the working fix in
# `spawn_runtime_thread`, and removes the latent gap (an isolate registered
# with no tokio handle) in case a future V8/deno_core version enables
# background compilation during snapshotting. These tests pin "large
# bootstrap scripts build successfully" as a permanent regression guard,
# even though they were not observed to fail before the fix.
@pytest.mark.parametrize("size_bytes", [200_000, 500_000, 1_000_000, 5_000_000])
def test_large_snapshot_bootstrap_script_builds_successfully(size_bytes):
    snapshot = SnapshotBuilder(
        bootstrap=_filler(size_bytes) + "\nglobalThis.__ok = 1;"
    ).build()
    assert isinstance(snapshot, bytes)
    assert len(snapshot) > 0


def test_large_snapshot_bootstrap_script_via_execute_script():
    builder = SnapshotBuilder()
    builder.execute_script("<bootstrap>", _filler(1_000_000) + "\nglobalThis.__ok = 1;")
    snapshot = builder.build()
    assert isinstance(snapshot, bytes)
    assert len(snapshot) > 0


def test_snapshot_from_large_bootstrap_still_usable_at_runtime():
    """Not just "doesn't crash while building" -- the resulting snapshot must
    still work when used to start a real Runtime."""
    snapshot = SnapshotBuilder(
        bootstrap=_filler(600_000) + "\nglobalThis.fromSnapshot = 42;"
    ).build()
    config = RuntimeConfig(snapshot=snapshot)
    with Runtime(config) as rt:
        assert rt.eval("fromSnapshot") == 42
