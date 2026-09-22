# JavaScript Evaluation

When you run JavaScript code with `pydeno`, you're evaluating code snippets or expressions and getting results back in Python. This page explains how evaluation works, what you get back, and how to handle different execution scenarios.

## Basic Evaluation

The simplest way to run JavaScript is with [`eval()`][pydeno.Runtime.eval]:

```python
from pydeno import Runtime

with Runtime() as runtime:
    result = runtime.eval("2 + 2")
    print(result)  # 4
```

`eval()` takes a string of JavaScript code, executes it, and returns the result. The result is automatically converted to a Python type (see [Type Conversion](types.md) for details).

## Return Values

JavaScript evaluation returns the value of the **last expression** in your code:

```python
with Runtime() as runtime:
    # Returns the result of the arithmetic expression
    result = runtime.eval("10 + 5")
    print(result)  # 15

    # Multiple statements - returns the last one
    result = runtime.eval("""
        const x = 10;
        const y = 20;
        x + y  // This value is returned
    """)
    print(result)  # 30

    # Functions return their result
    result = runtime.eval("Math.max(1, 2, 3, 4, 5)")
    print(result)  # 5
```

### Statements vs Expressions

Statements like variable declarations don't return values, instead it returns a [`undefined`][pydeno.undefined] object:

```python
from pydeno import undefined

with Runtime() as runtime:
    # Variable declaration returns undefined
    result = runtime.eval("let x = 42")
    print(result is undefined)  # True

    # But you can access the value afterward
    result = runtime.eval("x")
    print(result)  # 42
```

To return a value from multi-line code, make sure the last line is an expression:

```python
with Runtime() as runtime:
    result = runtime.eval("""
        function calculate() {
            return 10 * 5;
        }
        calculate()  // Call the function as the last expression
    """)
    print(result)  # 50
```

## Sync vs Async

`pydeno` offers two evaluation methods:

### Synchronous: `eval()`

Use for quick JavaScript computations that complete immediately:

```python
with Runtime() as runtime:
    result = runtime.eval("Math.sqrt(144)")
    print(result)  # 12.0
```

**When to use:**

- Simple calculations and data transformations
- Accessing variables and calling synchronous functions
- Quick scripts that don't involve asynchronous operations

### Asynchronous: `eval_async()`

Use when working with JavaScript Promises or async/await:

```python
import asyncio

async def main():
    with Runtime() as runtime:
        # Automatically waits for Promise to resolve
        result = await runtime.eval_async("Promise.resolve(42)")
        print(result)  # 42

        # Works with async/await syntax
        result = await runtime.eval_async("""
            (async () => {
                return await Promise.resolve('done');
            })()
        """)
        print(result)  # done

asyncio.run(main())
```

**When to use:**

- JavaScript code that returns Promises
- Async functions or code using `await`
- Integration with async Python code (asyncio, aiohttp, etc.)

!!! tip "Prefer `eval_async()` in async contexts"
    If you're already in an async Python function, use [`eval_async()`][pydeno.Runtime.eval_async]. It won't block your event loop and handles Promises naturally.


## Resource Limits

### Timeouts

Long-running JavaScript can be limited with timeouts:

```python
async def main():
    with Runtime() as runtime:
        try:
            # Will timeout after 1 second
            result = await runtime.eval_async(
                "while(true) {}",  # Infinite loop
                timeout=1.0
            )
        except RuntimeError as e:
            if "timed out" in str(e).lower():
                print("JavaScript took too long!")

asyncio.run(main())
```

The timeout is specified in seconds (float). Without a timeout, infinite loops will run forever.

!!! warning "Always set timeouts for untrusted code"
    If you're running user-provided JavaScript, always set a reasonable timeout to prevent resource exhaustion.

### Memory Limits

You can limit the JavaScript heap size to prevent excessive memory usage:

```python
from pydeno import Runtime, RuntimeConfig

config = RuntimeConfig(max_heap_size=10 * 1024 * 1024)  # 10MB limit

with Runtime(config) as runtime:
    try:
        # This will fail if it tries to allocate more than 10MB
        runtime.eval("""
            const huge = [];
            for (let i = 0; i < 10_000_000; i++) {
                huge.push({ data: 'x'.repeat(100) });
            }
        """)
    except RuntimeError as e:
        print(f"Out of memory: {e}")
        # Out of memory: Evaluation failed: Heap limit exceeded
```

When JavaScript code exceeds the configured heap limit, the runtime terminates and raises a `RuntimeError` with the message "Heap limit exceeded".

!!! warning "Set memory limits for untrusted code"
    Always configure `max_heap_size` when running untrusted JavaScript to prevent memory exhaustion attacks. The runtime will terminate gracefully when the limit is reached.

## Stopping Stuck Code

Any script can be stopped, including ones that never return to JavaScript.
There are three mechanisms, in increasing order of severity:

| Mechanism | Stops | Typical latency | Runtime afterwards |
|---|---|---|---|
| `timeout=` / `RuntimeConfig(timeout=)` | runaway loops, pending promises | the timeout | **still usable** |
| `TerminationHandle.terminate()` | the same, from another thread | 0.12 ms (loops), ~1.7 ms (pending promises) | terminated, not reusable |
| `RuntimeConfig(force_kill_grace=)` | a runtime wedged in a host callback | grace period | abandoned; create a new one |

**A timeout never costs you your runtime.** Bound host functions, module state
and globals all survive, so a `Runtime` you have configured and bound
functions into remains valid after a script of yours is killed:

```python
with Runtime(RuntimeConfig(timeout=0.2)) as runtime:
    runtime.bind_function("greet", lambda: "hello")
    try:
        runtime.eval("while(true){}")
    except RuntimeError:
        pass
    runtime.eval("greet()")  # still "hello" -- the binding is intact
```

`terminate()` is for a script you want gone *now*, from a watchdog thread,
and it does end the runtime: subsequent calls raise
[`RuntimeTerminated`][pydeno.RuntimeTerminated]. It works on every parked
shape — a promise nobody resolves, an `await` on one, a `.then` chain built
on one — because the runtime's dispatcher checks for a termination request
between event-loop polls. V8's own `terminate_execution()` cannot do this
alone: it only fires when V8 next *enters* JavaScript, which a
drained-but-pending event loop never does.

### When the runtime thread itself is stuck

One case defeats both of the above: a **synchronous host callback that never
returns**. The runtime thread is then blocked inside your Python code, so it
cannot notice a termination request and V8 cannot unwind anything.

```python
config = RuntimeConfig(force_kill_grace=pydeno.SUGGESTED_FORCE_KILL_GRACE)
with Runtime(config) as runtime:
    ...
```

With `force_kill_grace` set, a blocked caller waits that long for the runtime
to acknowledge a termination and then gives up, raising
[`RuntimeForceKilled`][pydeno.RuntimeForceKilled] (a subclass of
`RuntimeTerminated`, so existing handlers keep working).

!!! warning "`force_kill_grace` is opt-in for two real reasons"
    **It costs ~10% per synchronous call**, because every blocking wait becomes
    a sliced wait. Leave it unset unless you actually run untrusted *Python*
    callbacks — runaway JavaScript and never-resolving promises are already
    bounded without it.

    **It abandons rather than reclaims.** A V8 isolate cannot be dropped from
    another thread, so the wedged thread keeps its isolate and heap until the
    host call returns (if ever). The `Runtime` is permanently unusable and none
    of its bound functions or state carry over — create a new one, at the usual
    cost of a runtime creation.

## Error Handling

JavaScript errors are exposed as [`JavaScriptError`][pydeno.JavaScriptError], which includes the JavaScript stack trace, making debugging easier:

```python
with Runtime() as runtime:
    try:
        runtime.eval("throw new Error('Something went wrong')")
    except JavaScriptError as e:
        print(f"JavaScript error: {e}")
        # JavaScript error: Evaluation failed: Error: Something went wrong
```

## Global Scope Persistence

Variables and functions defined in one evaluation persist for future evaluations:

```python
with Runtime() as runtime:
    # Define a global variable
    runtime.eval("globalThis.counter = 0")

    # Use it in subsequent evaluations
    runtime.eval("counter++")
    runtime.eval("counter++")

    result = runtime.eval("counter")
    print(result)  # 2
```

This is useful for building interactive environments or maintaining session data.

To reset, just create a new runtime:

```python
# First runtime has state
with Runtime() as runtime1:
    runtime1.eval("let x = 100")
    print(runtime1.eval("x"))  # 100

# New runtime starts fresh
with Runtime() as runtime2:
    try:
        runtime2.eval("x")  # ReferenceError
    except JavaScriptError:
        print("x is not defined in new runtime")
```

## Common Patterns

### Quick Calculation

```python
import pydeno

# Using the module-level eval for one-off calculations
result = pydeno.eval("Math.pow(2, 10)")
print(result)  # 1024
```

### Building a REPL

```python
with Runtime() as runtime:
    while True:
        code = input("js> ")
        if code == "exit":
            break
        try:
            result = runtime.eval(code)
            print(result)
        except Exception as e:
            print(f"Error: {e}")
```

### Safe Evaluation with Timeout

```python
async def safe_eval(code: str, timeout: float = 5.0):
    with Runtime() as runtime:
        try:
            return await runtime.eval_async(code, timeout=timeout)
        except TimeoutError:
            return "Execution timed out"
        except Exception as e:
            return f"Error: {e}"

# Usage
result = await safe_eval("Promise.resolve(42)")
```

## Performance Tips

- **Load libraries once** via bootstrap code in [`RuntimeConfig`][pydeno.RuntimeConfig] rather than re-evaluating them
- **Minimize data transfer** do heavy computation in JavaScript, only return final results
- **Use snapshots** for frequently-used initialization code (see [Snapshots guide](../guides/advanced/snapshots.md))

## Next Steps

- Learn about [Type Conversion](types.md) to understand what types you can pass and receive
- See [Modules](../guides/modules.md) to organize JavaScript code with imports and exports
- Check out [Bindings](../guides/bindings.md) to call Python functions from JavaScript
