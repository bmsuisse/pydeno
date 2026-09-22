# WebAssembly

[WebAssembly][webassembly] (Wasm) lets you run compiled code inside JavaScript. With `pydeno`, you can load Wasm modules and call them from JavaScript, all within your Python application.

## Why Use WebAssembly?

WebAssembly offers portable, near-native performance. Use it when:

- **Performance matters**: CPU-intensive tasks (image processing, crypto, compression)
- **Porting native code**: Reuse C/C++/Rust libraries without rewriting in JavaScript
- **Sandboxing**: Run untrusted code with strict memory isolation
- **Cross-platform**: Same compiled code runs on various Wasm runtimes

## Loading WebAssembly

JavaScript's [`WebAssembly`][webassembly-mdn] API works in `pydeno`:

```python
from pydeno import Runtime

with Runtime() as runtime:
    # Load a .wasm file from Python
    # https://webassembly.js.org/example-add.wasm
    with open("add.wasm", "rb") as f:
        wasm_bytes = f.read()

    # Pass to JavaScript as Uint8Array
    runtime.bind_object("wasm", {"bytes": wasm_bytes})

    # Instantiate and use the module
    result = runtime.eval("""
        const module = new WebAssembly.Module(wasm.bytes);
        const instance = new WebAssembly.Instance(module);
        instance.exports.add(10, 20);
    """)
    print(result)  # 30
```

The Wasm module's exported functions are available on `instance.exports`.

## Async Instantiation

For better performance, use async instantiation:

```python
import asyncio

async def main():
    with Runtime() as runtime:
        with open("add.wasm", "rb") as f:
            runtime.bind_object("wasm", {"bytes": f.read()})

        result = await runtime.eval_async("""
            (async () => {
                const module = await WebAssembly.compile(wasm.bytes);
                const instance = await WebAssembly.instantiate(module);
                return instance.exports.add(5, 7);
            })()
        """)
        print(result)  # 12

asyncio.run(main())
```

This is non-blocking and recommended for large Wasm modules.

## Limitations

- **No file system**: Wasm can't access files directly. Pass data via JavaScript bindings.
- **No threads**: V8's Wasm doesn't support threads (SharedArrayBuffer-based parallelism). Use multiple runtimes instead.
- **Binary portability**: Wasm is portable, but some features (SIMD, etc.) depend on V8 version.

## Next Steps

- Learn about [Inspector](inspector.md) to debug Wasm modules with DevTools
- Explore [Bindings](../bindings.md) to pass data between Python and Wasm via JavaScript
- Check out [WebAssembly.org][webassembly] for tools and specifications

[webassembly]: https://webassembly.org/
[webassembly-mdn]: https://developer.mozilla.org/en-US/docs/WebAssembly
