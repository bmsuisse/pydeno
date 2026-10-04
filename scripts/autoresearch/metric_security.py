"""One number for the autoresearch security loops (see docs/contributing/autoresearch.md).

    python scripts/autoresearch/metric_security.py

Runs a battery of hostile-guest probes against `IsolatedRuntime(sandbox="require")` (and the bridge
probes against the in-process `Runtime` too). Each probe returns True when the ATTACK SUCCEEDED, i.e.
the sandbox failed. Prints `METRIC: <number of violations>` on stdout (lower is better, 0 is the goal)
and the failing probes on stderr. An unexpected exception inside a probe counts as a violation: a
probe that cannot prove the sandbox held has not shown that it held.

The battery grows: when review or red-teaming finds a new class of attack, add a probe here first.
"""

from __future__ import annotations

import sys
import time
import traceback
from collections.abc import Callable

from pydeno import IsolatedRuntime, Runtime, RuntimeConfig

TIMEOUT = 3.0
Probe = Callable[[], bool]
PROBES: dict[str, Probe] = {}


def probe(fn: Probe) -> Probe:
    PROBES[fn.__name__] = fn
    return fn


def iso(**kw):  # type: ignore[no-untyped-def]
    return IsolatedRuntime(
        RuntimeConfig(timeout=TIMEOUT), sandbox="require", request_timeout=10, **kw
    )


# --- the protocol channel and the guest's view of the host ------------------------------------------------
@probe
def console_noise_does_not_corrupt_replies() -> bool:
    with iso() as rt:
        rt.eval("console.log('hello'); 1 + 1")
        return rt.eval("1 + 2") != 3


@probe
def guest_sees_no_host_runtime_objects() -> bool:
    with iso() as rt:
        names = rt.eval(
            "['Deno','process','require','module','Buffer','fetch','XMLHttpRequest','WebSocket',"
            "'importScripts','Worker','SharedArrayBuffer','Atomics','WebAssembly']"
            ".filter(n => typeof globalThis[n] !== 'undefined')"
        )
        return bool(names)


@probe
def guest_cannot_forge_a_reply_frame() -> bool:
    with iso() as rt:
        try:
            rt.eval(
                'Deno.stdout.writeSync(new TextEncoder().encode(\'{"t":"reply","id":1,"v":"FORGED"}\\n\'))'
            )
        except Exception:  # noqa: BLE001
            return rt.eval("1 + 1") != 2
        return True


@probe
def guest_cannot_poison_the_serializer() -> bool:
    with iso() as rt:
        rt.eval('JSON.stringify = () => \'{"id":2,"result":"POISONED"}\'; 0')
        return rt.eval("1 + 1") != 2


@probe
def dynamic_import_and_module_loading_are_denied() -> bool:
    with iso() as rt:
        for expr in (
            "import('node:fs').then(() => 'loaded')",
            "import('file:///etc/passwd').then(() => 'loaded')",
        ):
            try:
                out = rt.eval(expr)
            except Exception:  # noqa: BLE001
                continue
            if out == "loaded" or (hasattr(out, "__await__")):
                return True
        return False


# --- limits ---------------------------------------------------------------------------------------------------
@probe
def infinite_loop_ends_at_the_deadline() -> bool:
    with iso() as rt:
        t = time.monotonic()
        try:
            rt.eval("for (;;) {}")
        except Exception:  # noqa: BLE001
            return time.monotonic() - t > TIMEOUT * 4
        return True


@probe
def memory_bomb_is_killed_and_the_next_sandbox_works() -> bool:
    t = time.monotonic()
    with IsolatedRuntime(
        RuntimeConfig(timeout=20),
        sandbox="require",
        max_memory=300 * 2**20,
        request_timeout=30,
    ) as rt:
        try:
            rt.eval("const a = []; for (;;) a.push(new Array(10000).fill(1))")
            return True
        except Exception:  # noqa: BLE001
            pass
    if time.monotonic() - t > 25:
        return True
    with iso() as rt:
        return rt.eval("1 + 1") != 2


@probe
def huge_result_is_refused_without_killing_the_parent() -> bool:
    with iso() as rt:
        try:
            out = rt.eval("'x'.repeat(2 ** 28)")
            return len(out) > 2**27  # returned an absurd value instead of refusing
        except Exception:  # noqa: BLE001
            return rt.eval("1 + 1") != 2 if not rt.is_closed() else False


# --- the host bridge: a guest must not interfere with a later host bind ----------------------------------
def _bind_probe(setup: str, kind: str, runtime) -> bool:  # type: ignore[no-untyped-def]
    """True if guest setup made a host bind silently do nothing (or run guest code) yet return normally."""
    with runtime() as rt:
        rt.eval(setup + "; 0")
        try:
            if kind == "object":
                rt.bind_object("tools", {"f": lambda: 1})
                visible = rt.eval(
                    "typeof tools !== 'undefined' && typeof tools.f === 'function'"
                )
            else:
                rt.bind_function("f", lambda: 1)
                visible = rt.eval("typeof f === 'function'")
            hits = rt.eval(
                "typeof globalThis.__hits === 'number' ? globalThis.__hits : 0"
            )
        except Exception:  # noqa: BLE001
            return False  # refused loudly: the sandbox held
        return (not visible) or hits > 0


_BIND_ATTACKS = {
    "proxy_namespace": (
        "globalThis.tools = new Proxy({}, {defineProperty: () => true})",
        "object",
    ),
    "getter_namespace": (
        "Object.defineProperty(globalThis, 'tools', {get() { return {} }, configurable: true})",
        "object",
    ),
    "iterator_hook": (
        "globalThis.__hits = 0; const o = Array.prototype[Symbol.iterator];"
        "Array.prototype[Symbol.iterator] = function () { globalThis.__hits++; return o.call(this) }",
        "object",
    ),
    "readonly_global": (
        "Object.defineProperty(globalThis, 'f', {value: 1, writable: false, configurable: false})",
        "function",
    ),
    "setter_global": (
        "globalThis.__hits = 0; Object.defineProperty(globalThis, 'f',"
        "{set(v) { globalThis.__hits++ }, get() { return 1 }, configurable: true})",
        "function",
    ),
    "intrinsic_namespace": ("globalThis.tools = Object.prototype", "object"),
}

for _name, (_setup, _kind) in _BIND_ATTACKS.items():
    for _rt_name, _factory in (("isolated", iso), ("inprocess", Runtime)):

        def _make(setup=_setup, kind=_kind, factory=_factory) -> Probe:  # type: ignore[no-untyped-def]
            return lambda: _bind_probe(setup, kind, factory)

        PROBES[f"bind_{_name}_{_rt_name}"] = _make()


# --- the platform the sandbox claims ---------------------------------------------------------------------------
@probe
def os_sandbox_is_complete_on_this_host() -> bool:
    from pydeno import sandbox_status

    return not sandbox_status().complete


# --- slice C (boundary) ---------------------------------------------------------------------------------------
# Host boundary and state (#75 slice C): tools and external functions, error redaction, journals and
# replay, `SessionPool` and its store, the `Pydeno` front door, the CLI and the text pydeno itself writes.
# Every probe runs in a fresh interpreter with a hard time cap (`_C_CAP`): a probe that hangs, crashes
# or raises counts as a violation, and no probe can leave threads or workers behind for the next one.
import asyncio as _c_asyncio  # noqa: E402
import os as _c_os  # noqa: E402
import subprocess as _c_subprocess  # noqa: E402

_C_CAP = 240.0
_C_KEY = b"slice-c-journal-key-0123456789"
_C_PROBES: dict[str, Probe] = {}
_C_BIDI = "‮⁦"


def _c_probe(fn: Probe) -> Probe:
    _C_PROBES[fn.__name__] = fn

    def capped() -> bool:
        try:
            done = _c_subprocess.run(
                [sys.executable, _c_os.path.abspath(__file__), "--slice-c-probe", fn.__name__],
                timeout=_C_CAP,
                capture_output=True,
                text=True,
            )
        except _c_subprocess.TimeoutExpired:
            raise RuntimeError(f"timed out after {_C_CAP:g}s") from None
        if done.returncode not in (0, 1):
            last = (done.stderr.strip().splitlines() or ["no output"])[-1]
            raise RuntimeError(last[:160])
        return done.returncode == 1

    PROBES[f"c_{fn.__name__}"] = capped
    return fn


def _c_child(name: str) -> int:
    try:
        return 1 if _C_PROBES[name]() else 0
    except BaseException:  # noqa: BLE001 - reported to the parent as a probe error
        traceback.print_exc()
        return 3


class _CStop(BaseException):
    """What a host tool might raise that is not an `Exception` (SystemExit, a cancellation)."""


def _c_reloads(blob: bytes, tools, **kw) -> bool:  # type: ignore[no-untyped-def]
    from pydeno import AgentSandbox, JournalError, ReplayDivergence

    try:
        AgentSandbox.load(blob, _C_KEY, tools, sandbox="require", **kw).close()
    except (JournalError, ReplayDivergence):
        return False
    return True


# 1. tools and external functions ------------------------------------------------------------------------------
@_c_probe
def tool_base_exception_leaves_an_unloadable_journal() -> bool:
    """A tool raising a BaseException (SystemExit, a cancellation) must not leave a journal that
    `dump()` returns but `load()` refuses (an unrestorable session; `SessionPool` users are told to
    `drop` it, which also resets its tool budget)."""
    from pydeno import AgentSandbox

    def stop(x):  # type: ignore[no-untyped-def]
        raise _CStop("tool text")

    tools = {"stop": stop, "ok": lambda: 1}
    with AgentSandbox(tools, sandbox="require") as s:
        s.execute("return 1")
        s.execute("try { await stop(1) } catch (e) {} return await ok()")
        blob = s.dump(_C_KEY)
    return not _c_reloads(blob, tools)


@_c_probe
def front_external_base_exception_leaves_an_unloadable_state() -> bool:
    from pydeno import Pydeno, PydenoError

    def stop(x):  # type: ignore[no-untyped-def]
        raise _CStop("tool text")

    with Pydeno(min_processes=1) as pool:
        with pool.checkout() as s:
            s.feed_run("const kept = 41")
            try:
                s.feed_run("try { await stop(1) } catch (e) {} 1", external_lookup={"stop": stop})
            except PydenoError:
                pass
            state = s.dump()
        with pool.checkout() as s2:
            try:
                s2.load_session(state)
            except PydenoError:
                return True
            return s2.feed_run("kept + 1") != 42


def _c_storm(n: int = 200) -> str:
    return (
        f"const rs = await Promise.allSettled(Array.from({{length: {n}}}, (_, i) => t(i)));"
        "return rs.filter(r => r.status === 'rejected').length"
    )


def _c_slow(i):  # type: ignore[no-untyped-def]
    time.sleep(0.0005 * (i % 4))
    return i


@_c_probe
def tool_call_storm_makes_replay_diverge() -> bool:
    """Concurrent calls past `max_inflight_host_calls` were refused depending on how fast the host
    answered: nothing records that, so replay diverged (an unrestorable session, chosen by the guest)."""
    from pydeno import AgentSandbox

    tools = {"t": _c_slow}
    with AgentSandbox(tools, max_tool_calls=10_000, sandbox="require") as s:
        r = s.execute(_c_storm())
        if not r.ok or r.result != 0:
            return True
        blob = s.dump(_C_KEY)
    return not _c_reloads(blob, tools)


@_c_probe
def async_tool_call_storm_makes_replay_diverge() -> bool:
    from pydeno import AsyncAgentSandbox, JournalError, ReplayDivergence

    async def t(i):  # type: ignore[no-untyped-def]
        await _c_asyncio.sleep(0.0005 * (i % 4))
        return i

    async def go() -> bool:
        async with AsyncAgentSandbox({"t": t}, max_tool_calls=10_000, sandbox="require") as s:
            r = await s.execute(_c_storm())
            if not r.ok or r.result != 0:
                return True
            blob = await s.dump(_C_KEY)
        try:
            restored = await AsyncAgentSandbox.load(blob, _C_KEY, {"t": t}, sandbox="require")
        except (JournalError, ReplayDivergence):
            return True
        await restored.close()
        return False

    return _c_asyncio.run(go())


@_c_probe
def tool_budget_bypassed_by_refused_or_concurrent_calls() -> bool:
    from pydeno import AgentSandbox

    ran = []

    def t(i):  # type: ignore[no-untyped-def]
        ran.append(i)
        return i

    with AgentSandbox({"t": t}, max_tool_calls=3, sandbox="require") as s:
        s.execute(_c_storm(100))
        s.execute("try { await t(-1) } catch (e) {} return 1")
        return len(ran) > 3 or s.calls_made > 3


@_c_probe
def tool_name_shadows_a_session_global() -> bool:
    """A tool whose name is one of the session's own globals is silently unreachable (or breaks
    every run) instead of being refused when the session is made."""
    from pydeno import AgentSandbox

    for name in ("__pydeno_agent_settle", "globalThis", "__pydeno_external"):
        try:
            AgentSandbox({name: lambda: 1}, sandbox="require").close()
        except (ValueError, TypeError):
            continue
        return True
    return False


@_c_probe
def tool_error_text_reaches_the_guest_by_default() -> bool:
    from pydeno import AgentSandbox, Pydeno, ToolCall

    def leak(x):  # type: ignore[no-untyped-def]
        raise ValueError("SECRET-host-path /srv/app")

    def leak_base(x):  # type: ignore[no-untyped-def]
        raise _CStop("SECRET-base")

    seen = []
    with AgentSandbox({"leak_base": leak_base}, sandbox="require") as s:
        # A BaseException ends the run (and the worker); neither the guest nor the error says why.
        r = s.execute("try { await leak_base(1) } catch (e) { return e.name + e.message }")
        seen.append(repr(r.result) + repr(r.error))
    with AgentSandbox({"leak": leak}, sandbox="require") as s:
        r = s.execute("try { await leak(1) } catch (e) { return e.name + e.message }")
        seen.append(repr(r.result) + repr(r.error))
        step = s.start("try { await leak(1) } catch (e) { return e.message }")
        if isinstance(step, ToolCall):
            step = s.resume(step, error=PermissionError("SECRET-approver"))
        seen.append(repr(getattr(step, "value", None)))
    with Pydeno(min_processes=1) as pool, pool.checkout() as sess:
        try:
            seen.append(
                repr(
                    sess.feed_run(
                        "try { await leak(1) } catch (e) { e.message }",
                        external_lookup={"leak": leak},
                    )
                )
            )
        except Exception as exc:  # noqa: BLE001
            seen.append(repr(exc))
    return any("SECRET" in text for text in seen)


@_c_probe
def foreign_or_used_tool_call_is_accepted() -> bool:
    from pydeno import AgentSandbox, ToolCall

    tools = {"t": lambda x: x}
    with AgentSandbox(tools, sandbox="require") as a, AgentSandbox(tools, sandbox="require") as b:
        sa, sb = a.start("return await t(1)"), b.start("return await t(2)")
        attempts = [
            lambda: a.resume(sb, 9),
            lambda: a.resume(ToolCall("t", (1,), sa.call_id, 0), 9),
        ]
        for attempt in attempts:
            try:
                attempt()
                return True
            except RuntimeError:
                pass
        a.resume(sa, 1)
        try:
            a.resume(sa, 2)
            return True
        except RuntimeError:
            pass
        restored = type(b).load(b.dump(_C_KEY), _C_KEY, tools, sandbox="require")
        try:
            restored.resume(sb, 3)
            return True
        except RuntimeError:
            pass
        finally:
            restored.close()
    return False


@_c_probe
def catalog_tool_reachable_or_charged_before_discovery() -> bool:
    from pydeno import AgentSandbox, SchemaTool

    ran = []
    secret = SchemaTool(
        name="secret_tool",
        description="internal",
        input_schema={"type": "object"},
        callable=lambda args: ran.append(args) or "hit",
    )
    with AgentSandbox({}, tools_catalog=[secret], max_tool_calls=5, sandbox="require") as s:
        s.execute(
            "for (const f of [() => tools.secret_tool({}), () => tools['secret_tool']({}),"
            " () => tools.constructor({}), () => tools.__proto__.x({})]) { try { await f() } catch {} }"
            "return 1"
        )
        return bool(ran) or s.calls_made > 0


@_c_probe
def front_invalid_answer_consumes_the_snapshot() -> bool:
    """An answer the session refuses (not an Exception) used up the snapshot, leaving the session
    paused forever: neither resumable nor feedable."""
    from pydeno import AsyncPydeno, Pydeno

    with Pydeno(min_processes=1) as pool, pool.checkout() as s:
        snap = s.feed_start("await f(1)", external_lookup={"f": lambda x: x})
        try:
            snap.resume({"exception": "not an exception"})
        except TypeError:
            pass
        try:
            snap.resume(value=1)
        except RuntimeError:
            return True

    async def go() -> bool:
        async with AsyncPydeno(min_processes=1) as pool:
            async with pool.checkout() as s:
                snap = await s.feed_start("await f(1)", external_lookup={"f": lambda x: x})
                try:
                    await snap.resume(error="not an exception")  # type: ignore[arg-type]
                except TypeError:
                    pass
                try:
                    await snap.resume(value=1)
                except RuntimeError:
                    return True
        return False

    return _c_asyncio.run(go())


@_c_probe
def front_syntax_check_is_steered_by_the_guest() -> bool:
    """The front door asks the worker whether a failed feed compiled, with JavaScript the guest can
    reach (`Object.getPrototypeOf`). A guest could make a feed that ran (and called externals) be
    reported as `PydenoSyntaxError` ("nothing of it ran"), or change its own state outside the
    journal so that the dump no longer replays."""
    from pydeno import Pydeno, PydenoError, PydenoSyntaxError

    ran = []
    with Pydeno(min_processes=1) as pool:
        with pool.checkout() as s:
            s.feed_run(
                "const real = Object.getPrototypeOf;"
                "Object.getPrototypeOf = function (o) {"
                " globalThis.n = (globalThis.n || 0) + 1;"
                " if (globalThis.lie) throw new SyntaxError('lie'); return real(o) }"
            )
            try:
                s.feed_run("throw new SyntaxError('mine')")
            except PydenoError:
                pass
            s.feed_run("globalThis.lie = true")
            try:
                s.feed_run(
                    "await mark(1); throw new SyntaxError('after a side effect')",
                    external_lookup={"mark": lambda x: ran.append(x)},
                )
            except PydenoSyntaxError:
                if ran:
                    return True
            except PydenoError:
                pass
            s.feed_run("globalThis.lie = false")
            state = s.dump()
        with pool.checkout() as s2:
            try:
                s2.load_session(state)
            except PydenoError:
                return True
    return False


# 2. journals and state ----------------------------------------------------------------------------------------
@_c_probe
def tampered_truncated_or_spliced_journal_loads() -> bool:
    from pydeno import AgentSandbox, JournalError

    tools = {"t": lambda x: x}
    with AgentSandbox(tools, sandbox="require") as s:
        s.run("const a = await t(1)")
        one = s.dump(_C_KEY, associated_data=b"tenant-a")
        s.run("const b = await t(2)")
        two = s.dump(_C_KEY, associated_data=b"tenant-a")
    head = len(b"pydeno-agent2\x00") + 32
    forged = [
        one[:-1],
        one + b" ",
        one[:head] + two[head:],
        one[:-3] + bytes([one[-3] ^ 1]) + one[-2:],
        one.replace(b'"t"', b'"u"'),
    ]
    for blob in forged:
        try:
            AgentSandbox.load(blob, _C_KEY, tools, associated_data=b"tenant-a", sandbox="require").close()
            return True
        except JournalError:
            pass
    for ad in (b"tenant-b", b""):
        try:
            AgentSandbox.load(one, _C_KEY, tools, associated_data=ad, sandbox="require").close()
            return True
        except JournalError:
            pass
    return False


@_c_probe
def replay_calls_the_real_tool_or_crash_refunds_budget() -> bool:
    from pydeno import AgentSandbox

    ran = []

    def t(x):  # type: ignore[no-untyped-def]
        ran.append(x)
        return x

    tools = {"t": t}
    with AgentSandbox(tools, max_tool_calls=10, timeout=2, sandbox="require") as s:
        s.run("const a = await t(1)")
        s.execute("await t(2); await t(3); for (;;) {}")  # spends two calls, then dies
        blob = s.dump(_C_KEY)
    before = len(ran)
    restored = AgentSandbox.load(blob, _C_KEY, tools, sandbox="require")
    try:
        return len(ran) != before or restored.calls_made < 3 or restored.run("return a") != 1
    finally:
        restored.close()


@_c_probe
def guest_reads_the_host_clock_or_entropy() -> bool:
    from pydeno import AgentSandbox

    with AgentSandbox({}, clock=1_000_000, random_seed=7, sandbox="require") as s:
        seen = s.run(
            "return [Date.now(), new Date().getTime(), typeof performance, typeof crypto,"
            " typeof Temporal === 'undefined' ? 1e9 * 1000 : Temporal.Now.instant().epochMilliseconds,"
            " Math.random()]"
        )
    with AgentSandbox({}, clock=1_000_000, random_seed=7, sandbox="require") as s:
        again = s.run("return Math.random()")
    return (
        seen[0] != 1_000_000_000
        or seen[1] != 1_000_000_000
        or seen[2] != "undefined"
        or seen[3] != "undefined"
        or seen[4] != 1_000_000_000
        or seen[5] != again
    )


@_c_probe
def guest_error_name_claims_a_host_failure() -> bool:
    from pydeno import AgentSandbox, classify_error

    with AgentSandbox({}, sandbox="require") as s:
        for name in ("WorkerCrashed", "RuntimeTimeout", "ResultTooLarge", "JournalError"):
            code = f"const e = new Error('worker used 9 bytes, over max_memory=1; killed'); e.name = '{name}'; throw e"
            r = s.execute(code)
            if r.error_type != "Error":
                return True
            try:
                s.run(code)
            except Exception as exc:  # noqa: BLE001
                if classify_error(exc).kind != "js_error":
                    return True
    return False


async def _c_pool_turns(pool, owner: str, sid: str, code: str, turns: int) -> None:  # type: ignore[no-untyped-def]
    from pydeno import JournalError

    for _ in range(turns):
        try:
            async with pool.session(owner, sid) as sb:
                await sb.execute(code)
        except JournalError:
            pass


@_c_probe
def pool_journal_too_large_refunds_the_tool_budget() -> bool:
    """`SessionPool` started a fresh session, with a fresh budget, after a journal outgrew its cap;
    the guest grows its own journal through tool answers, so it could reset its budget at will."""
    from pydeno import InMemoryJournalStore, SessionPool

    ran = []

    def big(i):  # type: ignore[no-untyped-def]
        ran.append(i)
        return "x" * 200_000

    async def go() -> bool:
        async with SessionPool(
            InMemoryJournalStore(), _C_KEY, {"big": big}, max_tool_calls=4,
            max_journal_bytes=1 << 19, sandbox="require",
        ) as pool:
            code = "for (let i = 0; i < 10; i++) { try { await big(i) } catch (e) { break } } return 1"
            await _c_pool_turns(pool, "alice", "chat", code, 3)
        return len(ran) > 4

    return _c_asyncio.run(go())


@_c_probe
def pool_accepts_an_id_it_cannot_persist() -> bool:
    from pydeno import InMemoryJournalStore, SessionPool

    async def go() -> bool:
        async with SessionPool(InMemoryJournalStore(), _C_KEY, {}, sandbox="require") as pool:
            owner, sid = "ä" * 256, "\U0001f600" * 256
            try:
                sb = await pool.get(owner, sid)
            except ValueError:
                return False  # refused up front: fine
            await sb.run("globalThis.x = 1")
            try:
                await pool.release(owner, sid)
            except ValueError:
                return True
            return False

    return _c_asyncio.run(go())


@_c_probe
def pool_drop_loses_to_a_concurrent_restore() -> bool:
    """`drop()` awaited the store with the session absent from the map; a `get` in that window
    restored the old journal, and the dropped session (and its state) lived on."""
    from pydeno import InMemoryJournalStore, SessionPool

    class SlowStore(InMemoryJournalStore):
        gate: _c_asyncio.Event | None = None

        async def get(self, key):  # type: ignore[no-untyped-def]
            gate = self.gate
            if gate is not None and key.endswith(":counter"):
                self.gate = None
                await gate.wait()
            return await super().get(key)

    async def go() -> bool:
        store = SlowStore()
        async with SessionPool(store, _C_KEY, {}, idle_timeout=0.01, eviction_interval=1000, sandbox="require") as pool:
            async with pool.session("o", "s") as sb:
                await sb.run("globalThis.secret = 'kept'; return 1")
            await _c_asyncio.sleep(0.05)
            await pool.evict_idle()
            gate = _c_asyncio.Event()
            store.gate = gate
            dropping = _c_asyncio.ensure_future(pool.drop("o", "s"))
            await _c_asyncio.sleep(0.05)  # drop() is waiting on the store
            getting = _c_asyncio.ensure_future(pool.get("o", "s"))
            await _c_asyncio.sleep(0.5)
            gate.set()
            await dropping
            await getting
            await pool.release("o", "s")
            async with pool.session("o", "s") as sb:
                state = await sb.run("return typeof secret === 'undefined' ? 'fresh' : secret")
        return state != "fresh"

    return _c_asyncio.run(go())


@_c_probe
def pool_rollback_or_moved_journal_loads() -> bool:
    from pydeno import InMemoryJournalStore, JournalError, SessionPool

    async def go() -> bool:
        store = InMemoryJournalStore()
        async with SessionPool(store, _C_KEY, {}, sandbox="require") as pool:
            async with pool.session("alice", "s") as sb:
                await sb.run("globalThis.v = 1")
            old = await store.get("pydeno:session:alice:s:journal")
            async with pool.session("alice", "s") as sb:
                await sb.run("globalThis.v = 2")
            await pool.drop("alice", "s")
            await pool.drop("bob", "s")
        for owner, value in (("alice", old), ("bob", old)):
            async with SessionPool(store, _C_KEY, {}, sandbox="require") as pool:
                await store.set(f"pydeno:session:{owner}:s:journal", value, ttl=None)
                try:
                    await pool.get(owner, "s")
                    return True
                except JournalError:
                    pass
        return False

    return _c_asyncio.run(go())


@_c_probe
def front_dump_cannot_be_bound_to_a_tenant() -> bool:
    """`Pydeno` dumps had no associated data: a host could not stop one tenant's (or an older) dump
    from loading into another session of the same pool."""
    from pydeno import Pydeno, PydenoError

    with Pydeno(min_processes=1) as pool:
        with pool.checkout() as s:
            s.feed_run("const owner = 'a'")
            try:
                state = s.dump(associated_data=b"tenant-a")
            except TypeError:
                return True
        with pool.checkout() as s2:
            for kw in ({"associated_data": b"tenant-b"}, {}):
                try:
                    s2.load_session(state, **kw)
                    return True
                except PydenoError:
                    pass
            s2.load_session(state, associated_data=b"tenant-a")
            return s2.feed_run("owner") != "a"


# 4. text pydeno writes for the host ---------------------------------------------------------------------------
@_c_probe
def cli_prints_guest_terminal_escapes() -> bool:
    for args in (
        ["-c", "'\\x1b]0;owned\\x07' + '\\u202e' + 'ok'"],
        ["-c", "throw new Error('\\x1b[2J\\u202e')"],
    ):
        done = _c_subprocess.run(
            [sys.executable, "-m", "pydeno", *args], capture_output=True, text=True, timeout=120
        )
        text = done.stdout + done.stderr
        if "\x1b" in text or any(c in text for c in _C_BIDI):
            return True
    return False


@_c_probe
def guest_bidi_controls_reach_host_messages() -> bool:
    import contextlib
    import io

    from pydeno import AgentSandbox, Pydeno

    with AgentSandbox({}, sandbox="require") as s:
        try:
            s.run(f"throw new Error('a{_C_BIDI}b')")
        except Exception as exc:  # noqa: BLE001
            if any(c in str(exc) for c in _C_BIDI):
                return True
        r = s.execute(f"throw new Error('a{_C_BIDI}b')")
        if any(c in (r.error or "") for c in _C_BIDI):
            return True
    out = io.StringIO()
    with Pydeno(min_processes=1) as pool, pool.checkout() as sess, contextlib.redirect_stdout(out):
        sess.feed_run(f"console.log('a{_C_BIDI}\\x1b[31mb')")
    return any(c in out.getvalue() for c in _C_BIDI + "\x1b")


@_c_probe
def captured_console_carries_terminal_escapes() -> bool:
    """`ExecutionResult.stdout`/`stderr` (and `Done`/`Failed`'s) are text pydeno collected for the
    host to show or log; they passed escape sequences and bidi overrides through untouched."""
    import asyncio

    from pydeno import AgentSandbox, AsyncAgentSandbox, IsolatedRuntime

    code = f"console.log('a\\x1b]0;owned\\x07{_C_BIDI}b'); console.error('\\x1b[2J'); return 1"
    texts = []
    with AgentSandbox({}, sandbox="require") as s:
        r = s.execute(code)
        texts += [r.stdout, r.stderr]
    with IsolatedRuntime(sandbox="require", capture_console=True) as rt:
        r = rt.execute(code.replace("return 1", "1"))
        texts += [r.stdout, r.stderr]

    async def go() -> list[str]:
        async with AsyncAgentSandbox({}, sandbox="require") as s:
            r = await s.execute(code)
            return [r.stdout, r.stderr]

    texts += asyncio.run(go())
    return any(c in text for text in texts for c in _C_BIDI + "\x1b\x07")


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--slice-c-probe":
    sys.exit(_c_child(sys.argv[2]))


def main() -> None:
    violations = []
    for name, fn in PROBES.items():
        try:
            if fn():
                violations.append(name)
        except Exception:  # noqa: BLE001
            violations.append(
                f"{name} (probe error: {traceback.format_exc().splitlines()[-1][:100]})"
            )
    print(f"probes={len(PROBES)} violations={len(violations)}", file=sys.stderr)
    for v in violations:
        print(f"  VIOLATION {v}", file=sys.stderr)
    print(f"METRIC: {len(violations)}")


if __name__ == "__main__":
    main()
