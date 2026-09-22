# Modules

JavaScript modules let you organize code into reusable pieces. With `pydeno`, you can provide JavaScript modules to your runtime and use them with standard `import` statements.

## Why Use Modules?

Instead of cramming everything into one big eval:

- **Organize**: Split code into logical pieces
- **Reuse**: Import the same module from multiple scripts
- **Standard**: Use ES6 `import`/`export` syntax JavaScript developers expect

## Static Modules

The simplest way is to register modules upfront with [`add_static_module()`][pydeno.Runtime.add_static_module]:

```python
import asyncio
from pydeno import Runtime

async def main():
    with Runtime() as runtime:
        # Register a module
        runtime.add_static_module("math", """
            export function add(a, b) {
                return a + b;
            }

            export const PI = 3.14159;
        """)

        # Use it with dynamic import
        result = await runtime.eval_async("""
            (async () => {
                const { add, PI } = await import('math');
                return add(10, 5) * PI;
            })()
        """)
        print(result)  # 47.12385

asyncio.run(main())
```

!!! note "Dynamic imports with eval"
    Since `eval()` and `eval_async()` don't support top-level `await`, wrap your import in an async IIFE: `(async () => { ... })()`

## Loading from Files

Load modules from your filesystem:

```python
with Runtime() as runtime:
    # Read a .js file
    with open("utils.js") as f:
        code = f.read()

    runtime.add_static_module("utils", code)
```

## Custom Module Resolution

For more control, use [`set_module_resolver()`][pydeno.Runtime.set_module_resolver] to intercept import statements:

```python
async def main():
    with Runtime() as runtime:
        def resolver(specifier: str, referrer: str):
            # Handle custom URL schemes
            if specifier.startswith("lib:"):
                return specifier
            # Let other imports fall through
            return None

        runtime.set_module_resolver(resolver)
```

The resolver receives:

- **specifier**: The string in the `import` statement (`"math"`, `"./utils"`, etc.)
- **referrer**: The URL of the importing module

Return the resolved module URL, or `None` to let other resolvers handle it.

## Custom Module Loader

Use [`set_module_loader()`][pydeno.Runtime.set_module_loader] to fetch module code dynamically:

```python
async def main():
    with Runtime() as runtime:
        async def loader(specifier: str):
            if specifier.startswith("lib:"):
                # Load from database, network, etc.
                module_name = specifier[4:]  # Remove "lib:"
                return f"export const name = '{module_name}';"
            raise ValueError(f"Unknown module: {specifier}")

        runtime.set_module_loader(loader)

        def resolver(specifier: str, referrer: str):
            if specifier.startswith("lib:"):
                return specifier
            return None

        runtime.set_module_resolver(resolver)

        # Now you can import custom modules
        result = await runtime.eval_async("""
            (async () => {
                const mod = await import('lib:mylib');
                return mod.name;
            })()
        """)
        print(result)  # "mylib"

asyncio.run(main())
```

## Evaluating Modules Directly

Instead of using `import` inside `eval`, you can evaluate a module directly with [`eval_module_async()`][pydeno.Runtime.eval_module_async]:

```python
async def main():
    with Runtime() as runtime:
        runtime.add_static_module("entry", """
            export const result = 42;
            export function greet(name) {
                return `Hello, ${name}!`;
            }
        """)

        # Evaluate the module and get its namespace
        namespace = await runtime.eval_module_async("entry")

        print(namespace["result"])          # 42
        print(namespace["greet"]("World"))  # "Hello, World!"

asyncio.run(main())
```

This is cleaner when you want to evaluate a module once and access its exports from Python.

## Practical Examples

### Organizing Utilities

```python
import asyncio
from pydeno import Runtime

async def main():
    with Runtime() as runtime:
        runtime.add_static_module("string-utils", """
            export function capitalize(str) {
                return str.charAt(0).toUpperCase() + str.slice(1);
            }

            export function reverse(str) {
                return str.split('').reverse().join('');
            }
        """)

        runtime.add_static_module("array-utils", """
            export function sum(arr) {
                return arr.reduce((a, b) => a + b, 0);
            }

            export function average(arr) {
                return sum(arr) / arr.length;
            }
        """)

        result = await runtime.eval_async("""
            (async () => {
                const { capitalize } = await import('string-utils');
                const { average } = await import('array-utils');

                return {
                    title: capitalize('hello world'),
                    avg: average([1, 2, 3, 4, 5])
                };
            })()
        """)
        print(result)  # {'title': 'Hello world', 'avg': 3.0}

asyncio.run(main())
```

### Virtual File System

```python
async def main():
    with Runtime() as runtime:
        # Simulate a virtual filesystem
        virtual_fs = {
            "config.js": "export const API_URL = 'https://api.example.com';",
            "auth.js": "export function isAuthenticated() { return true; }",
        }

        async def loader(specifier: str):
            # Strip file:/// prefix to get original name
            name = specifier.replace("file:///", "")
            if name in virtual_fs:
                return virtual_fs[name]
            raise ValueError(f"Module not found: {specifier}")

        runtime.set_module_loader(loader)
        runtime.set_module_resolver(lambda spec, ref: f"file:///{spec}" if spec in virtual_fs else None)

        result = await runtime.eval_async("""
            (async () => {
                const { API_URL } = await import('config.js');
                const { isAuthenticated } = await import('auth.js');

                return { url: API_URL, auth: isAuthenticated() };
            })()
        """)
        print(result)  # {'url': 'https://api.example.com', 'auth': True}

asyncio.run(main())
```

### Loading Third-Party Libraries

You can fetch JavaScript libraries from CDNs and load them as modules:

```python
import requests
import asyncio

async def main():
    with Runtime() as runtime:
        # Fetch a library from CDN
        response = requests.get("https://cdn.jsdelivr.net/npm/ms@2.1.3/+esm")
        runtime.add_static_module("ms", response.text)

        result = await runtime.eval_async("""
            (async () => {
                const ms = await import('ms');
                return ms.default('2 days');  // Convert to milliseconds
            })()
        """)
        print(result)  # 172800000

asyncio.run(main())
```

!!! tip "Preloading libraries in snapshots"
    For better performance, you can pre-load CDN libraries using [Snapshots](advanced/snapshots.md#preloading-cdn-libraries) to avoid repeated fetching and initialization across multiple runtimes.

## Module Resolution Order

When JavaScript tries to `import "something"`, the resolution happens in this order:

1. **Custom resolver** (if set with `set_module_resolver()`)
2. **Static modules** (registered with `add_static_module()`)
3. **Error** if no match found

## Limitations

- **No file system access**: JavaScript can't read files directly. You must provide all code via modules or bindings.
- **No Node.js built-ins**: Modules like `fs`, `http`, `path` aren't available unless you polyfill them.
- **No CommonJS**: Only ES6 modules (`import`/`export`) are supported, not `require()`.

## Next Steps

- Learn about [Bindings](bindings.md) to expose Python functions JavaScript modules can call
- Explore [Snapshots](advanced/snapshots.md) to pre-load modules for faster startup
- Check out the [API Reference](../api/runtime.md) for complete module methods
