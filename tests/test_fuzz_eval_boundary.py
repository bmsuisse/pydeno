"""Property-based / fuzz coverage at the eval boundary, via `hypothesis`.

The rest of the suite is example-driven: it checks behaviours someone thought
of. These tests instead generate inputs nobody thought of and assert the one
invariant that must hold for *every* input a sandbox is handed:

    peno either returns a result or raises a catchable Python
    exception -- never SIGABRT, never a Rust panic, never a hang.

That invariant is the whole product promise of a sandbox, and it is exactly
what the two tokio-context SIGABRT bugs violated (see
`docs/contributing/upstream-divergence.md`, sections 2 and 3): a crash there
takes down the *host* process, so no amount of Python-side error handling can
recover from it.

Every test here runs in-process, so a regression that aborts the process shows
up as the whole test session dying rather than as a failure -- which is the
correct, loud signal.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import HealthCheck, Verbosity, given, settings
from hypothesis import strategies as st

from peno import JavaScriptError, Runtime, RuntimeConfig, undefined

# V8 isolate creation costs ~2.5ms, so a per-example Runtime would dominate.
# These profiles keep the suite honest but fast; raise max_examples locally
# (`--hypothesis-seed=... -p no:randomly`) when hunting a specific class.
FUZZ = settings(
    max_examples=150,
    deadline=None,  # V8 compile time is spiky; a deadline here only flakes.
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    verbosity=Verbosity.quiet,
)

# A short timeout so a pathological program is *proven* to be interruptible
# rather than merely slow.
TIMEOUT_CONFIG = RuntimeConfig(timeout=2.0)


# --------------------------------------------------------------------------
# Value round-tripping
# --------------------------------------------------------------------------

# JSON-safe Python values, recursively nested. Depth is bounded well inside
# the default max_serialization_depth so a legitimate value is not rejected
# for depth reasons -- that limit has its own dedicated tests.
json_safe = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(2**53) + 1, max_value=2**53 - 1),
        st.floats(allow_nan=False, allow_infinity=False, width=64),
        st.text(max_size=64),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=6),
        st.dictionaries(st.text(min_size=1, max_size=16), children, max_size=6),
    ),
    max_leaves=25,
)


def _normalize(value: object) -> object:
    """Fold the documented, lossy parts of the mapping before comparing.

    Two documented behaviours are not identities and are asserted elsewhere
    rather than fought here:
      - JS has one nullish value on the op-argument path, so Python `None`
        comes back as the `JsUndefined` sentinel;
      - JS numbers are all doubles, so an integral float round-trips as an
        `int`.
    """
    if value is None or value is undefined:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    return value


class TestValueRoundTrip:
    @given(value=json_safe)
    @FUZZ
    def test_arbitrary_json_safe_value_survives_a_host_call(
        self, value: object
    ) -> None:
        """Python -> JS -> Python through bound functions must be lossless up
        to the documented mappings, for any generated value.

        Goes out through a tool's return value and back in as another tool's
        argument -- i.e. the `revive`/`prepare` paths in the bridge JS, which
        is how a real tool call moves data.
        """
        received: list[object] = []

        with Runtime() as rt:
            rt.bind_function("produce", lambda: value)
            rt.bind_function("echo", lambda v: received.append(v) or True)
            rt.eval("echo(produce())")

        assert _normalize(received[0]) == _normalize(value)

    @given(value=json_safe)
    @FUZZ
    def test_a_tool_returning_an_arbitrary_value_does_not_crash(
        self, value: object
    ) -> None:
        with Runtime() as rt:
            rt.bind_function("produce", lambda: value)
            result = rt.eval("produce()")
        assert _normalize(result) == _normalize(value)

    @given(
        text=st.text(
            # "Cs" (lone surrogates) is excluded deliberately: those are not
            # encodable text, and peno refusing them with a clean
            # RuntimeError is the correct behaviour, asserted separately in
            # TestKnownConversionResiduals.
            alphabet=st.characters(
                min_codepoint=1, max_codepoint=0x10FFFF, exclude_categories=["Cs"]
            ),
            max_size=200,
        )
    )
    @FUZZ
    def test_arbitrary_unicode_strings_round_trip(self, text: str) -> None:
        """Astral-plane characters and control bytes -- including U+0001,
        which the typed-error path uses as its marker, so a string containing
        it must not be mistaken for an error envelope."""
        received: list[object] = []
        with Runtime() as rt:
            rt.bind_function("produce", lambda: text)
            rt.bind_function("echo", lambda v: received.append(v) or True)
            rt.eval("echo(produce())")
        assert received[0] == text

    @given(value=st.floats(allow_nan=True, allow_infinity=True))
    @FUZZ
    def test_special_floats_do_not_crash(self, value: float) -> None:
        """NaN/Infinity have no JSON representation; whatever peno does
        with them, it must not be crash or hang."""
        with Runtime() as rt:
            rt.bind_function("produce", lambda: value)
            try:
                result = rt.eval("produce()")
            except (JavaScriptError, RuntimeError, ValueError, OverflowError):
                return
        if isinstance(result, float) and math.isnan(result):
            assert math.isnan(value)


# --------------------------------------------------------------------------
# Adversarial source text
# --------------------------------------------------------------------------


def _eval_must_not_crash(code: str) -> None:
    """The core invariant: a result, or a catchable Python exception."""
    with Runtime(TIMEOUT_CONFIG) as rt:
        try:
            rt.eval(code)
        except (JavaScriptError, RuntimeError, ValueError, MemoryError, OverflowError):
            pass  # A clean, catchable refusal is a correct outcome.


class TestAdversarialSource:
    @given(code=st.text(max_size=400))
    @FUZZ
    def test_arbitrary_text_as_a_program(self, code: str) -> None:
        """Almost all of these are SyntaxErrors. That is the point: the
        failure must arrive as a Python exception, not a signal."""
        _eval_must_not_crash(code)

    @given(
        code=st.text(
            alphabet=st.sampled_from(
                list("(){}[];,.+-*/%<>=!&|?:'\"`\\\n\t $_0123456789abcfilnoqrstuvwxy")
            ),
            max_size=400,
        )
    )
    @FUZZ
    def test_js_flavoured_noise_as_a_program(self, code: str) -> None:
        """Weighted toward characters that actually appear in JS, so more
        examples get past the parser and into the compiler/runtime."""
        _eval_must_not_crash(code)

    @given(depth=st.integers(min_value=1, max_value=2000))
    @FUZZ
    def test_deeply_nested_literals(self, depth: int) -> None:
        """Nested literals blow V8's *parser* stack, which historically is a
        rich source of hard crashes in embedded engines."""
        _eval_must_not_crash("[" * depth + "]" * depth)
        _eval_must_not_crash("{a:" * depth + "1" + "}" * depth)

    @given(depth=st.integers(min_value=1, max_value=500))
    @FUZZ
    def test_deeply_nested_parentheses_and_calls(self, depth: int) -> None:
        _eval_must_not_crash("(" * depth + "1" + ")" * depth)
        _eval_must_not_crash("f(" * depth + ")" * depth)

    @given(count=st.integers(min_value=1, max_value=20000))
    @FUZZ
    def test_very_long_expressions(self, count: int) -> None:
        """Large source is exactly what tripped the tokio-context SIGABRT:
        past ~148KB V8 switches to streaming compilation and schedules a
        delayed foreground task."""
        _eval_must_not_crash("+".join(["1"] * count))

    @given(size=st.integers(min_value=1, max_value=400_000))
    @settings(
        max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow]
    )
    def test_source_around_the_streaming_compile_threshold(self, size: int) -> None:
        """Directly targets the SIGABRT regression class (upstream-divergence 2)
        by generating sizes on both sides of V8's streaming-compile threshold."""
        _eval_must_not_crash(f"/* {'x' * size} */ 1")

    @given(
        pattern=st.text(
            alphabet=st.sampled_from(list("ab()*+?|[]^$.\\{}")), max_size=40
        ),
        subject=st.text(alphabet=st.sampled_from(list("ab")), max_size=40),
    )
    @FUZZ
    def test_pathological_regexes_are_interruptible(
        self, pattern: str, subject: str
    ) -> None:
        """Catastrophic backtracking must hit the execution timeout and
        surface as an exception, not wedge the process.

        This is the test that would fail if the cross-thread termination
        handle (upstream-divergence section 1) regressed.
        """
        escaped_pattern = pattern.replace("\\", "\\\\").replace("'", "\\'")
        escaped_subject = subject.replace("\\", "\\\\").replace("'", "\\'")
        _eval_must_not_crash(
            f"try {{ new RegExp('{escaped_pattern}').test('{escaped_subject}') }} "
            f"catch (e) {{ 0 }}"
        )

    @given(count=st.integers(min_value=1, max_value=5000))
    @FUZZ
    def test_large_string_construction(self, count: int) -> None:
        _eval_must_not_crash(f"'x'.repeat({count * 1000}).length")

    @given(
        n=st.integers(min_value=1, max_value=10_000_000),
    )
    @FUZZ
    def test_unbounded_loops_hit_the_timeout(self, n: int) -> None:
        """A loop the guest chose must never outlive the configured timeout."""
        _eval_must_not_crash(f"let s=0; for (let i=0;i<{n};i++) s+=i; s")

    @given(code=st.text(max_size=200))
    @FUZZ
    def test_arbitrary_text_as_a_json_payload(self, code: str) -> None:
        """Guest-supplied text through JSON.parse, a very common shape for
        untrusted data reaching a sandbox."""
        encoded = code.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        _eval_must_not_crash(f"try {{ JSON.parse('{encoded}') }} catch (e) {{ 0 }}")


def _escape_js_string(text: str) -> str:
    """Escape Python text so it is a valid single-quoted JS string literal."""
    return (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


# JS *value* source text, recursively nested: objects, arrays, Date, Set,
# BigInt, typed arrays, the special floats, and -- crucially -- a large-string
# leaf, so `max_serialization_bytes` is reachable from the fuzzer at all.
#
# The previous version of this strategy generated arbitrary *text* and
# interpolated it into a single string literal (`sink('{escaped}')`), so every
# example passed exactly one JS string: no object, array, Date, Set, BigInt or
# typed array ever reached the tool. It fuzzed source-text escaping, one layer
# down from the marshaling boundary its name claimed.
js_value_source = st.recursive(
    st.one_of(
        st.just("null"),
        st.just("undefined"),
        st.booleans().map(lambda b: "true" if b else "false"),
        st.sampled_from(["NaN", "Infinity", "-Infinity", "-0", "0"]),
        st.integers(min_value=-(2**53) + 1, max_value=2**53 - 1).map(repr),
        st.floats(allow_nan=False, allow_infinity=False, width=64).map(repr),
        st.integers(min_value=-(2**80), max_value=2**80).map(lambda i: f"{i}n"),
        st.text(max_size=48).map(lambda t: "'" + _escape_js_string(t) + "'"),
        st.integers(min_value=0, max_value=200_000).map(lambda n: f"'x'.repeat({n})"),
        st.lists(st.integers(min_value=0, max_value=255), max_size=16).map(
            lambda xs: f"new Uint8Array({list(xs)!r})"
        ),
        st.integers(min_value=0, max_value=2_000_000_000_000).map(
            lambda ms: f"new Date({ms})"
        ),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=6).map(lambda xs: "[" + ",".join(xs) + "]"),
        st.lists(children, max_size=6).map(
            lambda xs: "new Set([" + ",".join(xs) + "])"
        ),
        st.lists(children, max_size=6).map(
            lambda xs: "({" + ",".join(f"k{i}: {x}" for i, x in enumerate(xs)) + "})"
        ),
    ),
    max_leaves=12,
)


class TestAdversarialHostCallArguments:
    """Fuzz the *argument* direction: JS handing junk to a Python tool."""

    @given(source=js_value_source)
    @FUZZ
    def test_a_tool_called_with_arbitrary_generated_js_values(
        self, source: str
    ) -> None:
        """Genuinely varied JS *values* -- not one string literal -- reaching a
        host tool. The invariant is the sandbox promise: a value or a catchable
        exception, never a crash; and if it was accepted, it actually arrived."""
        with Runtime(TIMEOUT_CONFIG) as rt:
            received: list[object] = []
            rt.bind_function("sink", lambda v: received.append(v) or "ok")
            try:
                out = rt.eval(f"sink({source})")
            except (JavaScriptError, RuntimeError, ValueError):
                return
            # An accepted call must have reached Python with a value, rather
            # than being silently dropped or replaced by a placeholder.
            assert out == "ok"
            assert len(received) == 1

    @given(count=st.integers(min_value=0, max_value=400))
    @FUZZ
    def test_a_tool_called_with_many_arguments(self, count: int) -> None:
        with Runtime(TIMEOUT_CONFIG) as rt:
            rt.bind_function("sink", lambda *a: len(a))
            try:
                got = rt.eval(f"sink(...Array.from({{length: {count}}}, (_, i) => i))")
            except (JavaScriptError, RuntimeError, ValueError):
                return
            assert got == count

    # `max_serialization_depth` for the depth tests below. Small enough that
    # hypothesis reaches both sides of it on nearly every example, and well
    # under `serde_v8`'s own internal recursion limit so the knob under test
    # is the one that does the refusing.
    DEPTH_LIMIT = 12

    @given(depth=st.integers(min_value=1, max_value=400))
    @FUZZ
    def test_a_tool_called_with_a_deeply_nested_object(self, depth: int) -> None:
        """Past `max_serialization_depth` this must be refused, and inside it
        accepted -- exactly, not "either outcome is fine".

        The previous version caught `(JavaScriptError, RuntimeError,
        ValueError)` and asserted **nothing**, so it passed identically with
        the depth limit removed. That is how the v0.2.0 review's finding M3b --
        the configured depth limit being ignored inbound entirely -- survived a
        test written to catch it.

        Accounting: a value nested `depth` levels consumes `depth + 1` tracker
        levels, because the scalar leaf counts as one.
        """
        config = RuntimeConfig(max_serialization_depth=self.DEPTH_LIMIT, timeout=2.0)
        with Runtime(config) as rt:
            rt.bind_function("sink", lambda v: True)
            script = (
                f"let v = 1; for (let i = 0; i < {depth}; i++) v = {{n: v}}; sink(v)"
            )
            try:
                rt.eval(script)
                accepted = True
            except (JavaScriptError, RuntimeError, ValueError):
                accepted = False

        assert accepted == (depth + 1 <= self.DEPTH_LIMIT), (
            f"op argument nested {depth} deep was "
            f"{'accepted' if accepted else 'refused'} against "
            f"max_serialization_depth={self.DEPTH_LIMIT}"
        )

    @given(depth=st.integers(min_value=1, max_value=400))
    @FUZZ
    def test_the_depth_limit_is_the_same_in_both_directions(self, depth: int) -> None:
        """The same payload, the same configured limit: an `eval` result and an
        op argument must agree.

        This is the asymmetry assertion. Before the fix the identical value was
        refused as a result and accepted as an argument -- so the limit bound
        the trusted party and not the untrusted one.
        """
        config = RuntimeConfig(max_serialization_depth=self.DEPTH_LIMIT, timeout=2.0)
        build = f"let v = 1; for (let i = 0; i < {depth}; i++) v = {{n: v}}; "

        def accepts(script: str) -> bool:
            with Runtime(config) as rt:
                rt.bind_function("sink", lambda v: True)
                try:
                    rt.eval(script)
                    return True
                except (JavaScriptError, RuntimeError, ValueError):
                    return False

        inbound = accepts(build + "sink(v)")
        outbound = accepts(build + "v")
        assert inbound == outbound, (
            f"depth {depth} against max_serialization_depth={self.DEPTH_LIMIT}: "
            f"inbound accepted={inbound}, outbound accepted={outbound}"
        )

    # `max_serialization_bytes` for the size tests. The margin keeps the
    # assertion robust against the small per-value bookkeeping overhead the
    # tracker adds on top of raw payload length.
    BYTE_LIMIT = 1 << 20
    BYTE_MARGIN = 4096

    @given(size=st.integers(min_value=0, max_value=4 << 20))
    @FUZZ
    def test_a_tool_called_with_a_large_argument(self, size: int) -> None:
        """`max_serialization_bytes` must bind on the inbound direction.

        No strategy in this file used to generate a *large* argument at all, so
        the byte budget was outside the fuzzer's reach and finding M3a -- 37.7 MB
        accepted in one call against a 10 MB cap -- was unreachable from here.
        """
        if abs(size - self.BYTE_LIMIT) < self.BYTE_MARGIN:
            return  # too close to the boundary to assert either way
        config = RuntimeConfig(max_serialization_bytes=self.BYTE_LIMIT, timeout=5.0)
        with Runtime(config) as rt:
            rt.bind_function("sink", lambda v: len(v))
            try:
                got = rt.eval(f"sink('x'.repeat({size}))")
                accepted = True
            except (JavaScriptError, RuntimeError, ValueError):
                accepted = False
                got = None

        assert accepted == (size < self.BYTE_LIMIT), (
            f"a {size}-byte argument was {'accepted' if accepted else 'refused'} "
            f"against max_serialization_bytes={self.BYTE_LIMIT}"
        )
        if accepted:
            assert got == size

    @given(count=st.integers(min_value=2, max_value=6))
    @FUZZ
    def test_the_inbound_byte_budget_is_aggregate_not_per_argument(
        self, count: int
    ) -> None:
        """N arguments each under the limit must not together exceed it.

        The mirror of `TestSerializationBudgetIsAggregate` in
        test_gil_and_limits.py, which covered only the Python->JS half -- which
        is how the JS->Python half stayed broken through v0.2.0.
        """
        each = (self.BYTE_LIMIT * 3) // 4  # comfortably under on its own
        config = RuntimeConfig(max_serialization_bytes=self.BYTE_LIMIT, timeout=5.0)
        with Runtime(config) as rt:
            rt.bind_function("sink", lambda *a: sum(len(x) for x in a))
            # One argument of this size is fine ...
            assert rt.eval(f"sink('x'.repeat({each}))") == each
            # ... but `count` of them are not, because the budget is the call's.
            with pytest.raises((JavaScriptError, RuntimeError, ValueError)):
                rt.eval(f"sink(...Array({count}).fill('x'.repeat({each})))")

    @given(
        exc_name=st.sampled_from(
            ["ValueError", "KeyError", "RuntimeError", "TypeError", "OSError"]
        ),
        message=st.text(max_size=80),
    )
    @FUZZ
    def test_any_tool_exception_yields_a_catchable_js_error(
        self, exc_name: str, message: str
    ) -> None:
        """The typed-error path must hold for arbitrary messages -- including
        ones containing the ': ' separator or the U+0001 marker itself."""
        # A module-level `__builtins__` is a dict, not a module, so the
        # `getattr(__builtins__, ...)` this used to try was always dead and
        # the mapping below was always what ran. A silently-dead branch in a
        # test is indistinguishable from a working one, so it is gone.
        exc_type = {
            "ValueError": ValueError,
            "KeyError": KeyError,
            "RuntimeError": RuntimeError,
            "TypeError": TypeError,
            "OSError": OSError,
        }[exc_name]

        def failing() -> None:
            raise exc_type(message)

        with Runtime(TIMEOUT_CONFIG) as rt:
            rt.bind_function("failing", failing)
            out = rt.eval(
                "try { failing(); 'no-throw' } "
                "catch (e) { (e && e.name) ? 'caught:' + e.name : 'bad:' + String(e) }"
            )

        assert out.startswith("caught:"), f"guest JS could not catch it: {out!r}"
        assert out == f"caught:{exc_name}"


class TestKnownConversionResiduals:
    """Behaviours the fuzz suite surfaced that are documented, not bugs.

    Pinned here so a change to any of them is a deliberate, visible decision
    rather than a silent one.
    """

    def test_a_dunder_proto_key_round_trips_through_the_op_paths(self) -> None:
        """Regression test for the prototype bug the fuzzer found.

        A Python dict key "__proto__" used to hit JS's inherited __proto__
        *setter*, so the key vanished and the object guest JS received had a
        replaced prototype -- host data silently became inherited behaviour.
        The bridge now installs properties with Object.defineProperty.
        """
        with Runtime() as rt:
            rt.bind_function(
                "produce", lambda: {"__proto__": {"isAdmin": True}, "ok": 1}
            )
            assert rt.eval("JSON.stringify(Object.keys(produce()))") == (
                '["__proto__","ok"]'
            )
            # The decisive part: it is data, not a prototype swap.
            assert rt.eval("produce().isAdmin") is undefined
            assert (
                rt.eval("Object.getPrototypeOf(produce()) === Object.prototype") is True
            )
            assert (
                rt.eval("Object.prototype.hasOwnProperty.call(produce(), '__proto__')")
                is True
            )

    def test_dunder_proto_survives_from_js_back_to_python(self) -> None:
        with Runtime() as rt:
            received: list[object] = []
            rt.bind_function("echo", lambda v: received.append(v) or True)
            rt.eval(
                "const o = {}; Object.defineProperty(o, '__proto__', "
                "{value: 7, enumerable: true, writable: true, configurable: true}); "
                "echo(o)"
            )
        assert received[0] == {"__proto__": 7}

    def test_bind_object_nested_dunder_proto_is_a_known_residual(self) -> None:
        """`bind_object`'s *nested* values are built by serde_v8 on the Rust
        side, before any bridge JS runs, so they still lose a "__proto__" key.

        Scope of the residual, deliberately accepted rather than fixed:
          - `bind_object` takes host-authored configuration supplied at bind
            time, not untrusted data arriving at runtime -- the untrusted flow
            (a tool returning parsed JSON) goes through the op paths, which
            are fixed above.
          - Only the one object's prototype is affected; `Object.prototype`
            itself is never polluted (asserted below).
          - Closing it would mean hand-rolling JSValue -> v8 conversion to use
            `create_data_property` instead of serde_v8's `Object::set`,
            duplicating the whole conversion table for this one key name.
        ponytail: revisit only if untrusted data ever reaches bind_object.
        """
        with Runtime() as rt:
            rt.bind_object("data", {"value": {"__proto__": {"isAdmin": True}, "ok": 1}})
            # Known: the nested key is dropped.
            assert rt.eval("JSON.stringify(Object.keys(data.value))") == '["ok"]'
            # But it never escalates to global prototype pollution.
            assert rt.eval("({}).isAdmin") is undefined
            assert rt.eval("Object.prototype.isAdmin") is undefined

        # And top-level bind_object keys are handled by the bridge, so they
        # are already correct.
        with Runtime() as rt:
            rt.bind_object("cfg", {"__proto__": {"isAdmin": True}, "ok": 2})
            assert rt.eval("cfg.isAdmin") is undefined

    def test_a_js_function_argument_is_refused_not_silently_emptied(self) -> None:
        """Regression test for the v0.2.0 review's finding M4.

        A JS function handed to a host tool used to arrive as `{}` -- bare or
        nested -- with no error, because `serde_v8` sees a function as an
        object with no own enumerable properties and `JSValue`'s hand-written
        `Deserialize` has no function arm. A tool expecting `onProgress`, a
        comparator or a continuation got an empty options object and failed
        somewhere unrelated, or silently carried on.

        The fix refuses it, naming the argument path, rather than registering
        the function and handing the tool a live `JsFunction`: a held
        reference needs a documented lifetime (who owns it, when it is
        released, what it does after the supplying call returned), and that is
        a feature to design, not a bug fix. An error is recoverable; `{}` is
        not detectable.
        """
        received: list[object] = []
        with Runtime() as rt:
            rt.bind_function("take", lambda cb: received.append(cb) or "stored")

            for payload, path in [
                ("take(function(){return 7})", "args[0]"),
                ("take(() => 1)", "args[0]"),
                ("take({cb: () => 1})", "args[0].cb"),
                ("take([[() => 1]])", "args[0][0][0]"),
                ("take(new Set([() => 1]))", "args[0].<set item 0>"),
            ]:
                with pytest.raises(JavaScriptError) as caught:
                    rt.eval(payload)
                message = str(caught.value)
                assert "Cannot pass a JavaScript function" in message, message
                assert path in message, f"{path!r} not named in {message}"

        assert received == [], f"a function still reached the host: {received!r}"

    def test_a_js_symbol_argument_is_refused_rather_than_aborting(self) -> None:
        """`serde_v8` panics on a Symbol ("unknown ValueType for v8::Value"),
        and a panic inside the op aborts the *host process* -- which guest JS
        must never be able to cause. Refused in the bridge instead, where it
        is an ordinary catchable JS error.
        """
        with Runtime() as rt:
            rt.bind_function("take", lambda v: "stored")
            for payload in ("take(Symbol('s'))", "take({s: Symbol.iterator})"):
                with pytest.raises(JavaScriptError, match="Symbol"):
                    rt.eval(payload)
            # The runtime is still usable, i.e. nothing was torn down.
            assert rt.eval("take('ok')") == "stored"

    def test_a_lone_surrogate_is_refused_cleanly(self) -> None:
        """Not encodable text; a clean Python exception is the right answer.

        It arrives as JavaScriptError because the refusal happens inside the
        host-call op and therefore travels back as a JS exception -- which is
        the correct, catchable shape, not a crash.
        """
        with Runtime() as rt:
            rt.bind_function("produce", lambda: "\ud800")
            with pytest.raises(JavaScriptError, match="Unsupported Python type"):
                rt.eval("produce()")

    def test_a_string_containing_the_error_marker_is_not_an_error(self) -> None:
        """U+0001 is the typed-error marker. A tool *returning* a string that
        starts with it must stay a value, not become an exception."""
        with Runtime() as rt:
            rt.bind_function("produce", lambda: "\u0001ValueError: not really")
            assert rt.eval("produce()") == "\u0001ValueError: not really"
            assert rt.eval("typeof produce()") == "string"


def test_the_process_is_still_alive_and_correct() -> None:
    """Canary: if any fuzz case above aborted the process this never runs,
    and if one left the runtime in a bad state this catches it."""
    with Runtime() as rt:
        assert rt.eval("1 + 1") == 2
