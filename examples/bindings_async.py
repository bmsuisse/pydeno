"""Async function bindings - bridging Python async functions to JavaScript Promises."""

import asyncio
from pydeno import Runtime


async def python_fetch(url):
    """Simulate an async HTTP fetch using Python."""
    await asyncio.sleep(0.1)  # simulate network delay
    return {"url": url, "status": 200, "body": f"Content from {url}"}


async def python_db_query(query):
    """Simulate an async database query."""
    await asyncio.sleep(0.05)
    return [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]


async def main() -> None:
    """
    Demonstrates binding async Python functions to JavaScript.

    Key points:
    - Python async functions automatically become JavaScript Promises
    - JavaScript can await them naturally
    - Arguments and return values are automatically converted
    - Both sync and async functions can coexist
    """
    print("=== Async Function Bindings ===\n")

    with Runtime() as rt:
        # Bind async Python functions
        rt.bind_function("fetch", python_fetch)
        rt.bind_function("dbQuery", python_db_query)

        # JavaScript sees them as async functions returning Promises
        result = await rt.eval_async(
            """
            (async () => {
                // Fetch data from Python
                const response = await fetch("https://api.example.com/data");
                console.log("Fetch response:", response);

                // Query database via Python
                const users = await dbQuery("SELECT * FROM users");
                console.log("DB result:", users);

                return {
                    fetchedBody: response.body,
                    userCount: users.length,
                    firstUser: users[0].name
                };
            })()
            """
        )

        print(f"Fetched body: {result['fetchedBody']}")
        print(f"User count: {result['userCount']}")
        print(f"First user: {result['firstUser']}")

    print("\n=== Using Decorator Style ===\n")

    with Runtime() as rt:
        # Cleaner syntax with decorator
        @rt.bind()
        async def process_data(items):
            """Process items asynchronously in Python."""
            await asyncio.sleep(0.1)
            return [item.upper() for item in items]

        @rt.bind(name="calculateScore")
        def calc(x, y):
            """Sync function with custom JS name."""
            return x * y + 10

        result = await rt.eval_async(
            """
            (async () => {
                const processed = await process_data(['hello', 'world']);
                const score = calculateScore(5, 3);
                return { processed, score };
            })()
            """
        )

        print(f"Processed: {result['processed']}")
        print(f"Score: {result['score']}")

    print("\n=== Combining with Object Bindings ===\n")

    with Runtime() as rt:
        # Bind configuration data
        rt.bind_object("config", {
            "apiKey": "secret-key-123",
            "timeout": 5000,
            "retries": 3,
        })

        # Bind async function that uses the config
        @rt.bind()
        async def api_call(endpoint):
            await asyncio.sleep(0.05)
            # In real code, would use config.apiKey here
            return {"endpoint": endpoint, "authenticated": True}

        result = await rt.eval_async(
            """
            (async () => {
                if (config.apiKey) {
                    return await api_call("/users");
                }
                throw new Error("No API key");
            })()
            """
        )

        print(f"API call result: {result}")


if __name__ == "__main__":
    asyncio.run(main())
