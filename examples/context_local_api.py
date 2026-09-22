"""
Context-local API demonstration.

The pydeno module provides convenience functions that automatically manage
a per-task/thread runtime. This is the easiest way to use pydeno for simple
scripts and interactive sessions.

Key features:
- Automatic runtime creation and cleanup
- Per-asyncio-task isolation
- Per-thread isolation
- No manual lifecycle management needed
"""

import asyncio
import pydeno


def basic_usage():
    """Simplest way to use pydeno - just call pydeno.eval()."""
    print("=== Basic Usage ===\n")

    # Evaluate JavaScript directly
    result = pydeno.eval("2 + 2")
    print(f"2 + 2 = {result}")

    result = pydeno.eval("Math.sqrt(144)")
    print(f"Math.sqrt(144) = {result}")

    # State persists across evaluations in the same context
    pydeno.eval("let counter = 0;")
    pydeno.eval("counter++;")
    pydeno.eval("counter++;")
    result = pydeno.eval("counter")
    print(f"counter = {result}")

    print()


def bindings_example():
    """Bind Python functions and objects to JavaScript."""
    print("=== Bindings Example ===\n")

    # Bind a Python function
    def greet(name):
        return f"Hello, {name}!"

    pydeno.bind_function("greet", greet)
    result = pydeno.eval("greet('World')")
    print(f"greet('World') = {result}")

    # Bind a Python object
    config = {
        "debug": True,
        "timeout": 30,
        "version": "1.0.0",
    }
    pydeno.bind_object("config", config)

    result = pydeno.eval("config.version")
    print(f"config.version = {result}")

    result = pydeno.eval("config.debug && config.timeout > 0")
    print(f"config.debug && config.timeout > 0 = {result}")

    print()


async def async_example():
    """Use async evaluation with the context-local API."""
    print("=== Async Example ===\n")

    # Evaluate async JavaScript
    result = await pydeno.eval_async("Promise.resolve(42)")
    print(f"Promise.resolve(42) = {result}")

    # Bind async Python function for delayed operations
    async def delayed_operation():
        await asyncio.sleep(0.1)
        return "done"

    pydeno.bind_function("delayedOp", delayed_operation)

    # Call it from JavaScript - it becomes a Promise
    result = await pydeno.eval_async(
        "delayedOp()",
        timeout=1.0,
    )
    print(f"Delayed promise result: {result}")

    print()


async def per_task_isolation():
    """Each asyncio task gets its own isolated runtime."""
    print("=== Per-Task Isolation ===\n")

    async def task_worker(task_id, delay):
        # Each task has its own runtime with isolated state
        pydeno.eval(f"globalThis.taskId = {task_id};")
        pydeno.eval("globalThis.counter = 0;")

        for _ in range(3):
            await asyncio.sleep(delay)
            pydeno.eval("counter++;")

        counter = pydeno.eval("counter")
        tid = pydeno.eval("taskId")
        print(f"Task {task_id}: counter={counter}, taskId={tid}")

    # Run multiple tasks concurrently - each gets its own runtime
    await asyncio.gather(
        task_worker(1, 0.01),
        task_worker(2, 0.015),
        task_worker(3, 0.02),
    )

    print()


def get_default_runtime_example():
    """Access the context-local runtime explicitly if needed."""
    print("=== Accessing Default Runtime ===\n")

    # Get the context-local runtime
    runtime = pydeno.get_default_runtime()

    # You can use it like any other Runtime instance
    runtime.eval("globalThis.data = []")
    runtime.eval("data.push(1, 2, 3)")

    # Or continue using pydeno.eval() - same runtime
    pydeno.eval("data.push(4, 5)")

    result = runtime.eval("data")
    print(f"data = {result}")

    # Get stats
    stats = runtime.get_stats()
    print(f"Runtime stats: {stats}")

    print()


async def main():
    """
    The context-local API is perfect for:
    - Simple scripts
    - Interactive sessions (REPL, Jupyter notebooks)
    - Web handlers where each request is isolated
    - Any scenario where you don't need to share state across contexts

    Use explicit Runtime() when you need:
    - Shared state across operations
    - Custom configuration (memory limits, snapshots, inspector)
    - Manual control over runtime lifecycle
    """
    basic_usage()
    bindings_example()
    await async_example()
    await per_task_isolation()
    get_default_runtime_example()


if __name__ == "__main__":
    asyncio.run(main())
