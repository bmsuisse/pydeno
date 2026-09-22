"""
Resource limits and security controls.

This example demonstrates how to protect against:
- Runaway JavaScript code (infinite loops, memory leaks)
- Malicious code (DoS attacks, resource exhaustion)
- Untrusted user input

Key controls:
- Memory limits (max_heap_size)
- Execution timeouts
- Error handling for different failure modes
"""

import asyncio
from pydeno import JavaScriptError, Runtime, RuntimeConfig


def memory_limits():
    """Demonstrate memory limits to prevent heap exhaustion."""
    print("=== Memory Limits ===\n")

    # Create runtime with 10MB heap limit
    config = RuntimeConfig(max_heap_size=10 * 1024 * 1024)

    with Runtime(config) as runtime:
        print("Runtime configured with 10MB heap limit\n")

        # Small allocation - will succeed
        try:
            runtime.eval("const small = new Array(1000).fill(42)")
            print("✓ Small allocation succeeded")
        except Exception as e:
            print(f"✗ Small allocation failed: {e}")

        # Moderate allocation - will succeed within limit
        try:
            runtime.eval("""
                const moderate = [];
                for (let i = 0; i < 50_000; i++) {
                    moderate.push({ id: i, data: 'x'.repeat(10) });
                }
            """)
            print("✓ Moderate allocation succeeded (within limit)")
        except Exception as e:
            print(f"✗ Moderate allocation failed: {type(e).__name__}")

        # Check heap usage
        stats = runtime.get_stats()
        heap_mb = stats.heap_used_bytes / (1024 * 1024)
        print(f"  Heap usage: {heap_mb:.2f} MB / 10 MB\n")

        # Large allocation - will exceed heap limit
        print("Attempting large allocation that exceeds heap limit...")
        try:
            runtime.eval("""
                const huge = [];
                for (let i = 0; i < 10_000_000; i++) {
                    huge.push({ id: i, data: 'x'.repeat(100) });
                }
            """)
            print("✗ Large allocation succeeded (should have failed)")
        except RuntimeError as e:
            # Will catch: "Evaluation failed: Heap limit exceeded"
            print(f"✓ Caught: {e}")

        print()


async def execution_timeouts():
    """Demonstrate timeouts to prevent infinite loops."""
    print("=== Execution Timeouts ===\n")

    with Runtime() as runtime:
        # Quick operation - completes within timeout
        try:
            result = await runtime.eval_async(
                "Math.sqrt(144)",
                timeout=1.0,
            )
            print(f"✓ Quick operation completed: {result}")
        except TimeoutError:
            print("✗ Quick operation timed out")

        # Slow operation - completes but takes time
        try:
            result = await runtime.eval_async(
                """
                let sum = 0;
                for (let i = 0; i < 1_000_000; i++) {
                    sum += i;
                }
                sum
                """,
                timeout=2.0,
            )
            print(f"✓ Slow operation completed: {result}")
        except TimeoutError:
            print("✗ Slow operation timed out")

        # Infinite loop - should timeout
        try:
            await runtime.eval_async(
                "while (true) { }",
                timeout=0.5,
            )
            print("✗ Infinite loop did not timeout (unexpected)")
        except (TimeoutError, RuntimeError) as e:
            if "timed out" in str(e).lower():
                print("✓ Infinite loop caught by timeout")
            else:
                raise

    print()


async def error_handling():
    """Comprehensive error handling for different failure modes."""
    print("=== Error Handling ===\n")

    config = RuntimeConfig(
        max_heap_size=50 * 1024 * 1024,
        timeout=5.0,
    )

    with Runtime(config) as runtime:
        # 1. JavaScript syntax error
        print("1. Syntax Error:")
        try:
            runtime.eval("const x = ;")
        except JavaScriptError as e:
            print(f"   Caught: {type(e).__name__}: {e}\n")

        # 2. JavaScript runtime error
        print("2. Runtime Error:")
        try:
            runtime.eval("nonexistent.property")
        except JavaScriptError as e:
            print(f"   Caught: {type(e).__name__}: {e}\n")

        # 3. JavaScript exception
        print("3. Thrown Exception:")
        try:
            runtime.eval("throw new Error('Something went wrong')")
        except JavaScriptError as e:
            print(f"   Caught: {type(e).__name__}: {e}\n")

        # 4. Timeout error
        print("4. Timeout:")
        try:
            await runtime.eval_async(
                "new Promise(() => { /* never resolves */ })",
                timeout=0.5,
            )
        except (TimeoutError, RuntimeError) as e:
            if "timed out" in str(e).lower():
                print("   Caught: Timeout\n")
            else:
                raise

        # 5. Type errors
        print("5. Type Error:")
        try:
            runtime.eval("null.toString()")
        except JavaScriptError as e:
            print(f"   Caught: {type(e).__name__}: {e}\n")

    print()


def safe_user_code_execution():
    """Pattern for safely executing untrusted user code."""
    print("=== Safe User Code Execution ===\n")

    def execute_user_code(code: str, timeout: float = 3.0):
        """
        Safely execute user-provided JavaScript code.

        Returns:
            tuple: (success: bool, result: str, error_type: str | None)
        """
        config = RuntimeConfig(
            max_heap_size=20 * 1024 * 1024,  # 20MB limit
            timeout=timeout,
        )

        try:
            with Runtime(config) as runtime:
                # Run with timeout
                result = runtime.eval(code)
                return (True, str(result), None)

        except (TimeoutError, RuntimeError) as e:
            if "timed out" in str(e).lower():
                return (False, None, "TimeoutError")
            raise

        except JavaScriptError as e:
            return (False, str(e), "JavaScriptError")

        except Exception as e:
            return (False, str(e), "RuntimeError")

    # Test various user inputs
    test_cases = [
        ("2 + 2", "Valid arithmetic"),
        ("[1, 2, 3].map(x => x * 2)", "Valid array operation"),
        ("while(true) {}", "Infinite loop"),
        ("throw new Error('User error')", "User exception"),
        ("nonexistent()", "Undefined function"),
    ]

    for code, description in test_cases:
        success, result, error_type = execute_user_code(code, timeout=1.0)

        if success:
            print(f"✓ {description}: {result}")
        else:
            print(f"✗ {description}: {error_type}")

    print()


async def multi_tenant_isolation():
    """Demonstrate resource isolation in multi-tenant scenario."""
    print("=== Multi-Tenant Isolation ===\n")

    # Each tenant gets its own runtime with limits
    TENANT_CONFIG = RuntimeConfig(
        max_heap_size=30 * 1024 * 1024,  # 30MB per tenant
        timeout=5.0,
    )

    async def run_tenant_code(tenant_id: int, code: str):
        """Execute code for a specific tenant."""
        try:
            with Runtime(TENANT_CONFIG) as runtime:
                # Set tenant context
                runtime.eval(f"globalThis.TENANT_ID = {tenant_id}")

                # Execute tenant code
                result = await runtime.eval_async(code, timeout=2.0)

                print(f"  Tenant {tenant_id}: Success - {result}")

        except (TimeoutError, RuntimeError) as e:
            if "timed out" in str(e).lower():
                print(f"  Tenant {tenant_id}: Timeout")
            else:
                raise

        except JavaScriptError as e:
            print(f"  Tenant {tenant_id}: JavaScript error - {e}")

    # Simulate multiple tenants running code concurrently
    print("Running code for 3 tenants concurrently:\n")

    await asyncio.gather(
        run_tenant_code(1, "[1, 2, 3].reduce((a, b) => a + b)"),
        run_tenant_code(2, "Math.sqrt(144)"),
        run_tenant_code(3, "'hello'.toUpperCase()"),
    )

    print()


async def main():
    """
    Best practices for resource control:

    1. Always set max_heap_size for untrusted code
    2. Always use timeout for untrusted code (especially async)
    3. Catch and handle specific exception types
    4. Isolate tenants with separate runtimes
    5. Monitor runtime stats for resource usage

    Security checklist:
    ✓ Memory limits configured
    ✓ Execution timeouts set
    ✓ Error handling in place
    ✓ Per-tenant isolation
    ✓ No dangerous APIs exposed (file I/O, network, etc.)
    """
    memory_limits()
    await execution_timeouts()
    await error_handling()
    safe_user_code_execution()
    await multi_tenant_isolation()


if __name__ == "__main__":
    asyncio.run(main())
