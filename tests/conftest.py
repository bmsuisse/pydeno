"""Platform-specific tests are *deselected* off their platform, never skipped.

CI enforces a skip budget of zero (`scripts/check_test_report.py`): a skipped test is
an unverified test, and a budget above zero lets real skips hide among the benign ones.
The OS sandbox is inherently per-platform (seccomp and Landlock on Linux, Seatbelt on
macOS), so those tests exist for one platform only. Deselecting them removes them from
the collected set on the other platform, which the report check handles
(`tests == collected`), instead of making them a standing exception to the budget.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

# The container matrix (`scripts/linux_matrix.sh`) states which sandbox layers the kernel
# it hands the tests is supposed to allow. Unset means "an ordinary machine".
_EXPECTED_SANDBOX = os.environ.get("PYDENO_EXPECT_SANDBOX")
_FULL_SANDBOXES = ("landlock+seccomp", "seatbelt")


# IsolatedRuntime, AgentSandbox and the sandbox are POSIX-only (they import `resource`; the OS
# sandbox is Seatbelt / Landlock+seccomp), so these modules cannot even be imported on Windows.
# Not collected there, which is the "deselected, never skipped" rule applied at file level.
collect_ignore = (
    [
        "test_secure_defaults.py",
        "test_fork_template.py",
        "test_sandbox_pid1.py",
        "test_lazy_imports.py",
        "test_isolated_guest_error_text.py",
        "test_agent_sandbox.py",
        "test_agent_async_lifecycle.py",
        "test_tool_catalog_budget.py",
        "test_agent_limit_names.py",
        "test_frame_buffer_bounds.py",
        "test_isolated_limit_values.py",
        "test_sync_handler_deadline.py",
        "test_limit_values_review.py",
        "test_module_timeout_recovery.py",
        "test_timeout_error_prototype.py",
        "test_bridge_poisoning.py",
        "test_default_runtime_isolation.py",
        "test_idle_cpu.py",
        "test_isolated_attack_classes.py",
        "test_isolated_capability_denial.py",
        "test_isolated_determinism.py",
        "test_isolated_fuzz.py",
        "test_isolated_hardening_round2.py",
        "test_isolated_hardening_round3.py",
        "test_isolated_libraries.py",
        "test_isolated_command_loop.py",
        "test_isolated_lifecycle.py",
        "test_isolated_limits.py",
        "test_isolated_review_findings.py",
        "test_isolated_runtime.py",
        "test_isolated_tools.py",
        "test_polyfill_timers.py",
        "test_redteam_syscalls.py",
        "test_sandbox_attest.py",
        "test_sandbox_syscall_tables.py",
        "test_seccomp_program.py",
        "test_tool_bridge.py",
        "test_stress_concurrency.py",
        "test_timeout_overhead.py",
        "test_sandbox_attest_edges.py",
        "test_review_regressions.py",
        "test_aio_isolated_runtime.py",
        "test_async_reply_backpressure.py",
        "test_aio_agent.py",
        "test_session_pool.py",
        "test_strict_eval.py",
        "test_sandbox_pool.py",
        "test_agent_execution_result.py",
        "test_agent_journal_recovery.py",
        "test_agent_schema_tools.py",
        "test_status.py",
        "test_linux_resource_probes.py",
        "test_worker_signal_authority.py",
        "test_errors_taxonomy.py",
        "test_classify_empty_root.py",
        "test_preflight.py",
        "test_seatbelt_profile.py",
        "test_aio_agent_results.py",
        "test_aio_agent_recovery.py",
        "test_aio_agent_schema_tools.py",
        "test_agent_replay_public_errors.py",
        "test_cli.py",
        "test_http_fetch_agent.py",
        "test_front_door.py",
        "test_front_door_async.py",
        "test_front_door_probes.py",
        "test_front_door_review.py",
        "test_front_door_isolation.py",
        "test_front_door_budgets.py",
        "test_front_door_refusals.py",
        "test_redteam_boundary.py",
        "test_front_door_worker_death.py",
        "test_wire_frames.py",
        "test_worker_startup.py",
        "test_result_conversion_bounds.py",
        "test_isolated_guest_surface.py",
        "test_isolated_wasm.py",
        "test_gate_hooks.py",
        "test_pr105_gate_wasm.py",
        "test_worker_capacity.py",
        "test_stream_source_owner.py",
        "test_sandbox_violation.py",
        "test_sandbox_canaries.py",
        "test_tool_timeout.py",
    ]
    if sys.platform == "win32"
    else []
)

# `sandbox="require"` is the constructors' default. The matrix's degraded cells (a kernel that
# denies Landlock or seccomp) run the whole suite, and "require" correctly refuses there, so on
# those hosts only, the tests that do not pass `sandbox=` get "auto". `original_sandbox_defaults`
# keeps what the signatures really say.
_ORIGINAL_SANDBOX_DEFAULTS: dict[str, str] = {}


def _degraded_hosts_default_to_auto() -> None:
    if sys.platform == "win32":
        return
    from pydeno import AsyncIsolatedRuntime, IsolatedRuntime

    for cls in (IsolatedRuntime, AsyncIsolatedRuntime):
        kwdefaults = cls.__init__.__kwdefaults__
        _ORIGINAL_SANDBOX_DEFAULTS[cls.__name__] = kwdefaults["sandbox"]
        if _EXPECTED_SANDBOX is not None and _EXPECTED_SANDBOX not in _FULL_SANDBOXES:
            kwdefaults["sandbox"] = "auto"


_degraded_hosts_default_to_auto()


@pytest.fixture(scope="session")
def original_sandbox_defaults() -> dict[str, str]:
    return dict(_ORIGINAL_SANDBOX_DEFAULTS)


_PLATFORM_MARKERS = {
    "release_performance": os.environ.get("PYDENO_TEST_PROFILE") != "debug",
    "linux_only": sys.platform.startswith("linux"),
    "darwin_only": sys.platform == "darwin",
    # Assertions that every dangerous operation is denied only make sense where every
    # layer is in force. Degraded environments (a kernel without Landlock, say) run the
    # expectation tests instead, which check the layers that *are* there.
    "full_sandbox": _EXPECTED_SANDBOX is None or _EXPECTED_SANDBOX in _FULL_SANDBOXES,
    # Fires real privileged syscalls (`tests/test_redteam_syscalls.py`): only inside a
    # container, and only where the seccomp layer is meant to be in force.
    "redteam": (
        os.environ.get("PYDENO_REDTEAM_CONTAINER") == "1"
        and (_EXPECTED_SANDBOX is None or "seccomp" in _EXPECTED_SANDBOX)
    ),
    # Needs to start as root to mean anything (a container; CI runners are not root).
    "as_root": hasattr(os, "geteuid") and os.geteuid() == 0,
    # Pairs pydeno with pydantic-monty (an optional extra, in the `testing` group).
    "needs_monty": importlib.util.find_spec("pydantic_monty") is not None,
    # Exercises the pydantic-ai integration (the optional `pydantic-ai-slim` package).
    "needs_pydantic_ai": importlib.util.find_spec("pydantic_ai") is not None,
}


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    # A parametrised value with no explicit `ids=` becomes part of the test id, and `pytest --co`
    # prints ids verbatim. One 8 MiB id made the CI runner's log handling stall for an hour, with
    # nothing in the log to say why, so refuse such ids here, where the message can name the test.
    too_long = [item.nodeid[:120] for item in items if len(item.nodeid) > 400]
    if too_long:
        raise pytest.UsageError(
            "test ids over 400 characters (give the parametrisation explicit `ids=`): "
            + "; ".join(too_long[:5])
        )
    kept: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        wrong_platform = any(
            item.get_closest_marker(name) is not None and not here
            for name, here in _PLATFORM_MARKERS.items()
        )
        (deselected if wrong_platform else kept).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept
