"""
Type conversion between Python and JavaScript.

pydeno automatically converts types between Python and JavaScript, making it
easy to work with familiar types on both sides.

This example demonstrates:
- Standard type mappings (primitives, collections)
- Special types (undefined, BigInt, Date, binary data)
- Round-trip conversions
- Edge cases and limitations
"""

from datetime import datetime, timezone
from pydeno import Runtime, undefined


def primitive_types():
    """Basic primitive type conversions."""
    print("=== Primitive Types ===\n")

    with Runtime() as runtime:
        # Numbers
        result = runtime.eval("42")
        print(f"JavaScript 42 → Python: {result} (type: {type(result).__name__})")

        result = runtime.eval("3.14")
        print(f"JavaScript 3.14 → Python: {result} (type: {type(result).__name__})")

        # Strings
        result = runtime.eval("'hello'")
        print(f"JavaScript 'hello' → Python: {result} (type: {type(result).__name__})")

        # Booleans
        result = runtime.eval("true")
        print(f"JavaScript true → Python: {result} (type: {type(result).__name__})")

        result = runtime.eval("false")
        print(f"JavaScript false → Python: {result} (type: {type(result).__name__})")

        # null and undefined
        result = runtime.eval("null")
        print(f"JavaScript null → Python: {result} (type: {type(result).__name__})")

        result = runtime.eval("undefined")
        print(f"JavaScript undefined → Python: {result} (type: {type(result).__name__})")

    print()


def collection_types():
    """Array and object conversions."""
    print("=== Collection Types ===\n")

    with Runtime() as runtime:
        # Arrays → Lists
        result = runtime.eval("[1, 2, 3, 4, 5]")
        print(f"JavaScript [1,2,3,4,5] → Python: {result}")
        print(f"  Type: {type(result).__name__}\n")

        # Objects → Dicts
        result = runtime.eval("({ name: 'Alice', age: 30, active: true })")
        print(f"JavaScript object → Python: {result}")
        print(f"  Type: {type(result).__name__}\n")

        # Nested structures
        result = runtime.eval("""
            ({
                users: [
                    { id: 1, name: 'Alice' },
                    { id: 2, name: 'Bob' }
                ],
                count: 2
            })
        """)
        print(f"Nested structure: {result}")
        print(f"  users[0]: {result['users'][0]}\n")

        # Sets
        result = runtime.eval("new Set([1, 2, 3, 2, 1])")
        print(f"JavaScript Set → Python: {result}")
        print(f"  Type: {type(result).__name__}\n")

    print()


def special_types():
    """Special type conversions (BigInt, Date, undefined, binary)."""
    print("=== Special Types ===\n")

    with Runtime() as runtime:
        # BigInt
        result = runtime.eval("BigInt('9007199254740991')")
        print(f"JavaScript BigInt → Python: {result}")
        print(f"  Type: {type(result).__name__}\n")

        # Date → datetime
        result = runtime.eval("new Date('2024-01-15T10:30:00.000Z')")
        print(f"JavaScript Date → Python: {result}")
        print(f"  Type: {type(result).__name__}\n")

        # undefined sentinel
        result = runtime.eval("undefined")
        print(f"JavaScript undefined → Python: {result}")
        print(f"  Is pydeno.undefined: {result is undefined}\n")

        # Binary data (Uint8Array)
        result = runtime.eval("new Uint8Array([72, 101, 108, 108, 111])")
        print(f"JavaScript Uint8Array → Python: {result}")
        print(f"  Type: {type(result).__name__}")
        print(f"  Decoded: {result.decode('utf-8')}\n")

    print()


def python_to_js():
    """Sending Python data to JavaScript."""
    print("=== Python → JavaScript ===\n")

    with Runtime() as runtime:
        # Bind Python data
        runtime.bind_object("data", {
            "number": 42,
            "pi": 3.14159,
            "text": "Hello from Python",
            "flag": True,
            "nothing": None,
            "items": [1, 2, 3],
            "user": {"name": "Alice", "id": 123},
            "tags": {"python", "javascript", "pydeno"},
            "timestamp": datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc),
            "binary": b"Hello",
        })

        # Access in JavaScript
        print("Accessing Python data from JavaScript:\n")

        result = runtime.eval("data.number + 10")
        print(f"  data.number + 10 = {result}")

        result = runtime.eval("data.text.toUpperCase()")
        print(f"  data.text.toUpperCase() = {result}")

        result = runtime.eval("data.items.map(x => x * 2)")
        print(f"  data.items.map(x => x * 2) = {result}")

        result = runtime.eval("data.user.name")
        print(f"  data.user.name = {result}")

        result = runtime.eval("Array.from(data.tags).sort()")
        print(f"  Array.from(data.tags).sort() = {result}")

        result = runtime.eval("data.timestamp.toISOString()")
        print(f"  data.timestamp.toISOString() = {result}")

        result = runtime.eval("data.binary[0]")
        print(f"  data.binary[0] = {result} (byte value for 'H')")

    print()


def round_trip_conversions():
    """Data that survives round-trip conversions."""
    print("=== Round-Trip Conversions ===\n")

    with Runtime() as runtime:
        test_cases = [
            (42, "number"),
            (3.14, "float"),
            ("hello", "string"),
            (True, "boolean"),
            (None, "None/null"),
            ([1, 2, 3], "list/array"),
            ({"key": "value"}, "dict/object"),
            ({1, 2, 3}, "set/Set"),
            (b"bytes", "bytes/Uint8Array"),
        ]

        for value, description in test_cases:
            runtime.bind_object("test", {"value": value})
            result = runtime.eval("test.value")
            matches = result == value or (isinstance(value, set) and result == value)
            status = "✓" if matches else "✗"
            print(f"{status} {description}: {value} → {result}")

    print()


def special_values():
    """Special numeric values (NaN, Infinity)."""
    print("=== Special Numeric Values ===\n")

    with Runtime() as runtime:
        # NaN
        result = runtime.eval("NaN")
        print(f"JavaScript NaN → Python: {result}")
        print(f"  Is NaN: {result != result}  (NaN != NaN is True)\n")

        # Infinity
        result = runtime.eval("Infinity")
        print(f"JavaScript Infinity → Python: {result}")
        print(f"  Is infinite: {result == float('inf')}\n")

        # -Infinity
        result = runtime.eval("-Infinity")
        print(f"JavaScript -Infinity → Python: {result}")
        print(f"  Is -infinite: {result == float('-inf')}\n")

    print()


def edge_cases():
    """Edge cases and limitations."""
    print("=== Edge Cases & Limitations ===\n")

    with Runtime() as runtime:
        # Empty collections
        print("Empty collections:")
        print(f"  [] → {runtime.eval('[]')}")
        print(f"  {{}} → {runtime.eval('({})')}")
        print(f"  new Set() → {runtime.eval('new Set()')}\n")

        # Nested depth (limit: 64 levels)
        print("Nested structures (within limits):")
        result = runtime.eval("{ a: { b: { c: { d: 'deep' } } } }")
        print(f"  4 levels deep: {result}\n")

        # Large numbers
        print("Large numbers:")
        print(f"  Number.MAX_SAFE_INTEGER: {runtime.eval('Number.MAX_SAFE_INTEGER')}")
        print(f"  BigInt for larger: {runtime.eval('BigInt(Number.MAX_SAFE_INTEGER) + 1n')}\n")

        # Unicode strings
        print("Unicode strings:")
        result = runtime.eval("'Hello 世界 🌍'")
        print(f"  {result}\n")

    print()


def main():
    """
    Type Conversion Summary:

    Standard mappings:
    - None ↔ null
    - bool ↔ boolean
    - int/float ↔ number
    - str ↔ string
    - list ↔ Array
    - dict ↔ Object
    - set ↔ Set

    Special types:
    - pydeno.undefined ↔ undefined (distinct from None/null)
    - int (large) ↔ BigInt
    - datetime ↔ Date
    - bytes/bytearray/memoryview ↔ Uint8Array

    Special values:
    - float('nan') ↔ NaN
    - float('inf') ↔ Infinity
    - float('-inf') ↔ -Infinity

    Limits:
    - Max nesting depth: 64 levels
    - Max value size: 10MB
    - Numbers: -2^53 to 2^53 (use BigInt for larger)
    """
    primitive_types()
    collection_types()
    special_types()
    python_to_js()
    round_trip_conversions()
    special_values()
    edge_cases()


if __name__ == "__main__":
    main()
