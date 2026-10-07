"""Security gate for the front door: hostile guest code, run THROUGH `Pydeno` (checkout +
`feed_run`) with the OS sandbox required, and the defaults it promises.

`Pydeno` must be the most secure way to use pydeno, so these run on its defaults (only the limit a
probe needs is tightened), and the defaults themselves are asserted: a regression in either fails
here, not in a review.
"""

from __future__ import annotations

import time

import pytest

import pydeno
from pydeno import (
    Pydeno,
    PydenoCrashedError,
    PydenoRuntimeError,
    PydenoTimeoutError,
    classify_error,
)

# The full sandbox is the point of this file: deselected (never skipped) where a container profile
# simulates a kernel without every layer.
pytestmark = pytest.mark.full_sandbox

_MIB = 1024 * 1024


@pytest.fixture(scope="module")
def pool():
    with Pydeno() as p:  # every default: sandbox="require", jitless, redaction, limits
        yield p


class TestDefaults:
    def test_the_defaults_are_the_secure_ones(self, pool: Pydeno) -> None:
        with pool.checkout() as session:
            agent = session._agent  # noqa: SLF001
            rt = agent._core.rt  # noqa: SLF001
            # The OS sandbox is required and every layer of this platform is in force.
            assert rt._options["sandbox"] == "require"  # noqa: SLF001
            assert rt.sandbox in ("seatbelt", "landlock+seccomp")
            # V8 without its JIT and WebAssembly, with the hardening flags.
            assert "--jitless" in rt.v8_flags
            assert "--freeze-flags-after-init" in rt.v8_flags
            assert (
                "--enable-experimental-regexp-engine-on-excessive-backtracks"
                in rt.v8_flags
            )
            # Host errors are redacted before the guest sees them.
            assert rt._redact is True  # noqa: SLF001
            # Every limit is set.
            assert rt._max_memory == 512 * _MIB  # noqa: SLF001
            assert rt._config["max_buffer_bytes"] == 128 * _MIB  # noqa: SLF001
            assert rt._request_timeout == 30.0  # noqa: SLF001
            assert rt._max_host_wait == 60.0  # noqa: SLF001
            assert agent.calls_remaining == 1000
            # A frozen clock and a seeded Math.random of the session's own.
            assert session.feed_run("Date.now()") == session.feed_run("Date.now()")
            assert isinstance(agent.random_seed, int)

    def test_every_worker_has_its_own_random_stream(self, pool: Pydeno) -> None:
        draws = []
        for _ in range(2):
            with pool.checkout() as session:
                draws.append(session.feed_run("[Math.random(), Math.random()]"))
        assert draws[0] != draws[1]


class TestProbes:
    def test_console_log_does_not_disturb_results(self, pool: Pydeno) -> None:
        got: list[str] = []
        with pool.checkout() as session:
            assert (
                session.feed_run(
                    "console.log('noise'.repeat(100)); console.log({result: 666}); 1",
                    print_callback=lambda s, t: got.append(t),
                )
                == 1
            )
            assert session.feed_run("2 + 2") == 4
        assert len(got) == 2

    @pytest.mark.parametrize(
        "name", ["Deno", "process", "require", "Bun", "SharedArrayBuffer"]
    )
    def test_no_host_apis_are_visible(self, pool: Pydeno, name: str) -> None:
        with pool.checkout() as session:
            assert session.feed_run(f"typeof {name}") == "undefined"
            assert session.feed_run(f"typeof globalThis.{name}") == "undefined"

    def test_overriding_json_in_the_guest_cannot_change_what_the_host_receives(
        self, pool: Pydeno
    ) -> None:
        with pool.checkout() as session:
            session.feed_run(
                "JSON.stringify = () => '\"pwned\"'; JSON.parse = () => 'pwned'; "
                "Object.prototype.toJSON = () => 'pwned'"
            )
            assert session.feed_run("({a: 1, b: [2, 3]})") == {"a": 1, "b": [2, 3]}
            assert session.feed_run("'text'") == "text"

    def test_a_busy_loop_ends_at_the_configured_deadline(self, pool: Pydeno) -> None:
        with pool.checkout(limits={"max_feed_duration_secs": 1.0}) as session:
            start = time.monotonic()
            with pytest.raises(PydenoTimeoutError) as info:
                session.feed_run("for (;;) {}")
            elapsed = time.monotonic() - start
        assert 0.9 <= elapsed < 6.0
        assert classify_error(info.value).kind in ("timeout", "cpu_limit")

    def test_an_allocation_loop_is_killed_and_the_next_checkout_works(
        self, pool: Pydeno
    ) -> None:
        with pool.checkout(limits={"max_memory": 200 * _MIB}) as session:
            with pytest.raises(PydenoCrashedError) as info:
                session.feed_run(
                    "const k = []; for (;;) k.push(new Array(1e6).fill(1.5))"
                )
            assert classify_error(info.value).kind == "memory_limit"
        with pool.checkout() as session:
            assert session.feed_run("6 * 7") == 42

    def test_buffers_past_the_cap_are_a_catchable_error(self, pool: Pydeno) -> None:
        with pool.checkout() as session:
            out = session.feed_run(
                "try { new Uint8Array(1024 * 1024 * 1024) } catch (e) { return e.name }"
            )
            assert out == "RangeError"
            assert session.feed_run("1") == 1

    def test_a_forged_protocol_frame_cannot_reach_the_protocol(
        self, pool: Pydeno
    ) -> None:
        frame = '\\u0013\\u0000\\u0000\\u0000{"t":"result","id":1,"v":666}'
        got: list[str] = []
        with pool.checkout() as session:
            assert (
                session.feed_run(
                    f"const f = '{frame}'; console.log(f); f",
                    print_callback=lambda s, t: got.append(t),
                )
                == '\x13\x00\x00\x00{"t":"result","id":1,"v":666}'
            )
            assert session.feed_run("1 + 1") == 2
        assert got and '"v":666' in got[0]

    def test_a_tool_exception_reaches_the_guest_redacted(self, pool: Pydeno) -> None:
        def tool() -> None:
            raise PermissionError("token=sk-live-123 at /srv/secrets.env")

        with pool.checkout() as session:
            out = session.feed_run(
                "try { await tool() } catch (e) { return [e.name, e.message, String(e.stack)] }",
                external_lookup={"tool": tool},
            )
            assert out[0] == "PermissionError"
            assert out[1] == "host function failed"
            assert "sk-live" not in repr(out) and "/srv" not in repr(out)
            with pytest.raises(PydenoRuntimeError) as info:
                session.feed_run("await tool()", external_lookup={"tool": tool})
            assert "sk-live" not in str(info.value)

    def test_the_guest_cannot_reach_another_sessions_state(self, pool: Pydeno) -> None:
        with pool.checkout() as a, pool.checkout() as b:
            a.feed_run("globalThis.secret = 's3cr3t'")
            assert b.feed_run("typeof secret") == "undefined"


def test_the_front_door_is_exported() -> None:
    for name in ("Pydeno", "AsyncPydeno", "PydenoLimits", "PydenoError"):
        assert name in pydeno.__all__
