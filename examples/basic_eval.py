"""Basic usage of pydeno - showcasing both context-local API and explicit Runtime."""

import pydeno
from pydeno import Runtime


def context_local_example():
    """Using the context-local API (recommended for simple cases)."""
    print("=== Context-Local API ===")
    print("The easiest way - automatic per-task/thread isolation\n")

    # Simple evaluation
    print("2 + 2 =", pydeno.eval("2 + 2"))
    print("Math.sqrt(25) =", pydeno.eval("Math.sqrt(25)"))

    # State persists across evaluations in the same context
    pydeno.eval("globalThis.counter = 0;")
    for _ in range(3):
        print("counter ->", pydeno.eval("++counter"))

    print()


def explicit_runtime_example():
    """Using explicit Runtime for more control."""
    print("=== Explicit Runtime API ===")
    print("More control over runtime lifecycle\n")

    with Runtime() as rt:
        print("2 + 2 =", rt.eval("2 + 2"))
        rt.eval("globalThis.counter = 0;")
        for _ in range(3):
            print("counter ->", rt.eval("++counter"))

    print()


def main() -> None:
    """
    pydeno provides two ways to run JavaScript:

    1. Context-local API (pydeno.eval): Automatic runtime management per task/thread
       - Perfect for simple scripts and interactive sessions
       - Each asyncio task or thread gets its own isolated runtime
       - Cleanup happens automatically

    2. Explicit Runtime: Manual control over runtime lifecycle
       - Use when you need to share state across multiple operations
       - More control over configuration (memory limits, timeouts, etc.)
       - Must manage cleanup (use context manager or call .close())
    """
    context_local_example()
    explicit_runtime_example()


if __name__ == "__main__":
    main()
