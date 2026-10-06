"""`sandbox_status()`: which OS-sandbox layers this host can apply, found out without starting an
isolate or running any guest code.

`IsolatedRuntime(sandbox="require")` refuses to start on a host that cannot apply every layer, but
you learn that on the first request. A service wants to know at start-up, so it can fail its health
check, pick a different deployment, or log what it is running with. This does the same work the
worker does before it creates its isolate, and nothing more:

1. in a forked child, run the real `harden_process()` + `_sandbox.apply()` + the startup self-test
   `_sandbox.attest()` (the child is confined, the caller never is: a sandbox cannot be lifted, so
   everything that applies one happens in a process that exits straight afterwards);
2. in a second forked child hardened like a worker, read its resident memory, CPU time and thread count
   the way the supervisor reads a worker's (`/proc` on Linux, `proc_pidinfo` on macOS), because a
   limit that cannot be measured never fires; then verify SIGKILL authority on that child;
3. in the caller, ask the kernel for its Landlock ABI version (a version query changes nothing).

It never raises, waits at most about a second even if a probe hangs, and reaps every child it
starts. Nothing in the caller's own confinement, environment, resource limits or privileges
changes. Like `os.fork()` anywhere, forking from a process with other threads is only safe because
the children touch nothing but `ctypes` and `os`; they are killed after a deadline if they do not
finish.

`complete` is what `sandbox="require"` would accept: every layer in
`_sandbox.REQUIRED_LAYERS` for this platform applied, the startup self-test found nothing the
sandbox should have stopped, the worker's resource usage can be read and the hardened probe can
be terminated, and (Linux) the process is
either not root or can drop root.
"""

from __future__ import annotations

# PEP 810 (Python 3.15): these stdlib modules are loaded on first use, not at import. A plain
# list, so it is inert on 3.10-3.14. Never list what the isolation worker imports before it
# applies its sandbox (`_worker`, `_sandbox`, `_wire`, `_wasm`, `_awaitable`): a lazy import
# there would run after the sandbox closed the filesystem.
__lazy_modules__ = ["ctypes", "platform"]

import ctypes
import json
import os
import platform
import select
import signal
import sys
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import _sandbox

__all__ = ["Layer", "SandboxStatus", "sandbox_status"]

_PROBE_DEADLINE = 1.0  # seconds; a healthy probe takes a few milliseconds
_MAX_REPLY = 64 * 1024


@dataclass(frozen=True)
class Layer:
    """One protection: did it take effect on a probe process, and what the probe saw."""

    applied: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {"applied": self.applied, "detail": self.detail}


def _na(platform_name: str) -> Layer:
    return Layer(False, f"not applicable on {platform_name}")


@dataclass(frozen=True)
class SandboxStatus:
    platform: str
    kernel: str
    #: What `_sandbox.apply()` returned in the probe: "seatbelt", "landlock+seccomp", ... or "none".
    applied: str
    #: Layers `sandbox="require"` demands on this platform.
    required: frozenset[str]
    seatbelt: Layer  # macOS
    landlock: Layer  # Linux
    seccomp: Layer  # Linux
    empty_root: Layer  # Linux; a bonus layer, not required
    no_new_privs: Layer  # Linux
    privileges: Layer  # Linux: not root, or root that can be dropped
    resource_probes: Layer  # can rss / CPU / threads of a child be read
    self_test: Layer  # the startup attestation found no forbidden operation working
    complete: bool
    warnings: list[str] = field(default_factory=list)
    termination: Layer = field(
        default_factory=lambda: Layer(False, "termination authority was not probed")
    )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "platform": self.platform,
            "kernel": self.kernel,
            "applied": self.applied,
            "required": sorted(self.required),
            "complete": self.complete,
            "warnings": list(self.warnings),
        }
        for name in _LAYER_FIELDS:
            out[name] = getattr(self, name).to_dict()
        return out

    def explain(self) -> str:
        """A short, human-readable account, suitable for a start-up log."""
        lines = [
            f"pydeno sandbox on {self.platform} (kernel {self.kernel}): "
            + (
                "COMPLETE, sandbox='require' will start."
                if self.complete
                else "INCOMPLETE, worker startup refuses in all sandbox modes."
                if not self.termination.applied
                else "INCOMPLETE, sandbox='require' would refuse to start."
            ),
            f"  applied in probe: {self.applied}; required here: {sorted(self.required) or 'nothing known'}",
        ]
        for name in _LAYER_FIELDS:
            layer: Layer = getattr(self, name)
            tag = (
                "ok     "
                if layer.applied
                else "n/a    "
                if layer.detail.startswith("not applicable")
                else "missing"
            )
            req = " (required)" if name in self.required else ""
            lines.append(f"  [{tag}] {name}{req}: {layer.detail}")
        lines.extend(f"  warning: {w}" for w in self.warnings)
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.explain()


_LAYER_FIELDS = (
    "seatbelt",
    "landlock",
    "seccomp",
    "empty_root",
    "no_new_privs",
    "privileges",
    "resource_probes",
    "termination",
    "self_test",
)


# --------------------------------------------------------------------------- forked probes


def _run_forked(
    fn: Callable[[], dict[str, Any]], deadline: float
) -> tuple[dict[str, Any] | None, str]:
    """Run `fn` in a forked child and return its JSON-able dict, or (None, why). The child exits
    with `os._exit`, so nothing it applies (a sandbox, a dropped uid, an unshared namespace) can
    reach the caller; it is killed if it overruns `deadline`."""
    try:
        r, w = os.pipe()
    except OSError as exc:
        return None, f"pipe failed: {exc}"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter(
                "ignore"
            )  # 3.12+: "fork() in a multi-threaded process"
            pid = os.fork()
    except OSError as exc:
        os.close(r)
        os.close(w)
        return None, f"fork failed: {exc}"
    if pid == 0:  # child: never returns
        code = 1
        try:
            os.close(r)
            payload = json.dumps(fn()).encode()
            os.write(w, payload)
            code = 0
        except BaseException:  # noqa: BLE001
            code = 2
        finally:
            os._exit(code)
    os.close(w)
    chunks: list[bytes] = []
    size = 0
    note = ""
    end = time.monotonic() + deadline
    try:
        while True:
            left = end - time.monotonic()
            if left <= 0:
                note = f"probe did not finish within {deadline:g}s and was killed"
                break
            ready, _, _ = select.select([r], [], [], left)
            if not ready:
                continue
            data = os.read(r, 4096)
            if not data:
                break
            chunks.append(data)
            size += len(data)
            if size > _MAX_REPLY:
                note = "probe reply too large"
                break
    except OSError as exc:
        note = f"reading the probe failed: {exc}"
    finally:
        os.close(r)
        _reap(pid, killed=bool(note))
    if note:
        return None, note
    try:
        return json.loads(b"".join(chunks)), ""
    except ValueError:
        return None, "probe produced no result (it crashed)"


def _reap(pid: int, *, killed: bool) -> None:
    """Wait for our own child, killing it first if it overran. Only ever signals `pid`, which this
    module forked."""
    if killed:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass  # SIGCHLD ignored by the caller: already reaped by the kernel
    except OSError:
        pass


def _confinement_probe() -> dict[str, Any]:
    """Runs in the throwaway child, mirroring what the worker does before its isolate exists."""
    out: dict[str, Any] = {}
    out["hardened"] = {
        k: v
        for k, v in _sandbox.harden_process().items()
        if isinstance(v, (int, str, bool))
    }
    if sys.platform.startswith("linux"):
        # Asked before `apply()`: the seccomp filter then refuses this very prctl.
        out["nnp_before"] = _libc_prctl_get(39)  # PR_GET_NO_NEW_PRIVS
    # `verify_kill`: exercise the seccomp kill action in the throwaway child (a worker only asks
    # the kernel whether it is supported, to keep one audited kill per start out of the logs).
    applied = _sandbox.apply(verify_kill=True)
    out["applied"] = applied
    out["extras"] = list(_sandbox.EXTRAS)
    out["landlock_abi"] = _sandbox.LANDLOCK_ABI
    out["landlock_note"] = _sandbox.LANDLOCK_NOTE
    out["seccomp_kill"] = _sandbox.SECCOMP_KILL
    out["kernel_caps"] = list(_sandbox.KERNEL_CAPS)
    out["missing"] = sorted(_sandbox.missing_layers(applied))
    if applied != "none" and not out["missing"]:
        try:
            out["breaches"] = _sandbox.attest()
        except BaseException as exc:  # noqa: BLE001
            out["self_test_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return out


def _libc_prctl_get(option: int) -> int | None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        value = libc.prctl(option, 0, 0, 0, 0)
        return int(value) if value >= 0 else None
    except (OSError, AttributeError):
        return None


def _measure_resource_probes(deadline: float) -> tuple[dict[str, Any], str]:
    """Measure a hardened child: same-uid visibility does not prove a worker is readable."""
    fds: list[int] = []
    try:
        r, w = os.pipe()
        fds.extend((r, w))
        ready_r, ready_w = os.pipe()
        fds.extend((ready_r, ready_w))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            pid = os.fork()
    except OSError as exc:
        for fd in fds:
            os.close(fd)
        return {}, f"could not start a probe child: {exc}"
    if pid == 0:  # child: wait until the parent closes the pipe, then leave
        try:
            os.close(w)
            os.close(ready_r)
            # In particular, a root worker becomes nobody. hidepid=2 can hide that worker
            # even when an ordinary fork with the caller's uid is readable.
            _sandbox.harden_process()
            os.write(ready_w, b"1")
            os.close(ready_w)
            select.select([r], [], [], deadline)
        finally:
            os._exit(0)
    os.close(r)
    os.close(ready_w)
    ready = False
    try:
        # Never race the privilege drop, and never wait indefinitely if hardening stalls.
        if not select.select([ready_r], [], [], deadline)[0]:
            return (
                {},
                "resource probe child did not finish hardening before the deadline",
            )
        if os.read(ready_r, 1) != b"1":
            return {}, "resource probe child exited before finishing hardening"
        ready = True
        result = {
            "rss": _sandbox.rss_bytes(pid),
            "cpu": _sandbox.cpu_seconds(pid),
            "threads": _sandbox.thread_count(pid),
        }
        # Exercise SIGKILL on this disposable child rather than infer authority from uid or
        # capability names. If denied, closing the control pipe still lets it exit normally.
        try:
            os.kill(pid, signal.SIGKILL)
            result["termination"] = True
        except OSError:
            result["termination"] = False
    finally:
        os.close(ready_r)
        os.close(w)  # the child's select returns; it exits
        _reap(pid, killed=not ready)
    return result, ""


# --------------------------------------------------------------------------- landlock ABI


def _landlock_abi() -> tuple[int | None, str]:
    """The kernel's Landlock ABI version, via the version query (changes nothing). (None, why) if
    Landlock is not there."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        abi = libc.syscall(444, None, 0, 1)  # landlock_create_ruleset(NULL, 0, VERSION)
        if abi >= 1:
            return int(abi), ""
        err = ctypes.get_errno()
        reasons = {
            38: "kernel has no Landlock (ENOSYS)",
            95: "Landlock is not enabled in this kernel (EOPNOTSUPP)",
            1: "the syscall is blocked (EPERM, e.g. a container seccomp profile)",
        }
        return None, reasons.get(err, f"landlock_create_ruleset failed (errno {err})")
    except (OSError, AttributeError, ValueError) as exc:
        return None, f"could not query: {exc}"


def _userns_hint() -> str:
    parts = []
    for path, label in (
        ("/proc/sys/kernel/unprivileged_userns_clone", "unprivileged_userns_clone"),
        ("/proc/sys/user/max_user_namespaces", "max_user_namespaces"),
        (
            "/proc/sys/kernel/apparmor_restrict_unprivileged_userns",
            "apparmor_restrict_userns",
        ),
    ):
        try:
            with open(path) as fh:
                parts.append(f"{label}={fh.read().strip()}")
        except OSError:
            pass
    return ", ".join(parts)


# --------------------------------------------------------------------------- public


def sandbox_status() -> SandboxStatus:
    """Report which sandbox layers this host can apply. Safe at service start-up; see the module
    docstring for exactly what it does. Never raises."""
    try:
        return _sandbox_status()
    except Exception as exc:  # noqa: BLE001 - a status call must not take a service down
        return _failed(f"{type(exc).__name__}: {exc}")


def _platform_key() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def _failed(why: str) -> SandboxStatus:
    plat = _platform_key()
    missing = Layer(False, f"status probe failed: {why}")
    return SandboxStatus(
        platform=plat,
        kernel=platform.release(),
        applied="none",
        required=_sandbox.REQUIRED_LAYERS.get(plat, frozenset()),
        seatbelt=missing,
        landlock=missing,
        seccomp=missing,
        empty_root=missing,
        no_new_privs=missing,
        privileges=missing,
        resource_probes=missing,
        self_test=missing,
        complete=False,
        warnings=[f"status probe failed: {why}"],
    )


def _sandbox_status() -> SandboxStatus:
    plat = _platform_key()
    required = _sandbox.REQUIRED_LAYERS.get(plat, frozenset())
    warns: list[str] = []
    linux, mac = plat == "linux", plat == "darwin"

    if not (linux or mac):
        why = f"no OS sandbox exists for platform {sys.platform!r}"
        layer = Layer(False, why)
        return SandboxStatus(
            plat, platform.release(), "none", required, layer, layer, layer, layer,
            layer, layer, layer, layer, False, [why + "; sandbox='require' cannot be satisfied"],
        )  # fmt: skip

    start = time.monotonic()
    # Imported before forking so the children never import (and never take an import lock).
    probe, note = _run_forked(_confinement_probe, _PROBE_DEADLINE)
    if probe is None:
        warns.append(note)
        probe = {}
    applied = probe.get("applied", "none")
    if not isinstance(applied, str):
        applied = "none"
    have = frozenset() if applied == "none" else frozenset(applied.split("+"))
    extras = probe.get("extras", [])
    hardened = probe.get("hardened", {})
    breaches = probe.get("breaches")
    self_error = probe.get("self_test_error")
    ran_probe = "applied" in probe

    # --- per layer ---------------------------------------------------------------------------
    def probe_layer(name: str, ok_detail: str, bad_detail: str) -> Layer:
        if not ran_probe:
            return Layer(False, note or "the probe did not run")
        return Layer(name in have, ok_detail if name in have else bad_detail)

    seatbelt = (
        probe_layer(
            "seatbelt",
            "deny-by-default Seatbelt profile took effect in a forked probe",
            "sandbox_init refused the profile (is this process already sandboxed?)",
        )
        if mac
        else _na(plat)
    )

    abi: int | None = None
    if linux:
        abi, abi_why = _landlock_abi()
        abi_text = f"kernel Landlock ABI {abi}" if abi else abi_why
        landlock_note = probe.get("landlock_note")
        landlock = probe_layer(
            "landlock",
            "restricting the filesystem works and the canary (a directory readable a moment "
            f"earlier) was refused afterwards; {abi_text}",
            f"could not be applied; {abi_text}"
            + (
                f"; {landlock_note}"
                if isinstance(landlock_note, str) and landlock_note
                else ""
            ),
        )
        killed = probe.get("seccomp_kill") == "verified"
        seccomp = probe_layer(
            "seccomp",
            "the allow-list filter survived a throwaway child and installed; a never-legitimate "
            + (
                "call in that child was killed"
                if killed
                else "call in that child was NOT killed (the self-test reports it)"
            ),
            "a filter could not be installed (blocked by the container profile, an "
            "unsupported architecture, or no_new_privs refused)",
        )
        if not ran_probe:
            empty_root = Layer(False, note or "the probe did not run")
        elif "emptyroot" in extras:
            caps = probe.get("kernel_caps") or []
            empty_root = Layer(
                True,
                "private mount/net/IPC/UTS namespaces and an empty root"
                + (
                    f"; threads capped by the kernel at {_sandbox.TASK_LIMIT}"
                    if "tasklimit" in caps
                    else "; no per-worker kernel thread cap (needs Linux 5.14+)"
                ),
            )
        else:
            hint = _userns_hint()
            empty_root = Layer(
                False,
                "unprivileged user namespaces are not allowed here"
                + (f" ({hint})" if hint else ""),
            )
        # Landlock and seccomp each set no_new_privs and fail if they cannot, so an applied layer
        # proves it (it cannot be read back afterwards: the filter refuses that prctl).
        already = probe.get("nnp_before") == 1
        if not ran_probe:
            no_new_privs = Layer(False, note or "the probe did not run")
        elif have & {"landlock", "seccomp"}:
            no_new_privs = Layer(
                True,
                "set by the Landlock/seccomp step in the probe"
                + (" (the caller already had it)" if already else ""),
            )
        else:
            no_new_privs = Layer(False, "no layer that sets no_new_privs applied")
        uid_before = hardened.get("uid_before") if isinstance(hardened, dict) else None
        uid_after = hardened.get("uid_after") if isinstance(hardened, dict) else None
        if not ran_probe:
            privileges = Layer(False, note or "the probe did not run")
        elif uid_before == 0 and uid_after == 0:
            privileges = Layer(
                False,
                "running as root and root could not be dropped; sandbox='require' refuses this",
            )
        elif uid_before == 0:
            privileges = Layer(
                True, f"running as root; dropped to uid {uid_after} in the probe"
            )
        else:
            privileges = Layer(True, f"not root (uid {uid_before})")
    else:
        landlock = seccomp = empty_root = no_new_privs = privileges = _na(plat)

    if not ran_probe:
        self_test = Layer(False, note or "the probe did not run")
    elif breaches:
        self_test = Layer(
            False, f"a forbidden operation still worked: {sorted(breaches)}"
        )
    elif self_error:
        self_test = Layer(False, f"the self-test itself failed: {self_error}")
    elif breaches is None:
        self_test = Layer(False, "not run: the platform's layers were not all applied")
    else:
        self_test = Layer(
            True, "every forbidden operation was refused (the worker's startup check)"
        )

    res, res_note = _measure_resource_probes(_PROBE_DEADLINE)
    termination = Layer(
        res.get("termination") is True,
        "SIGKILL of a hardened probe child succeeded"
        if res.get("termination") is True
        else res_note or "supervisor termination authority is unavailable",
    )
    unreadable = [
        label
        for key, label in (
            ("rss", "memory"),
            ("cpu", "CPU time"),
            ("threads", "thread count"),
        )
        if res.get(key) is None
    ]
    source = "/proc" if linux else "proc_pidinfo"
    if res_note:
        resource_probes = Layer(False, res_note)
    elif unreadable:
        resource_probes = Layer(
            False,
            f"cannot read a hardened child's {', '.join(unreadable)} via {source}: "
            "max_memory, the CPU cap and the thread cap would never fire",
        )
    else:
        resource_probes = Layer(
            True,
            f"a hardened child's memory, CPU time and thread count are readable via {source}",
        )

    # --- verdict -----------------------------------------------------------------------------
    named = {"seatbelt": seatbelt, "landlock": landlock, "seccomp": seccomp}
    layers_ok = ran_probe and all(
        named[name].applied for name in required if name in named
    )
    complete = bool(
        layers_ok
        and not _sandbox.missing_layers(applied)
        and self_test.applied
        and resource_probes.applied
        and termination.applied
        and (not linux or privileges.applied)
    )

    # --- advice ------------------------------------------------------------------------------
    for name in sorted(required):
        if name in named and not named[name].applied and ran_probe:
            warns.append(
                f"required layer {name!r} is not available: {named[name].detail}"
            )
    if linux and ran_probe and not empty_root.applied:
        warns.append(
            "no empty root: the worker can still tell which host paths exist (seccomp and "
            "Landlock deny access, not existence)"
        )
    if linux and ran_probe and not privileges.applied:
        warns.append(
            "run the service as a non-root user, or give it the capability to drop root"
        )
    if not resource_probes.applied:
        warns.append(
            "the worker's resource limits cannot be enforced here; sandbox='require' refuses to start"
        )
    if not termination.applied:
        warns.append(
            f"{termination.detail}; worker startup refuses in all sandbox modes"
        )
    if ran_probe and breaches:
        warns.append(
            "the sandbox leaks: do not run untrusted code with this configuration"
        )
    if linux and ran_probe and "landlock" in have:
        if abi is not None and abi < 4:
            warns.append(
                f"Landlock ABI {abi} has no TCP rules; the seccomp filter still denies sockets"
            )
    if (time.monotonic() - start) > 0.3:
        warns.append("the probe took over 300 ms; this host is slow to fork")

    return SandboxStatus(
        platform=plat,
        kernel=platform.release(),
        applied=applied,
        required=required,
        seatbelt=seatbelt,
        landlock=landlock,
        seccomp=seccomp,
        empty_root=empty_root,
        no_new_privs=no_new_privs,
        privileges=privileges,
        resource_probes=resource_probes,
        termination=termination,
        self_test=self_test,
        complete=complete,
        warnings=warns,
    )
