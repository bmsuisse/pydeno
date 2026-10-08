"""#139: the docs state the defaults the code ships (0.11.0 said 600 s for the isolated runtimes' 60 s,
sandbox="auto", pyo3 0.27.2). The front door's own max_host_wait_secs stays 600 s."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pydeno import IsolatedRuntime
from pydeno._front import DEFAULT_LIMITS
from pydeno._isolated import DEFAULT_MAX_HOST_WAIT

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.source_tree


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _secs(value: float) -> str:
    return f"{int(value)} s"


def test_the_shipped_defaults() -> None:
    with IsolatedRuntime() as rt:
        assert rt._max_host_wait == DEFAULT_MAX_HOST_WAIT == 60.0  # noqa: SLF001
        assert rt._max_inflight == 64  # noqa: SLF001
        assert rt._options["sandbox"] == "require"  # noqa: SLF001


def test_front_door_wait_budget_is_600() -> None:
    assert DEFAULT_LIMITS["max_host_wait_secs"] == 600.0


def test_security_md_hardening_checklist_states_the_real_defaults() -> None:
    text = _read("SECURITY.md")
    assert f"({_secs(DEFAULT_MAX_HOST_WAIT)}, 64, 10 s)" in text
    assert "(600 s, 64, 10 s)" not in text


def test_isolation_guide_max_host_wait_row() -> None:
    row = next(
        ln
        for ln in _read("docs/guides/advanced/isolation.md").splitlines()
        if ln.startswith("| `max_host_wait` |")
    )
    assert f"| {_secs(DEFAULT_MAX_HOST_WAIT)} |" in row


def test_no_guide_states_600_s_for_the_isolated_wait() -> None:
    text = _read("docs/guides/pydantic-ai.md")
    assert re.search(r"max_host_wait`\s+\(60 s", text)
    assert "600 s" not in text
    assert "600 s" not in _read("docs/contributing/sandboxed-tool-process.md")


def test_front_door_docs_say_600() -> None:
    quick = _read("docs/guides/quickstart-pydeno.md")
    assert "| `max_host_wait_secs` | 600 s" in quick
    assert "pydeno only. Default 600 |" in quick
    front = _read("python/pydeno/_front.py")
    assert "(default 600 s)" in front


def test_review_prep_does_not_call_auto_the_default() -> None:
    text = _read("docs/contributing/security-review-prep.md")
    assert 'sandbox="auto"` (the default)' not in text
    assert 'default is `"require"`' in text


def test_stale_pyo3_ignores_are_gone() -> None:
    lock = _read("Cargo.lock")
    pyo3 = re.search(r'name = "pyo3"\nversion = "([^"]+)"', lock)
    assert pyo3 is not None
    assert tuple(int(x) for x in pyo3.group(1).split(".")[:2]) >= (0, 29)
    sec = _read(".github/workflows/security.yml")
    assert "RUSTSEC-2026-0176" not in sec and "RUSTSEC-2026-0177" not in sec
    for rel in (
        "docs/contributing/security-review-prep.md",
        "docs/contributing/supply-chain.md",
    ):
        assert "excepted in `deny.toml` as unreachable" not in _read(rel)
    deny = _read("deny.toml")
    assert "RUSTSEC-2026-0176" not in re.sub(r"(?m)^#.*$", "", deny)
    assert "RUSTSEC-2026-0177" not in re.sub(r"(?m)^#.*$", "", deny)
    osv = re.sub(r"(?m)^#.*$", "", _read("osv-scanner.toml"))
    assert "RUSTSEC-2026-0176" not in osv and "RUSTSEC-2026-0177" not in osv
