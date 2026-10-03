"""`sandbox_status()`: what this host can apply, found without starting an isolate.

Platform-specific assertions carry `linux_only` / `darwin_only` / `full_sandbox` and are deselected
elsewhere (never skipped). Everything else runs on every platform: the shape of the answer, that it
never raises, never leaves a process behind, never changes the caller, and agrees with what
`IsolatedRuntime(sandbox="require")` actually does.
"""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import sys
import time
from typing import Any

import pytest

from pydeno import IsolatedRuntime, WorkerCrashed
from pydeno import _sandbox, _status
from pydeno._status import Layer, SandboxStatus, sandbox_status


@pytest.fixture
def forked(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record every child the status call forks."""
    pids: list[int] = []
    real = os.fork

    def fork() -> int:
        pid = real()
        if pid:
            pids.append(pid)
        return pid

    monkeypatch.setattr(os, "fork", fork)
    return pids


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # our own child: signal 0 only asks whether it still exists
    except ProcessLookupError:
        return True
    return False


class TestShape:
    def test_it_returns_a_frozen_status(self) -> None:
        st = sandbox_status()
        assert isinstance(st, SandboxStatus)
        with pytest.raises(dataclasses.FrozenInstanceError):
            st.complete = True  # type: ignore[misc]
        for f in dataclasses.fields(st):
            if isinstance(getattr(st, f.name), Layer):
                with pytest.raises(dataclasses.FrozenInstanceError):
                    getattr(st, f.name).applied = True

    def test_to_dict_is_json(self) -> None:
        d = sandbox_status().to_dict()
        json.dumps(d)
        for name in (
            "seatbelt", "landlock", "seccomp", "empty_root",
            "no_new_privs", "privileges", "resource_probes", "self_test",
        ):  # fmt: skip
            assert set(d[name]) == {"applied", "detail"}
            assert isinstance(d[name]["detail"], str) and d[name]["detail"]
        assert isinstance(d["complete"], bool) and isinstance(d["warnings"], list)
        assert d["platform"] in ("linux", "darwin") or d["complete"] is False

    def test_explain_is_text_about_every_layer(self) -> None:
        st = sandbox_status()
        text = st.explain()
        assert "seatbelt" in text and "landlock" in text and "resource_probes" in text
        assert ("INCOMPLETE" in text) == (not st.complete)
        assert str(sandbox_status()).startswith("pydeno sandbox on")

    def test_it_is_quick(self) -> None:
        sandbox_status()  # warm: first import of ctypes helpers
        best = min(_timed() for _ in range(3))
        assert best < 0.3, f"{best * 1000:.0f} ms"

    def test_complete_follows_the_required_layers(self) -> None:
        st = sandbox_status()
        have = frozenset() if st.applied == "none" else frozenset(st.applied.split("+"))
        if st.complete:
            assert not _sandbox.missing_layers(st.applied)
            assert st.required <= have
            assert st.self_test.applied and st.resource_probes.applied
        if _sandbox.missing_layers(st.applied):
            assert not st.complete


def _timed() -> float:
    start = time.perf_counter()
    sandbox_status()
    return time.perf_counter() - start


class TestSafety:
    def test_no_process_is_left_behind(self, forked: list[int]) -> None:
        sandbox_status()
        assert forked, "the probes run in forked children"
        assert all(_gone(pid) for pid in forked)

    def test_the_caller_is_not_confined_or_changed(self) -> None:
        env, cwd, uid = dict(os.environ), os.getcwd(), os.getuid()
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        sandbox_status()
        assert dict(os.environ) == env and os.getcwd() == cwd and os.getuid() == uid
        assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == mask
        # What a sandboxed process could not do: read a file, start a process, open a socket.
        with open(__file__, "rb") as fh:
            assert fh.read(1)
        assert os.spawnv(os.P_WAIT, sys.executable, [sys.executable, "-c", "pass"]) == 0
        import socket

        socket.socket().close()
        if sys.platform.startswith("linux"):
            with open("/proc/self/status") as fh:
                assert "NoNewPrivs:\t0" in fh.read() or os.environ.get(
                    "PYDENO_ALREADY_NNP"
                )

    def test_it_never_raises_when_fork_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_fork() -> int:
            raise OSError(11, "Resource temporarily unavailable")

        monkeypatch.setattr(os, "fork", no_fork)
        st = sandbox_status()
        assert not st.complete and st.warnings
        assert "fork failed" in st.explain()

    def test_it_never_raises_when_the_internals_do(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*a: Any, **k: Any) -> Any:
            raise RuntimeError("boom")

        monkeypatch.setattr(_status, "_run_forked", boom)
        st = sandbox_status()
        assert not st.complete
        assert "boom" in st.warnings[0]

    def test_a_probe_that_hangs_is_killed_and_reaped(self, forked: list[int]) -> None:
        def hang() -> dict[str, Any]:
            time.sleep(60)
            return {}

        start = time.monotonic()
        result, note = _status._run_forked(hang, 0.3)  # noqa: SLF001
        assert result is None and "killed" in note
        assert time.monotonic() - start < 3
        assert forked and all(_gone(pid) for pid in forked)

    def test_a_probe_that_crashes_is_reported(self) -> None:
        def crash() -> dict[str, Any]:
            os._exit(9)

        result, note = _status._run_forked(crash, 2.0)  # noqa: SLF001
        assert result is None and note

    def test_the_probe_cannot_confine_the_caller(self) -> None:
        """The child applies the real sandbox; the parent can still open files afterwards."""

        def confine() -> dict[str, Any]:
            return {"applied": _sandbox.apply()}

        result, _ = _status._run_forked(confine, 5.0)  # noqa: SLF001
        assert result is not None
        with open(__file__, "rb") as fh:
            assert fh.read(1)


class TestVerdicts:
    """The verdict logic with a stand-in probe, on whatever platform this runs."""

    def _status_with(
        self, monkeypatch: pytest.MonkeyPatch, probe: dict[str, Any]
    ) -> SandboxStatus:
        monkeypatch.setattr(_status, "_run_forked", lambda fn, deadline: (probe, ""))
        return sandbox_status()

    def test_nothing_applied_is_incomplete_and_says_why(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        st = self._status_with(
            monkeypatch,
            {
                "applied": "none",
                "extras": [],
                "hardened": {"uid_before": 1000, "uid_after": 1000},
            },
        )
        assert not st.complete and st.applied == "none"
        assert any("required layer" in w for w in st.warnings) or st.platform not in (
            "linux",
            "darwin",
        )

    def test_a_leaky_sandbox_is_incomplete(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        full = "seatbelt" if sys.platform == "darwin" else "landlock+seccomp"
        st = self._status_with(
            monkeypatch,
            {
                "applied": full,
                "extras": [],
                "breaches": ["read-file"],
                "hardened": {"uid_before": 1000, "uid_after": 1000},
                "no_new_privs": 1,
            },
        )
        assert not st.self_test.applied and not st.complete
        assert "leaks" in " ".join(st.warnings)

    @pytest.mark.linux_only
    def test_root_that_cannot_be_dropped_is_incomplete(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        st = self._status_with(
            monkeypatch,
            {
                "applied": "landlock+seccomp",
                "extras": [],
                "breaches": [],
                "hardened": {"uid_before": 0, "uid_after": 0},
                "no_new_privs": 1,
            },
        )
        assert not st.privileges.applied and not st.complete


class TestAgreesWithRequire:
    def test_complete_is_exactly_whether_require_starts(self) -> None:
        st = sandbox_status()
        try:
            rt = IsolatedRuntime(sandbox="require")
        except WorkerCrashed:
            assert not st.complete, st.explain()
            return
        try:
            assert st.complete, st.explain()
            assert rt.sandbox == st.applied
        finally:
            rt.close()


@pytest.mark.darwin_only
@pytest.mark.full_sandbox
class TestMacOS:
    def test_seatbelt_applies_and_the_self_test_passes(self) -> None:
        st = sandbox_status()
        assert st.applied == "seatbelt" and st.seatbelt.applied
        assert st.self_test.applied and st.resource_probes.applied and st.complete
        assert not st.landlock.applied and "not applicable" in st.landlock.detail


@pytest.mark.linux_only
@pytest.mark.full_sandbox
class TestLinux:
    def test_landlock_and_seccomp_apply(self) -> None:
        st = sandbox_status()
        assert st.landlock.applied and "ABI" in st.landlock.detail
        assert st.seccomp.applied and st.no_new_privs.applied
        assert st.applied.startswith("landlock+seccomp")
        assert st.self_test.applied and st.resource_probes.applied
        assert st.complete or not st.privileges.applied
        assert not st.seatbelt.applied

    def test_the_kernel_is_named(self) -> None:
        st = sandbox_status()
        assert st.kernel and st.platform == "linux"
