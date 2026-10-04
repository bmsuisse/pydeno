"""Native worker death stays outside the Python host; the same pool recovers."""

from __future__ import annotations

import os
import signal
import sys

import pytest

from pydeno import AsyncPydeno, Pydeno, PydenoCrashedError

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX worker signals")
_EXPECTED = os.environ.get("PYDENO_EXPECT_SANDBOX")
MODE = "require" if _EXPECTED in (None, "landlock+seccomp", "seatbelt") else "auto"


@pytest.mark.parametrize("sig", ["SIGABRT", "SIGKILL"])
def test_sync_worker_death_is_contained_and_pool_recovers(sig: str) -> None:
    with Pydeno(sandbox=MODE, min_processes=1) as pool:
        with pool.checkout() as session:
            assert session.feed_run("40 + 2") == 42
            pid = session.worker_pid
            assert pid is not None and pid != os.getpid()
            os.kill(pid, getattr(signal, sig))
            with pytest.raises(PydenoCrashedError):
                session.feed_run("1")
        with pool.checkout() as fresh:
            assert fresh.worker_pid != pid
            assert fresh.feed_run("40 + 2") == 42


@pytest.mark.parametrize("sig", ["SIGABRT", "SIGKILL"])
async def test_async_worker_death_is_contained_and_pool_recovers(sig: str) -> None:
    async with AsyncPydeno(sandbox=MODE, min_processes=1) as pool:
        async with pool.checkout() as session:
            assert await session.feed_run("40 + 2") == 42
            pid = session.worker_pid
            assert pid is not None and pid != os.getpid()
            os.kill(pid, getattr(signal, sig))
            with pytest.raises(PydenoCrashedError):
                await session.feed_run("1")
        async with pool.checkout() as fresh:
            assert fresh.worker_pid != pid
            assert await fresh.feed_run("40 + 2") == 42
