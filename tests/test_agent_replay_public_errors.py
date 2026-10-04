"""A public pydeno error caught by the guest must replay: its recorded message is not redacted twice."""

import asyncio

from pydeno import AgentSandbox, SchemaTool

KEY = b"k" * 32
LOOKUP = SchemaTool("lookup", "look", {"type": "object"}, lambda a: a)
CATALOG = [SchemaTool("secret_op", "secret thing", {"type": "object"}, lambda a: "ran")]
WRONG_ARITY = "try { await lookup(1, 2) } catch (e) { return e.message }"
UNKNOWN_TOOL = "try { await describe_tool('nope') } catch (e) { return e.message }"


def test_a_caught_argument_error_replays() -> None:
    with AgentSandbox({"lookup": LOOKUP}) as sb:
        live = sb.run(WRONG_ARITY)
        blob = sb.dump(KEY)
    with AgentSandbox.load(blob, KEY, {"lookup": LOOKUP}) as restored:
        assert "takes one object argument" in live
        assert restored.calls_made == 1


def test_a_caught_catalog_error_replays() -> None:
    with AgentSandbox({}, tools_catalog=CATALOG) as sb:
        sb.run(UNKNOWN_TOOL)
        blob = sb.dump(KEY)
    AgentSandbox.load(blob, KEY, {}, tools_catalog=CATALOG).close()


def test_the_async_class_restores_it_too() -> None:
    from pydeno import AsyncAgentSandbox

    async def main() -> None:
        async with AsyncAgentSandbox({"lookup": LOOKUP}) as sb:
            await sb.run(WRONG_ARITY)
            blob = await sb.dump(KEY)
        restored = await AsyncAgentSandbox.load(blob, KEY, {"lookup": LOOKUP})
        await restored.close()

    asyncio.run(main())
