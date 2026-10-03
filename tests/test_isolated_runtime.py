"""`IsolatedRuntime`: containment, API parity, and an untrusted-worker boundary.

The containment tests are the other half of `test_monty_parity_security.py`:
the strict-xfail sinks there crash or hang an in-process `Runtime`; here every
one of them must end in a catchable error while the host (this pytest process)
keeps running.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from wire_reference import native_encode
from pydeno import (
    IsolatedRuntime,
    JavaScriptError,
    RuntimeConfig,
    RuntimeTimeout,
    ToolBridge,
    WorkerCrashed,
    _wire,
    undefined,
)
from test_monty_parity_security import _SINKS

MIB = 1024 * 1024


def _iso(**kwargs: object) -> IsolatedRuntime:
    cfg = RuntimeConfig(timeout=kwargs.pop("timeout", 5.0))  # type: ignore[arg-type]
    return IsolatedRuntime(cfg, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# wire format
# ---------------------------------------------------------------------------


class TestWireCodec:
    @pytest.mark.parametrize(
        "value",
        [
            None,
            True,
            0,
            -7,
            2**53,
            2**80,
            -(2**80),
            1.5,
            -0.0,
            float("inf"),
            float("-inf"),
            "héllo ☃",
            b"\x00\xff bytes",
            [1, [2, [3]]],
            {"a": 1, "b": [None]},
            {"$": "not a tag"},
            {1: "int key", 2: "another"},
            {1, 2, 3},
            datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            undefined,
        ],
    )
    def test_round_trip(self, value: object) -> None:
        back = _wire.decode_value(native_encode(value))
        assert back == value
        assert type(back) is type(value)

    def test_nan_round_trips(self) -> None:
        back = _wire.decode_value(native_encode(float("nan")))
        assert back != back

    def test_negative_zero_keeps_its_sign(self) -> None:
        assert str(_wire.decode_value(native_encode(-0.0))) == "-0.0"

    def test_unsupported_type_is_refused_not_pickled(self) -> None:
        with pytest.raises(_wire.WireError):
            native_encode(object())

    @pytest.mark.parametrize(
        "hostile",
        [
            {"$": "zzz", "v": 1},
            {"$": "int", "v": "not a number"},
            {"$": "int", "v": "9" * 5000},
            {"$": "f", "v": "banana"},
            {"$": "b", "v": "***not base64***"},
            {"$": "dt", "v": "not a date"},
            {"$": "set", "v": [[1]]},
            {"$": "d", "v": [[1]]},
            {"$": "d", "v": [[[1], 2]]},
            {"$": "int"},
            {"$": "u", "v": 1},
            {"$": "int", "v": "1", "extra": 1},
        ],
    )
    def test_malformed_tagged_values_are_rejected(self, hostile: object) -> None:
        with pytest.raises(_wire.WireError):
            _wire.decode_value(hostile)

    def test_depth_is_bounded(self) -> None:
        node: object = 0
        for _ in range(_wire.MAX_DEPTH + 5):
            node = [node]
        with pytest.raises(_wire.WireError):
            _wire.decode_value(node)

    def test_node_count_is_bounded(self) -> None:
        with pytest.raises(_wire.WireError):
            _wire.decode_value([0] * (_wire.MAX_NODES + 1))

    @pytest.mark.parametrize(
        "frame",
        [
            b"{not json",
            b"[]",
            b"{}",
            b'{"t": 5}',
            b"NaN",
            b'{"t": "x", "v": NaN}',
            b"\xff\xfe",
        ],
    )
    def test_bad_frames_raise_only_wire_error(self, frame: bytes) -> None:
        with pytest.raises(_wire.WireError):
            _wire.loads(frame)

    def test_pathologically_nested_json_does_not_raise_recursion_error(self) -> None:
        with pytest.raises(_wire.WireError):
            _wire.loads(b'{"t":"x","v":' + b"[" * 200_000 + b"]" * 200_000 + b"}")

    def test_oversized_frame_is_rejected_before_it_is_buffered(self) -> None:
        r, w = os.pipe()
        try:
            os.write(w, (2**31).to_bytes(4, "little"))
            with pytest.raises(_wire.WireError, match="exceeds"):
                _wire.FrameReader(r).read()
        finally:
            os.close(r)
            os.close(w)

    def test_truncated_frame_is_an_error_not_a_hang(self) -> None:
        r, w = os.pipe()
        try:
            os.write(w, (10).to_bytes(4, "little") + b"abc")
            os.close(w)
            with pytest.raises(_wire.WireError, match="mid-frame"):
                _wire.FrameReader(r).read()
        finally:
            os.close(r)

    def test_a_deadline_applies_to_a_silent_peer(self) -> None:
        r, w = os.pipe()
        try:
            with pytest.raises(TimeoutError):
                _wire.FrameReader(r).read(time.monotonic() + 0.05)
        finally:
            os.close(r)
            os.close(w)


# ---------------------------------------------------------------------------
# parity with Runtime
# ---------------------------------------------------------------------------


class TestParityWithRuntime:
    def test_eval_and_state(self) -> None:
        with _iso() as rt:
            assert rt.eval("1 + 2") == 3
            rt.eval("globalThis.x = 41")
            assert rt.eval("x + 1") == 42

    def test_javascript_errors_keep_their_type_and_leave_the_runtime_usable(
        self,
    ) -> None:
        with _iso() as rt:
            with pytest.raises(JavaScriptError, match="boom"):
                rt.eval("throw new Error('boom')")
            assert not rt.is_closed()
            assert rt.eval("2 + 2") == 4

    def test_a_soft_timeout_raises_runtime_timeout_and_the_worker_survives(
        self,
    ) -> None:
        with _iso(timeout=0.5) as rt:
            with pytest.raises(RuntimeTimeout):
                rt.eval("while (true) {}")
            assert not rt.is_closed()
            assert rt.eval("1") == 1

    def test_eval_async_resolves_promises(self) -> None:
        async def go() -> object:
            with _iso() as rt:
                return await rt.eval_async("Promise.resolve(7).then(x => x * 6)")

        assert asyncio.run(go()) == 42

    def test_host_functions_sync(self) -> None:
        with _iso() as rt:
            rt.bind_function("add", lambda a, b: a + b)
            assert rt.eval("add(40, 2)") == 42

    def test_host_functions_async_run_concurrently(self) -> None:
        async def slow(n: int) -> int:
            await asyncio.sleep(0.4)
            return n * 2

        async def go() -> tuple[object, float]:
            with _iso() as rt:
                rt.bind_function("slow", slow)
                start = time.monotonic()
                out = await rt.eval_async("Promise.all([slow(1), slow(2), slow(3)])")
                return out, time.monotonic() - start

        out, elapsed = asyncio.run(go())
        assert out == [2, 4, 6]
        assert elapsed < 1.0, "three 0.4s calls must overlap, as they do in-process"

    def test_bind_object(self) -> None:
        with _iso() as rt:
            tokens = rt.bind_object(
                "api", {"double": lambda n: n * 2, "version": "1.0"}
            )
            assert set(tokens) == {"double"}
            assert rt.eval("api.double(21)") == 42
            assert rt.eval("api.version") == "1.0"

    def test_values_cross_both_ways(self) -> None:
        seen: list[object] = []
        with _iso() as rt:
            rt.bind_function("keep", lambda v: seen.append(v) or v)
            assert rt.eval("keep(2n ** 70n) === 2n ** 70n") is True
            assert seen[-1] == 2**70
            assert rt.eval("keep(new Uint8Array([1, 2, 3])).length") == 3
            assert seen[-1] == b"\x01\x02\x03"
            assert rt.eval("keep(new Date(0)).getTime()") == 0
            assert isinstance(seen[-1], datetime)
            assert rt.eval("keep(new Set([1, 2])).size") == 2
            assert seen[-1] == {1, 2}
            assert rt.eval("keep(undefined) === undefined") is True
            assert seen[-1] is undefined

    def test_a_host_function_that_raises_does_not_kill_anything(self) -> None:
        def boom() -> None:
            raise ValueError("tool exploded")

        with _iso() as rt:
            rt.bind_function("boom", boom)
            assert rt.eval("try { boom(); 'no' } catch (e) { 'caught' }") == "caught"
            assert rt.eval("1 + 1") == 2

    def test_a_host_function_cannot_reenter_the_runtime(self) -> None:
        with _iso() as rt:
            rt.bind_function("reenter", lambda: rt.eval("1"))
            assert (
                rt.eval("try { reenter(); 'no' } catch (e) { 'blocked' }") == "blocked"
            )

    def test_tool_bridge_attach_budget_and_detach(self) -> None:
        bridge = ToolBridge({"echo": lambda x: x}, namespace="tools", max_calls=2)
        with _iso() as rt:
            bridge.attach(rt)
            assert rt.eval("tools.echo(1)") == 1
            assert rt.eval("tools.echo(2)") == 2
            with pytest.raises(JavaScriptError):
                rt.eval("tools.echo(3)")
            assert bridge.detach(rt) == 1
            with pytest.raises(JavaScriptError):
                rt.eval("tools.echo(1)")

    def test_revoke_op_stops_the_host_function_being_reachable(self) -> None:
        calls: list[int] = []
        with _iso() as rt:
            token = rt.bind_function("ping", lambda: calls.append(1) or "pong")
            assert rt.eval("ping()") == "pong"
            assert rt.revoke_op(token) is True
            with pytest.raises(JavaScriptError):
                rt.eval("ping()")
        assert calls == [1]

    def test_static_modules(self) -> None:
        async def go() -> object:
            with _iso() as rt:
                rt.add_static_module("m", "export const answer = 42;")
                return await rt.eval_async("import('m').then(ns => ns.answer)")

        assert asyncio.run(go()) == 42

    def test_unsupported_config_is_refused_up_front(self) -> None:
        from pydeno import InspectorConfig

        with pytest.raises(ValueError, match="inspector"):
            IsolatedRuntime(RuntimeConfig(inspector=InspectorConfig()))

    def test_a_non_crossable_result_is_a_type_error_not_a_crash(self) -> None:
        with _iso() as rt:
            with pytest.raises(TypeError):
                rt.eval("(() => 1)")
            assert rt.eval("1") == 1


# ---------------------------------------------------------------------------
# containment: the point of the feature
# ---------------------------------------------------------------------------


class TestContainment:
    @pytest.mark.parametrize("name", list(_SINKS))
    def test_native_sink_cannot_take_the_host_down(self, name: str) -> None:
        """Each of these aborts or wedges an in-process Runtime (see the xfails)."""
        rt = IsolatedRuntime(
            RuntimeConfig(timeout=1.0, max_heap_size=64 * MIB), timeout_grace=1.5
        )
        start = time.monotonic()
        with pytest.raises((WorkerCrashed, RuntimeTimeout, JavaScriptError)) as caught:
            rt.eval(_SINKS[name])
        assert time.monotonic() - start < 15
        if not isinstance(caught.value, JavaScriptError):
            assert rt.is_closed(), "a killed or crashed worker must not be reused"
        rt.close()
        # The host is fine and a fresh runtime works.
        with IsolatedRuntime() as fresh:
            assert fresh.eval("1 + 1") == 2

    def test_a_hard_deadline_kills_a_wedged_worker(self) -> None:
        rt = IsolatedRuntime(RuntimeConfig(), request_timeout=1.0)
        start = time.monotonic()
        with pytest.raises(RuntimeTimeout, match="hard deadline"):
            # A spin, not the sparse-array sort: that sort allocates gigabytes, so on a fast
            # machine the memory ceiling can win the race against a 1 s deadline.
            rt.eval("while (true) {}")
        assert time.monotonic() - start < 6
        assert rt.is_closed()
        with pytest.raises(WorkerCrashed, match="closed"):
            rt.eval("1")

    def test_the_hard_deadline_does_not_charge_host_callbacks(self) -> None:
        def slow_tool() -> str:
            time.sleep(1.5)
            return "done"

        with IsolatedRuntime(RuntimeConfig(), request_timeout=1.0) as rt:
            rt.bind_function("slow_tool", slow_tool)
            assert rt.eval("slow_tool()") == "done"

    def test_memory_ceiling_kills_the_worker(self) -> None:
        if sys.platform not in ("linux", "darwin"):
            pytest.skip("RSS polling is implemented for Linux and macOS")
        # An explicit big buffer cap: the default one would turn this into a catchable RangeError,
        # and this test is about the RSS ceiling behind it.
        rt = IsolatedRuntime(
            RuntimeConfig(max_buffer_bytes=8192 * MIB),
            max_memory=300 * MIB,
            request_timeout=30,
        )
        with pytest.raises(WorkerCrashed, match="max_memory"):
            rt.eval("new Uint8Array(1500 * 1024 * 1024).fill(1).length")
        assert rt.is_closed()

    @pytest.mark.parametrize(
        ("js", "jitless"),
        [
            # The two sinks `max_buffer_bytes` cannot see: V8 reserves these pages
            # through its own page allocator. Touched pages show up in RSS.
            (
                "new Uint8Array(new WebAssembly.Memory({initial: 8192}).buffer).fill(1).length",
                False,  # WebAssembly does not exist under --jitless
            ),
            (
                "const b = new ArrayBuffer(8, {maxByteLength: 2 ** 31}); b.resize(2 ** 29);"
                " new Uint8Array(b).fill(1).length",
                True,
            ),
        ],
    )
    def test_memory_ceiling_covers_what_the_buffer_cap_cannot(
        self, js: str, jitless: bool
    ) -> None:
        if sys.platform not in ("linux", "darwin"):
            pytest.skip("RSS polling is implemented for Linux and macOS")
        rt = IsolatedRuntime(
            RuntimeConfig(), max_memory=300 * MIB, request_timeout=30, jitless=jitless
        )
        with pytest.raises(WorkerCrashed, match="max_memory"):
            rt.eval(js)

    def test_the_worker_exits_by_itself_when_over_budget(self) -> None:
        """The in-worker watchdog fires before the parent's poll, with its own exit code."""
        if sys.platform not in ("linux", "darwin"):
            pytest.skip("RSS reading is implemented for Linux and macOS")
        rt = IsolatedRuntime(
            RuntimeConfig(max_buffer_bytes=8192 * MIB),
            max_memory=200 * MIB,
            request_timeout=30,
        )
        rt._max_memory = None  # noqa: SLF001 - disable the parent's check; only the worker's remains
        with pytest.raises(WorkerCrashed, match="went over max_memory"):
            rt.eval("new Uint8Array(900 * 1024 * 1024).fill(1); for (;;) {}")

    def test_worker_death_is_reported_not_hung(self) -> None:
        rt = IsolatedRuntime(RuntimeConfig(), request_timeout=20)
        os.kill(rt._proc.pid, 9)  # noqa: SLF001
        with pytest.raises(WorkerCrashed, match="SIGKILL|died|gone"):
            rt.eval("1")

    def test_cancelling_eval_async_kills_the_worker_instead_of_leaking_it(self) -> None:
        async def go() -> IsolatedRuntime:
            rt = IsolatedRuntime(RuntimeConfig(), request_timeout=20)
            task = asyncio.ensure_future(rt.eval_async("new Promise(() => {})"))
            await asyncio.sleep(0.3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return rt

        rt = asyncio.run(go())
        assert rt.is_closed()
        assert rt._proc.poll() is not None  # noqa: SLF001

    def test_close_is_idempotent_and_reaps_the_process(self) -> None:
        rt = IsolatedRuntime()
        proc = rt._proc  # noqa: SLF001
        rt.close()
        rt.close()
        assert proc.poll() is not None


# ---------------------------------------------------------------------------
# OS sandbox and V8 hardening
# ---------------------------------------------------------------------------

_SANDBOX_PROBE = textwrap.dedent(
    """
    import asyncio, json, os, socket, subprocess, sys, threading
    from pydeno import _sandbox
    import pydeno._awaitable  # noqa  (lazy imports must precede the sandbox)
    asyncio.run(asyncio.sleep(0))
    read_rss = _sandbox.rss_reader()
    import errno
    victim = os.path.join(sys.argv[3], "victim")
    open(victim, "w").write("precious")   # created before the sandbox: it must stay untouchable
    layers = _sandbox.apply()
    out = {"layers": layers, "extras": list(_sandbox.EXTRAS)}
    out["rss_after_sandbox"] = "ok" if (read_rss() or 0) > 0 else "dead"
    def attempt(name, fn):
        try:
            fn(); out[name] = "allowed"
        except OSError as e:
            out[name] = "denied:" + errno.errorcode.get(e.errno, str(e.errno))
        except Exception as e:
            out[name] = "denied:" + type(e).__name__
    attempt("read_file", lambda: open("/etc/hosts").read(1))
    attempt("write_file", lambda: open("/tmp/pydeno_sandbox_probe", "w").write("x"))
    attempt("listdir", lambda: os.listdir("/"))
    attempt("tcp", lambda: socket.create_connection(("127.0.0.1", 9), timeout=1))
    attempt("udp", lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("127.0.0.1", 9)))
    attempt("exec", lambda: subprocess.run(["/bin/echo", "x"], capture_output=True, check=True))
    attempt("fork", lambda: os.fork())
    attempt("stat_existing", lambda: os.stat("/etc/hosts"))
    attempt("stat_missing", lambda: os.stat("/nonexistent-pydeno-probe"))
    attempt("access_existing", lambda: os.access("/etc/hosts", os.F_OK) or (_ for _ in ()).throw(OSError(13, "no")))
    attempt("readlink", lambda: os.readlink("/etc/localtime" if os.path.islink("/etc/localtime") else "/proc/self/exe"))
    attempt("kill_parent", lambda: os.kill(os.getppid(), 0))
    attempt("killpg_parent", lambda: os.killpg(os.getpgid(os.getppid()), 0))
    def unix_send(addr):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.sendto(b"escaped", addr)
    attempt("unix_send_path", lambda: unix_send(sys.argv[1]))
    attempt("unix_send_abstract", lambda: unix_send("\\0" + sys.argv[2]))
    attempt("mkdir", lambda: os.mkdir("/tmp/pydeno_sandbox_probe_dir"))
    attempt("symlink", lambda: os.symlink("/etc/hosts", "/tmp/pydeno_sandbox_probe_ln"))
    attempt("chmod", lambda: os.chmod(victim, 0o777))
    attempt("chown", lambda: os.chown(victim, os.getuid(), os.getgid()))
    attempt("utime", lambda: os.utime(victim, (0, 0)))
    attempt("unlink", lambda: os.unlink(victim))
    attempt("rename", lambda: os.rename(victim, victim + ".moved"))
    attempt("overwrite", lambda: open(victim, "w").write("pwned"))
    attempt("read_victim", lambda: open(victim).read())
    attempt("setuid", lambda: os.setuid(os.getuid()))
    attempt("setgid", lambda: os.setgid(os.getgid()))
    attempt("setgroups", lambda: os.setgroups([]))
    if sys.platform.startswith("linux"):
        attempt("setxattr", lambda: os.setxattr(victim, "user.pwned", b"1"))
    attempt("env_file", lambda: open("/proc/self/environ" if sys.platform != "darwin" else "/etc/passwd").read(1))
    try:
        os.kill(os.getpid(), 0); out["kill_self"] = "allowed"
    except OSError:
        out["kill_self"] = "denied"
    # things the worker legitimately needs must keep working
    attempt("socketpair", lambda: socket.socketpair())
    t = threading.Thread(target=lambda: None); t.start(); t.join(); out["thread"] = "ok"
    out["asyncio"] = asyncio.run(asyncio.sleep(0, "ok"))
    print(json.dumps(out))
    """
)


def _probe_sandbox() -> dict[str, str]:
    import json
    import subprocess

    import socket
    import tempfile
    import uuid

    # Two datagram sockets the sandboxed process must not be able to reach: one on the
    # filesystem, one in the abstract namespace (Linux only; Landlock below ABI 6 does not
    # govern it, so seccomp has to).
    tmp = tempfile.mkdtemp(prefix="pydeno-sb-")
    path = os.path.join(tmp, "s")
    abstract = f"pydeno-sb-{uuid.uuid4().hex}"
    path_srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    path_srv.bind(path)
    path_srv.setblocking(False)
    abs_srv = None
    if sys.platform.startswith("linux"):
        abs_srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        abs_srv.bind("\0" + abstract)
        abs_srv.setblocking(False)
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-c", _SANDBOX_PROBE, path, abstract, tmp],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            start_new_session=True,  # like the real worker: its own process group
        )
        assert done.returncode == 0, done.stderr
        out = json.loads(done.stdout.strip().splitlines()[-1])
        for name, srv in (("path", path_srv), ("abstract", abs_srv)):
            if srv is None:
                continue
            try:
                got = srv.recv(16)
            except BlockingIOError:
                got = b""
            out[f"{name}_datagram_received"] = got.decode() or "nothing"
        # Judge the file from outside: the probe's own report could be wrong, the file cannot.
        victim = os.path.join(tmp, "victim")
        out["victim_exists"] = os.path.exists(victim)
        if out["victim_exists"]:
            with open(victim) as fh:
                out["victim_content"] = fh.read()
            out["victim_mode"] = oct(os.stat(victim).st_mode & 0o777)
            out["victim_mtime_zero"] = os.stat(victim).st_mtime == 0
        out["victim_moved_exists"] = os.path.exists(victim + ".moved")
        return out
    finally:
        path_srv.close()
        if abs_srv is not None:
            abs_srv.close()
        for leftover in (
            path,
            os.path.join(tmp, "victim"),
            os.path.join(tmp, "victim.moved"),
        ):
            try:
                os.unlink(leftover)
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass


_HAS_OS_SANDBOX = sys.platform == "darwin" or sys.platform.startswith("linux")


def _layer_expected(layer: str, probe: dict[str, object]) -> bool:
    """Should `layer` be in force? The container matrix says so through
    `PYDENO_EXPECT_SANDBOX`; outside it, trust whatever the kernel gave us."""
    expected = os.environ.get("PYDENO_EXPECT_SANDBOX")
    return layer in (expected if expected is not None else str(probe["layers"]))


@pytest.fixture(scope="module")
def sandbox_probe() -> dict[str, object]:
    if not _HAS_OS_SANDBOX:
        pytest.skip("no OS sandbox on this platform")
    return _probe_sandbox()  # type: ignore[return-value]


# Every operation a V8 escape would reach for. Each is attempted from inside a process
# that has applied `_sandbox.apply()`, so a denial here is the sandbox's, not the guest's.
_FORBIDDEN = [
    "read_file",
    "write_file",
    "listdir",
    "tcp",
    "udp",
    "exec",
    "fork",
    "kill_parent",
    "killpg_parent",
    "unix_send_path",
    "mkdir",
    "symlink",
    "chmod",
    "chown",
    "utime",
    "unlink",
    "rename",
    "overwrite",
    "read_victim",
    "setgroups",
    "env_file",
]
# `setuid(getuid())` is a harmless no-op that succeeds on macOS; the seccomp filter
# denies the whole family so that a worker running as root (a container) cannot change
# who it is. Asserted in the Linux-only seccomp test below.


class TestOsSandbox:
    def test_the_probe_applied_exactly_the_layers_the_environment_expects(
        self, sandbox_probe: dict[str, object]
    ) -> None:
        """The container matrix knows which kernel features it left available (default
        profile: all; one that hides Landlock: seccomp only; and so on) and says so in
        `PYDENO_EXPECT_SANDBOX`. Outside it, anything but "none" is expected."""
        expected = os.environ.get("PYDENO_EXPECT_SANDBOX")
        if expected is None:
            assert sandbox_probe["layers"] != "none", sandbox_probe
        else:
            assert sandbox_probe["layers"] == expected, sandbox_probe

    @pytest.mark.full_sandbox
    @pytest.mark.parametrize("operation", _FORBIDDEN)
    def test_forbidden_operation_is_denied(
        self, sandbox_probe: dict[str, object], operation: str
    ) -> None:
        assert str(sandbox_probe[operation]).startswith("denied"), (
            operation,
            sandbox_probe,
        )

    @pytest.mark.full_sandbox
    def test_the_victim_file_is_untouched_judged_from_outside(
        self, sandbox_probe: dict[str, object]
    ) -> None:
        """The probe's own report could be wrong; the file on disk cannot."""
        assert sandbox_probe["victim_exists"] is True, sandbox_probe
        assert sandbox_probe["victim_content"] == "precious"
        assert sandbox_probe["victim_mode"] != "0o777"
        assert sandbox_probe["victim_mtime_zero"] is False
        assert sandbox_probe["victim_moved_exists"] is False

    @pytest.mark.full_sandbox
    def test_no_datagram_escapes_to_a_unix_socket_on_the_filesystem(
        self, sandbox_probe: dict[str, object]
    ) -> None:
        assert sandbox_probe["path_datagram_received"] == "nothing"

    @pytest.mark.linux_only
    @pytest.mark.full_sandbox
    def test_no_datagram_escapes_to_an_abstract_unix_socket(
        self, sandbox_probe: dict[str, object]
    ) -> None:
        assert str(sandbox_probe["unix_send_abstract"]).startswith("denied")
        assert sandbox_probe["abstract_datagram_received"] == "nothing"

    @pytest.mark.linux_only
    @pytest.mark.parametrize(
        "operation",
        [
            "chmod",
            "chown",
            "utime",
            "setuid",
            "setgid",
            "setgroups",
            "setxattr",
            "kill_parent",
            "unix_send_path",
            "fork",
            "exec",
        ],
    )
    def test_seccomp_denies_with_eperm_not_just_any_failure(
        self, sandbox_probe: dict[str, object], operation: str
    ) -> None:
        """Landlock answers EACCES; EPERM is the seccomp filter speaking, which is what
        proves these syscalls themselves are blocked rather than the path being missing.

        Whether seccomp is *supposed* to be active here is the environment's say
        (`PYDENO_EXPECT_SANDBOX`, set by the container matrix), never this test's: a layer
        that vanishes where it was expected is a failure, not a skip."""
        if _layer_expected("seccomp", sandbox_probe):
            assert sandbox_probe[operation] == "denied:EPERM", (
                operation,
                sandbox_probe,
            )
        else:
            assert "seccomp" not in str(sandbox_probe["layers"])

    @pytest.mark.linux_only
    @pytest.mark.parametrize(
        "operation", ["read_file", "write_file", "listdir", "mkdir"]
    )
    def test_landlock_denies_filesystem_access_with_eacces(
        self, sandbox_probe: dict[str, object], operation: str
    ) -> None:
        if _layer_expected("landlock", sandbox_probe):
            # With the empty root in force the path does not exist at all, which is a stronger
            # "no" than Landlock's EACCES: the denial happens before Landlock is consulted.
            # (`/` itself still exists, as the empty root, so listing *it* is Landlock's EACCES.)
            expected = (
                "denied:ENOENT"
                if "emptyroot" in sandbox_probe["extras"]  # type: ignore[operator]
                and operation != "listdir"
                else "denied:EACCES"
            )
            if operation == "mkdir" and _layer_expected("seccomp", sandbox_probe):
                # `mkdirat` is on the seccomp deny list, which answers before the filesystem
                # layers are consulted.
                expected = "denied:EPERM"
            assert sandbox_probe[operation] == expected, (operation, sandbox_probe)
        else:
            assert "landlock" not in str(sandbox_probe["layers"])

    @pytest.mark.full_sandbox
    def test_what_path_metadata_a_sandboxed_process_can_still_learn_is_as_documented(
        self, sandbox_probe: dict[str, object]
    ) -> None:
        """Pinned so the documentation cannot drift from reality, and so that if a kernel ever
        gains a way to hide this, the test says so.

        - Path *existence* is observable on both platforms: a missing path answers ENOENT before
          any sandbox check runs.
        - macOS then refuses `stat` on an existing path (EPERM), so size and times stay hidden.
        - Linux Landlock does not govern metadata, so `stat` works: size, times and owner of any
          path are visible. (Contents are not.)
        """
        assert sandbox_probe["stat_missing"] == "denied:ENOENT"
        if sys.platform == "darwin":
            assert sandbox_probe["stat_existing"] == "denied:EPERM"
        elif "emptyroot" in sandbox_probe["extras"]:  # type: ignore[operator]
            # nothing exists in an empty root, so existence is hidden as well
            assert sandbox_probe["stat_existing"] == "denied:ENOENT"
        else:
            assert sandbox_probe["stat_existing"] == "allowed"

    @pytest.mark.linux_only
    @pytest.mark.full_sandbox
    def test_the_empty_root_hides_every_host_path_when_it_is_available(
        self, sandbox_probe: dict[str, object]
    ) -> None:
        """Where unprivileged user namespaces are allowed, the worker's world is empty: every
        probe path answers ENOENT, so it cannot even tell what is installed on the host. Where
        they are not allowed the layer is simply absent, and the residual above applies."""
        if "emptyroot" not in sandbox_probe["extras"]:  # type: ignore[operator]
            assert sandbox_probe["stat_existing"] == "allowed"
            return
        for name in ("stat_existing", "stat_missing", "access_existing", "readlink"):
            assert str(sandbox_probe[name]).startswith("denied"), (name, sandbox_probe)
        assert sandbox_probe["stat_existing"] == "denied:ENOENT"

    def test_a_process_may_still_signal_itself(
        self, sandbox_probe: dict[str, object]
    ) -> None:
        assert sandbox_probe["kill_self"] == "allowed"

    @pytest.mark.parametrize("capability", ["socketpair", "thread", "asyncio"])
    def test_what_the_worker_needs_keeps_working(
        self, sandbox_probe: dict[str, object], capability: str
    ) -> None:
        assert sandbox_probe[capability] in ("allowed", "ok"), sandbox_probe

    def test_the_memory_watchdog_survives_the_sandbox(
        self, sandbox_probe: dict[str, object]
    ) -> None:
        # opened before the sandbox: Landlock forbids opening /proc files afterwards
        assert sandbox_probe["rss_after_sandbox"] == "ok"

    def test_the_worker_reports_the_layers_it_applied(self) -> None:
        with IsolatedRuntime() as rt:
            expected = os.environ.get("PYDENO_EXPECT_SANDBOX")
            if expected is not None:
                assert rt.sandbox == expected
            elif sys.platform == "darwin":
                assert rt.sandbox == "seatbelt"
            elif sys.platform.startswith("linux"):
                assert "seccomp" in rt.sandbox
            assert rt.v8_flags == ["--jitless"]

    def test_the_empty_root_can_be_turned_off_and_then_is_absent(self) -> None:
        with IsolatedRuntime(empty_root=False) as rt:
            assert "emptyroot" not in rt.sandbox_extras
            assert rt.eval("1 + 1") == 2

    def test_extras_are_reported_and_only_the_known_one(self) -> None:
        with IsolatedRuntime() as rt:
            assert set(rt.sandbox_extras) <= {"emptyroot"}
            if sys.platform == "darwin":
                assert (
                    rt.sandbox_extras == []
                )  # Seatbelt has no such layer; it is not needed

    def test_no_extras_when_there_is_no_sandbox(self) -> None:
        with IsolatedRuntime(sandbox="off") as rt:
            assert rt.sandbox_extras == []

    def test_the_worker_works_the_same_with_and_without_the_empty_root(self) -> None:
        results = []
        for empty_root in (True, False):
            with IsolatedRuntime(
                RuntimeConfig(timeout=10.0), empty_root=empty_root
            ) as rt:
                rt.bind_function("double", lambda n: n * 2)
                results.append(
                    (
                        rt.eval("double(21)"),
                        rt.eval("[1, 2, 3].map(x => x * 2)"),
                        rt.eval("2n ** 70n"),
                    )
                )
        assert results[0] == results[1]

    def test_sandbox_off_applies_nothing(self) -> None:
        with IsolatedRuntime(sandbox="off") as rt:
            assert rt.sandbox == "none"
            assert rt.eval("1") == 1

    def test_require_fails_closed_where_nothing_can_be_applied(
        self, tmp_path: Path
    ) -> None:
        # A worker that reports "none" must be refused when the caller demanded a sandbox.
        # (Exercised through the real worker: a platform without a sandbox raises at init.)
        # `require` means every layer of the platform, so a kernel missing one (the
        # no-landlock / no-seccomp matrix profiles) must be refused too, not just "none".
        expected = os.environ.get("PYDENO_EXPECT_SANDBOX")
        if expected is not None:
            applied = set(expected.split("+")) - {"none"}
            full = {"linux": {"landlock", "seccomp"}, "darwin": {"seatbelt"}}.get(
                "linux" if sys.platform.startswith("linux") else sys.platform
            )
            nothing_available = full is None or not full <= applied
        else:
            nothing_available = not (
                sys.platform == "darwin" or sys.platform.startswith("linux")
            )
        if nothing_available:
            with pytest.raises(WorkerCrashed, match="sandbox"):
                IsolatedRuntime(sandbox="require")
        else:
            with IsolatedRuntime(sandbox="require") as rt:
                assert rt.sandbox != "none"

    def test_unknown_sandbox_mode_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="sandbox"):
            IsolatedRuntime(sandbox="sometimes")

    def test_a_sandboxed_worker_still_runs_every_feature(self) -> None:
        """The sandbox removes the filesystem: nothing the worker does may need it."""

        async def go() -> None:
            async def double(n: int) -> int:
                await asyncio.sleep(0)
                return n * 2

            with IsolatedRuntime(RuntimeConfig(timeout=5.0)) as rt:
                rt.bind_function("double", double)
                rt.bind_function("blob", lambda: b"\x00\x01")
                rt.bind_function(
                    "when", lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
                )
                rt.bind_object("o", {"s": {1, 2}, "n": 2**70})
                rt.add_static_module("m", "export const x = 1;")
                assert await rt.eval_async("double(21)") == 42
                assert rt.eval("blob().length") == 2
                assert rt.eval("when().getUTCFullYear()") == 2026
                assert rt.eval("o.n === 2n ** 70n") is True
                assert await rt.eval_async("import('m').then(m => m.x)") == 1
                assert rt.eval("[...o.s].length") == 2
                assert (
                    rt.eval("new Uint8Array(32 * 1024 * 1024).fill(1).length")
                    == 32 * MIB
                )

        asyncio.run(go())


class TestModulesAndConsole:
    """Host callbacks the worker holds only stubs for: module resolver/loader, console."""

    def test_a_custom_loader_supplies_module_source(self) -> None:
        async def go() -> object:
            with _iso() as rt:
                rt.set_module_resolver(
                    lambda spec, ref: spec if spec.startswith("custom:") else None
                )
                rt.set_module_loader(
                    lambda spec: (
                        "export const answer = 42; export default 'hi';"
                        if spec == "custom:entry"
                        else (_ for _ in ()).throw(ValueError(f"unknown module {spec}"))
                    )
                )
                return await rt.eval_module_async("custom:entry")

        ns = asyncio.run(go())
        assert ns["answer"] == 42

    def test_an_async_loader_works(self) -> None:
        async def loader(spec: str) -> str:
            await asyncio.sleep(0.01)
            return f"export const name = {spec!r};"

        async def go() -> object:
            with _iso() as rt:
                rt.set_module_resolver(lambda spec, ref: spec)
                rt.set_module_loader(loader)
                return await rt.eval_module_async("custom:async")

        assert asyncio.run(go())["name"] == "custom:async"

    def test_imports_between_host_supplied_modules(self) -> None:
        sources = {
            "custom:a": "import { b } from 'custom:b'; export const a = b + 1;",
            "custom:b": "export const b = 41;",
        }

        async def go() -> object:
            with _iso() as rt:
                rt.set_module_resolver(lambda spec, ref: spec)
                rt.set_module_loader(lambda spec: sources[spec])
                return await rt.eval_module_async("custom:a")

        assert asyncio.run(go())["a"] == 42

    def test_a_loader_that_raises_is_a_catchable_error_and_the_runtime_survives(
        self,
    ) -> None:
        def loader(spec: str) -> str:
            raise ValueError("no such module")

        async def go() -> None:
            with _iso() as rt:
                rt.set_module_resolver(lambda spec, ref: spec)
                rt.set_module_loader(loader)
                # RuntimeError, exactly as an in-process Runtime reports a failed module load.
                with pytest.raises(RuntimeError, match="Failed to load module"):
                    await rt.eval_module_async("custom:missing")
                assert not rt.is_closed()
                assert rt.eval("1 + 1") == 2

        asyncio.run(go())

    def test_no_resolver_and_no_loader_means_nothing_is_importable(self) -> None:
        async def go() -> None:
            with _iso() as rt:
                for spec in (
                    "file:///etc/passwd",
                    "http://127.0.0.1/x.js",
                    "node:fs",
                    "data:text/javascript,1",
                ):
                    with pytest.raises(JavaScriptError):
                        await rt.eval_async(f"import({spec!r})")

        asyncio.run(go())

    def test_a_hostile_specifier_cannot_reach_anything_but_the_hosts_loader(
        self,
    ) -> None:
        seen: list[str] = []

        def loader(spec: str) -> str:
            seen.append(spec)
            raise ValueError("denied")

        async def go() -> None:
            with _iso() as rt:
                rt.set_module_resolver(lambda spec, ref: spec)
                rt.set_module_loader(loader)
                for spec in (
                    "file:///etc/passwd",
                    "../../../etc/passwd",
                    "x" * 100_000,
                ):
                    with pytest.raises(JavaScriptError):
                        await rt.eval_async(f"import({spec!r})")

        asyncio.run(go())
        # The only thing that ever ran was the host's own loader, which said no.
        assert all(isinstance(s, str) for s in seen)

    def test_static_module_by_sync_eval_module(self) -> None:
        with _iso() as rt:
            rt.add_static_module("lib", "export const value = 7;")
            assert rt.eval_module("static:lib")["value"] == 7

    def test_console_reaches_the_hosts_callback(self) -> None:
        seen: list[tuple[str, list[object]]] = []
        cfg = RuntimeConfig(
            timeout=5.0,
            enable_console=True,
            on_console=lambda lvl, args: seen.append((lvl, args)),
        )
        with IsolatedRuntime(cfg) as rt:
            rt.eval(
                "console.log('a', 1, {x: 2}); console.warn('w'); console.error('e')"
            )
        levels = [lvl for lvl, _ in seen]
        assert levels == ["log", "warn", "error"]
        assert seen[0][1] == ["a", 1, {"x": 2}]

    def test_a_broken_console_callback_never_breaks_the_guest(self) -> None:
        def boom(level: str, args: list[object]) -> None:
            raise RuntimeError("logger exploded")

        cfg = RuntimeConfig(timeout=5.0, enable_console=True, on_console=boom)
        with IsolatedRuntime(cfg) as rt:
            assert rt.eval("console.log('x'); 7") == 7

    def test_console_arguments_that_cannot_cross_are_dropped_not_fatal(self) -> None:
        cfg = RuntimeConfig(
            timeout=5.0, enable_console=True, on_console=lambda *a: None
        )
        with IsolatedRuntime(cfg) as rt:
            assert rt.eval("console.log(() => 1, Symbol('s')); 'alive'") == "alive"

    def test_a_flood_of_console_output_does_not_trip_the_deadline(self) -> None:
        count = {"n": 0}
        cfg = RuntimeConfig(
            timeout=20.0,
            enable_console=True,
            on_console=lambda lvl, args: count.__setitem__("n", count["n"] + 1),
        )
        with IsolatedRuntime(cfg, request_timeout=20) as rt:
            rt.eval("for (let i = 0; i < 2000; i++) console.log(i)")
        assert count["n"] == 2000

    def test_on_console_fires_regardless_of_enable_console_like_in_process(
        self,
    ) -> None:
        """`enable_console` only controls the guest's own stdout echo; the callback is independent."""
        seen_isolated: list[object] = []
        seen_inprocess: list[object] = []
        cfg_iso = RuntimeConfig(
            timeout=5.0, on_console=lambda *a: seen_isolated.append(a)
        )
        cfg_in = RuntimeConfig(
            timeout=5.0, on_console=lambda *a: seen_inprocess.append(a)
        )
        with IsolatedRuntime(cfg_iso) as rt:
            rt.eval("console.log('x', 1)")
        from pydeno import Runtime

        Runtime(cfg_in).eval("console.log('x', 1)")
        assert seen_isolated == seen_inprocess == [("log", ["x", 1])]

    def test_without_a_callback_console_calls_are_inert(self) -> None:
        with IsolatedRuntime(RuntimeConfig(timeout=5.0)) as rt:
            assert rt.eval("console.log('nobody listening'); 5") == 5

    def test_inspector_is_still_refused(self) -> None:
        from pydeno import InspectorConfig

        with pytest.raises(ValueError, match="inspector"):
            IsolatedRuntime(RuntimeConfig(inspector=InspectorConfig()))


class TestSafeDefaults:
    def test_limits_are_on_unless_you_remove_them(self) -> None:
        with IsolatedRuntime() as rt:
            assert rt._max_memory == 1024 * MIB  # noqa: SLF001
            assert rt._hard_timeout(None) == 60.0  # noqa: SLF001
            assert rt._hard_timeout(2.0) == 4.0  # noqa: SLF001

    def test_none_removes_a_limit(self) -> None:
        with IsolatedRuntime(max_memory=None, request_timeout=None) as rt:
            assert rt._max_memory is None  # noqa: SLF001
            assert rt._hard_timeout(None) is None  # noqa: SLF001

    def test_an_explicit_deadline_wins_over_the_soft_timeout(self) -> None:
        with IsolatedRuntime(request_timeout=7) as rt:
            assert rt._hard_timeout(2.0) == 7.0  # noqa: SLF001

    def test_a_guest_cannot_loop_on_cheap_host_calls_forever(self) -> None:
        """The guest's clock stops while a host function runs, so quick calls need a cap."""
        calls: list[int] = []
        rt = IsolatedRuntime(max_host_calls=50, request_timeout=30)
        rt.bind_function("tick", lambda: calls.append(1))
        start = time.monotonic()
        with pytest.raises(WorkerCrashed, match="max_host_calls"):
            rt.eval("for (;;) tick()")
        assert time.monotonic() - start < 10
        assert len(calls) == 50
        assert rt.is_closed()

    def test_the_host_call_budget_is_not_hit_by_ordinary_use(self) -> None:
        with IsolatedRuntime(max_host_calls=5) as rt:
            rt.bind_function("one", lambda: 1)
            assert rt.eval("one() + one() + one()") == 3

    def test_negative_budget_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            IsolatedRuntime(max_host_calls=-1)


class TestHostileSource:
    """Monty's `source_nesting` / `parse_large_literals` categories, on the isolated path."""

    @pytest.mark.parametrize(
        "source",
        [
            "(" * 100_000 + "1" + ")" * 100_000,
            "[" * 100_000 + "]" * 100_000,
            "a" + ".x" * 200_000,
            "1" + "+1" * 500_000,
            "'" + "x" * (8 * MIB) + "'.length",
            "[" + "0," * 2_000_000 + "0].length",
            "`" + "${" * 50_000,
            "/" + "(" * 20_000 + "/",
            "\x00\x01\x02 not javascript ￾",
        ],
        # Explicit ids: the default id is the source itself, up to 8 MiB on one line of `pytest
        # --co` output, and the CI runner's log handling stalls on lines that long.
        ids=[
            "100k-nested-parens",
            "100k-nested-brackets",
            "200k-member-chain",
            "500k-plus-chain",
            "8mib-string-literal",
            "2m-element-array-literal",
            "50k-template-openers",
            "20k-regex-parens",
            "control-characters",
        ],
    )
    def test_hostile_source_ends_in_a_catchable_error_and_the_host_survives(
        self, source: str
    ) -> None:
        rt = IsolatedRuntime(RuntimeConfig(timeout=4.0), timeout_grace=4.0)
        try:
            try:
                rt.eval(source)
            except (JavaScriptError, WorkerCrashed, RuntimeTimeout, TypeError):
                pass
        finally:
            rt.close()
        with IsolatedRuntime() as fresh:
            assert fresh.eval("1 + 1") == 2


class TestWireFuzz:
    """The parent decodes whatever a (possibly compromised) worker sends. Like Monty's
    cargo-fuzz targets: nothing may raise except `WireError`, and nothing may hang."""

    def _seeds(self) -> list[bytes]:
        values = [
            None,
            True,
            2**80,
            float("nan"),
            b"\x00\xff",
            {"a": [1, {"b": None}]},
            {1, 2},
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            undefined,
            {1: 2},
        ]
        msgs = [{"t": "result", "id": 1, "v": native_encode(v)} for v in values]
        msgs.append(
            {
                "t": "call",
                "cid": 1,
                "hid": 1,
                "args": [native_encode(v) for v in values],
            }
        )
        return [_wire.dumps(m) for m in msgs]

    def test_random_bytes(self) -> None:
        import random

        rng = random.Random(0xD3_0)
        for _ in range(3000):
            blob = rng.randbytes(rng.randrange(0, 200))
            try:
                msg = _wire.loads(blob)
                _wire.decode_value(msg.get("v"))
            except _wire.WireError:
                pass

    def test_mutated_valid_messages(self) -> None:
        import random

        rng = random.Random(0xD3_1)
        seeds = self._seeds()
        for _ in range(6000):
            data = bytearray(rng.choice(seeds))
            for _ in range(rng.randrange(1, 6)):
                kind = rng.randrange(4)
                pos = rng.randrange(len(data)) if data else 0
                if kind == 0 and data:
                    data[pos] = rng.randrange(256)
                elif kind == 1 and data:
                    del data[pos : pos + rng.randrange(1, 8)]
                elif kind == 2:
                    data[pos:pos] = rng.randbytes(rng.randrange(1, 8))
                elif data:
                    data[pos : pos + 1] = rng.choice(
                        [b'"', b"[", b"{", b"]", b"}", b"$", b"0", b"-"]
                    )
            try:
                msg = _wire.loads(bytes(data))
                _wire.decode_value(msg.get("v"))
                for arg in (
                    msg.get("args", []) if isinstance(msg.get("args"), list) else []
                ):
                    _wire.decode_value(arg)
            except _wire.WireError:
                pass

    def test_random_frame_streams_never_hang_or_leak_other_errors(self) -> None:
        import random

        rng = random.Random(0xD3_2)
        for _ in range(200):
            r, w = os.pipe()
            try:
                os.write(w, rng.randbytes(rng.randrange(0, 300)))
                os.close(w)
                reader = _wire.FrameReader(r, max_frame=1024)
                for _ in range(5):
                    try:
                        if reader.read(time.monotonic() + 1.0) is None:
                            break
                    except _wire.WireError:
                        break
            finally:
                os.close(r)


class TestV8Hardening:
    def test_jitless_removes_webassembly_and_the_jit(self) -> None:
        with IsolatedRuntime() as rt:
            assert rt.eval("typeof WebAssembly") == "undefined"

    def test_jitless_can_be_turned_off_for_webassembly(self) -> None:
        with IsolatedRuntime(jitless=False) as rt:
            assert rt.v8_flags == []
            assert rt.eval("typeof WebAssembly") == "object"

    def test_extra_flags_are_applied_before_the_isolate(self) -> None:
        with IsolatedRuntime(
            v8_flags=["--disallow-code-generation-from-strings"]
        ) as rt:
            with pytest.raises(JavaScriptError):
                rt.eval("new Function('return 1')()")

    def test_unknown_flags_are_refused_not_ignored(self) -> None:
        with pytest.raises(WorkerCrashed, match="recognise"):
            IsolatedRuntime(v8_flags=["--definitely-not-a-flag"])

    def test_the_host_timezone_and_locale_do_not_reach_the_guest(self) -> None:
        with IsolatedRuntime() as rt:
            assert rt.eval("Intl.DateTimeFormat().resolvedOptions().timeZone") == "UTC"
            assert rt.eval("new Date().getTimezoneOffset()") == 0

    def test_flags_cannot_be_changed_once_a_runtime_exists_in_the_process(self) -> None:
        from pydeno import Runtime
        from pydeno._pydeno import _set_v8_flags

        Runtime()
        with pytest.raises(RuntimeError, match="before the first Runtime"):
            _set_v8_flags(["--jitless"])


# ---------------------------------------------------------------------------
# the worker is untrusted
# ---------------------------------------------------------------------------

_FAKE = textwrap.dedent(
    """
    import json, os, struct, sys, time
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
    read()
    send({"t": "ready", "version": 1, "sandbox": "none"})
    cmd = read()
    MODE = %r
    if MODE == "oversize":
        raw(struct.pack("<I", 2**31)); time.sleep(30)
    elif MODE == "badjson":
        raw(struct.pack("<I", 9) + b"{not json"); time.sleep(30)
    elif MODE == "unknown_call":
        send({"t": "call", "cid": 1, "hid": 999, "args": []}); time.sleep(30)
    elif MODE == "bad_tag":
        send({"t": "result", "id": cmd["id"], "v": {"$": "zzz", "v": 1}}); time.sleep(30)
    elif MODE == "wrong_id":
        send({"t": "result", "id": 12345, "v": 1}); time.sleep(30)
    elif MODE == "deep":
        v = 0
        for _ in range(500):
            v = [v]
        send({"t": "result", "id": cmd["id"], "v": v}); time.sleep(30)
    elif MODE == "env":
        send({"t": "result", "id": cmd["id"], "v": sorted(os.environ)})
        time.sleep(30)
    elif MODE == "drip":
        # a valid-looking 1 MiB frame header, then one byte every 20ms, forever
        raw(struct.pack("<I", 1 << 20))
        while True:
            raw(b" "); time.sleep(0.02)
    elif MODE == "bigerr":
        send({"t": "error", "id": cmd["id"], "kind": "RuntimeError", "msg": "x" * (5 << 20)})
        time.sleep(30)
    elif MODE == "unknown_kind":
        send({"t": "error", "id": cmd["id"], "kind": "SystemExit", "msg": "nope"})
        time.sleep(30)
    elif MODE == "call_flood":
        # a compromised worker hammering the parent with host calls
        for i in range(1, 100000):
            send({"t": "call", "cid": i, "hid": 1, "args": []})
    elif MODE == "dup_result":
        send({"t": "result", "id": cmd["id"], "v": 1})
        send({"t": "result", "id": cmd["id"], "v": 2}); time.sleep(30)
    elif MODE == "escape_error":
        send({"t": "error", "id": cmd["id"], "kind": "RuntimeError",
              "msg": "boom\\x1b[2J\\x1b]0;pwned\\x07\\nat line 2"})
        time.sleep(30)
    elif MODE == "dup_token":
        # answers every command with the same capability token
        while True:
            send({"t": "result", "id": cmd["id"], "v": 7})
            cmd = read()
    elif MODE == "wrong_object_keys":
        # answers every command with a token map that does not match what was bound
        while True:
            send({"t": "result", "id": cmd["id"], "v": {"unrelated": 7}})
            cmd = read()
    """
)


def _fake_worker(tmp_path: Path, mode: str) -> str:
    script = tmp_path / f"fake_{mode}.py"
    script.write_text(_FAKE % mode)
    wrapper = tmp_path / f"fake_{mode}.sh"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


class TestUntrustedWorker:
    @pytest.mark.parametrize(
        "mode", ["oversize", "badjson", "unknown_call", "bad_tag", "wrong_id", "deep"]
    )
    def test_a_hostile_frame_discards_the_worker_and_spares_the_parent(
        self, tmp_path: Path, mode: str
    ) -> None:
        rt = IsolatedRuntime(
            RuntimeConfig(), python=_fake_worker(tmp_path, mode), request_timeout=20
        )
        start = time.monotonic()
        with pytest.raises(WorkerCrashed, match="protocol"):
            rt.eval("1")
        assert time.monotonic() - start < 10, "must not wait out the hostile worker"
        assert rt.is_closed()
        assert rt._proc.poll() is not None  # noqa: SLF001

    def test_a_worker_dripping_a_frame_one_byte_at_a_time_cannot_stall_the_parent(
        self, tmp_path: Path
    ) -> None:
        """No frame ever completes, but bytes keep arriving, so the pipe is never idle. The
        hard deadline must still fire."""
        rt = IsolatedRuntime(
            RuntimeConfig(), python=_fake_worker(tmp_path, "drip"), request_timeout=1.5
        )
        start = time.monotonic()
        with pytest.raises(RuntimeTimeout, match="hard deadline"):
            rt.eval("1")
        assert time.monotonic() - start < 10
        assert rt.is_closed()
        assert rt._proc.poll() is not None  # noqa: SLF001

    def test_a_huge_error_message_from_the_worker_is_truncated(
        self, tmp_path: Path
    ) -> None:
        rt = IsolatedRuntime(
            RuntimeConfig(), python=_fake_worker(tmp_path, "bigerr"), request_timeout=20
        )
        try:
            with pytest.raises(RuntimeError) as caught:
                rt.eval("1")
        finally:
            rt.close()
        assert len(str(caught.value)) < 70_000
        assert "more characters" in str(caught.value)

    def test_an_unknown_error_kind_cannot_pick_an_arbitrary_exception_class(
        self, tmp_path: Path
    ) -> None:
        """The worker names an exception *class* in the frame. Only a fixed set is honoured,
        so it cannot make the parent raise (or construct) something like `SystemExit`."""
        rt = IsolatedRuntime(
            RuntimeConfig(),
            python=_fake_worker(tmp_path, "unknown_kind"),
            request_timeout=20,
        )
        try:
            with pytest.raises(RuntimeError) as caught:
                rt.eval("1")
        finally:
            rt.close()
        assert type(caught.value) is RuntimeError

    def test_a_duplicate_result_is_a_protocol_violation_not_a_second_answer(
        self, tmp_path: Path
    ) -> None:
        rt = IsolatedRuntime(
            RuntimeConfig(),
            python=_fake_worker(tmp_path, "dup_result"),
            request_timeout=20,
        )
        try:
            assert rt.eval("1") == 1  # the first answer is taken ...
            with pytest.raises(WorkerCrashed):
                rt.eval("2")  # ... the stray second one poisons the next command
        finally:
            rt.close()

    def test_a_compromised_worker_flooding_host_calls_hits_the_budget(
        self, tmp_path: Path
    ) -> None:
        calls: list[int] = []
        rt = IsolatedRuntime(
            RuntimeConfig(),
            python=_fake_worker(tmp_path, "call_flood"),
            max_host_calls=100,
            request_timeout=30,
        )
        rt._handlers[1] = (lambda: calls.append(1), False)  # noqa: SLF001 - a bound host function
        start = time.monotonic()
        with pytest.raises(WorkerCrashed, match="max_host_calls"):
            rt.eval("1")
        assert time.monotonic() - start < 15
        assert len(calls) == 100

    def test_the_worker_starts_with_an_empty_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PYDENO_TEST_SECRET", "hunter2")
        rt = IsolatedRuntime(RuntimeConfig(), python=_fake_worker(tmp_path, "env"))
        try:
            keys = rt.eval("1")
        finally:
            rt.close()
        assert "PYDENO_TEST_SECRET" not in keys
        assert not {"PATH", "HOME", "USER", "VIRTUAL_ENV"} & set(keys)

    def test_a_worker_that_never_starts_is_an_error_not_a_hang(
        self, tmp_path: Path
    ) -> None:
        wrapper = tmp_path / "dead.sh"
        wrapper.write_text("#!/bin/sh\nexit 3\n")
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
        with pytest.raises(WorkerCrashed):
            IsolatedRuntime(RuntimeConfig(), python=str(wrapper))


class TestWorkerCannotForgeCapabilityBookkeeping:
    """The tokens are the worker's claims. A lying worker must not be able to make a revoked (or
    never-revoked) host handler outlive its capability."""

    def test_a_reused_token_ends_the_session(self, tmp_path: Path) -> None:
        rt = IsolatedRuntime(
            RuntimeConfig(),
            python=_fake_worker(tmp_path, "dup_token"),
            request_timeout=20,
        )
        assert rt.bind_function("harmless", lambda: 1) == 7
        with pytest.raises(WorkerCrashed, match="reused"):
            rt.bind_function("privileged", lambda: 2)
        assert rt.is_closed()

    def test_a_token_map_that_does_not_match_what_was_bound_ends_the_session(
        self, tmp_path: Path
    ) -> None:
        rt = IsolatedRuntime(
            RuntimeConfig(),
            python=_fake_worker(tmp_path, "wrong_object_keys"),
            request_timeout=20,
        )
        with pytest.raises(WorkerCrashed, match="malformed"):
            rt.bind_object("api", {"add": lambda a, b: a + b})
        assert rt.is_closed()

    def test_revoking_drops_the_host_handler_even_if_the_worker_never_answers(
        self, tmp_path: Path
    ) -> None:
        rt = IsolatedRuntime(
            RuntimeConfig(),
            python=_fake_worker(tmp_path, "dup_token"),
            request_timeout=20,
        )
        token = rt.bind_function("f", lambda: 1)
        (hid,) = list(rt._handlers)  # noqa: SLF001
        rt.revoke_op(token)
        assert hid not in rt._handlers  # noqa: SLF001

    def test_terminal_escapes_in_a_remote_error_message_are_neutralised(
        self, tmp_path: Path
    ) -> None:
        rt = IsolatedRuntime(
            RuntimeConfig(),
            python=_fake_worker(tmp_path, "escape_error"),
            request_timeout=20,
        )
        with pytest.raises(RuntimeError) as excinfo:
            rt.eval("1")
        text = str(excinfo.value)
        assert "\x1b" not in text and "\x07" not in text
        assert "at line 2" in text  # newlines survive: a JS stack is made of them
