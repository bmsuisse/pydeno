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

## Next Steps

- Learn about [Snapshots](snapshots.md) to pre-load code for faster debugging
- Explore [WebAssembly](webassembly.md) to debug Wasm modules
- Check the [API Reference](../../api/runtime.md) for complete options
