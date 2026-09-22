"""Minimal example demonstrating pydeno module loading from Python."""

import asyncio
from pydeno import Runtime


async def main() -> None:
    with Runtime() as rt:
        # Static module imported by bare name "math"
        rt.add_static_module("math", "export const answer = 42;")

        # Resolver + loader hand complete control over custom specifiers to Python
        def resolver(specifier: str, referrer: str) -> str | None:
            if specifier.startswith("custom:"):
                return specifier
            return None  # fall back to static module map

        async def loader(specifier: str) -> str:
            if specifier == "custom:message":
                return "export const text = 'Hello from custom loader';"
            raise ValueError(f"Unknown module: {specifier}")

        rt.set_module_resolver(resolver)
        rt.set_module_loader(loader)

        # Entry module imports everything and re-exports a summary object
        rt.add_static_module(
            "entry",
            """
            import { answer } from 'math';
            import { text } from 'custom:message';
            export const summary = { answer, text };
            """,
        )

        namespace = await rt.eval_module_async("entry")
        summary = namespace["summary"]
        print("Answer:", summary["answer"])  # -> 42
        print("Message:", summary["text"])  # -> Hello from custom loader


if __name__ == "__main__":
    asyncio.run(main())
