"""Values with no faithful Python form must raise, not silently become an empty dict (issue #26)."""

import pytest

from pydeno import Runtime


@pytest.mark.parametrize(
    "expr",
    [
        "new Map([[1, 2]])",
        "({m: new Map([['a', 1]])})",
        "[new Map()]",
        "new WeakMap()",
        "new WeakSet()",
        "new Error('boom')",
        "({e: new TypeError('x')})",
    ],
)
def test_unconvertible_values_raise(expr: str) -> None:
    with Runtime() as rt, pytest.raises(Exception, match="Cannot serialize"):
        rt.eval(expr)


def test_the_suggested_conversions_work() -> None:
    with Runtime() as rt:
        assert rt.eval("Object.fromEntries(new Map([['a', 1]]))") == {"a": 1}
        assert rt.eval("[...new Map([[1, 2]])]") == [[1, 2]]
        assert rt.eval("({name: 'Error', message: new Error('boom').message})") == {
            "name": "Error",
            "message": "boom",
        }
