"""Example: resolving JS + WASM files from Python-managed resolver/loader."""

import asyncio
from pathlib import Path
from pydeno import Runtime


async def main() -> None:
    base_dir = Path(__file__).resolve().parent

    with Runtime() as rt:

        def resolver(specifier: str, referrer: str) -> str | None:
            if specifier.startswith((".", "/")):
                return (base_dir / specifier).resolve().as_uri()
            return None

        async def loader(specifier: str) -> str:
            path = Path(specifier.removeprefix("file://"))
            if path.suffix == ".js":
                return path.read_text()
            elif path.suffix == ".wasm":
                bytes_str = ", ".join(map(str, path.read_bytes()))
                return (
                    f"const bytes = new Uint8Array([{bytes_str}]);"
                    "const module = new WebAssembly.Module(bytes);"
                    "const instance = new WebAssembly.Instance(module, {});"
                    "export const add = (a, b) => instance.exports.add(a, b);"
                )

            raise ValueError(f"Cannot load {specifier}")

        rt.set_module_resolver(resolver)
        rt.set_module_loader(loader)

        result = await rt.eval_async("import('./add.wasm').then(M => M.add(10, 13));")
        print("10 + 13 =", result)


if __name__ == "__main__":
    asyncio.run(main())
