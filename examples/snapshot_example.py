"""
Snapshot demonstration - pre-initialize V8 for faster startup.

Snapshots capture the V8 heap state after running initialization code,
allowing new runtimes to start from that pre-initialized state instead of
starting from scratch.

Use cases:
- Serverless functions (minimize cold start time)
- Multi-tenant systems (same libraries for all tenants)
- CLI tools (pre-load common dependencies)
- Testing (consistent starting state)
"""

import time
from pydeno import Runtime, RuntimeConfig, SnapshotBuilder


def measure_startup(label, config=None, iterations=10):
    """Measure average runtime creation time."""
    times = []

    for _ in range(iterations):
        start = time.perf_counter()
        runtime = Runtime(config) if config else Runtime()
        runtime.close()
        elapsed = time.perf_counter() - start
        times.append(elapsed)

    avg_time = sum(times) / len(times) * 1000  # Convert to ms
    print(f"{label}: {avg_time:.2f}ms (avg of {iterations} runs)")
    return avg_time


def basic_snapshot():
    """Create a simple snapshot with pre-loaded globals."""
    print("=== Basic Snapshot ===\n")

    # Create snapshot with initialization code
    builder = SnapshotBuilder()
    builder.execute_script(
        "init.js",
        """
        globalThis.VERSION = '1.0.0';
        globalThis.APP_NAME = 'MyApp';
        globalThis.utils = {
            double: (x) => x * 2,
            square: (x) => x * x,
        };
        """,
    )

    snapshot = builder.build()
    print(f"Snapshot created ({len(snapshot)} bytes)\n")

    # Use the snapshot
    config = RuntimeConfig(snapshot=snapshot)
    with Runtime(config) as runtime:
        # Globals are already available without re-initialization
        print(f"VERSION: {runtime.eval('VERSION')}")
        print(f"APP_NAME: {runtime.eval('APP_NAME')}")
        print(f"utils.double(5): {runtime.eval('utils.double(5)')}")
        print(f"utils.square(7): {runtime.eval('utils.square(7)')}")

    print()


def library_preloading():
    """Pre-load JavaScript libraries for faster subsequent use."""
    print("=== Library Pre-loading ===\n")

    # Simulate loading a library (like lodash, date-fns, etc.)
    library_code = """
    // Simplified "library" - in real use, this would be a full library
    globalThis.myLib = {
        version: '2.5.1',

        // Array utilities
        chunk(array, size) {
            const chunks = [];
            for (let i = 0; i < array.length; i += size) {
                chunks.push(array.slice(i, i + size));
            }
            return chunks;
        },

        // String utilities
        capitalize(str) {
            return str.charAt(0).toUpperCase() + str.slice(1);
        },

        // Math utilities
        clamp(value, min, max) {
            return Math.min(Math.max(value, min), max);
        },
    };
    """

    builder = SnapshotBuilder()
    builder.execute_script("myLib.js", library_code)
    snapshot = builder.build()

    print(f"Snapshot with library: {len(snapshot)} bytes\n")

    # Use the pre-loaded library
    config = RuntimeConfig(snapshot=snapshot)
    with Runtime(config) as runtime:
        result = runtime.eval("myLib.version")
        print(f"Library version: {result}")

        result = runtime.eval("myLib.chunk([1, 2, 3, 4, 5, 6], 2)")
        print(f"myLib.chunk([1,2,3,4,5,6], 2): {result}")

        result = runtime.eval("myLib.capitalize('hello')")
        print(f"myLib.capitalize('hello'): {result}")

        result = runtime.eval("myLib.clamp(150, 0, 100)")
        print(f"myLib.clamp(150, 0, 100): {result}")

    print()


def performance_comparison():
    """Compare startup times with and without snapshots."""
    print("=== Performance Comparison ===\n")

    # Expensive initialization code
    init_code = """
    globalThis.config = {
        apiUrl: 'https://api.example.com',
        timeout: 5000,
        retries: 3,
    };

    globalThis.helpers = {
        fibonacci(n) {
            if (n <= 1) return n;
            return this.fibonacci(n - 1) + this.fibonacci(n - 2);
        },
        factorial(n) {
            return n <= 1 ? 1 : n * this.factorial(n - 1);
        },
    };

    // Simulate some expensive initialization
    for (let i = 0; i < 100; i++) {
        globalThis['var' + i] = i;
    }
    """

    # Measure without snapshot
    print("Without snapshot (cold start every time):")
    without_snapshot_time = 0
    for _ in range(5):
        start = time.perf_counter()
        runtime = Runtime()
        runtime.eval(init_code)
        runtime.close()
        without_snapshot_time += time.perf_counter() - start

    without_snapshot_avg = (without_snapshot_time / 5) * 1000
    print(f"  Average: {without_snapshot_avg:.2f}ms\n")

    # Create snapshot
    builder = SnapshotBuilder()
    builder.execute_script("init.js", init_code)
    snapshot = builder.build()

    # Measure with snapshot
    print("With snapshot (pre-initialized):")
    config = RuntimeConfig(snapshot=snapshot)
    with_snapshot_time = 0
    for _ in range(5):
        start = time.perf_counter()
        runtime = Runtime(config)
        runtime.close()
        with_snapshot_time += time.perf_counter() - start

    with_snapshot_avg = (with_snapshot_time / 5) * 1000
    print(f"  Average: {with_snapshot_avg:.2f}ms\n")

    speedup = without_snapshot_avg / with_snapshot_avg
    print(f"Speedup: {speedup:.1f}x faster with snapshot")
    print(f"Time saved: {without_snapshot_avg - with_snapshot_avg:.2f}ms per runtime\n")


def multi_runtime_pattern():
    """Pattern for creating multiple runtimes from same snapshot."""
    print("=== Multi-Runtime Pattern ===\n")

    # Create shared snapshot
    builder = SnapshotBuilder()
    builder.execute_script(
        "common.js",
        """
        globalThis.TENANT_VERSION = '3.0';
        globalThis.logEvent = (event) => {
            return { timestamp: Date.now(), event };
        };
        """,
    )
    snapshot = builder.build()

    # Create multiple runtimes with same base state
    config = RuntimeConfig(snapshot=snapshot)

    print("Creating 3 runtimes from same snapshot:\n")
    for i in range(1, 4):
        with Runtime(config) as runtime:
            # Each runtime starts with the same snapshot
            # but has independent state after creation
            runtime.eval(f"globalThis.TENANT_ID = {i}")

            version = runtime.eval("TENANT_VERSION")
            tenant = runtime.eval("TENANT_ID")
            log = runtime.eval("logEvent('startup')")

            print(f"Runtime {i}: version={version}, tenant={tenant}, log={log}")

    print()


def main():
    """
    Snapshots are powerful for:

    1. Performance: Reduce cold start time (can be 5-10x faster)
    2. Consistency: All runtimes start with same state
    3. Efficiency: Share initialization code across runtimes
    4. Convenience: Pre-load libraries once, use everywhere

    Best practices:
    - Use for expensive initialization (libraries, configs, utilities)
    - Keep snapshots pure (no I/O, no side effects)
    - Test snapshot creation during build/deploy
    - Consider snapshot size vs startup time tradeoff
    """
    basic_snapshot()
    library_preloading()
    performance_comparison()
    multi_runtime_pattern()


if __name__ == "__main__":
    main()
