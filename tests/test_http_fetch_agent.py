"""`http_fetch` bound as a tool: ToolBridge, AgentSandbox, AsyncAgentSandbox; redaction, budget,
journal replay.

POSIX-only (ToolBridge.attach and AgentSandbox import `pydeno._isolated`), so this file is in the Windows `collect_ignore` list; the tool
itself is covered portably in `test_http_fetch.py`, whose local server this reuses.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from test_http_fetch import Server, make

from pydeno import AgentSandbox, AsyncAgentSandbox, Runtime, ToolBridge

KEY = b"0123456789abcdef0123456789abcdef"


@pytest.fixture
def server() -> Iterator[Server]:
    s = Server()
    yield s
    s.stop()


def url(server: Server, path: str = "/ok") -> str:
    return f"http://fetch.test:{server.port}{path}"


class TestAgentSandbox:
    def test_fetch_and_describe(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"])
        with AgentSandbox({"fetch_url": tool}) as s:
            assert f"fetch.test:{server.port}/" in s.describe_tools()
            assert s.run(f"return (await fetch_url({url(server)!r})).body") == "hello"

    def test_bytes_body_reaches_the_guest_as_bytes(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"], response="bytes")
        with AgentSandbox({"fetch_url": tool}) as s:
            body = s.run(
                f"const b = (await fetch_url({url(server, '/bytes')!r})).body\n"
                "return [b instanceof Uint8Array, b.length]"
            )
        assert body == [True, 256]

    def test_refusal_reason_survives_redaction(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"])
        with AgentSandbox(
            {"fetch_url": tool}
        ) as s:  # redact_host_errors defaults to True
            out = s.run(
                "try { await fetch_url('http://evil.test/') } catch (e) { return [e.name, e.message] }"
            )
        assert out == ["HttpFetchBlocked", "the URL is not in the allow-list"]
        assert server.requests == []

    def test_counts_against_the_tool_budget(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"])
        with AgentSandbox({"fetch_url": tool}, max_tool_calls=1) as s:
            s.run(f"await fetch_url({url(server)!r})")
            name = s.run(
                f"try {{ await fetch_url({url(server)!r}) }} catch (e) {{ return e.name }}"
            )
        assert name == "ToolBudgetError"
        assert len(server.requests) == 1

    def test_replay_does_not_reissue_the_request(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"])
        with AgentSandbox({"fetch_url": tool}, max_tool_calls=5) as s:
            s.run(f"globalThis.r = await fetch_url({url(server)!r})")
            blob = s.dump(KEY)
        assert len(server.requests) == 1
        with AgentSandbox.load(blob, KEY, {"fetch_url": tool}) as restored:
            assert restored.run("return [r.status, r.body]") == [200, "hello"]
            assert restored.calls_made == 1
        assert len(server.requests) == 1

    def test_async_form_in_a_sync_session(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"])
        with AgentSandbox({"fetch_url": tool.aio}) as s:
            assert s.run(f"return (await fetch_url({url(server)!r})).status") == 200


class TestAsyncAgentSandbox:
    @pytest.mark.parametrize("form", ["sync", "aio"])
    async def test_both_forms(self, server: Server, form: str) -> None:
        tool = make([f"fetch.test:{server.port}"])
        async with AsyncAgentSandbox(
            {"fetch_url": tool if form == "sync" else tool.aio}, max_tool_calls=2
        ) as s:
            assert (
                await s.run(f"return (await fetch_url({url(server)!r})).body")
                == "hello"
            )
            out = await s.run(
                "try { await fetch_url('http://evil.test/') } catch (e) { return e.name }"
            )
            assert out == "HttpFetchBlocked"
        assert len(server.requests) == 1


class TestToolBridge:
    def test_tool_bridge_sync(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"])
        bridge = ToolBridge({"fetch_url": tool}, max_calls=2)
        with Runtime() as rt:
            bridge.attach(rt)
            assert (
                rt.eval(f"tools.fetch_url('http://fetch.test:{server.port}/ok').body")
                == "hello"
            )
            assert (
                rt.eval(
                    "try { tools.fetch_url('http://evil.test/') } catch (e) { e.name }"
                )
                == "HttpFetchBlocked"
            )
            with pytest.raises(Exception, match="ToolBudgetError"):
                rt.eval(f"tools.fetch_url('http://fetch.test:{server.port}/ok')")
        assert len(server.requests) == 1

    async def test_tool_bridge_async(self, server: Server) -> None:
        tool = make([f"fetch.test:{server.port}"])
        bridge = ToolBridge({"fetch_url": tool.aio}, max_calls=1)
        with Runtime() as rt:
            bridge.attach(rt)
            out = await rt.eval_async(
                f"tools.fetch_url('http://fetch.test:{server.port}/ok')", timeout=10
            )
            assert out["status"] == 200
            name = await rt.eval_async(
                "tools.fetch_url('http://fetch.test/').then(() => 'ok', (e) => e.name)",
                timeout=10,
            )
            assert name == "ToolBudgetError"
        assert bridge.calls_remaining == 0

    def test_guest_headers_through_the_bridge(self, server: Server) -> None:
        bridge = ToolBridge({"fetch_url": make([f"fetch.test:{server.port}"])})
        with Runtime() as rt:
            bridge.attach(rt)
            name = rt.eval(
                f"try {{ tools.fetch_url('http://fetch.test:{server.port}/ok', "
                "{headers: {'X-Evil': '1'}}); 'sent' } catch (e) { e.name }"
            )
        assert name == "TypeError"
        assert server.requests == []
