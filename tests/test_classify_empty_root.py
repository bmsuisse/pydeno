"""#143: an `empty_root="require"` that cannot be satisfied is `sandbox_unavailable`, not retryable."""

from __future__ import annotations

import re
from pathlib import Path

import pydeno
from pydeno import IsolatedRuntime
from pydeno._errors import classify_error
from pydeno._isolated import WorkerCrashed

_WORKER = Path(pydeno.__file__).parent / "_worker.py"


def test_worker_text_is_classified_sandbox_unavailable() -> None:
    # The exact text the worker raises, as the host wraps it.
    msg = (
        "worker failed to start: init failed: empty_root='require' but the empty-root layer could not be "
        "applied here (it needs unprivileged user namespaces and a single-threaded worker)"
        " [applied: landlock, seccomp]"
    )
    info = classify_error(WorkerCrashed(msg))
    assert info.kind == "sandbox_unavailable"
    assert info.retryable is False


def test_classifier_matches_what_the_worker_source_says() -> None:
    src = _WORKER.read_text()
    m = re.search(r'"(empty_root=\'require\' but the empty-root layer[^"]*)"', src)
    assert m, "worker no longer raises the empty_root='require' refusal"
    for prefix in (
        "",
        "init failed: ",
    ):  # the host adds "init failed: " to what the worker says
        info = classify_error(
            WorkerCrashed(f"worker failed to start: {prefix}{m.group(1)}")
        )
        assert info.kind == "sandbox_unavailable"


def test_other_start_failures_stay_retryable() -> None:
    info = classify_error(WorkerCrashed("worker failed to start: something else broke"))
    assert info.kind == "worker_crashed"
    assert info.retryable is True


def test_real_runtime_with_unsatisfiable_empty_root() -> None:
    # Where user namespaces work this starts fine; where they do not (Ubuntu's
    # apparmor_restrict_userns, most containers) the refusal must not read as retryable.
    try:
        rt = IsolatedRuntime(empty_root="require", sandbox="auto")
    except WorkerCrashed as exc:
        assert "empty_root='require'" in str(exc)
        info = classify_error(exc)
        assert info.kind == "sandbox_unavailable"
        assert not info.retryable
    else:
        rt.close()
