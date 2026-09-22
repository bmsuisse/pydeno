"""
Basic bindings - exposing Python functions and data to JavaScript.

Bindings are the bridge between Python and JavaScript. They let you:
- Call Python functions from JavaScript
- Pass Python data to JavaScript
- Keep your API surface clean and controlled
"""

from pydeno import Runtime


def function_bindings():
    """Bind Python functions to JavaScript."""
    print("=== Function Bindings ===\n")

    with Runtime() as runtime:
        # Simple function with one argument
        def greet(name):
            return f"Hello, {name}!"

        runtime.bind_function("greet", greet)
        result = runtime.eval("greet('Alice')")
        print(f"greet('Alice') = {result}")

        # Function with multiple arguments
        def add(a, b, c=0):
            return a + b + c

        runtime.bind_function("add", add)
        print(f"add(1, 2) = {runtime.eval('add(1, 2)')}")
        print(f"add(1, 2, 3) = {runtime.eval('add(1, 2, 3)')}")

        # Function that returns various types
        def get_user(user_id):
            return {
                "id": user_id,
                "name": "Alice",
                "tags": ["admin", "active"],
            }

        runtime.bind_function("getUser", get_user)
        result = runtime.eval("getUser(123)")
        print(f"getUser(123) = {result}")

    print()


def object_bindings():
    """Bind Python data to JavaScript."""
    print("=== Object Bindings ===\n")

    with Runtime() as runtime:
        # Bind configuration data
        runtime.bind_object("config", {
            "debug": True,
            "apiUrl": "https://api.example.com",
            "timeout": 5000,
            "retries": 3,
        })

        result = runtime.eval("config.debug")
        print(f"config.debug = {result}")

        result = runtime.eval("config.apiUrl + '/users'")
        print(f"config.apiUrl + '/users' = {result}")

        # Bind various data types
        runtime.bind_object("data", {
            "numbers": [1, 2, 3, 4, 5],
            "user": {"name": "Bob", "age": 30},
            "active": True,
        })

        result = runtime.eval("data.numbers.reduce((a, b) => a + b)")
        print(f"Sum of data.numbers = {result}")

        result = runtime.eval("data.user.name.toUpperCase()")
        print(f"data.user.name.toUpperCase() = {result}")

    print()


def practical_examples():
    """Practical use cases for bindings."""
    print("=== Practical Examples ===\n")

    with Runtime() as runtime:
        # Example 1: Logging from JavaScript
        print("1. Logging from JavaScript:")

        def log(level, message):
            print(f"  [{level.upper()}] {message}")

        runtime.bind_function("log", log)

        runtime.eval("""
            log('info', 'Application started');
            log('warn', 'Low memory warning');
            log('error', 'Connection failed');
        """)

        print()

        # Example 2: Data validation
        print("2. Data validation:")

        def validate_email(email, *args):
            # Accept extra args that JS array methods pass (index, array)
            return "@" in email and "." in email.split("@")[1]

        runtime.bind_function("validateEmail", validate_email)

        result = runtime.eval("""
            const emails = [
                'user@example.com',
                'invalid',
                'test@domain.org',
                'no-at-sign.com'
            ];
            emails.filter(validateEmail)
        """)
        print(f"  Valid emails: {result}")

        print()

        # Example 3: Feature flags
        print("3. Feature flags:")

        runtime.bind_object("features", {
            "darkMode": True,
            "betaFeatures": False,
            "maxFileSize": 10_000_000,
        })

        result = runtime.eval("""
            if (features.darkMode) {
                'Using dark theme';
            } else {
                'Using light theme';
            }
        """)
        print(f"  Theme: {result}")

        result = runtime.eval("features.maxFileSize / 1_000_000 + 'MB'")
        print(f"  Max file size: {result}")

    print()


def decorator_style():
    """Use decorator syntax for cleaner code."""
    print("=== Decorator Style ===\n")

    with Runtime() as runtime:
        # Use the @runtime.bind() decorator
        @runtime.bind()
        def calculate(x, y):
            return x * y + 10

        @runtime.bind()
        def process_items(items):
            return [item.upper() for item in items]

        # Function name becomes the JavaScript name
        result = runtime.eval("calculate(5, 3)")
        print(f"calculate(5, 3) = {result}")

        result = runtime.eval("process_items(['hello', 'world'])")
        print(f"process_items(['hello', 'world']) = {result}")

        # Custom JavaScript name
        @runtime.bind(name="getUserInfo")
        def get_user_details(user_id):
            return {"id": user_id, "name": "Charlie"}

        result = runtime.eval("getUserInfo(456)")
        print(f"getUserInfo(456) = {result}")

    print()


def snapshots_are_snapshots():
    """Remember: bound objects are snapshots, not live references."""
    print("=== Objects Are Snapshots ===\n")

    with Runtime() as runtime:
        counter = {"value": 0}
        runtime.bind_object("counter", counter)

        # JavaScript modifies its copy
        runtime.eval("counter.value = 100")

        # Python's original is unchanged
        print(f"Python counter.value = {counter['value']}")  # Still 0

        # For dynamic data, bind a function instead
        def get_counter():
            return counter

        runtime.bind_function("getCounter", get_counter)

        counter["value"] = 42  # Update Python value
        result = runtime.eval("getCounter().value")
        print(f"getCounter().value = {result}")  # Gets fresh value: 42

    print()


def main():
    """
    Bindings are the primary way to integrate Python and JavaScript:

    - bind_function(): Expose Python callables to JavaScript
    - bind_object(): Pass Python data to JavaScript
    - @runtime.bind(): Decorator syntax for cleaner code

    Best practices:
    - Keep functions simple and fast
    - Use meaningful JavaScript-style names (camelCase)
    - Only expose what JavaScript actually needs
    - For dynamic data, bind functions that return fresh values
    """
    function_bindings()
    object_bindings()
    practical_examples()
    decorator_style()
    snapshots_are_snapshots()


if __name__ == "__main__":
    main()
