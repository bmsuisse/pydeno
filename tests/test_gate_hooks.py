"""The gate in every entry point that compiles guest source: `Pydeno` / `AsyncPydeno` feeds,
`AgentSandbox` / `AsyncAgentSandbox` runs, `IsolatedRuntime` / `AsyncIsolatedRuntime` evals, static
modules, module loaders and the bootstrap.

What every test here pins: a denial (or a gate that cannot decide) raises before anything reaches
the worker, consumes nothing (no tool budget, journal record, in-flight slot or state), and the
string the gate saw is exactly the string that ran."""

from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest

from pydeno import (
    AgentSandbox,
    AsyncAgentSandbox,
    AsyncIsolatedRuntime,
    AsyncPydeno,
    GateContext,
    GateDenied,
    GateUnavailable,
    IsolatedRuntime,
    JavaScriptError,
    Pydeno,
    RuntimeConfig,
    SourcePolicy,
    Verdict,
    all_of,
    classify_error,
    static_gate,
)
from pydeno import _isolated

_EXPECTED = os.environ.get("PYDENO_EXPECT_SANDBOX")
MODE = "require" if _EXPECTED in (None, "landlock+seccomp", "seatbelt") else "auto"
KEY = b"k" * 32
ALLOW = Verdict(True, "")
NO_EVAL = static_gate(SourcePolicy(forbid_eval=True, forbid_function=True))


class Recorder:
    """A thread-safe sync gate: records every call, denies sources containing `deny_marker`."""

    def __init__(self, deny_marker: str = "DENY") -> None:
        self.deny_marker = deny_marker
        self.calls: list[tuple[str, GateContext]] = []
        self.lock = threading.Lock()

    def __call__(self, source: str, context: GateContext) -> Verdict:
        with self.lock:
            self.calls.append((source, context))
        if self.deny_marker in source:
            return Verdict(False, "marked", ("marked",))
        return ALLOW


class Shifty(str):
    """Its own `__str__` / `__eq__` lie; only the underlying text may ever be used."""

    def __str__(self) -> str:
        return "globalThis.pwned = true"

    def __eq__(self, other: object) -> bool:
        return True

    __hash__ = str.__hash__


def deny_all(source: str, context: GateContext) -> Verdict:
    return Verdict(False, "nothing runs", ("all",))


# ---------------------------------------------------------------------------
# IsolatedRuntime
# ---------------------------------------------------------------------------


def test_isolated_runtime_gates_every_eval_path() -> None:
    rec = Recorder()
    with IsolatedRuntime(gate=rec, capture_console=True) as rt:
        rt.bind_function("tool", lambda: 1)
        assert rt.eval("globalThis.n = 1; n") == 1
        with pytest.raises(GateDenied) as info:
            rt.eval("globalThis.n = 2; 'DENY'")
        assert info.value.labels == ("marked",)
        with pytest.raises(GateDenied):
            rt.execute("globalThis.n = 3; 'DENY'")
        assert rt.execute("n").result == 1  # nothing denied ever ran

        async def go() -> None:
            with pytest.raises(GateDenied):
                await rt.eval_async("globalThis.n = 4; 'DENY'")
            with pytest.raises(GateDenied):
                await rt.execute_async("globalThis.n = 5; 'DENY'")
            assert await rt.eval_async("Promise.resolve(n)") == 1

        asyncio.run(go())
        modes = [c.mode for _, c in rec.calls]
        assert modes == [
            "eval",
            "eval",
            "execute",
            "execute",
            "eval_async",
            "execute_async",
            "eval_async",
        ]
        ctx = rec.calls[0][1]
        assert ctx.entry_point == "IsolatedRuntime.eval"
        assert ctx.tools == ("tool",)


def test_isolated_runtime_gates_modules_and_loaders() -> None:
    rec = Recorder()
    with IsolatedRuntime(gate=rec) as rt:
        rt.add_static_module("ok", "export const v = 1;")
        with pytest.raises(GateDenied):
            rt.add_static_module("bad", "export const v = 'DENY';")
        assert rt.eval_module("ok")["v"] == 1
        with pytest.raises(RuntimeError, match="resolution denied for bad"):
            rt.eval_module("bad")  # never registered

        sources = {
            "loaded:a": "export const a = 2;",
            "loaded:b": "export const b = 'DENY';",
        }
        rt.set_module_resolver(
            lambda spec, ref: spec if spec.startswith("loaded:") else None
        )
        rt.set_module_loader(lambda spec: sources[spec])
        load = "import('{}').then(m => Object.values(m)[0])"
        assert asyncio.run(rt.eval_async(load.format("loaded:a"))) == 2
        # A dynamic import meets the gate in the loader, and the eval raises its refusal.
        with pytest.raises(GateDenied) as info:
            asyncio.run(rt.eval_async(load.format("loaded:b")))
        assert info.value.reason == "marked"
        assert classify_error(info.value).kind == "gate_denied"
        assert rt.eval("1 + 1") == 2  # the runtime survives every refusal
        specs = [(c.mode, c.specifier) for _, c in rec.calls if c.specifier]
        assert ("add_static_module", "ok") in specs
        assert ("module_loader", "loaded:a") in specs
        assert ("module_loader", "loaded:b") in specs

    # The module an `eval_module` loads through a loader meets it too.
    with IsolatedRuntime(gate=rec) as rt:
        rt.set_module_resolver(lambda spec, ref: spec)
        rt.set_module_loader(lambda spec: "export const b = 'DENY';")
        with pytest.raises(GateDenied):
            rt.eval_module("loaded:b")
        assert rt.eval("2") == 2


def test_a_refused_bootstrap_starts_no_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_worker(*a: object, **k: object) -> object:
        raise AssertionError("a worker was started")

    monkeypatch.setattr(_isolated, "_take_worker", no_worker)
    monkeypatch.setattr(_isolated, "_start_worker", no_worker)
    with pytest.raises(GateDenied):
        IsolatedRuntime(
            RuntimeConfig(bootstrap="globalThis.x = eval('1')"), gate=NO_EVAL
        )
    monkeypatch.undo()
    rec = Recorder()
    with IsolatedRuntime(
        RuntimeConfig(bootstrap="globalThis.boot = 7"), gate=rec
    ) as rt:
        assert rt.eval("boot") == 7
    assert rec.calls[0][1].mode == "bootstrap"


def test_a_gate_with_the_wrong_signature_fails_at_construction() -> None:
    def three(source: str, context: GateContext, extra: object) -> Verdict:
        return ALLOW

    for build in (
        lambda: IsolatedRuntime(gate=three),
        lambda: AsyncIsolatedRuntime(gate=three),
        lambda: AgentSandbox({}, gate=three),
        lambda: AsyncAgentSandbox({}, gate=three),
        lambda: Pydeno(sandbox=MODE, gate=three),
        lambda: AsyncPydeno(sandbox=MODE, gate=three),
        lambda: IsolatedRuntime(gate="not callable"),
    ):
        with pytest.raises(TypeError):
            build()


def test_a_one_argument_gate_works_in_a_hook() -> None:
    def clf(source):  # type: ignore[no-untyped-def]
        return Verdict("DENY" not in source, "one-arg")

    with IsolatedRuntime(gate=clf) as rt:
        assert rt.eval("1 + 1") == 2
        with pytest.raises(GateDenied, match="one-arg"):
            rt.eval("'DENY'")


async def test_an_async_one_argument_gate_works_in_a_hook() -> None:
    async def clf(source):  # type: ignore[no-untyped-def]
        return Verdict("DENY" not in source, "async one-arg")

    async with AsyncAgentSandbox({}, gate=clf) as sb:
        assert await sb.run("return 3") == 3
        with pytest.raises(GateDenied, match="async one-arg"):
            await sb.run("return 'DENY'")


def test_a_sync_runtime_refuses_an_async_gate() -> None:
    async def agate(source: str, context: GateContext) -> Verdict:
        return ALLOW

    with pytest.raises(TypeError, match="async"):
        IsolatedRuntime(gate=agate)
    with pytest.raises(TypeError, match="async"):
        AgentSandbox({}, gate=agate)


def test_the_engine_runs_exactly_what_the_gate_saw() -> None:
    rec = Recorder()
    with IsolatedRuntime(gate=rec) as rt:
        assert rt.eval(Shifty("typeof pwned")) == "undefined"
        assert rt.eval("typeof pwned") == "undefined"
        seen = rec.calls[0][0]
        assert type(seen) is str and str.__eq__(seen, "typeof pwned")
        # Unicode the gate saw is the Unicode the engine compiled: an escaped `eval` is eval.
        assert rt.eval("\\u0065val('1 + 1')") == 2
        with pytest.raises(GateDenied):
            rt.eval("\\u0065val('globalThis.leak = 1'); 'DENY'")
        assert rt.eval("typeof leak") == "undefined"


def test_unicode_tricks_reach_the_gate_unchanged() -> None:
    gate = static_gate(SourcePolicy(forbid_eval=True))
    with IsolatedRuntime(gate=gate) as rt:
        for code in (
            "\\u0065val('1')",
            "// note eval('globalThis.a = 1')",
            "globalThis['\\x65val']('1')",
        ):
            with pytest.raises(GateDenied):
                rt.eval(code)
        assert rt.eval("typeof a") == "undefined"
        # A fullwidth `ｅｖａｌ` is a different identifier to the engine as well.
        with pytest.raises(JavaScriptError, match="ReferenceError"):
            rt.eval("ｅｖａｌ('1')")


def test_a_huge_source_is_refused_before_the_gate_and_the_worker() -> None:
    rec = Recorder()
    with IsolatedRuntime(gate=rec) as rt:
        with pytest.raises(GateDenied) as info:
            rt.eval("'" + "x" * (17 * 1024 * 1024) + "'")
        assert info.value.top_label == "source-too-large"
        assert rec.calls == []
        assert rt.eval("1") == 1


def test_a_broken_gate_blocks_and_base_exceptions_propagate() -> None:
    state = {"mode": "raise"}

    def gate(source: str, context: GateContext) -> Verdict:
        if state["mode"] == "raise":
            raise RuntimeError("classifier down")
        if state["mode"] == "none":
            return None  # type: ignore[return-value]
        if state["mode"] == "interrupt":
            raise KeyboardInterrupt
        return ALLOW

    with IsolatedRuntime(gate=gate) as rt:
        with pytest.raises(GateUnavailable) as info:
            rt.eval("globalThis.ran = 1")
        assert classify_error(info.value).retryable
        state["mode"] = "none"
        with pytest.raises(GateUnavailable):
            rt.eval("globalThis.ran = 1")
        state["mode"] = "interrupt"
        with pytest.raises(KeyboardInterrupt):
            rt.eval("globalThis.ran = 1")
        state["mode"] = "allow"
        assert rt.eval("typeof ran") == "undefined"


async def test_async_isolated_runtime_awaits_its_gate() -> None:
    seen: list[GateContext] = []

    async def agate(source: str, context: GateContext) -> Verdict:
        seen.append(context)
        await asyncio.sleep(0)
        return Verdict("DENY" not in source, "marked", ("marked",))

    async with AsyncIsolatedRuntime(
        RuntimeConfig(bootstrap="globalThis.b = 1"), gate=agate
    ) as rt:
        await rt.bind_function("tool", lambda: 1)
        assert await rt.eval("b") == 1
        with pytest.raises(GateDenied):
            await rt.eval("globalThis.b = 2; 'DENY'")
        with pytest.raises(GateDenied):
            await rt.add_static_module("m", "export const x = 'DENY';")
        await rt.set_module_resolver(lambda spec, ref: spec)
        await rt.set_module_loader(lambda spec: "export const y = 'DENY';")
        with pytest.raises(GateDenied):
            await rt.eval_module("loaded:y")
        assert await rt.eval("b") == 1
    assert [c.mode for c in seen][:2] == ["bootstrap", "eval"]
    assert seen[1].tools == ("tool",)
    assert seen[1].entry_point == "AsyncIsolatedRuntime.eval"


async def test_async_isolated_runtime_gate_timeout() -> None:
    async def hangs(source: str, context: GateContext) -> Verdict:
        await asyncio.sleep(30)
        return ALLOW

    async with AsyncIsolatedRuntime(gate=hangs, gate_timeout=0.1) as rt:
        started = time.monotonic()
        with pytest.raises(GateUnavailable, match="gate_timeout"):
            await rt.eval("1")
        assert time.monotonic() - started < 5
        assert not rt.is_closed()


# ---------------------------------------------------------------------------
# AgentSandbox
# ---------------------------------------------------------------------------


def test_agent_sandbox_denial_consumes_nothing() -> None:
    calls: list[int] = []

    def tool(x: int) -> int:
        calls.append(x)
        return x

    rec = Recorder()
    with AgentSandbox({"tool": tool}, max_tool_calls=3, gate=rec) as sb:
        assert sb.run("return await tool(1)") == 1
        journal = sb._journal()  # noqa: SLF001
        for method in (sb.run, sb.execute, sb.start):
            with pytest.raises(GateDenied):
                method("await tool(2); return 'DENY'")
        assert sb._journal() == journal  # noqa: SLF001
        assert (sb.calls_made, sb.calls_remaining, calls) == (1, 2, [1])
        assert sb.execute("return await tool(3)").result == 3
        assert [c.mode for _, c in rec.calls] == [
            "run",
            "run",
            "execute",
            "start",
            "execute",
        ]
        assert rec.calls[0][1].tools == ("tool",)
        assert rec.calls[0][1].entry_point == "AgentSandbox.run"
        blob = sb.dump(KEY)

    # Replay is not gated: a gate that now refuses everything still loads the session...
    with AgentSandbox.load(blob, KEY, {"tool": tool}, gate=deny_all) as loaded:
        assert loaded.calls_made == 2
        assert calls == [1, 3]  # replayed answers, not real calls
        # ...but every new run meets it.
        with pytest.raises(GateDenied):
            loaded.run("return 1")


def test_agent_sandbox_refuses_a_gated_runtime() -> None:
    rt = IsolatedRuntime(gate=NO_EVAL, capture_console=True, random_seed=1)
    try:
        with pytest.raises(ValueError, match="gate"):
            AgentSandbox({}, runtime=rt)
    finally:
        rt.close()


def test_agent_sandbox_gate_reentry_is_refused_not_deadlocked() -> None:
    holder: dict[str, AgentSandbox] = {}

    def reentrant(source: str, context: GateContext) -> Verdict:
        holder["sb"].run("return 1")  # the session is busy: this raises
        return ALLOW

    with AgentSandbox({}, gate=reentrant) as sb:
        holder["sb"] = sb
        with pytest.raises(GateUnavailable, match="RuntimeError"):
            sb.run("return 2")


async def test_async_agent_sandbox_gate_and_cancellation() -> None:
    gate_started = asyncio.Event()
    hold = {"on": False}

    async def agate(source: str, context: GateContext) -> Verdict:
        if hold["on"]:
            gate_started.set()
            await asyncio.sleep(30)
        return Verdict("DENY" not in source, "marked", ("marked",))

    calls: list[int] = []

    async def tool(x: int) -> int:
        calls.append(x)
        return x

    async with AsyncAgentSandbox({"tool": tool}, max_tool_calls=5, gate=agate) as sb:
        assert await sb.run("return await tool(1)") == 1
        journal = sb._journal()  # noqa: SLF001
        for method in (sb.run, sb.execute, sb.start):
            with pytest.raises(GateDenied):
                await method("await tool(2); return 'DENY'")
        assert sb._journal() == journal and calls == [1]  # noqa: SLF001
        # Cancelled while the gate runs: the cancellation propagates, the session is untouched.
        hold["on"] = True
        task = asyncio.ensure_future(sb.run("return 3"))
        await gate_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        hold["on"] = False
        assert not sb.is_closed()
        assert await sb.run("return await tool(4)") == 4
        assert sb.calls_made == 2


# ---------------------------------------------------------------------------
# Pydeno / AsyncPydeno
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gated_pool():
    rec = Recorder()
    with Pydeno(sandbox=MODE, gate=rec, limits={"max_suspensions": 3}) as pool:
        yield pool, rec


def test_pydeno_feeds_are_gated_and_a_denial_changes_nothing(gated_pool) -> None:
    pool, rec = gated_pool
    rec.calls.clear()
    seen: list[int] = []

    def ext(x: int) -> int:
        seen.append(x)
        return x

    with pool.checkout() as session:
        assert session.feed_run("const a = 1; a") == 1
        state = session.dump()
        with pytest.raises(GateDenied):
            session.feed_run(
                "globalThis.b = await ext(5); 'DENY'", external_lookup={"ext": ext}
            )
        with pytest.raises(GateDenied):
            session.feed_start("await ext(6); 'DENY'", external_lookup={"ext": ext})
        assert seen == []
        assert session.dump() == state  # no journal record
        assert session.feed_run("typeof b") == "undefined"
        # The whole external-call budget (3) is still there.
        assert (
            session.feed_run(
                "(await ext(1)) + (await ext(2)) + (await ext(3))",
                external_lookup={"ext": ext},
            )
            == 6
        )
        source, ctx = rec.calls[1]
        assert (
            source == "globalThis.b = await ext(5); 'DENY'"
        )  # the feed's code, not the rewrite
        assert (ctx.mode, ctx.entry_point, ctx.tools) == (
            "feed_run",
            "PydenoSession.feed_run",
            ("ext",),
        )
        assert rec.calls[2][1].mode == "feed_start"


def test_pydeno_replay_is_not_gated(gated_pool) -> None:
    pool, rec = gated_pool
    with pool.checkout() as session:
        session.feed_run("globalThis.kept = 41")
        state = session.dump()
        rec.calls.clear()
        session.load_session(state)
        assert rec.calls == []  # load replays without consulting the gate
        assert session.feed_run("kept + 1") == 42
        assert len(rec.calls) == 1


def test_pydeno_str_subclass_and_input_mutation(gated_pool) -> None:
    pool, rec = gated_pool
    rec.calls.clear()
    inputs = {"v": 1}
    lookup = {"ext": lambda: 1}

    class Meddler(Recorder):
        def __call__(self, source: str, context: GateContext) -> Verdict:
            inputs["v"] = 999  # the caller's dicts change while the gate runs...
            lookup["ext2"] = lambda: 2
            return super().__call__(source, context)

    with Pydeno(sandbox=MODE, gate=Meddler(), min_processes=1) as own:
        with own.checkout() as session:
            result = session.feed_run(
                Shifty("v + 0"), inputs=inputs, external_lookup=lookup
            )
            assert result == 1  # ...but this feed was prepared from what was passed
            assert session.feed_run("typeof pwned") == "undefined"
            assert session.feed_run("typeof ext2") == "undefined"


def test_pydeno_huge_source_and_broken_gate(gated_pool) -> None:
    pool, rec = gated_pool
    rec.calls.clear()
    with pool.checkout() as session:
        with pytest.raises(GateDenied) as info:
            session.feed_run("'" + "x" * (17 * 1024 * 1024) + "'")
        assert info.value.top_label == "source-too-large" and rec.calls == []
        assert session.feed_run("1 + 1") == 2

    def broken(source: str, context: GateContext) -> Verdict:
        raise OSError("network")

    with Pydeno(sandbox=MODE, gate=broken, min_processes=1) as own:
        with own.checkout() as session:
            with pytest.raises(GateUnavailable) as info:
                session.feed_run("globalThis.x = 1")
            assert classify_error(info.value).kind == "gate_unavailable"
            assert session.worker_pid is not None  # the session is still alive


def test_pydeno_gate_reentry_and_cross_session_use() -> None:
    holder: dict[str, object] = {}

    answers: list[object] = []

    def gate(source: str, context: GateContext) -> Verdict:
        if source == "'reenter'":
            holder["same"].feed_run("1")  # type: ignore[attr-defined]
        if source == "'other'":
            answers.append(holder["other"].feed_run("40 + 2"))  # type: ignore[attr-defined]
        return ALLOW

    with Pydeno(sandbox=MODE, gate=gate, min_processes=2) as pool:
        with pool.checkout() as same, pool.checkout() as other:
            holder.update(same=same, other=other)
            with pytest.raises(GateUnavailable) as info:
                same.feed_run(
                    "'reenter'"
                )  # its own busy session: refused, not deadlocked
            assert isinstance(info.value.__cause__, Exception)
            assert (
                same.feed_run("'other'") == "other"
            )  # another session from inside a gate
            assert answers == [42]
            assert same.feed_run("'still usable'") == "still usable"


def test_pydeno_one_sync_gate_across_threads_and_tool_threads() -> None:
    rec = Recorder()
    with Pydeno(sandbox=MODE, gate=rec, min_processes=4) as pool:
        with pool.checkout() as inner:

            def ext(n: int) -> int:
                # On the outer session's tool thread: a feed of another session meets the gate.
                return inner.feed_run(f"{n} * 2")

            results: dict[int, object] = {}
            errors: list[BaseException] = []

            def worker(i: int) -> None:
                try:
                    with pool.checkout() as session:
                        for j in range(3):
                            results[i * 10 + j] = session.feed_run(f"{i * 10 + j} + 0")
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            with pool.checkout() as outer:
                assert (
                    outer.feed_run("await ext(21)", external_lookup={"ext": ext}) == 42
                )
            for t in threads:
                t.join()
            assert errors == []
            assert results == {k: k for k in results} and len(results) == 12
    sources = [s for s, _ in rec.calls]
    assert len(sources) == 12 + 2
    assert "21 * 2" in sources and "await ext(21)" in sources


def test_pydeno_refuses_an_async_gate() -> None:
    async def agate(source: str, context: GateContext) -> Verdict:
        return ALLOW

    with pytest.raises(TypeError, match="async"):
        Pydeno(sandbox=MODE, gate=agate)


async def test_async_pydeno_awaits_its_gate_and_survives_cancellation() -> None:
    hold = {"on": False}
    started = asyncio.Event()

    async def agate(source: str, context: GateContext) -> Verdict:
        if hold["on"]:
            started.set()
            await asyncio.sleep(30)
        return Verdict("DENY" not in source, "marked", ("marked",))

    gate = all_of(NO_EVAL, agate)
    async with AsyncPydeno(sandbox=MODE, gate=gate, min_processes=1) as pool:
        async with pool.checkout() as session:
            assert await session.feed_run("const a = 1; a") == 1
            state = await session.dump()
            with pytest.raises(GateDenied, match="marked"):
                await session.feed_run("globalThis.b = 1; 'DENY'")
            with pytest.raises(GateDenied) as info:
                await session.feed_start("eval('globalThis.b = 2')")
            assert info.value.top_label == "forbidden-eval"
            assert await session.dump() == state
            hold["on"] = True
            task = asyncio.ensure_future(session.feed_run("globalThis.b = 3"))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            hold["on"] = False
            assert session.worker_pid is not None
            assert await session.feed_run("typeof b") == "undefined"
            await session.load_session(state)  # replay, ungated
            assert await session.feed_run("a + 1") == 2


async def test_async_pydeno_gate_timeout() -> None:
    async def slow(source: str, context: GateContext) -> Verdict:
        await asyncio.sleep(30)
        return ALLOW

    async with AsyncPydeno(
        sandbox=MODE, gate=slow, gate_timeout=0.1, min_processes=1
    ) as pool:
        async with pool.checkout() as session:
            with pytest.raises(GateUnavailable, match="gate_timeout"):
                await session.feed_run("1")
            assert session.worker_pid is not None


# ---------------------------------------------------------------------------
# review round 2: the scanner fails closed end to end, and the hooks' edges
# ---------------------------------------------------------------------------

FEED_BYPASSES = {
    # run as a feed (the body of an async function): each one called the tool before
    "await-regex": 'await /`/\nsecretTool("await-regex")\n// `',
    "html-comment": 'let x = 1 <!-- `\nsecretTool("html")\n// `',
    "function-expression": 'let y = function(){} / secretTool("fn") / 2; y',
    "await-object": 'await {} / secretTool("obj") / 1',
    "octal-escape": 'globalThis["secr\\145tTool"]("octal")',
    "eval-await-regex": 'await /`/\nconst r = eval("secretTool(1)")\n// `\nr',
}
SCRIPT_BYPASSES = {
    "hashbang": '#! `\nglobalThis.hit = eval("6*7")\n// `',
    "html-comment": 'var a = 1 <!-- `\nglobalThis.hit = eval("7*7")\n// `',
    "octal-escape": 'globalThis.hit = globalThis["\\145val"]("8*8")',
    "reflect": 'globalThis.hit = Reflect.get(globalThis, "eval")("1")',
}
STRICT = SourcePolicy(
    forbid_eval=True, forbid_function=True, forbidden_globals={"secretTool"}
)


def test_known_bypasses_are_denied_through_pydeno() -> None:
    called: list[object] = []

    def secret_tool(*args: object) -> str:
        called.append(args)
        return "SECRET"

    with Pydeno(sandbox=MODE, gate=static_gate(STRICT), min_processes=1) as pool:
        for name, code in FEED_BYPASSES.items():
            with pool.checkout() as session:
                with pytest.raises(GateDenied):
                    session.feed_run(code, external_lookup={"secretTool": secret_tool})
                assert session.feed_run("1 + 1") == 2, name
    assert called == []


def test_known_bypasses_are_denied_through_isolated_runtime() -> None:
    with IsolatedRuntime(gate=static_gate(STRICT)) as rt:
        for code in SCRIPT_BYPASSES.values():
            with pytest.raises(GateDenied):
                rt.eval(code)
    with IsolatedRuntime() as rt:  # the same shapes really run without the gate
        for code in SCRIPT_BYPASSES.values():
            rt.eval("globalThis.hit = undefined")
            rt.eval(code)
            assert rt.eval("globalThis.hit") is not None


async def test_a_slow_sync_gate_does_not_stall_the_event_loop() -> None:
    def slow(source: str, context: GateContext) -> Verdict:
        time.sleep(0.5)
        return ALLOW

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    async with AsyncIsolatedRuntime(gate=slow) as rt:
        task = asyncio.ensure_future(ticker())
        try:
            assert await rt.eval("1 + 1") == 2
        finally:
            task.cancel()
    assert ticks >= 15  # the loop kept running while the gate slept on its thread


async def test_a_hung_sync_gate_is_abandoned_at_its_timeout_in_async_classes() -> None:
    release = threading.Event()

    def hangs(source: str, context: GateContext) -> Verdict:
        release.wait(10)
        return ALLOW

    try:
        async with AsyncIsolatedRuntime(gate=hangs, gate_timeout=0.2) as rt:
            started = time.monotonic()
            with pytest.raises(GateUnavailable, match="gate_timeout"):
                await rt.eval("1")
            assert time.monotonic() - started < 2
    finally:
        release.set()


def test_async_classes_refuse_an_unbounded_gate_timeout() -> None:
    for build in (
        lambda: AsyncIsolatedRuntime(gate=NO_EVAL, gate_timeout=None),
        lambda: AsyncAgentSandbox({}, gate=NO_EVAL, gate_timeout=None),
        lambda: AsyncPydeno(sandbox=MODE, gate=NO_EVAL, gate_timeout=None),
    ):
        with pytest.raises(ValueError, match="gate_timeout"):
            build()
    # The sync classes accept it: a sync gate cannot be interrupted anyway.
    with IsolatedRuntime(gate=NO_EVAL, gate_timeout=None) as rt:
        assert rt.eval("1") == 1


async def test_async_gate_check_refuses_an_unbounded_timeout() -> None:
    from pydeno import async_gate_check

    with pytest.raises(ValueError, match="timeout"):
        await async_gate_check(NO_EVAL, "1", timeout=None)


def test_a_closed_session_or_runtime_does_not_call_the_gate() -> None:
    rec = Recorder()
    sb = AgentSandbox({}, gate=rec)
    sb.close()
    for method in (sb.run, sb.execute, sb.start):
        with pytest.raises(RuntimeError):
            method("return 1")
    rt = IsolatedRuntime(gate=rec)
    rt.close()
    with pytest.raises(Exception, match="closed"):
        rt.eval("1")
    assert rec.calls == []


async def test_a_closed_async_session_does_not_call_the_gate() -> None:
    rec = Recorder()
    async with AsyncAgentSandbox({}, gate=rec) as sb:
        pass
    with pytest.raises(RuntimeError):
        await sb.run("return 1")
    assert rec.calls == []


async def test_loader_refusals_belong_to_the_command_that_imported() -> None:
    async def gate(source: str, context: GateContext) -> Verdict:
        if context.mode == "module_loader":
            await asyncio.sleep(0.2)  # a slow verdict, while another command waits
        return Verdict("DENY" not in source, "marked", ("marked",))

    async with AsyncIsolatedRuntime(gate=gate) as rt:
        await rt.set_module_resolver(lambda spec, ref: spec)
        await rt.set_module_loader(lambda spec: "export const v = 'DENY';")
        importing = rt.eval("import('loaded:bad').then(() => 1)")
        plain = rt.eval("2")
        first, second = await asyncio.gather(importing, plain, return_exceptions=True)
        assert isinstance(first, GateDenied)
        assert second == 2


async def test_a_refused_import_the_guest_catches_is_still_raised() -> None:
    async with AsyncIsolatedRuntime(gate=Recorder()) as rt:
        await rt.set_module_resolver(lambda spec, ref: spec)
        await rt.set_module_loader(lambda spec: "export const v = 'DENY';")
        with pytest.raises(GateDenied):
            await rt.eval("import('loaded:x').then(() => 'ran', () => 'caught')")
        assert await rt.eval("1") == 1


def test_a_loader_refusal_keeps_its_cause() -> None:
    def broken(source: str, context: GateContext) -> Verdict:
        if context.mode == "module_loader":
            raise ConnectionError("classifier down")
        return ALLOW

    with IsolatedRuntime(gate=broken) as rt:
        rt.set_module_resolver(lambda spec, ref: spec)
        rt.set_module_loader(lambda spec: "export const v = 1;")
        with pytest.raises(GateUnavailable) as info:
            rt.eval_module("loaded:x")
        assert isinstance(info.value.__cause__, ConnectionError)


def test_sandbox_pool_passes_the_gate_to_its_runtimes() -> None:
    from pydeno import SandboxPool

    rec = Recorder()
    with SandboxPool(size=1, sandbox=MODE, gate=rec) as pool:
        with pool.checkout() as rt:
            assert rt.eval("1 + 1") == 2
            with pytest.raises(GateDenied):
                rt.eval("'DENY'")
    assert [c.entry_point for _, c in rec.calls] == ["IsolatedRuntime.eval"] * 2


# ---------------------------------------------------------------------------
# review round 3: comment line continuations and `with` + constructor, end to end
# ---------------------------------------------------------------------------

ROUND3_FEEDS = {
    "comment-backslash-lf": '//x\\\nsecretTool("lc")',
    "comment-backslash-crlf": '//x\\\r\nsecretTool("crlf")',
    "comment-backslash-u2028": '//x\\ secretTool("u2028")',
    "html-comment-backslash": 'let q = 1 <!--x\\\nsecretTool("html")',
}


@pytest.mark.parametrize("strict_eval", [False, True], ids=["plain", "strict-eval"])
def test_comment_continuations_are_denied_through_pydeno(strict_eval: bool) -> None:
    called: list[object] = []

    def secret_tool(*args: object) -> str:
        called.append(args)
        return "SECRET"

    with Pydeno(
        sandbox=MODE, gate=static_gate(STRICT), strict_eval=strict_eval, min_processes=1
    ) as pool:
        with pool.checkout() as session:
            for code in ROUND3_FEEDS.values():
                with pytest.raises(GateDenied):
                    session.feed_run(code, external_lookup={"secretTool": secret_tool})
    assert called == []


def test_round3_shapes_are_denied_through_isolated_runtime() -> None:
    shapes = {
        "hashbang-backslash": '#!x\\\nglobalThis.hit = eval("1")',
        "comment-backslash": '//x\\\nglobalThis.hit = eval("1")',
        "with-constructor": 'with (()=>0) { globalThis.hit = constructor("return 6*7")() }',
        "with-extends": (
            "with (()=>0) { class A extends constructor('globalThis.hit = 1') {}; new A() }"
        ),
    }
    with IsolatedRuntime(gate=static_gate(STRICT)) as rt:
        for code in shapes.values():
            with pytest.raises(GateDenied):
                rt.eval(code)
    with IsolatedRuntime() as rt:  # they do run without the gate
        for code in shapes.values():
            rt.eval("globalThis.hit = undefined")
            rt.eval(code)
            assert rt.eval("globalThis.hit") is not None


# ---------------------------------------------------------------------------
# the `constructor` rule end to end
# ---------------------------------------------------------------------------

#: Feeds that reach the Function constructor (each sets globalThis.hit when it runs).
REACHES_FUNCTION = {
    "with-call": "with (() => 0) { constructor('globalThis.hit = 1')() }",
    "with-statement": "with (() => 0) { 0; constructor('globalThis.hit = 1')() }",
    "with-extends": (
        "with (() => 0) { class A extends constructor('globalThis.hit = 1') {}; new A() }"
    ),
    "with-new": "with (() => 0) { new constructor('globalThis.hit = 1')() }",
    "with-return": (
        "with (() => 0) { const f = () => { return constructor('globalThis.hit = 1') }; f()() }"
    ),
    "with-of": "with (() => 0) { for (const c of [constructor]) c('globalThis.hit = 1')() }",
    "with-comment": "with (() => 0) { constructor/*x*/('globalThis.hit = 1')() }",
    "with-template": "with (() => 0) { constructor(`globalThis.hit = 1`)() }",
    "property": "(() => 0).constructor('globalThis.hit = 1')()",
    "object-value": "({ f: Function }).f('globalThis.hit = 1')()",
    "destructuring": "const { constructor: F } = () => 0; F('globalThis.hit = 1')()",
}
#: Harmless code with `constructor` the rule accepts: it must run under the gate.
ACCEPTED = {
    "class": "class A { constructor(a) { this.a = a } }; new A(7).a",
    "object-method": "const o = { constructor() { return 8 } }; Object.keys(o).length + 7",
}


def test_constructor_shapes_really_reach_function_without_a_gate() -> None:
    with IsolatedRuntime() as rt:
        for name, code in REACHES_FUNCTION.items():
            rt.eval("globalThis.hit = undefined")
            rt.eval(code)
            assert rt.eval("globalThis.hit") == 1, name


def test_constructor_shapes_are_denied_through_pydeno() -> None:
    gate = static_gate(SourcePolicy(forbid_function=True))
    with Pydeno(sandbox=MODE, gate=gate, min_processes=1) as pool:
        with pool.checkout() as session:
            for name, code in REACHES_FUNCTION.items():
                with pytest.raises(GateDenied):
                    session.feed_run(code)
                assert session.feed_run("typeof hit") == "undefined", name
            assert session.feed_run(ACCEPTED["class"]) == 7
            assert session.feed_run(ACCEPTED["object-method"]) == 8


def test_a_constructor_name_built_at_run_time_is_stopped_by_strict_eval() -> None:
    from pydeno import PydenoRuntimeError

    gate = static_gate(SourcePolicy(forbid_function=True))
    code = '(() => 0)["constr" + "uctor"]("globalThis.hit = 1")()'
    with Pydeno(sandbox=MODE, gate=gate, strict_eval=True, min_processes=1) as pool:
        with pool.checkout() as session:
            with pytest.raises(PydenoRuntimeError, match="EvalError"):
                session.feed_run(code)  # the gate cannot see it; the engine refuses it
            assert session.feed_run("typeof hit") == "undefined"


def test_dynamic_import_after_html_close_comment_reaches_allowlisted_loader() -> None:
    # PortSwigger's 2020 NiceScript bypass: `-->` is an Annex B script comment.
    # A working control prevents a syntax error from masquerading as a gate denial.
    loaded: list[str] = []

    def loader(specifier: str) -> str:
        loaded.append(specifier)
        return "export const value = 42;"

    with IsolatedRuntime(request_timeout=2) as rt:
        rt.set_module_resolver(
            lambda spec, ref: spec if spec == "loaded:html-close" else None
        )
        rt.set_module_loader(loader)
        assert (
            asyncio.run(
                rt.eval_async("import\n-->\n('loaded:html-close').then(m => m.value)")
            )
            == 42
        )
    assert loaded == ["loaded:html-close"]


def test_gate_denies_html_close_comment_import_before_loader_runs() -> None:
    loaded: list[str] = []

    def loader(specifier: str) -> str:
        loaded.append(specifier)
        return "export const value = 42;"

    gate = static_gate(SourcePolicy(forbid_dynamic_import=True))
    with IsolatedRuntime(gate=gate, request_timeout=2) as rt:
        rt.set_module_resolver(
            lambda spec, ref: spec if spec == "loaded:html-close" else None
        )
        rt.set_module_loader(loader)
        with pytest.raises(GateDenied) as denied:
            asyncio.run(
                rt.eval_async("import\n-->\n('loaded:html-close').then(m => m.value)")
            )
        assert "forbidden-dynamic-import" in denied.value.labels
        assert loaded == []
        assert rt.eval("1 + 1") == 2


# -- load(regate_replay=True) ---------------------------------------------------


def _dump_eval_session() -> bytes:
    with AgentSandbox({}, sandbox=MODE) as ungated:
        ungated.run("eval('1+1')")
        return ungated.dump(KEY)


def test_replay_is_not_regated_by_default() -> None:
    blob = _dump_eval_session()
    with AgentSandbox.load(blob, KEY, {}, sandbox=MODE, gate=NO_EVAL) as loaded:
        assert not loaded.is_closed()


def test_regate_replay_refuses_a_run_the_gate_now_forbids() -> None:
    blob = _dump_eval_session()
    with pytest.raises(GateDenied):
        AgentSandbox.load(blob, KEY, {}, sandbox=MODE, gate=NO_EVAL, regate_replay=True)


def test_regate_replay_denies_before_any_worker_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pydeno._agent as agent_module

    blob = _dump_eval_session()

    def no_worker(*a: object, **k: object) -> None:
        raise AssertionError("a worker was started for a refused journal")

    monkeypatch.setattr(agent_module, "IsolatedRuntime", no_worker)
    with pytest.raises(GateDenied):
        AgentSandbox.load(blob, KEY, {}, sandbox=MODE, gate=NO_EVAL, regate_replay=True)


def test_regate_replay_loads_what_the_gate_allows() -> None:
    with AgentSandbox({}, sandbox=MODE) as first:
        first.run("globalThis.x = 41")
        blob = first.dump(KEY)
    rec = Recorder()
    with AgentSandbox.load(
        blob, KEY, {}, sandbox=MODE, gate=rec, regate_replay=True
    ) as loaded:
        assert loaded.run("return x + 1") == 42
    source, context = rec.calls[0]
    assert (source, context.mode) == ("globalThis.x = 41", "replay")


def test_regate_replay_needs_a_gate() -> None:
    blob = _dump_eval_session()
    with pytest.raises(ValueError, match="needs a gate"):
        AgentSandbox.load(blob, KEY, {}, sandbox=MODE, regate_replay=True)


async def test_async_regate_replay_refuses_and_allows() -> None:
    blob = _dump_eval_session()
    async with await AsyncAgentSandbox.load(blob, KEY, {}, sandbox=MODE, gate=NO_EVAL):
        pass
    with pytest.raises(GateDenied):
        await AsyncAgentSandbox.load(
            blob, KEY, {}, sandbox=MODE, gate=NO_EVAL, regate_replay=True
        )

    async def agate(source: str, context: GateContext) -> Verdict:
        return ALLOW

    async with await AsyncAgentSandbox.load(
        blob, KEY, {}, sandbox=MODE, gate=agate, regate_replay=True
    ):
        pass


# -- a static gate's policy is bound into the journal -------------------------------------------

EASY = static_gate(SourcePolicy(forbid_eval=True))
TIGHT = static_gate(SourcePolicy(forbid_eval=True, forbid_webassembly=True))


def _dump_under(gate: object, code: str = "globalThis.x = 1") -> bytes:
    with AgentSandbox({}, sandbox=MODE, gate=gate) as session:
        session.run(code)
        return session.dump(KEY)


def test_a_journal_loads_under_the_static_gate_it_was_made_under() -> None:
    blob = _dump_under(EASY)
    same = static_gate(SourcePolicy(forbid_eval=True))  # equal policy, another instance
    with AgentSandbox.load(blob, KEY, {}, sandbox=MODE, gate=same) as loaded:
        assert loaded.run("return x") == 1


def test_a_journal_is_refused_under_a_different_static_gate() -> None:
    from pydeno import JournalError

    blob = _dump_under(EASY)
    with pytest.raises(JournalError, match="static gate"):
        AgentSandbox.load(blob, KEY, {}, sandbox=MODE, gate=TIGHT)


def test_a_journal_is_refused_without_the_gate_it_was_made_under() -> None:
    from pydeno import JournalError

    blob = _dump_under(EASY)
    with pytest.raises(JournalError, match="static gate"):
        AgentSandbox.load(blob, KEY, {}, sandbox=MODE)
    with pytest.raises(JournalError, match="static gate"):
        AgentSandbox.load(blob, KEY, {}, sandbox=MODE, gate=Recorder())


def test_the_policy_check_runs_before_any_worker_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pydeno._agent as agent_module
    from pydeno import JournalError

    blob = _dump_under(EASY)

    def no_worker(*a: object, **k: object) -> None:
        raise AssertionError("a worker was started for a refused journal")

    monkeypatch.setattr(agent_module, "IsolatedRuntime", no_worker)
    with pytest.raises(JournalError, match="static gate"):
        AgentSandbox.load(blob, KEY, {}, sandbox=MODE, gate=TIGHT)


def test_regate_replay_accepts_a_changed_static_gate_that_still_allows_the_runs() -> (
    None
):
    blob = _dump_under(EASY)
    with AgentSandbox.load(
        blob, KEY, {}, sandbox=MODE, gate=TIGHT, regate_replay=True
    ) as loaded:
        assert loaded.run("return x") == 1


def test_regate_replay_still_refuses_a_run_the_changed_gate_forbids() -> None:
    blob = _dump_under(EASY, "globalThis.y = typeof WebAssembly")
    with pytest.raises(GateDenied):
        AgentSandbox.load(blob, KEY, {}, sandbox=MODE, gate=TIGHT, regate_replay=True)


def test_journals_without_a_static_gate_are_unchanged_and_load_anywhere() -> None:
    from pydeno import _agent

    for gate in (None, Recorder()):
        blob = _dump_under(gate)
        journal = _agent._open_journal(blob, KEY, b"", 1 << 20)  # noqa: SLF001
        assert "gate_policy" not in journal["config"]
        with AgentSandbox.load(blob, KEY, {}, sandbox=MODE, gate=TIGHT) as loaded:
            assert loaded.run("return x") == 1


def test_a_static_gate_journal_records_only_the_policy_hash() -> None:
    from pydeno import _agent

    journal = _agent._open_journal(_dump_under(EASY), KEY, b"", 1 << 20)  # noqa: SLF001
    recorded = journal["config"]["gate_policy"]
    assert isinstance(recorded, str) and len(recorded) == 64
    assert (
        recorded
        != _agent._open_journal(  # noqa: SLF001
            _dump_under(TIGHT), KEY, b"", 1 << 20
        )["config"]["gate_policy"]
    )


def test_the_policy_hash_ignores_set_order_and_tells_policies_apart() -> None:
    from pydeno._gate import _policy_hash

    a = SourcePolicy(forbidden_identifiers=frozenset({"a", "b", "c"}))
    b = SourcePolicy(forbidden_identifiers=frozenset({"c", "b", "a"}))
    assert _policy_hash(a) == _policy_hash(b)
    assert _policy_hash(a) != _policy_hash(SourcePolicy(forbidden_identifiers={"a"}))
    assert _policy_hash(a) != _policy_hash(SourcePolicy())
    assert _policy_hash(SourcePolicy(max_source_bytes=None)) != _policy_hash(
        SourcePolicy()
    )


async def test_the_async_session_binds_and_checks_the_policy_too() -> None:
    from pydeno import JournalError

    async with AsyncAgentSandbox({}, sandbox=MODE, gate=EASY) as session:
        await session.run("globalThis.x = 1")
        blob = await session.dump(KEY)
    with pytest.raises(JournalError, match="static gate"):
        await AsyncAgentSandbox.load(blob, KEY, {}, sandbox=MODE, gate=TIGHT)
    async with await AsyncAgentSandbox.load(
        blob, KEY, {}, sandbox=MODE, gate=TIGHT, regate_replay=True
    ) as loaded:
        assert await loaded.run("return x") == 1
    async with await AsyncAgentSandbox.load(
        blob, KEY, {}, sandbox=MODE, gate=EASY
    ) as loaded:
        assert await loaded.run("return x") == 1
    # a sync AgentSandbox journal made under the same policy is the same journal
    with AgentSandbox({}, sandbox=MODE, gate=EASY) as sync_session:
        sync_session.run("globalThis.x = 1")
        sync_blob = sync_session.dump(KEY)
    with pytest.raises(JournalError, match="static gate"):
        await AsyncAgentSandbox.load(sync_blob, KEY, {}, sandbox=MODE, gate=TIGHT)
