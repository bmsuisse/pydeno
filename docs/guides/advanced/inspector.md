# Inspector & Debugging

The inspector enables debugging via the [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/), allowing you to use [Chrome DevTools](https://developer.chrome.com/docs/devtools) to set breakpoints and inspect variables.

!!! warning "Experimental Feature"
    Debugger support is currently limited and experimental. Some advanced DevTools features may not work as expected.

## Quick Start

Enable the inspector when creating the runtime:

```python
from pydeno import Runtime, RuntimeConfig, InspectorConfig

# Configure inspector (waits for DevTools to connect)
config = RuntimeConfig(inspector=InspectorConfig(wait_for_connection=True))

with Runtime(config) as runtime:
    # Get inspector endpoints
    endpoints = runtime.inspector_endpoints()
    if endpoints:
        print(f"Open: {endpoints.devtools_frontend_url}")

    # Run your code
    runtime.eval("""
        function calculate(x) {
            debugger;  // Pause here
            return x * 2;
        }

        calculate(21);
    """)
```

Copy the URL and paste it into Chrome or Edge. DevTools opens and you can debug like normal JavaScript.

## Using the Debugger

When DevTools is connected, execution stops at `debugger;` and you can:

- Inspect local variables
- Step through code
- Evaluate expressions
- Set more breakpoints

## Configuration Options

[`InspectorConfig`][pydeno.InspectorConfig] provides several options:

- `host`: Bind address (default: `"127.0.0.1"`)
- `port`: DevTools port (default: `9229`)
- `wait_for_connection`: Block execution until a debugger connects (default: `False`)
- `break_on_next_statement`: Pause on the first statement after connection (default: `False`)
- `target_url`: Optional URL reported to DevTools
- `display_name`: Optional display title in `chrome://inspect`

Use [`runtime.inspector_endpoints()`][pydeno.Runtime.inspector_endpoints] to get the `devtools_frontend_url` and `websocket_url` for connecting.

## Builds without the inspector

The inspector server (an HTTP/WebSocket listener built on `hyper`, `hyper-util`, `fastwebsockets`
and tokio's `net` feature) is the cargo feature `inspector`. It is on by default, so the published
wheels include it. A minimal build drops it:

```bash
maturin build --release --no-default-features
```

In that build `InspectorConfig` still exists, so code that constructs one still imports. But
creating a `Runtime` with `RuntimeConfig(inspector=...)` raises
`RuntimeError: ... pydeno was built without inspector support ...` instead of starting without
the debugger. `pydeno._pydeno._INSPECTOR_AVAILABLE` tells you which build you have.

`IsolatedRuntime` already refuses an inspector config, and its worker's sandbox denies network
sockets. The minimal build is defence in depth (no listener code or network crates in the
binary), not a fix for a reachable hole. CI builds it on every push (`minimal-build` in
`.github/workflows/test.yml`), runs the isolated-runtime suites against it, and prints its size
and dependency difference.

## Next Steps

- Learn about [Snapshots](snapshots.md) to pre-load code for faster debugging
- Explore [WebAssembly](webassembly.md) to debug Wasm modules
- Check the [API Reference](../../api/runtime.md) for complete options
