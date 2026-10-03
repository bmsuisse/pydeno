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


_PLATFORM_MARKERS = {
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
