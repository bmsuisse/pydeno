"""`strict_eval=True`: no code generation from strings in the guest (#42).

The option appends V8's ``--disallow-code-generation-from-strings`` after the hardening flags
(``--freeze-flags-after-init`` among them), so ``eval`` and every function constructor throw
``EvalError`` however the guest reaches them, while the host's own ``eval`` of a script still
works. It is a worker-spawn option: a pool fixes it for every worker it hands out, and a session
records it in its journal, so a journal cannot silently replay under the other setting.

What the flag does *not* cover is pinned here too: WebAssembly with ``jitless=False``, and
``import()``, which is the module loader's decision in either mode.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from pydeno import (
    AgentSandbox,
    AsyncAgentSandbox,
    AsyncIsolatedRuntime,
    AsyncPydeno,
    AsyncSandboxPool,
    IsolatedRuntime,
    JavaScriptError,
    Pydeno,
    PydenoError,
    RuntimeConfig,
    SandboxPool,
)
from pydeno._agent import JournalError, _open, _seal_journal
from pydeno._isolated import (
    _HARDENING_V8_FLAGS,
    _STRICT_EVAL_FLAG,
    _strict_eval_requested,
    _strict_eval_setting,
    _worker_v8_flags,
)

_EXPECTED = os.environ.get("PYDENO_EXPECT_SANDBOX")
MODE = "require" if _EXPECTED in (None, "landlock+seccomp", "seatbelt") else "auto"
KEY = b"k" * 32

# Every way a guest can reach a string compiler, including after tampering with the prototype
# chain. Each must throw EvalError under strict_eval.
GENERATORS = [
    "eval('1 + 1')",
    "(0, eval)('1 + 1')",
    "globalThis['ev' + 'al']('1')",
    "const e = eval; e('1')",
    "new Function('return 1')()",
    "Function('return 1')()",
    "new Function()",
    "(function () {}).constructor('return 1')()",
    "(() => {}).constructor('return 1')()",
    "[].constructor.constructor('return 1')()",
    "Reflect.construct(Function, ['return 1'])()",
    "Function.prototype.constructor('return 1')()",
    "Object.getPrototypeOf(async function () {}).constructor('return 1')",
    "Object.getPrototypeOf(function* () {}).constructor('yield 1')",
    "Object.getPrototypeOf(async function* () {}).constructor('yield 1')",
    "Function.prototype.call.call(Function, null, 'return 1')()",
    "Function.apply(null, ['return 1'])()",
    "Function.bind(null, 'return 1')()()",
    "Object.defineProperty(Function.prototype, 'constructor', {value: Function});"
    " (function () {}).constructor('return 1')()",
    "class X extends Function {}; new X('return 1')()",
]

# Not code generation from strings, and so allowed in strict mode too.
ALLOWED = {
    "eval(42)": 42,  # a non-string argument is returned as is
    "JSON.parse('{\"a\": 1}').a": 1,
    "new RegExp('a+').test('aa')": True,
    "[1, 2, 3].map(x => x * 2)": [2, 4, 6],
}


def _assert_strict(rt: IsolatedRuntime) -> None:
    for code in GENERATORS:
        with pytest.raises(JavaScriptError, match="EvalError") as info:
            rt.eval(code)
        assert "Code generation from strings disallowed" in str(info.value), code
    for code, expected in ALLOWED.items():
        assert rt.eval(code) == expected, code
    assert rt.eval("1 + 1") == 2  # the host's own script is not code generation


# ---------------------------------------------------------------------------
# the flag list
# ---------------------------------------------------------------------------


class TestFlags:
    def test_strict_flag_comes_after_the_hardening_flags_and_the_callers(self) -> None:
        flags = _worker_v8_flags(
            jitless=True, random_seed=7, v8_flags=["--stack-size=900"], strict_eval=True
        )
        assert flags == [
            "--jitless",
            *_HARDENING_V8_FLAGS,
            "--random-seed=7",
            "--stack-size=900",
            _STRICT_EVAL_FLAG,
        ]
        assert "--freeze-flags-after-init" in flags[: flags.index(_STRICT_EVAL_FLAG)]

    def test_default_flags_are_unchanged(self) -> None:
        assert _worker_v8_flags(
            jitless=True, random_seed=None, v8_flags=(), strict_eval=False
        ) == ["--jitless", *_HARDENING_V8_FLAGS]

    @pytest.mark.parametrize(
        "flags, expected",
        [
            ([], None),
            (["--disallow-code-generation-from-strings"], True),
            (["--disallow_code_generation_from_strings"], True),
            (["--disallow-code-generation-from-strings=true"], True),
            (["--disallow-code-generation-from-strings=false"], False),
            (["--no-disallow-code-generation-from-strings"], False),
            (["--nodisallow-code-generation-from-strings"], False),
            (["--no_disallow_code_generation_from_strings"], False),
            (
                [
                    "--no-disallow-code-generation-from-strings",
                    "--disallow-code-generation-from-strings",
                ],
                True,
            ),
            (["--jitless", "--stack-size=900"], None),
        ],
    )
    def test_v8_spellings_of_the_flag(
        self, flags: list[str], expected: bool | None
    ) -> None:
        assert _strict_eval_setting(flags) is expected

    @pytest.mark.parametrize(
        "flag",
        [
            "--no-disallow-code-generation-from-strings",
            "--nodisallow-code-generation-from-strings",
            "--disallow-code-generation-from-strings=false",
        ],
    )
    def test_strict_eval_refuses_flags_that_switch_it_off(self, flag: str) -> None:
        with pytest.raises(ValueError, match="contradicts"):
            IsolatedRuntime(strict_eval=True, v8_flags=[flag], prewarm=False)
        with pytest.raises(ValueError, match="contradicts"):
            AsyncIsolatedRuntime(strict_eval=True, v8_flags=[flag], prewarm=False)

    def test_strict_eval_must_be_a_bool(self) -> None:
        with pytest.raises(TypeError, match="strict_eval"):
            IsolatedRuntime(strict_eval=1, prewarm=False)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="strict_eval"):
            AsyncIsolatedRuntime(strict_eval="yes")  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="strict_eval"):
            Pydeno(strict_eval=1)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="strict_eval"):
            AsyncPydeno(strict_eval=1)  # type: ignore[arg-type]

    def test_requested_from_options(self) -> None:
        assert _strict_eval_requested({"strict_eval": True})
        assert _strict_eval_requested({"v8_flags": [_STRICT_EVAL_FLAG]})
        assert not _strict_eval_requested({})
        assert not _strict_eval_requested({"strict_eval": False})


# ---------------------------------------------------------------------------
# runtimes
# ---------------------------------------------------------------------------


class TestIsolatedRuntime:
    def test_strict_refuses_every_string_compiler(self) -> None:
        with IsolatedRuntime(sandbox=MODE, strict_eval=True) as rt:
            assert rt.strict_eval is True
            assert rt.v8_flags[-1] == _STRICT_EVAL_FLAG
            assert "--freeze-flags-after-init" in rt.v8_flags
            assert "--jitless" in rt.v8_flags
            _assert_strict(rt)

    def test_default_is_not_strict(self) -> None:
        with IsolatedRuntime(sandbox=MODE) as rt:
            assert rt.strict_eval is False
            assert _STRICT_EVAL_FLAG not in rt.v8_flags
            assert rt.eval("eval('1 + 1')") == 2
            assert rt.eval("(0, eval)('2 + 2')") == 4
            assert rt.eval("new Function('a', 'return a * 3')(5)") == 15
            assert rt.eval("[].constructor.constructor('return 7')()") == 7

    def test_the_raw_v8_flag_counts_as_strict(self) -> None:
        with IsolatedRuntime(sandbox=MODE, v8_flags=[_STRICT_EVAL_FLAG]) as rt:
            assert rt.strict_eval is True
            with pytest.raises(JavaScriptError, match="EvalError"):
                rt.eval("eval('1')")

    def test_the_guest_cannot_undo_it_by_replacing_globals(self) -> None:
        """Rebinding `eval` / `Function` in the guest only changes what the names point at; the
        context's code-generation switch stays off."""
        with IsolatedRuntime(sandbox=MODE, strict_eval=True) as rt:
            rt.eval(
                "globalThis.eval = (s) => 'shadowed'; globalThis.Function = function () {}; 0"
            )
            assert rt.eval("eval('1')") == "shadowed"
            with pytest.raises(JavaScriptError, match="EvalError"):
                rt.eval("(function () {}).constructor('return 1')()")
            with pytest.raises(JavaScriptError, match="EvalError"):
                rt.eval("Object.getPrototypeOf(async () => {}).constructor('return 1')")

    def test_a_module_and_a_bootstrap_still_run(self) -> None:
        async def go() -> object:
            async with AsyncIsolatedRuntime(
                RuntimeConfig(bootstrap="globalThis.boot = 41;"),
                sandbox=MODE,
                strict_eval=True,
            ) as rt:
                await rt.add_static_module("m", "export const v = boot + 1;")
                ns = await rt.eval_module("m")
                return ns["v"]

        assert asyncio.run(go()) == 42

    def test_set_timeout_with_a_string_never_compiles_it(self) -> None:
        """Not the flag's doing: the web polyfills' `setTimeout` ignores a string argument in
        either mode, and a bare isolate has no `setTimeout` at all."""
        from pydeno import WEB_POLYFILLS

        for strict in (False, True):
            with IsolatedRuntime(
                RuntimeConfig(bootstrap=WEB_POLYFILLS), sandbox=MODE, strict_eval=strict
            ) as rt:
                assert rt.eval("setTimeout('globalThis.ran = 1', 0)") == 0
                assert rt.eval("typeof ran") == "undefined"
        with IsolatedRuntime(sandbox=MODE, strict_eval=True) as rt:
            assert rt.eval("typeof setTimeout") == "undefined"

    def test_dynamic_import_is_the_loaders_decision_in_either_mode(self) -> None:
        """`import()` is not code generation from strings to V8; pydeno's loader refuses every
        specifier the host did not register, strict or not."""

        async def go(strict: bool) -> str:
            async with AsyncIsolatedRuntime(sandbox=MODE, strict_eval=strict) as rt:
                try:
                    await rt.eval(
                        "import('data:text/javascript,export default 7').then(m => m.default)",
                        timeout=5,
                    )
                except JavaScriptError as exc:
                    return str(exc)
                return "loaded"

        for strict in (False, True):
            assert "Module resolution denied" in asyncio.run(go(strict))

    def test_webassembly_is_not_covered_when_the_jit_is_on(self) -> None:
        """Documented limitation: with `jitless=False` the guest can still compile Wasm bytes."""
        wasm = (
            "new Uint8Array([0,97,115,109,1,0,0,0,1,5,1,96,0,1,127,3,2,1,0,7,5,1,1,102,0,0,"
            "10,6,1,4,0,65,42,11])"
        )
        with IsolatedRuntime(sandbox=MODE, strict_eval=True, jitless=False) as rt:
            assert (
                rt.eval(
                    f"new WebAssembly.Instance(new WebAssembly.Module({wasm})).exports.f()"
                )
                == 42
            )
            with pytest.raises(JavaScriptError, match="EvalError"):
                rt.eval("eval('1')")
        with IsolatedRuntime(
            sandbox=MODE, strict_eval=True
        ) as rt:  # jitless: no Wasm at all
            assert rt.eval("typeof WebAssembly") == "undefined"


class TestAsyncIsolatedRuntime:
    async def test_strict(self) -> None:
        async with AsyncIsolatedRuntime(sandbox=MODE, strict_eval=True) as rt:
            assert rt.strict_eval is True
            assert rt.v8_flags[-1] == _STRICT_EVAL_FLAG
            for code in GENERATORS:
                with pytest.raises(JavaScriptError, match="EvalError"):
                    await rt.eval(code)
            assert await rt.eval("1 + 1") == 2

    async def test_default(self) -> None:
        async with AsyncIsolatedRuntime(sandbox=MODE) as rt:
            assert rt.strict_eval is False
            assert await rt.eval("new Function('return 3')()") == 3


# ---------------------------------------------------------------------------
# pools: a spawn option, never a per-checkout one
# ---------------------------------------------------------------------------


class TestPools:
    def test_every_checkout_of_a_strict_pool_is_strict(self) -> None:
        with SandboxPool(size=1, sandbox=MODE, strict_eval=True) as pool:
            for _ in range(3):  # the pooled worker, then cold starts and refills
                with pool.checkout() as rt:
                    assert rt.strict_eval is True
                    with pytest.raises(JavaScriptError, match="EvalError"):
                        rt.eval("new Function('return 1')()")

    def test_a_pool_never_hands_out_the_other_setting(self) -> None:
        with (
            SandboxPool(size=1, sandbox=MODE, strict_eval=True) as strict,
            SandboxPool(size=1, sandbox=MODE) as lax,
        ):
            with strict.checkout() as a, lax.checkout() as b:
                assert (a.strict_eval, b.strict_eval) == (True, False)
                assert b.eval("eval('5')") == 5

    def test_strict_eval_cannot_be_set_per_checkout(self) -> None:
        assert "strict_eval" not in SandboxPool.SESSION_OPTIONS
        with SandboxPool(size=1, sandbox=MODE) as pool:
            for value in (True, False):
                with pytest.raises(TypeError, match="fixed for the whole pool"):
                    pool.checkout(strict_eval=value)

    async def test_async_pool(self) -> None:
        assert "strict_eval" not in AsyncSandboxPool.SESSION_OPTIONS
        async with AsyncSandboxPool(size=1, sandbox=MODE, strict_eval=True) as pool:
            for _ in range(2):
                async with pool.checkout() as rt:
                    assert rt.strict_eval is True
                    with pytest.raises(JavaScriptError, match="EvalError"):
                        await rt.eval("eval('1')")
            with pytest.raises(TypeError, match="fixed for the whole pool"):
                await pool.checkout(strict_eval=False)


# ---------------------------------------------------------------------------
# sessions and their journals
# ---------------------------------------------------------------------------


def _config_of(blob: bytes) -> dict:
    return json.loads(_open(blob, KEY, b""))["config"]


class TestAgentJournal:
    def test_strict_session_records_it_and_replays_under_it(self) -> None:
        with AgentSandbox({}, sandbox=MODE, strict_eval=True) as sb:
            assert (
                sb.run("try { eval('1') } catch (e) { return e.name }") == "EvalError"
            )
            blob = sb.dump(KEY)
        assert _config_of(blob)["strict_eval"] is True
        with AgentSandbox.load(blob, KEY, {}, sandbox=MODE, strict_eval=True) as again:
            assert (
                again.run("try { new Function('') } catch (e) { return e.name }")
                == "EvalError"
            )

    def test_a_default_journal_is_written_exactly_as_before(self) -> None:
        with AgentSandbox({}, sandbox=MODE) as sb:
            sb.run("1")
            blob = sb.dump(KEY)
        assert "strict_eval" not in _config_of(blob)

    def test_a_strict_journal_does_not_load_into_a_lax_session(self) -> None:
        with AgentSandbox({}, sandbox=MODE, strict_eval=True) as sb:
            sb.run("1")
            blob = sb.dump(KEY)
        with pytest.raises(JournalError, match="strict_eval=True"):
            AgentSandbox.load(blob, KEY, {}, sandbox=MODE)

    def test_a_lax_journal_does_not_load_into_a_strict_session(self) -> None:
        with AgentSandbox({}, sandbox=MODE) as sb:
            sb.run("eval('1')")
            blob = sb.dump(KEY)
        with pytest.raises(JournalError, match="strict_eval=False"):
            AgentSandbox.load(blob, KEY, {}, sandbox=MODE, strict_eval=True)
        with pytest.raises(JournalError, match="strict_eval=False"):
            AgentSandbox.load(blob, KEY, {}, sandbox=MODE, v8_flags=[_STRICT_EVAL_FLAG])

    def test_an_adopted_runtime_is_checked_too(self) -> None:
        with AgentSandbox({}, sandbox=MODE, strict_eval=True) as sb:
            sb.run("1")
            blob = sb.dump(KEY)
        config = _config_of(blob)
        lax = IsolatedRuntime(
            sandbox=MODE, capture_console=True, random_seed=config["random_seed"]
        )
        with pytest.raises(JournalError, match="strict_eval"):
            AgentSandbox.load(blob, KEY, {}, runtime=lax)
        lax.close()
        strict = IsolatedRuntime(
            sandbox=MODE,
            capture_console=True,
            random_seed=config["random_seed"],
            strict_eval=True,
        )
        with AgentSandbox.load(blob, KEY, {}, runtime=strict) as again:
            assert again.run("return 1 + 1") == 2

    def test_an_adopted_strict_runtime_makes_a_strict_journal(self) -> None:
        rt = IsolatedRuntime(
            sandbox=MODE, capture_console=True, random_seed=3, strict_eval=True
        )
        with AgentSandbox({}, runtime=rt) as sb:
            sb.run("1")
            assert _config_of(sb.dump(KEY))["strict_eval"] is True

    @pytest.mark.parametrize("value", [False, 1, "true", None])
    def test_a_malformed_strict_eval_entry_is_refused(self, value: object) -> None:
        with AgentSandbox({}, sandbox=MODE) as sb:
            sb.run("1")
            config = _config_of(sb.dump(KEY))
        forged = _seal_journal({**config, "strict_eval": value}, [], KEY, b"")
        with pytest.raises(JournalError, match="strict_eval"):
            AgentSandbox.load(forged, KEY, {}, sandbox=MODE)

    async def test_async_session(self) -> None:
        async with AsyncAgentSandbox({}, sandbox=MODE, strict_eval=True) as sb:
            assert (
                await sb.run("try { eval('1') } catch (e) { return e.name }")
                == "EvalError"
            )
            blob = await sb.dump(KEY)
        assert _config_of(blob)["strict_eval"] is True
        with pytest.raises(JournalError, match="strict_eval"):
            await AsyncAgentSandbox.load(blob, KEY, {}, sandbox=MODE)
        again = await AsyncAgentSandbox.load(
            blob, KEY, {}, sandbox=MODE, strict_eval=True
        )
        async with again:
            assert await again.run("return 1 + 1") == 2


# ---------------------------------------------------------------------------
# the front door
# ---------------------------------------------------------------------------


class TestFrontDoor:
    def test_pydeno_strict_sessions_and_their_dumps(self) -> None:
        with Pydeno(
            sandbox=MODE, min_processes=1, strict_eval=True, dump_key=KEY
        ) as pool:
            with pool.checkout() as session:
                with pytest.raises(PydenoError, match="EvalError"):
                    session.feed_run("new Function('return 1')()")
                with pytest.raises(PydenoError, match="EvalError"):
                    session.feed_run("(function () {}).constructor('return 1')()")
                session.feed_run("var kept = 20")
                assert session.feed_run("kept + 1") == 21
                state = session.dump()
            with pool.checkout() as session:
                session.load_session(state)
                assert session.feed_run("kept") == 20
                with pytest.raises(PydenoError, match="EvalError"):
                    session.feed_run("eval('kept')")
        with Pydeno(sandbox=MODE, min_processes=1, dump_key=KEY) as lax:
            with lax.checkout() as session:
                assert session.feed_run("eval('1 + 1')") == 2
                with pytest.raises(PydenoError, match="strict_eval"):
                    session.load_session(state)
                lax_state = session.dump()
        with Pydeno(
            sandbox=MODE, min_processes=1, strict_eval=True, dump_key=KEY
        ) as pool:
            with pool.checkout() as session:
                with pytest.raises(PydenoError, match="strict_eval"):
                    session.load_session(lax_state)

    def test_a_session_with_its_own_memory_limit_is_strict_too(self) -> None:
        """`checkout(limits={"max_memory": ...})` starts a worker outside the pool; it gets the
        pool's spawn options all the same."""
        with Pydeno(sandbox=MODE, min_processes=1, strict_eval=True) as pool:
            with pool.checkout(limits={"max_memory": 256 * 1024 * 1024}) as session:
                with pytest.raises(PydenoError, match="EvalError"):
                    session.feed_run("eval('1')")

    async def test_async_pydeno(self) -> None:
        async with AsyncPydeno(
            sandbox=MODE, min_processes=1, strict_eval=True, dump_key=KEY
        ) as pool:
            async with pool.checkout() as session:
                with pytest.raises(PydenoError, match="EvalError"):
                    await session.feed_run("new Function('return 1')()")
                await session.feed_run("var kept = 5")
                state = await session.dump()
        async with AsyncPydeno(sandbox=MODE, min_processes=1, dump_key=KEY) as lax:
            async with lax.checkout() as session:
                assert await session.feed_run("eval('2')") == 2
                with pytest.raises(PydenoError, match="strict_eval"):
                    await session.load_session(state)
