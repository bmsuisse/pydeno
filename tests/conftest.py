"""Platform-specific tests are *deselected* off their platform, never skipped.

CI enforces a skip budget of zero (`scripts/check_test_report.py`): a skipped test is
an unverified test, and a budget above zero lets real skips hide among the benign ones.
The OS sandbox is inherently per-platform (seccomp and Landlock on Linux, Seatbelt on
macOS), so those tests exist for one platform only. Deselecting them removes them from
the collected set on the other platform, which the report check handles
(`tests == collected`), instead of making them a standing exception to the budget.
"""

from __future__ import annotations

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
}


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
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
