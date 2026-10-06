"""Detached iterator cleanup cannot interrupt its source event loop."""

import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize(
    "error", ["KeyboardInterrupt", "SystemExit", "CancelledError", "RuntimeError"]
)
@pytest.mark.parametrize("how", ["source", "runtime"])
def test_detached_stream_cleanup_does_not_disrupt_source_loop(error, how):
    script = textwrap.dedent("""
        import asyncio, sys
        from pydeno import Runtime

        async def main():
            attempted = asyncio.Event()
            reports = []
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(lambda loop, context: reports.append(context))
            exception = getattr(asyncio, sys.argv[1], None) or getattr(__import__('builtins'), sys.argv[1])
            class Source:
                def __aiter__(self):
                    return self
                async def __anext__(self):
                    return 42
                async def aclose(self):
                    attempted.set()
                    raise exception('detached cleanup failure')
            runtime = Runtime()
            try:
                source = runtime.stream_from_async_iterable(Source())
                runtime.eval('(stream) => { globalThis.source = stream; }')(source)
                assert await runtime.eval_async('(async () => (await source.getReader().read()).value)()') == 42
                if sys.argv[2] == 'source':
                    source.close()
                else:
                    runtime.close()
                await asyncio.wait_for(attempted.wait(), 2)
                # A detached task's BaseException must not stop the loop or cancel its caller.
                await asyncio.sleep(0.01)
                assert reports == [], reports
            finally:
                runtime.close()
        asyncio.run(main())
    """)
    done = subprocess.run(
        [sys.executable, "-c", script, error, how],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert done.returncode == 0, done.stdout + done.stderr
