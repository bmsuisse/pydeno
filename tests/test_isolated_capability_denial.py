"""A checklist of things untrusted code tries first, each of which must fail inside `IsolatedRuntime`.

The idea is borrowed from simonw/denobox's `test_sandbox.py` (read a file, write a file, reach the
network, spawn a process, read the environment, from both the sync and the async API). Two things
differ here, on purpose:

* Deno's tests check for a *permission-denied* message, because Deno's permission system is its
  sandbox. pydeno has no permission system: the guest simply has no `Deno`, `fetch`, `process` or
  `require` to call, and the OS sandbox sits beneath that for anything native. So each attempt must
  fail *as a JavaScript error*, and
* a failed call is not enough. Where the attempt could have had an effect on the host (writing a
  file, setting an environment variable), the test also checks that nothing happened.

Every case runs through both `eval` and `eval_async`, which take different paths in the worker.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from pydeno import IsolatedRuntime, JavaScriptError, RuntimeConfig

ATTEMPTS = {
    # --- Deno-style (what a model trained on Deno code reaches for) ---
    "deno_read_file": "Deno.readTextFileSync('/etc/passwd')",
    "deno_read_file_async": "(async () => await Deno.readTextFile('/etc/passwd'))()",
    "deno_write_file": "Deno.writeTextFileSync('{target}', 'owned')",
    "deno_write_file_async": "(async () => await Deno.writeTextFile('{target}', 'owned'))()",
    "deno_subprocess": "new Deno.Command('ls').outputSync()",
    "deno_env": "Deno.env.get('PATH')",
    "deno_namespace": "Deno.version",
    "deno_dynamic_import": "(async () => await import('https://example.com/x.js'))()",
    # --- Node-style ---
    "node_require_fs": "require('fs').readFileSync('/etc/passwd', 'utf8')",
    "node_require_child_process": "require('child_process').execSync('id')",
    "node_process_env": "process.env.PATH",
    "node_process_exit": "process.exit(0)",
    "node_import_fs": "(async () => await import('node:fs'))()",
    "node_buffer": "Buffer.from('x')",
    "node_global_process_binding": "process.binding('fs')",
    # --- browser-style ---
    "fetch": "(async () => await fetch('https://example.com'))()",
    "fetch_localhost": "(async () => await fetch('http://127.0.0.1:1/'))()",
    "xml_http_request": "new XMLHttpRequest()",
    "web_socket": "new WebSocket('wss://example.com')",
    "worker": "new Worker('data:text/javascript,1')",
    "import_scripts": "importScripts('https://example.com/x.js')",
    "navigator": "navigator.userAgent",
    "document": "document.cookie",
    "window": "window.location",
    "local_storage": "localStorage.setItem('k', 'v')",
    "indexed_db": "indexedDB.open('x')",
    "shared_array_buffer": "new SharedArrayBuffer(8)",
    "web_assembly": "new WebAssembly.Module(new Uint8Array([0, 97, 115, 109, 1, 0, 0, 0]))",
    # --- reaching for the host through the runtime's own plumbing ---
    "ops_guess": "__host_op_sync__(1, [])",
    "ops_negative": "__host_op_sync__(-1, [])",
    "ops_huge": "__host_op_sync__(2**52, [])",
    "ops_async_guess": "(async () => await __host_op_async__(1, []))()",
}


def _guest(code: str, target: Path) -> str:
    return code.replace("{target}", str(target))


@pytest.fixture
def rt() -> Any:
    with IsolatedRuntime(RuntimeConfig(timeout=10.0)) as runtime:
        yield runtime


def _run_sync(rt: IsolatedRuntime, code: str) -> Any:
    return rt.eval(code)


def _run_async(rt: IsolatedRuntime, code: str) -> Any:
    async def go() -> Any:
        return await rt.eval_async(f"(async () => ({code}))()", timeout=10)

    return asyncio.run(go())


@pytest.mark.parametrize("path", [_run_sync, _run_async], ids=["eval", "eval_async"])
@pytest.mark.parametrize("name", sorted(ATTEMPTS))
def test_the_attempt_fails_as_a_javascript_error(
    rt: IsolatedRuntime, tmp_path: Path, name: str, path: Any
) -> None:
    code = _guest(ATTEMPTS[name], tmp_path / "owned.txt")
    if path is _run_sync and code.startswith("(async"):
        # `eval` of an async expression returns before the promise settles, so the refusal is a
        # rejection nobody awaits here. The `eval_async` twin proves it is refused; this one
        # proves that merely starting it is harmless and leaves the runtime usable.
        path(rt, code)
    else:
        with pytest.raises(JavaScriptError):
            path(rt, code)
    assert rt.eval("1 + 1") == 2, "a refused attempt must leave the runtime usable"


@pytest.mark.parametrize("path", [_run_sync, _run_async], ids=["eval", "eval_async"])
def test_a_write_attempt_leaves_no_file_behind(
    rt: IsolatedRuntime, tmp_path: Path, path: Any
) -> None:
    target = tmp_path / "owned.txt"
    for key in ("deno_write_file", "deno_write_file_async"):
        try:
            path(rt, _guest(ATTEMPTS[key], target))
        except JavaScriptError:
            pass  # refused synchronously, or (eval_async) through the awaited rejection
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_the_hosts_environment_is_not_visible_to_the_guest(
    rt: IsolatedRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "PYDENO_TEST_SECRET_VALUE_12345"
    monkeypatch.setenv("PYDENO_TEST_SECRET", secret)
    with IsolatedRuntime(RuntimeConfig(timeout=10.0)) as fresh:
        # nothing named like an environment API exists, and a scan of every global finds no trace
        leak = fresh.eval(
            "JSON.stringify(Object.getOwnPropertyNames(globalThis).map("
            "n => { try { return String(globalThis[n]) } catch (e) { return '' } }))"
        )
    assert secret not in leak
    assert "PYDENO_TEST_SECRET" not in leak
    assert (
        os.environ["PYDENO_TEST_SECRET"] == secret
    )  # and the attempt did not touch ours


def test_a_failed_attempt_does_not_disclose_the_host(rt: IsolatedRuntime) -> None:
    """Error text for a refused capability must not carry host paths or usernames."""
    leaks = (
        str(Path.home()),
        os.getcwd(),
        os.environ.get("USER", "\0"),
        "/Users/",
        "/home/",
    )
    for code in ATTEMPTS.values():
        try:
            rt.eval(_guest(code, Path("/nonexistent/owned.txt")))
        except JavaScriptError as exc:
            text = f"{exc} {getattr(exc, 'name', '')} {getattr(exc, 'stack', '')}"
            assert not any(leak in text for leak in leaks if leak), (code, text)


@pytest.mark.parametrize("path", [_run_sync, _run_async], ids=["eval", "eval_async"])
def test_the_things_that_should_work_still_work(rt: IsolatedRuntime, path: Any) -> None:
    """The other half of the denobox checklist: locking down must not break pure computation."""
    assert path(rt, "Math.sqrt(16)") == 4
    assert path(rt, "'hello'.toUpperCase()") == "HELLO"
    assert path(rt, "[1, 2, 3].reduce((a, b) => a + b, 0)") == 6
    assert path(rt, "JSON.parse('{\"a\": 1}')") == {"a": 1}
    assert path(rt, "Array.from({length: 5}, (_, i) => i)") == [0, 1, 2, 3, 4]
    assert path(rt, "({name: 'test', value: 42})") == {"name": "test", "value": 42}
    assert path(rt, "null") is None
    assert path(rt, "true") is True
    assert path(rt, "[1, 2, 3].map(x => x * 2)") == [2, 4, 6]


def test_state_persists_between_evals_but_not_between_runtimes() -> None:
    with IsolatedRuntime(RuntimeConfig(timeout=10.0)) as a:
        a.eval("var x = 10")
        a.eval("var y = 20")
        assert a.eval("x + y") == 30
    with IsolatedRuntime(RuntimeConfig(timeout=10.0)) as b:
        assert b.eval("typeof x") == "undefined"


def test_errors_and_syntax_errors_are_catchable_and_carry_their_message(
    rt: IsolatedRuntime,
) -> None:
    with pytest.raises(JavaScriptError, match="test error"):
        rt.eval("throw new Error('test error')")
    with pytest.raises(JavaScriptError):
        rt.eval("this is not valid javascript {{{")
    assert rt.eval("1 + 1") == 2


def test_eval_after_close_raises_instead_of_hanging() -> None:
    runtime = IsolatedRuntime(RuntimeConfig(timeout=10.0))
    assert runtime.eval("1 + 1") == 2
    runtime.close()
    with pytest.raises(Exception):  # noqa: B017, PT011 - the exact type is not the point
        runtime.eval("1 + 1")
