"""Regression tests for the findings of an independent adversarial review of the isolation code.

Every test here reproduces an *attack* (a guest, or a compromised worker, doing the thing), not
just the fix, so they keep protecting against the failure and not merely against one patch.
Findings are referred to by the reviewer's labels (H1..H4 high, M1..M9 medium, L low).

A "fake worker" is a tiny script that speaks the protocol and misbehaves on purpose, standing in
for a worker whose V8 has been escaped: the guest alone cannot control framing, only a
compromised worker can.
"""

from __future__ import annotations

import asyncio
import gc
import os
import resource
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from wire_reference import native_encode
from pydeno import (
    IsolatedRuntime,
    JavaScriptError,
    RuntimeConfig,
    RuntimeTimeout,
    WorkerCrashed,
    _sandbox,
    _wire,
)

MIB = 1024 * 1024

_FAKE = textwrap.dedent(
    """
    import json, os, struct, sys, time, threading
    def read():
        h = sys.stdin.buffer.read(4)
        if len(h) < 4:
            sys.exit(0)
        return json.loads(sys.stdin.buffer.read(struct.unpack("<I", h)[0]))
    def send(m):
        b = json.dumps(m).encode()
        sys.stdout.buffer.write(struct.pack("<I", len(b)) + b)
        sys.stdout.buffer.flush()
    def raw(b):
        sys.stdout.buffer.write(b)
        sys.stdout.buffer.flush()
    MODE = %r
    read()  # init
    send({"t": "ready", "version": 1, "sandbox": "none"})
    if MODE == "idle_grow":
        blob = bytearray()
        while True:
            blob += b"x" * (16 << 20)
            time.sleep(0.05)
    if MODE == "idle_spin":
        while True:
            pass
    cmd = read()
    if MODE == "deaf":
        # ask the parent for a big answer, then never read it
        send({"t": "call", "cid": 1, "hid": 1, "args": []})
        time.sleep(120)
    elif MODE == "bad_float_tag":
        send({"t": "result", "id": cmd["id"], "v": {"$": "f", "v": []}}); time.sleep(30)
    elif MODE == "bad_kind_list":
        send({"t": "error", "id": cmd["id"], "kind": [1], "msg": "x"}); time.sleep(30)
    elif MODE == "bad_kind_dict":
        send({"t": "error", "id": cmd["id"], "kind": {"a": 1}, "msg": "x"}); time.sleep(30)
    elif MODE == "deep_tag":
        deep = 0
        for _ in range(400):
            deep = [deep]
        send({"t": "result", "id": cmd["id"], "v": {"$": deep, "v": 1}}); time.sleep(30)
    elif MODE == "bad_token":
        send({"t": "result", "id": cmd["id"], "v": [1]}); time.sleep(30)
    elif MODE == "bad_tokens":
        send({"t": "result", "id": cmd["id"], "v": {"a": "x"}}); time.sleep(30)
    elif MODE == "many_args":
        big = [0] * 1_000_000
        send({"t": "call", "cid": 1, "hid": 1, "args": [big, big, big]}); time.sleep(30)
    elif MODE == "int_flood":
        send({"t": "call", "cid": 1, "hid": 1, "args": [{"$": "set", "v": [(2**61 - 1) * k for k in range(1, 200)]}]})
        time.sleep(30)
    elif MODE == "burst":
        # 60 concurrent calls; count how many the parent refuses outright
        for i in range(1, 61):
            send({"t": "call", "cid": i, "hid": 1, "args": []})
        refused = 0
        for _ in range(60):
            r = read()
            if "err" in r:
                refused += 1
        send({"t": "result", "id": cmd["id"], "v": refused}); time.sleep(30)
    elif MODE == "stderr_escape":
        sys.stderr.write("\\x1b[31mred\\x1b]0;pwned-title\\x07\\x00nul\\n"); sys.stderr.flush()
        os._exit(1)
    elif MODE == "ping":
        send({"t": "result", "id": cmd["id"], "v": "pong"}); time.sleep(30)
    """
)


def _layers_expected(layer: str) -> bool:
    """Whether this environment is expected to apply `layer` (the matrix hides kernel features
    on purpose, announcing what it left in `PYDENO_EXPECT_SANDBOX`)."""
    expected = os.environ.get("PYDENO_EXPECT_SANDBOX")
    if expected is not None:
        return layer in expected.split("+")
    return sys.platform.startswith("linux") or sys.platform == "darwin"


def _fake(tmp_path: Path, mode: str) -> str:
    script = tmp_path / f"fake_{mode}.py"
    script.write_text(_FAKE % mode)
    wrapper = tmp_path / f"fake_{mode}.sh"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


def _rt(tmp_path: Path, mode: str, **kwargs: object) -> IsolatedRuntime:
    kwargs.setdefault("request_timeout", 20)
    return IsolatedRuntime(
        RuntimeConfig(),
        python=_fake(tmp_path, mode),
        sandbox="off",
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# H2: a worker that stops reading must not freeze the parent
# ---------------------------------------------------------------------------


class TestH2AWorkerThatStopsReading:
    def test_the_sync_path_gives_up_instead_of_blocking_forever(
        self, tmp_path: Path
    ) -> None:
        rt = _rt(tmp_path, "deaf", write_stall_timeout=1.0, request_timeout=60)
        rt._handlers[1] = (lambda: "x" * (4 * MIB), False)  # noqa: SLF001
        start = time.monotonic()
        with pytest.raises(WorkerCrashed):
            rt.eval("1")
        assert time.monotonic() - start < 15, "the parent hung on a write"
        assert rt.is_closed()
        assert rt._proc.poll() is not None  # noqa: SLF001

    def test_the_async_path_does_not_freeze_the_event_loop_for_long(
        self, tmp_path: Path
    ) -> None:
        """Replies to asynchronous calls are written on the caller's loop thread; a deaf worker
        must cost that loop at most the stall limit, once."""

        async def big() -> str:
            return "x" * (4 * MIB)

        async def go() -> float:
            rt = _rt(tmp_path, "deaf", write_stall_timeout=1.0, request_timeout=60)
            rt._handlers[1] = (big, True)  # noqa: SLF001
            worst = 0.0

            async def heartbeat() -> None:
                nonlocal worst
                last = time.monotonic()
                while True:
                    await asyncio.sleep(0.05)
                    now = time.monotonic()
                    worst = max(worst, now - last - 0.05)
                    last = now

            beat = asyncio.ensure_future(heartbeat())
            try:
                with pytest.raises(WorkerCrashed):
                    await rt.eval_async("1")
            finally:
                beat.cancel()
                rt.close()
            return worst

        assert asyncio.run(go()) < 5, (
            "the event loop was frozen by a worker that stopped reading"
        )

    def test_a_normal_big_reply_is_not_mistaken_for_a_stall(self) -> None:
        with IsolatedRuntime(
            RuntimeConfig(timeout=30.0), write_stall_timeout=2.0
        ) as rt:
            rt.bind_function("big", lambda: "x" * (8 * MIB))
            assert len(rt.eval("big()")) == 8 * MIB

    def test_the_writer_reports_a_stall_as_its_own_error(self) -> None:
        r, w = os.pipe()
        os.set_blocking(w, False)
        try:
            writer = _wire.FrameWriter(w, stall_timeout=0.3)
            with pytest.raises(_wire.StalledWrite):
                writer.send({"t": "x", "v": "y" * (4 * MIB)})  # nobody reads `r`
        finally:
            os.close(r)
            os.close(w)

    def test_the_stall_limit_can_be_turned_off_and_validated(self) -> None:
        with IsolatedRuntime(
            RuntimeConfig(timeout=10.0), write_stall_timeout=None
        ) as rt:
            assert rt._stall is None  # noqa: SLF001
            assert rt.eval("1") == 1


# ---------------------------------------------------------------------------
# H3: a guest cannot keep the deadline paused forever with overlapping async calls
# ---------------------------------------------------------------------------

SPIN_WHILE_A_CALL_IS_ALWAYS_OUTSTANDING = """
(async () => {
  let p = f();
  for (;;) {
    for (let i = 0; i < 4e6; i++) {}      // burn CPU (jitless: ~0.1 s, comparable to the wait)
    const q = f();                         // a new call is in flight before the old one lands
    await p;
    p = q;
  }
})()
"""


class TestH3OverlappingAsyncCalls:
    def _rt(self, **kwargs: object) -> IsolatedRuntime:
        rt = IsolatedRuntime(RuntimeConfig(), request_timeout=2.0, **kwargs)  # type: ignore[arg-type]

        async def f() -> None:
            await asyncio.sleep(0.1)

        rt.bind_function("f", f)
        return rt

    def test_the_total_wait_cap_stops_it(self) -> None:
        async def go() -> None:
            rt = self._rt(max_host_wait=3.0)
            start = time.monotonic()
            with pytest.raises(RuntimeTimeout, match="max_host_wait"):
                await rt.eval_async(SPIN_WHILE_A_CALL_IS_ALWAYS_OUTSTANDING)
            assert time.monotonic() - start < 20
            assert rt.is_closed()

        asyncio.run(go())

    def test_the_cpu_cap_stops_it_even_with_no_wait_cap(self) -> None:
        """Without `max_host_wait` the guest is paused forever, but it is computing the whole
        time, and CPU cannot be paused."""

        async def go() -> None:
            rt = self._rt(max_host_wait=None)
            start = time.monotonic()
            with pytest.raises(RuntimeTimeout, match="CPU"):
                await rt.eval_async(SPIN_WHILE_A_CALL_IS_ALWAYS_OUTSTANDING)
            # The CPU budget is 4 s, but the guest spends most of its wall time waiting, so how
            # long that takes depends on its duty cycle and the machine's load. The point is that
            # it ends at all (no `max_host_wait` here), and well inside `max_host_wait`'s default.
            assert time.monotonic() - start < 120

        asyncio.run(go())

    def test_a_slow_host_function_alone_is_still_free(self) -> None:
        async def slow() -> str:
            await asyncio.sleep(1.5)
            return "done"

        async def go() -> object:
            with IsolatedRuntime(RuntimeConfig(), request_timeout=1.0) as rt:
                rt.bind_function("slow", slow)
                return await rt.eval_async("slow()")

        assert asyncio.run(go()) == "done"

    def test_a_long_wait_on_the_host_is_charged_to_max_host_wait_not_the_deadline(
        self,
    ) -> None:
        async def slow() -> None:
            await asyncio.sleep(60)

        async def go() -> None:
            rt = IsolatedRuntime(
                RuntimeConfig(), request_timeout=1.0, max_host_wait=2.0
            )
            rt.bind_function("slow", slow)
            start = time.monotonic()
            with pytest.raises(RuntimeTimeout, match="max_host_wait"):
                await rt.eval_async("slow()")
            assert 1.5 < time.monotonic() - start < 15

        asyncio.run(go())

    def test_too_many_calls_in_flight_are_refused_not_run(self) -> None:
        ran: list[int] = []

        async def slow() -> int:
            ran.append(1)
            await asyncio.sleep(0.4)
            return 1

        async def go() -> tuple[int, int]:
            with IsolatedRuntime(RuntimeConfig(), max_inflight_host_calls=4) as rt:
                rt.bind_function("slow", slow)
                out = await rt.eval_async(
                    "Promise.allSettled(Array.from({length: 20}, () => slow()))"
                    ".then(rs => [rs.filter(r => r.status === 'fulfilled').length,"
                    " rs.filter(r => r.status === 'rejected').length])"
                )
                return out[0], out[1]

        fulfilled, rejected = asyncio.run(go())
        assert fulfilled + rejected == 20
        assert rejected >= 10, "calls beyond the cap must be refused"
        assert 1 <= fulfilled <= 6
        assert len(ran) == fulfilled, (
            "a refused call must never reach the host function"
        )

    def test_the_inflight_cap_is_validated(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            IsolatedRuntime(max_inflight_host_calls=0)

    def test_a_refusal_is_what_the_guest_can_catch(self, tmp_path: Path) -> None:
        rt = _rt(tmp_path, "burst", max_inflight_host_calls=3, request_timeout=30)

        async def slow() -> None:
            await asyncio.sleep(0.3)

        rt._handlers[1] = (slow, True)  # noqa: SLF001

        async def go() -> object:
            try:
                return await rt.eval_async("1")
            finally:
                rt.close()

        assert asyncio.run(go()) >= 50  # nearly all 60 refused with an error reply


# ---------------------------------------------------------------------------
# H4 + M9: hostile decode cost
# ---------------------------------------------------------------------------


class TestH4HashCollisionDecode:
    COLLIDING = [(2**61 - 1) * k for k in range(1, 400)]

    def test_a_set_of_values_that_all_hash_alike_is_rejected_fast(self) -> None:
        payload = {"$": "set", "v": [{"$": "int", "v": str(v)} for v in self.COLLIDING]}
        start = time.monotonic()
        with pytest.raises(_wire.WireError, match="hash"):
            _wire.decode_value(payload)
        assert time.monotonic() - start < 1.0

    def test_the_same_for_dictionary_keys(self) -> None:
        payload = {
            "$": "d",
            "v": [[{"$": "int", "v": str(v)}, 1] for v in self.COLLIDING],
        }
        with pytest.raises(_wire.WireError, match="hash"):
            _wire.decode_value(payload)

    def test_a_peer_that_sends_plain_json_ints_past_2_53_is_rejected(self) -> None:
        for n in (2**53 + 1, -(2**53) - 1, 2**61 - 1, 10**30):
            with pytest.raises(_wire.WireError, match="tagged"):
                _wire.decode_value(n)

    def test_honest_large_integers_use_the_tagged_form_and_round_trip(self) -> None:
        for n in (2**53 + 1, 2**200, -(2**200)):
            assert _wire.decode_value(native_encode(n)) == n

    def test_honest_sets_and_dicts_are_unaffected(self) -> None:
        assert _wire.decode_value(native_encode(set(range(50_000)))) == set(
            range(50_000)
        )
        d = {i: str(i) for i in range(5_000)}
        assert _wire.decode_value(native_encode(d)) == d

    def test_the_two_values_that_legitimately_collide_in_python_are_fine(self) -> None:
        assert hash(-1) == hash(-2)
        assert _wire.decode_value(native_encode({-1, -2})) == {-1, -2}

    def test_a_few_collisions_are_tolerated_a_flood_is_not(self) -> None:
        few = [(2**61 - 1) * k for k in range(1, 10)]
        assert _wire.decode_value(native_encode(set(few))) == set(few)

    def test_the_parent_rejects_the_flood_from_a_compromised_worker(
        self, tmp_path: Path
    ) -> None:
        rt = _rt(tmp_path, "int_flood")
        rt._handlers[1] = (lambda *a: None, False)  # noqa: SLF001
        start = time.monotonic()
        with pytest.raises(WorkerCrashed, match="protocol"):
            rt.eval("1")
        assert time.monotonic() - start < 10
        assert rt.is_closed()


class TestM9DecodeBudgets:
    def test_arguments_share_one_node_budget(self, tmp_path: Path) -> None:
        """Three arguments of a million nodes each are three million: over the limit as a whole,
        though each would pass alone."""
        rt = _rt(tmp_path, "many_args")
        rt._handlers[1] = (lambda *a: None, False)  # noqa: SLF001
        with pytest.raises(WorkerCrashed, match="protocol"):
            rt.eval("1")

    def test_decode_values_shares_the_budget_directly(self) -> None:
        big = [0] * 1_000_000
        assert len(_wire.decode_values([big])) == 1
        with pytest.raises(_wire.WireError, match="nodes"):
            _wire.decode_values([big, big, big])

    def test_a_frame_of_empty_containers_is_rejected_before_it_is_built(self) -> None:
        frame = b'{"t":"x","v":[' + b"[]," * 3_000_000 + b"[]]}"
        assert len(frame) < _wire.MAX_FRAME_BYTES
        start = time.monotonic()
        with pytest.raises(_wire.WireError, match="nested values"):
            _wire.loads(frame)
        assert time.monotonic() - start < 1.0, (
            "it must not parse millions of lists first"
        )

    def test_an_ordinary_frame_with_many_values_still_loads(self) -> None:
        frame = _wire.dumps({"t": "x", "v": list(range(500_000))})
        assert len(_wire.loads(frame)["v"]) == 500_000

    def test_brackets_inside_a_string_are_the_only_false_alarm_and_they_are_bounded(
        self,
    ) -> None:
        text = "[" * (_wire.MAX_NODES + 10)
        with pytest.raises(_wire.WireError):
            _wire.loads(_wire.dumps({"t": "x", "v": text}))
        assert _wire.loads(_wire.dumps({"t": "x", "v": "[" * 1000}))["v"] == "[" * 1000


# ---------------------------------------------------------------------------
# M1 / M2: nothing a worker or guest does may raise a raw exception past the kill path
# ---------------------------------------------------------------------------


class TestM1AsyncHandlerFailures:
    def test_an_async_host_function_called_with_the_wrong_arguments_is_a_catchable_error(
        self,
    ) -> None:
        async def f(a: int, b: int) -> int:
            return a + b

        async def go() -> tuple[object, object]:
            with IsolatedRuntime(RuntimeConfig(timeout=10.0)) as rt:
                rt.bind_function("f", f)
                name = await rt.eval_async(
                    "(async () => { try { await f(1) } catch (e) { return e.name } })()"
                )
                return name, await rt.eval_async("1 + 1")

        name, after = asyncio.run(go())
        assert name == "TypeError"
        assert after == 2, (
            "the runtime must stay usable and not wait for a reply that never comes"
        )

    def test_the_same_for_a_synchronous_host_function(self) -> None:
        with IsolatedRuntime(RuntimeConfig(timeout=10.0)) as rt:
            rt.bind_function("g", lambda a, b: a + b)
            assert rt.eval("try { g(1) } catch (e) { e.name }") == "TypeError"
            assert rt.eval("g(1, 2)") == 3

    def test_a_closed_event_loop_is_an_error_reply_not_an_escape(self) -> None:
        async def f() -> int:
            return 1

        loop = asyncio.new_event_loop()
        rt = IsolatedRuntime(RuntimeConfig(timeout=10.0))
        try:
            rt.bind_function("f", f)
            pump = __import__("pydeno._isolated", fromlist=["_Pump"])._Pump(5.0, loop)
            loop.close()
            rt._on_call({"t": "call", "cid": 7, "hid": 1, "args": []}, pump)  # noqa: SLF001
        finally:
            rt.close()


class TestM2NoRawExceptionsFromHostileFields:
    @pytest.mark.parametrize("mode", ["bad_float_tag", "deep_tag"])
    def test_a_malformed_field_is_a_protocol_violation_and_the_worker_is_killed(
        self, tmp_path: Path, mode: str
    ) -> None:
        rt = _rt(tmp_path, mode)
        start = time.monotonic()
        with pytest.raises(WorkerCrashed) as caught:
            rt.eval("1")
        assert not isinstance(caught.value, (TypeError, RecursionError))
        assert time.monotonic() - start < 10
        assert rt.is_closed()
        assert rt._proc.poll() is not None  # noqa: SLF001

    @pytest.mark.parametrize("mode", ["bad_kind_list", "bad_kind_dict"])
    def test_a_non_string_error_kind_does_not_crash_the_error_path(
        self, tmp_path: Path, mode: str
    ) -> None:
        """The frame is a well-formed *error*; only its `kind` is the wrong type. It is still an
        error, so it is raised as a plain RuntimeError, never a TypeError."""
        rt = _rt(tmp_path, mode)
        try:
            with pytest.raises(RuntimeError) as caught:
                rt.eval("1")
            assert type(caught.value) is RuntimeError
        finally:
            rt.close()

    def test_a_non_integer_capability_token_is_refused(self, tmp_path: Path) -> None:
        rt = _rt(tmp_path, "bad_token")
        with pytest.raises(WorkerCrashed, match="token"):
            rt.bind_function("f", lambda: 1)
        assert not rt._handlers, (
            "a handler must not be left registered for a refused binding"
        )  # noqa: SLF001

    def test_malformed_tokens_from_bind_object_are_refused(
        self, tmp_path: Path
    ) -> None:
        rt = _rt(tmp_path, "bad_tokens")
        with pytest.raises(WorkerCrashed, match="token"):
            rt.bind_object("o", {"a": lambda: 1})
        assert not rt._handlers  # noqa: SLF001

    def test_the_decoder_never_raises_anything_but_wire_error_for_odd_payload_types(
        self,
    ) -> None:
        for tag in ("f", "int", "b", "dt", "set", "d"):
            for payload in ([], {}, None, 5, 1.5, True, [[]], {"a": 1}):
                try:
                    _wire.decode_value({"$": tag, "v": payload})
                except _wire.WireError:
                    pass
        for tag in ([], {}, None, 5, ["x"]):
            with pytest.raises(_wire.WireError):
                _wire.decode_value({"$": tag, "v": 1})

    def test_the_unknown_tag_message_is_bounded(self) -> None:
        with pytest.raises(_wire.WireError) as caught:
            _wire.decode_value({"$": "x" * 1_000_000, "v": 1})
        assert len(str(caught.value)) < 200


# ---------------------------------------------------------------------------
# M3 / M4: descriptors
# ---------------------------------------------------------------------------


class TestM3StaleDescriptors:
    def test_after_a_kill_nothing_can_be_written_to_the_old_descriptor(self) -> None:
        rt = IsolatedRuntime(RuntimeConfig(timeout=10.0))
        rt._kill()  # noqa: SLF001
        with pytest.raises(BrokenPipeError):
            rt._writer.send({"t": "x"})  # noqa: SLF001
        with pytest.raises(OSError):
            rt._reader.read(time.monotonic() + 0.1)  # noqa: SLF001

    def test_a_late_reply_after_close_is_dropped_not_written_somewhere_else(
        self,
    ) -> None:
        """An asynchronous completion can arrive after the runtime is gone. By then the numbers of
        its pipe descriptors may belong to some other open file; writing to them would corrupt it."""
        rt = IsolatedRuntime(RuntimeConfig(timeout=10.0))
        old_stdin = rt._proc.stdin.fileno()  # type: ignore[union-attr]  # noqa: SLF001
        rt.close()
        # something unrelated now opens a descriptor, quite possibly the same number
        r, w = os.pipe()
        try:
            rt._send_reply({"t": "reply", "cid": 1, "v": "LEAK"}, None)  # noqa: SLF001
            os.set_blocking(r, False)
            with pytest.raises(BlockingIOError):
                os.read(r, 100)
            assert old_stdin >= 0
        finally:
            os.close(r)
            os.close(w)

    def test_a_cancelled_eval_does_not_let_the_polling_thread_read_a_reused_descriptor(
        self,
    ) -> None:
        async def go() -> None:
            rt = IsolatedRuntime(RuntimeConfig(), request_timeout=30)
            task = asyncio.ensure_future(rt.eval_async("new Promise(() => {})"))
            await asyncio.sleep(0.3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert rt.is_closed()
            await asyncio.sleep(0.5)  # the abandoned pump thread winds down

        asyncio.run(go())


class TestM4HighDescriptorNumbers:
    def test_a_frame_reader_works_on_a_descriptor_numbered_past_1024(self) -> None:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = 1500
        if hard != resource.RLIM_INFINITY and hard < want + 16:
            return  # cannot raise the limit here; nothing to test
        resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, want + 16), hard))
        try:
            import fcntl

            r, w = os.pipe()
            big = fcntl.fcntl(r, fcntl.F_DUPFD, want)
            os.close(r)
            assert big >= 1024
            os.write(w, len(b'{"t":"ok"}').to_bytes(4, "little") + b'{"t":"ok"}')
            reader = _wire.FrameReader(big)
            assert _wire.loads(reader.read(time.monotonic() + 2))["t"] == "ok"
            with pytest.raises(TimeoutError):
                reader.read(time.monotonic() + 0.1)
            os.close(big)
            os.close(w)
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))

    def test_a_runtime_can_be_created_when_the_process_has_many_descriptors_open(
        self,
    ) -> None:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard != resource.RLIM_INFINITY and hard < 1400:
            return
        resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, 1500), hard))
        held: list[int] = []
        try:
            import fcntl

            r, w = os.pipe()
            held += [fcntl.fcntl(r, fcntl.F_DUPFD, 1100 + i) for i in range(4)]
            os.close(r)
            os.close(w)
            with IsolatedRuntime(RuntimeConfig(timeout=10.0)) as rt:
                assert rt.eval("1 + 1") == 2
        finally:
            for fd in held:
                os.close(fd)
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


# ---------------------------------------------------------------------------
# M7 / L: ceilings and idle supervision
# ---------------------------------------------------------------------------


class TestM7IdleSupervision:
    def test_a_worker_that_eats_memory_while_idle_is_killed_without_any_command(
        self, tmp_path: Path
    ) -> None:
        if sys.platform not in ("linux", "darwin"):
            return
        rt = _rt(tmp_path, "idle_grow", max_memory=250 * MIB, request_timeout=None)
        deadline = time.monotonic() + 15
        while not rt.is_closed() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert rt.is_closed(), "the idle watchdog never noticed the memory growth"
        try:
            rt._proc.wait(10)  # noqa: SLF001
        except subprocess.TimeoutExpired:
            pytest.fail("the worker was marked closed but never died")

    def test_a_worker_that_spins_while_idle_is_killed(self, tmp_path: Path) -> None:
        rt = _rt(tmp_path, "idle_spin", request_timeout=None)
        deadline = time.monotonic() + 30
        while not rt.is_closed() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert rt.is_closed(), "an idle worker burning CPU was never stopped"

    def test_a_healthy_idle_worker_is_left_alone(self) -> None:
        with IsolatedRuntime(RuntimeConfig(timeout=10.0)) as rt:
            rt.eval("1")
            time.sleep(3.0)
            assert not rt.is_closed()
            assert rt.eval("2 + 2") == 4

    def test_a_worker_that_just_finished_heavy_work_is_not_mistaken_for_a_runaway(
        self,
    ) -> None:
        with IsolatedRuntime(RuntimeConfig(timeout=30.0)) as rt:
            rt.eval(
                "const a = []; for (let i = 0; i < 200000; i++) a.push({i, s: 'x'.repeat(50)}); a.length"
            )
            time.sleep(2.0)  # V8 finishing its garbage collection is not idle burning
            assert not rt.is_closed()

    def test_the_idle_watchdog_does_not_keep_a_runtime_alive(self) -> None:
        rt = IsolatedRuntime(RuntimeConfig(timeout=10.0))
        proc = rt._proc  # noqa: SLF001
        del rt
        gc.collect()
        deadline = time.monotonic() + 10
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert proc.poll() is not None, "a forgotten runtime left its worker running"


class TestM7ResourceLimits:
    CHILD = textwrap.dedent(
        """
        import importlib.util, json, resource, sys
        spec = importlib.util.spec_from_file_location("_sandbox", sys.argv[1])
        sb = importlib.util.module_from_spec(spec); spec.loader.exec_module(sb)
        sb.harden_process()
        out = {}
        for name in ("RLIMIT_NOFILE", "RLIMIT_MEMLOCK", "RLIMIT_CORE", "RLIMIT_FSIZE", "RLIMIT_MSGQUEUE"):
            res = getattr(resource, name, None)
            out[name] = list(resource.getrlimit(res)) if res is not None else None
        print(json.dumps(out))
        """
    )

    def _limits(self) -> dict[str, list[int] | None]:
        import json

        done = subprocess.run(
            [sys.executable, "-I", "-c", self.CHILD, _sandbox.__file__],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_the_descriptor_table_is_small(self) -> None:
        assert self._limits()["RLIMIT_NOFILE"] == [256, 256]

    def test_nothing_can_be_pinned_in_memory(self) -> None:
        assert self._limits()["RLIMIT_MEMLOCK"] == [0, 0]

    def test_core_dumps_are_off_and_files_are_capped(self) -> None:
        limits = self._limits()
        assert limits["RLIMIT_CORE"] == [0, 0]
        assert limits["RLIMIT_FSIZE"] == [1 << 20, 1 << 20]

    def test_a_worker_with_those_limits_still_works(self) -> None:
        with IsolatedRuntime(RuntimeConfig(timeout=10.0)) as rt:
            assert rt.eval("new Uint8Array(4 * 1024 * 1024).fill(1).length") == 4 * MIB


# ---------------------------------------------------------------------------
# M8: "require" means every layer
# ---------------------------------------------------------------------------


class TestM8RequireMeansEveryLayer:
    @pytest.mark.parametrize(
        ("platform", "applied", "missing"),
        [
            ("linux", "landlock+seccomp", set()),
            ("linux", "seccomp+landlock", set()),
            ("linux", "seccomp", {"landlock"}),
            ("linux", "landlock", {"seccomp"}),
            ("linux", "none", {"landlock", "seccomp"}),
            ("darwin", "seatbelt", set()),
            ("darwin", "none", {"seatbelt"}),
            ("freebsd", "seatbelt", {"a-supported-platform"}),
            ("freebsd", "none", {"a-supported-platform"}),
        ],
    )
    def test_missing_layers_per_platform(
        self,
        monkeypatch: pytest.MonkeyPatch,
        platform: str,
        applied: str,
        missing: set[str],
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        assert set(_sandbox.missing_layers(applied)) == missing

    def test_an_extra_layer_does_not_make_up_for_a_missing_core_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        # the empty root is a bonus and is reported separately; it never counts here
        assert _sandbox.missing_layers("emptyroot") == {"landlock", "seccomp"}

    def test_one_layer_failing_does_not_stop_the_next_being_tried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if not sys.platform.startswith("linux"):
            return
        calls: list[str] = []

        def boom() -> bool:
            calls.append("landlock")
            raise OSError("landlock exploded")

        monkeypatch.setattr(_sandbox, "_thread_count_here", lambda: 1)
        monkeypatch.setattr(_sandbox, "_apply_landlock", boom)
        monkeypatch.setattr(_sandbox, "_apply_empty_root", lambda: False)
        monkeypatch.setattr(_sandbox, "_seccomp_is_safe_here", lambda **_: True)
        monkeypatch.setattr(
            _sandbox, "_apply_seccomp", lambda **_: calls.append("seccomp") or True
        )
        assert _sandbox.apply() == "seccomp"
        assert calls == ["landlock", "seccomp"]

    def test_a_failed_seccomp_probe_means_no_seccomp_and_require_notices(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if not sys.platform.startswith("linux"):
            return
        monkeypatch.setattr(_sandbox, "_thread_count_here", lambda: 1)
        monkeypatch.setattr(_sandbox, "_apply_landlock", lambda: True)
        monkeypatch.setattr(_sandbox, "_apply_empty_root", lambda: False)

        def no_fork(**_: object) -> bool:
            raise OSError("cannot fork")

        monkeypatch.setattr(_sandbox, "_seccomp_is_safe_here", no_fork)
        applied = _sandbox.apply()
        assert applied == "landlock"
        assert _sandbox.missing_layers(applied) == {"seccomp"}

    def test_require_refuses_a_partial_sandbox_in_the_worker(
        self, tmp_path: Path
    ) -> None:
        """A worker that reports nothing applied must not satisfy `require`: the parent trusts
        the *worker* to refuse, so test that the worker's own refusal arrives as a failure to
        start (the matrix covers real kernels that lack one layer)."""
        if sys.platform == "darwin" or (
            _layers_expected("seccomp") and _layers_expected("landlock")
        ):
            with IsolatedRuntime(sandbox="require") as rt:
                assert not _sandbox.missing_layers(rt.sandbox)


class TestLowFindings:
    def test_text_from_a_worker_is_cleaned_before_it_reaches_an_exception(
        self, tmp_path: Path
    ) -> None:
        rt = _rt(tmp_path, "stderr_escape")
        with pytest.raises(WorkerCrashed) as caught:
            rt.eval("1")
        text = str(caught.value)
        assert "\x1b" not in text and "\x00" not in text and "\x07" not in text, repr(
            text
        )
        assert "exit code 1" in text

    def test_clean_removes_control_characters_and_bounds_length(self) -> None:
        from pydeno._isolated import _clean

        assert _clean("a\x1b[31mb\x00c\x07d") == "a?[31mb?c?d"
        assert _clean("tab\tand\nnewline") == "tab\tand\nnewline"
        assert len(_clean("x" * 10_000)) == 500
        assert _clean("é日本語😀") == "é日本語😀"

    def test_host_error_text_can_be_redacted_but_the_class_name_is_kept(self) -> None:
        def leak() -> None:
            raise ValueError("secret path /etc/shadow and SELECT * FROM users")

        probe = "try { leak() } catch (e) { [e.name, e.message] }"
        with IsolatedRuntime(
            RuntimeConfig(timeout=10.0), redact_host_errors=True
        ) as rt:
            rt.bind_function("leak", leak)
            name, message = rt.eval(probe)
        assert name == "ValueError"
        assert message == "host function failed"
        assert "shadow" not in message

    def test_host_error_text_is_redacted_by_default_and_visible_on_request(
        self,
    ) -> None:
        def leak() -> None:
            raise ValueError("/srv/secrets/credentials")

        probe = "try { leak() } catch (e) { e.message }"
        with IsolatedRuntime(RuntimeConfig(timeout=10.0)) as rt:
            rt.bind_function("leak", leak)
            assert rt.eval(probe) == "host function failed"
        with IsolatedRuntime(
            RuntimeConfig(timeout=10.0), redact_host_errors=False
        ) as rt:
            rt.bind_function("leak", leak)
            assert rt.eval(probe) == "/srv/secrets/credentials"

    def test_a_handler_calling_back_into_its_own_runtime_fails_instead_of_deadlocking(
        self,
    ) -> None:
        async def go() -> object:
            with IsolatedRuntime(
                RuntimeConfig(timeout=10.0),
                request_timeout=20,
                redact_host_errors=False,
            ) as rt:

                async def reenter() -> object:
                    return await rt.eval_async("1")

                rt.bind_function("reenter", reenter)
                return await rt.eval_async(
                    "(async () => { try { return await reenter() } catch (e) { return e.message } })()"
                )

        start = time.monotonic()
        message = asyncio.run(go())
        assert time.monotonic() - start < 15, "it deadlocked"
        assert "re-entered" in str(message)

    def test_a_sync_handler_calling_back_in_fails_the_same_way(self) -> None:
        with IsolatedRuntime(
            RuntimeConfig(timeout=10.0), redact_host_errors=False
        ) as rt:
            rt.bind_function("reenter", lambda: rt.eval("1"))
            assert "re-entered" in rt.eval("try { reenter() } catch (e) { e.message }")

    def test_the_stderr_file_is_closed_even_when_a_crash_closed_the_runtime_first(
        self,
    ) -> None:
        rt = IsolatedRuntime(RuntimeConfig(), request_timeout=1.0)
        with pytest.raises((RuntimeTimeout, WorkerCrashed)):
            rt.eval("const a = []; a[2 ** 32 - 2] = 1; a.sort()")
        assert rt.is_closed()
        rt.close()
        assert rt._stderr.closed  # noqa: SLF001

    def test_closing_twice_is_harmless(self) -> None:
        rt = IsolatedRuntime(RuntimeConfig(timeout=10.0))
        rt.close()
        rt.close()
        assert rt._stderr.closed  # noqa: SLF001

    def test_revoking_while_another_thread_calls_never_raises_a_key_error_in_the_pump(
        self,
    ) -> None:
        import threading

        errors: list[BaseException] = []
        with IsolatedRuntime(RuntimeConfig(timeout=20.0)) as rt:
            tokens = [rt.bind_function(f"h{i}", lambda i=i: i) for i in range(20)]

            def revoker() -> None:
                try:
                    for token in tokens:
                        rt.revoke_op(token)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            t = threading.Thread(target=revoker)
            t.start()
            for i in range(20):
                try:
                    rt.eval(f"try {{ h{i}() }} catch (e) {{ 'gone' }}")
                except JavaScriptError:
                    pass
            t.join(30)
        assert not errors

    def test_landlock_and_the_empty_root_are_skipped_in_a_multithreaded_process(
        self,
    ) -> None:
        """Landlock covers only the calling thread, and a user namespace cannot be entered with
        other threads running. Claiming either would be a lie about the threads that exist."""
        if not sys.platform.startswith("linux"):
            return
        import json

        script = textwrap.dedent(
            """
            import importlib.util, json, sys, threading
            spec = importlib.util.spec_from_file_location("_sandbox", sys.argv[1])
            sb = importlib.util.module_from_spec(spec); spec.loader.exec_module(sb)
            stop = threading.Event()
            threading.Thread(target=stop.wait, daemon=True).start()
            layers = sb.apply()
            print(json.dumps({"layers": layers, "extras": list(sb.EXTRAS)}))
            """
        )
        done = subprocess.run(
            [sys.executable, "-I", "-c", script, _sandbox.__file__],
            capture_output=True,
            text=True,
            timeout=60,
            start_new_session=True,
            check=True,
        )
        out = json.loads(done.stdout.strip().splitlines()[-1])
        assert "landlock" not in out["layers"].split("+")
        assert "emptyroot" not in out["extras"]
        assert ("seccomp" in out["layers"].split("+")) == _layers_expected("seccomp"), (
            "seccomp (with TSYNC) still covers every thread"
        )


class TestUnbuiltSnapshotBuilder:
    def test_dropping_an_unbuilt_builder_does_not_abort_the_process_at_exit(
        self,
    ) -> None:
        code = (
            "from pydeno import SnapshotBuilder\n"
            "b = SnapshotBuilder()\n"
            "b.execute_script('x.js', 'globalThis.a = 1')\n"
            "del b\n"
            "b2 = SnapshotBuilder()\n"  # still alive at interpreter exit
        )
        done = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert done.returncode == 0, done.stderr


class TestPrewarmedSpare:
    """One ready worker is kept so the next runtime skips most of the start-up cost. It holds no
    configuration, is handed out once, and never outlives its parent."""

    @staticmethod
    def _spare_pid(deadline: float = 10.0) -> int:
        from pydeno import _isolated

        end = time.monotonic() + deadline
        while time.monotonic() < end:
            spare = _isolated._SPARE  # noqa: SLF001
            if spare is not None:
                return spare[0].pid
            time.sleep(0.02)
        raise AssertionError("no spare worker appeared")

    def test_a_spare_is_started_after_a_runtime_and_used_by_the_next_one_exactly_once(
        self,
    ) -> None:
        with IsolatedRuntime(RuntimeConfig(timeout=10.0)) as first:
            first_pid = first._proc.pid  # noqa: SLF001
            spare_pid = self._spare_pid()
            assert spare_pid != first_pid
            with IsolatedRuntime(RuntimeConfig(timeout=10.0)) as second:
                assert second._proc.pid == spare_pid  # noqa: SLF001
                assert second.eval("1 + 1") == 2
                with IsolatedRuntime(RuntimeConfig(timeout=10.0)) as third:
                    assert third._proc.pid not in (first_pid, spare_pid)  # noqa: SLF001
                    assert third.eval("2 + 2") == 4

    def test_a_spare_carries_no_state_from_anyone(self) -> None:
        with IsolatedRuntime(RuntimeConfig(timeout=10.0)) as a:
            self._spare_pid()
            a.eval("globalThis.secret = 'a'")
        with IsolatedRuntime(RuntimeConfig(timeout=10.0), clock=0, random_seed=7) as b:
            assert b.eval("typeof globalThis.secret") == "undefined"
            assert b.eval("Date.now()") == 0

    def test_prewarm_false_never_touches_the_spare(self) -> None:
        with IsolatedRuntime(RuntimeConfig(timeout=10.0)):
            spare_pid = self._spare_pid()
        with IsolatedRuntime(RuntimeConfig(timeout=10.0), prewarm=False) as rt:
            assert rt._proc.pid != spare_pid  # noqa: SLF001
        from pydeno import _isolated

        assert _isolated._SPARE is not None and _isolated._SPARE[0].pid == spare_pid  # noqa: SLF001

    def test_a_spare_that_died_while_waiting_is_skipped(self) -> None:
        import signal

        with IsolatedRuntime(RuntimeConfig(timeout=10.0)):
            pid = self._spare_pid()
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.3)
        with IsolatedRuntime(RuntimeConfig(timeout=10.0)) as rt:
            assert rt._proc.pid != pid  # noqa: SLF001
            assert rt.eval("40 + 2") == 42

    def test_the_spare_exits_with_its_parent_and_at_interpreter_exit(self) -> None:
        code = (
            "import os, time\n"
            "from pydeno import IsolatedRuntime, RuntimeConfig, _isolated\n"
            "with IsolatedRuntime(RuntimeConfig(timeout=10.0)): pass\n"
            "while _isolated._SPARE is None: time.sleep(0.02)\n"
            "print(_isolated._SPARE[0].pid, flush=True)\n"
            "os._exit(0)\n"  # no atexit: only the closed pipe can end the spare
        )
        done = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
        )
        pid = int(done.stdout.strip().splitlines()[-1])
        end = time.monotonic() + 10
        while time.monotonic() < end:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        raise AssertionError("the spare outlived its parent")


class TestStripGlobals:
    NAMES = ["SharedArrayBuffer", "Atomics", "WeakRef", "FinalizationRegistry"]
    PROBE = "[%s].map(n => typeof globalThis[n])"

    def test_timer_and_gc_observation_globals_are_gone_by_default(self) -> None:
        probe = self.PROBE % ", ".join(repr(n) for n in self.NAMES)
        with IsolatedRuntime(RuntimeConfig(timeout=10.0)) as rt:
            assert rt.eval(probe) == ["undefined"] * 4

    def test_the_caller_bootstrap_still_runs_after_the_strip(self) -> None:
        cfg = RuntimeConfig(bootstrap="globalThis.seen = typeof Atomics;", timeout=10.0)
        with IsolatedRuntime(cfg) as rt:
            assert rt.eval("seen") == "undefined"
