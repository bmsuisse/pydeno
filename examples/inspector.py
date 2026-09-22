"""
Debug pydeno code via Chrome DevTools inspector.

This example demonstrates waiting for the debugger to attach before executing code.
This is useful for debugging initialization code or when you want to step through
from the very beginning.

Run this file, then open the chrome://inspect URL in Chrome to debug JavaScript execution.
"""

from pydeno import InspectorConfig, Runtime, RuntimeConfig


def main() -> None:
    """Demonstrate Chrome DevTools inspector integration."""
    print("=== Inspector Example ===\n")

    config = RuntimeConfig(
        inspector=InspectorConfig(
            display_name="pydeno Inspector Demo",
            wait_for_connection=True,  # Pause until debugger connects
        )
    )

    with Runtime(config) as runtime:
        endpoints = runtime.inspector_endpoints()
        if not endpoints:
            raise RuntimeError("Inspector is not enabled for this runtime")

        print("Open the chrome://inspect URL in Chrome to debug.")
        print("Waiting for debugger to attach...")

        # This will block until debugger connects and you click Resume
        runtime.eval("globalThis.counter = 0;")

        print("Debugger attached! Running code...\n")
        for i in range(3):
            result = runtime.eval("++counter")
            print(f"counter -> {result}")

        print("\nTriggering manual breakpoint with 'debugger;' statement...")
        runtime.eval("debugger; counter += 10; counter;")

        print("Done!\n")


if __name__ == "__main__":
    main()
