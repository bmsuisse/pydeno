//! Internal JSValue type for accurate JavaScript value representation.
//!
//! This module provides a replacement for `serde_json::Value` that can accurately
//! represent JavaScript values including NaN, ±Infinity, and properly detect
//! circular references and enforce depth/size limits.

use crate::runtime::error::{RuntimeError, RuntimeResult};
use indexmap::IndexMap;
use num_bigint::BigInt;
use serde::ser::SerializeMap;
use serde::{Deserialize, Serialize};
use serde_bytes::Bytes;

/// Maximum depth for JavaScript value serialization
pub const MAX_JS_DEPTH: usize = 100;
/// Maximum size in bytes for JavaScript value serialization
pub const MAX_JS_BYTES: usize = 10 * 1024 * 1024; // 10MB

/// Stack size for every thread that may run V8 or the recursive
/// `JSValue` serializers.
///
/// Rust's default for a spawned thread is 2 MiB, which is **not** enough. The
/// V8<->`JSValue` converters (`value_to_js_value_internal`, `js_value_to_v8`,
/// `js_value_to_python`, `python_to_js_value`) recurse once per nesting level
/// up to [`MAX_JS_DEPTH`], and an unoptimized build does not merge or shrink
/// those frames.
///
/// This 16 MiB figure is the OS thread's *own* reservation, and it is not the
/// bound that actually matters once `max_serialization_depth` is raised past
/// its default: **V8 imposes its own, much smaller stack limit -- roughly
/// 984 KB -- on anything that enters the isolate**, independent of how much
/// native stack the hosting OS thread was given. A caller can only raise
/// `max_serialization_depth` far enough to run past that V8-side budget while
/// still comfortably inside this 16 MiB OS reservation, so a generous OS
/// stack does not, by itself, make deep recursion safe; see
/// [`record_stack_anchor`] and [`LimitTracker::enter`] for the headroom check
/// that actually enforces the real (V8) budget rather than this one.
///
/// 16 MiB leaves room for V8's own stack limit, the `deno_core`/tokio frames
/// above the converter, and future growth in the converters themselves. It is
/// only a *reservation*: pages are committed lazily, so an idle runtime
/// thread does not pay for it.
///
/// # What this does *not* cover
///
/// `python_to_js_value` recurses on the thread that **called** -- it needs
/// that thread's GIL and its Python objects -- so this reservation is
/// irrelevant to it. A `threading.Thread` gets 512 KB on macOS and `pydeno`
/// cannot set the stack of a thread it did not spawn. The headroom check in
/// [`LimitTracker::enter`] is a no-op on such a thread (no anchor was ever
/// recorded there), so this class of caller is bounded only by
/// `max_serialization_depth` and the OS thread's own stack size, same as
/// before.
///
/// Measured (debug, macOS arm64, 512 KB caller thread): a nested-dict
/// argument converts safely to depth **900** and dies with SIGBUS at
/// **1000**, i.e. ~512 bytes per frame -- 55x cheaper than the V8-side
/// serializer, because these frames carry `Bound<PyAny>` handles rather than
/// V8 scopes. Against the default [`MAX_JS_DEPTH`] of 100 that is ~9x of
/// headroom, which is why the default configuration is safe from any thread;
/// `tests/test_thread_stack_size.py` pins it.
///
/// It is *not* safe to raise `max_serialization_depth` past ~900 and then
/// convert from a small-stack thread. Closing that properly means moving the
/// Python->JS conversion onto the runtime thread, which changes where the GIL
/// is held across the boundary and is a bigger change than a constant.
pub const RUNTIME_THREAD_STACK_SIZE: usize = 16 * 1024 * 1024;

/// Headroom, in bytes, that [`LimitTracker::enter`] reserves below the
/// runtime thread's recorded stack anchor before it refuses to recurse
/// further.
///
/// This is not the OS thread's stack size ([`RUNTIME_THREAD_STACK_SIZE`]) --
/// it is a much tighter budget approximating V8's own stack limit (~984 KB),
/// which is what actually breaks first when `max_serialization_depth` is
/// raised well past its default and a script builds a deeply nested value.
/// Without this check, `LimitTracker` only counted *logical* nesting levels;
/// raising the configured depth limit high enough (`max_serialization_depth
/// = 10**6`, an explicit, supported configuration) let a deeply nested
/// object exhaust the isolate's real stack budget before the depth counter
/// ever objected, corrupting the process instead of raising a catchable
/// Python exception.
///
/// Tuned empirically on macOS arm64 against this checkout, with the anchor
/// recorded at runtime-thread startup (before `JsRuntime::new`, so "used"
/// includes the tokio/dispatcher frames already on the stack by the time a
/// call reaches the serializer, not just the serializer's own recursion).
/// Building a plain object chain (`{n: {n: {n: ...}}}`) with
/// `max_serialization_depth=10**6` and *no* headroom check: an
/// `unoptimized + debuginfo` build hits a real, uncatchable V8 fatal error
/// (`Check failed: IsOnCentralStack()`, aborting the process) at depth ~40 --
/// this is the pre-existing bug this check closes, reproducible on `main` @
/// a51a0a4 before this fix. A `release` build survives much deeper (crashes
/// around depth ~1100-1200, consistent with far smaller optimized frames)
/// but is not immune, only harder to reach.
///
/// 640 KiB trips this check at depth 22 in *both* profiles -- comfortably
/// before the ~40-deep debug crash, and very conservative relative to
/// release's much deeper real boundary. One constant is deliberately shared
/// across both profiles rather than tuned per-profile: it is simpler, and
/// the cost of tripping early in an optimized build is a clear, catchable
/// `RuntimeError` well short of any real danger, not a correctness problem.
/// See `tests/test_serialization_headroom.py`.
const STACK_HEADROOM_BYTES: usize = 640 * 1024;

thread_local! {
    /// The runtime thread's stack anchor: the address of a local variable
    /// captured near the top of that thread's closure, before `JsRuntime::new`
    /// runs. `None` on every other thread (e.g. the Python caller thread that
    /// runs `python_to_js_value` directly), which is what makes the headroom
    /// check in [`LimitTracker::enter`] a no-op there.
    static STACK_ANCHOR: std::cell::Cell<Option<usize>> = const { std::cell::Cell::new(None) };
}

/// Record the current thread's stack anchor for the headroom check in
/// [`LimitTracker::enter`].
///
/// Must be called once, near the top of the runtime thread's closure and
/// before `JsRuntime::new`, while the stack is still shallow. Calling it a
/// second time on the same thread, or from any other thread, is harmless but
/// pointless -- only the runtime thread's recursive serializer calls benefit
/// from the check this enables.
pub fn record_stack_anchor() {
    let local_probe: u8 = 0;
    let addr = &local_probe as *const u8 as usize;
    STACK_ANCHOR.with(|cell| cell.set(Some(addr)));
}

/// Bytes of stack consumed between this thread's recorded anchor and `here`.
///
/// Returns `None` when no anchor was recorded on this thread (the check is
/// then skipped by the caller). Stack grows down on every platform pydeno
/// supports, so a healthy `anchor - here` distance grows as recursion goes
/// deeper.
fn stack_used_since_anchor() -> Option<usize> {
    STACK_ANCHOR.with(|cell| {
        cell.get().map(|anchor| {
            let here_probe: u8 = 0;
            let here = &here_probe as *const u8 as usize;
            anchor.saturating_sub(here)
        })
    })
}

/// Configurable serialization limits applied during Python<->JS transfers.
#[derive(Clone, Copy, Debug)]
pub struct SerializationLimits {
    pub max_depth: usize,
    pub max_bytes: usize,
}

impl SerializationLimits {
    pub const fn new(max_depth: usize, max_bytes: usize) -> Self {
        Self {
            max_depth,
            max_bytes,
        }
    }
}

impl Default for SerializationLimits {
    fn default() -> Self {
        Self::new(MAX_JS_DEPTH, MAX_JS_BYTES)
    }
}

const TYPE_TAG: &str = "__pydeno_type";
const UNDEFINED_TYPE: &str = "Undefined";
const DATE_TYPE: &str = "Date";
const DATE_EPOCH_KEY: &str = "epoch_ms";
const SET_TYPE: &str = "Set";
const SET_VALUES_KEY: &str = "values";
const BIGINT_TYPE: &str = "BigInt";
const BIGINT_VALUE_KEY: &str = "value";
const JS_STREAM_TYPE: &str = "JsStream";
const PY_STREAM_TYPE: &str = "PyStream";
const STREAM_ID_KEY: &str = "id";

/// Internal representation of JavaScript values that can round-trip accurately.
///
/// Unlike `serde_json::Value`, this enum can represent special numeric values
/// (NaN, ±Infinity) and enforces proper depth/size limits during conversion.
///
/// Note: The Serialize/Deserialize implementations are manually implemented
/// because the Function variant cannot be serialized.
#[derive(Clone, Debug, PartialEq)]
pub enum JSValue {
    /// JavaScript undefined
    Undefined,
    /// JavaScript null
    Null,
    /// JavaScript boolean
    Bool(bool),
    /// JavaScript integer (within i64 range)
    Int(i64),
    /// JavaScript BigInt
    BigInt(BigInt),
    /// JavaScript float (including NaN and ±Infinity)
    Float(f64),
    /// JavaScript string
    String(String),
    /// JavaScript bytes (Uint8Array / ArrayBuffer)
    Bytes(Vec<u8>),
    /// JavaScript array (preserves order)
    Array(Vec<JSValue>),
    /// JavaScript object (uses IndexMap to preserve insertion order)
    Object(IndexMap<String, JSValue>),
    /// JavaScript Date (epoch milliseconds, UTC)
    Date(i64),
    /// JavaScript Set (preserves insertion order captured from JS)
    Set(Vec<JSValue>),
    /// JavaScript function (proxy via registry ID)
    Function { id: u32 },
    /// JavaScript ReadableStream (proxied via runtime stream registry)
    JsStream { id: u32 },
    /// Python async iterable placeholder forwarded into JavaScript
    PyStream { id: u32 },
}

// Manual Serialize implementation that errors on Function variant
impl Serialize for JSValue {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        use serde::ser::Error;
        match self {
            JSValue::Undefined => {
                let mut map = serializer.serialize_map(Some(1))?;
                map.serialize_entry(TYPE_TAG, UNDEFINED_TYPE)?;
                map.end()
            }
            JSValue::Null => serializer.serialize_none(),
            JSValue::Bool(b) => serializer.serialize_bool(*b),
            JSValue::Int(i) => serializer.serialize_i64(*i),
            JSValue::BigInt(bigint) => {
                let mut map = serializer.serialize_map(Some(2))?;
                map.serialize_entry(TYPE_TAG, BIGINT_TYPE)?;
                map.serialize_entry(BIGINT_VALUE_KEY, &bigint.to_str_radix(10))?;
                map.end()
            }
            JSValue::Float(f) => serializer.serialize_f64(*f),
            JSValue::String(s) => serializer.serialize_str(s),
            JSValue::Bytes(bytes) => Bytes::new(bytes).serialize(serializer),
            JSValue::Array(arr) => arr.serialize(serializer),
            JSValue::Object(obj) => obj.serialize(serializer),
            JSValue::Date(epoch_ms) => {
                let mut map = serializer.serialize_map(Some(2))?;
                map.serialize_entry(TYPE_TAG, DATE_TYPE)?;
                map.serialize_entry(DATE_EPOCH_KEY, epoch_ms)?;
                map.end()
            }
            JSValue::Set(values) => {
                let mut map = serializer.serialize_map(Some(2))?;
                map.serialize_entry(TYPE_TAG, SET_TYPE)?;
                map.serialize_entry(SET_VALUES_KEY, values)?;
                map.end()
            }
            JSValue::Function { id } => Err(Error::custom(format!(
                "Cannot serialize JSValue::Function (id: {}). Functions must be called, not serialized.",
                id
            ))),
            JSValue::JsStream { id } => {
                let mut map = serializer.serialize_map(Some(2))?;
                map.serialize_entry(TYPE_TAG, JS_STREAM_TYPE)?;
                map.serialize_entry(STREAM_ID_KEY, id)?;
                map.end()
            }
            JSValue::PyStream { id } => {
                let mut map = serializer.serialize_map(Some(2))?;
                map.serialize_entry(TYPE_TAG, PY_STREAM_TYPE)?;
                map.serialize_entry(STREAM_ID_KEY, id)?;
                map.end()
            }
        }
    }
}

// Manual Deserialize implementation that rejects Function variant
impl<'de> Deserialize<'de> for JSValue {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        use serde::de;

        struct JSValueVisitor;

        impl<'de> de::Visitor<'de> for JSValueVisitor {
            type Value = JSValue;

            fn expecting(&self, formatter: &mut std::fmt::Formatter) -> std::fmt::Result {
                formatter
                    .write_str("a JavaScript value (null, bool, number, string, array, or object)")
            }

            fn visit_bool<E>(self, value: bool) -> Result<Self::Value, E> {
                Ok(JSValue::Bool(value))
            }

            fn visit_i64<E>(self, value: i64) -> Result<Self::Value, E> {
                Ok(JSValue::Int(value))
            }

            fn visit_u64<E>(self, value: u64) -> Result<Self::Value, E> {
                if value <= i64::MAX as u64 {
                    Ok(JSValue::Int(value as i64))
                } else {
                    Ok(JSValue::Float(value as f64))
                }
            }

            fn visit_f64<E>(self, value: f64) -> Result<Self::Value, E> {
                Ok(JSValue::Float(value))
            }

            fn visit_str<E>(self, value: &str) -> Result<Self::Value, E> {
                Ok(JSValue::String(value.to_owned()))
            }

            fn visit_string<E>(self, value: String) -> Result<Self::Value, E> {
                Ok(JSValue::String(value))
            }

            fn visit_bytes<E>(self, value: &[u8]) -> Result<Self::Value, E> {
                Ok(JSValue::Bytes(value.to_vec()))
            }

            fn visit_byte_buf<E>(self, value: Vec<u8>) -> Result<Self::Value, E> {
                Ok(JSValue::Bytes(value))
            }

            fn visit_none<E>(self) -> Result<Self::Value, E> {
                Ok(JSValue::Null)
            }

            fn visit_unit<E>(self) -> Result<Self::Value, E> {
                Ok(JSValue::Undefined)
            }

            fn visit_seq<A>(self, mut seq: A) -> Result<Self::Value, A::Error>
            where
                A: de::SeqAccess<'de>,
            {
                let mut vec = Vec::new();
                while let Some(elem) = seq.next_element()? {
                    vec.push(elem);
                }
                Ok(JSValue::Array(vec))
            }

            fn visit_map<A>(self, mut map: A) -> Result<Self::Value, A::Error>
            where
                A: de::MapAccess<'de>,
            {
                let mut object = IndexMap::new();
                let mut tag: Option<String> = None;

                while let Some((key, value)) = map.next_entry::<String, JSValue>()? {
                    if key == TYPE_TAG {
                        if let JSValue::String(tag_value) = value {
                            tag = Some(tag_value);
                        } else {
                            object.insert(key, value);
                        }
                    } else {
                        object.insert(key, value);
                    }
                }

                if let Some(tag_value) = tag.clone() {
                    match tag_value.as_str() {
                        UNDEFINED_TYPE => {
                            return Ok(JSValue::Undefined);
                        }
                        DATE_TYPE => {
                            if let Some(epoch_value) = object.get(DATE_EPOCH_KEY) {
                                if let JSValue::Int(epoch_ms) = epoch_value {
                                    return Ok(JSValue::Date(*epoch_ms));
                                } else if let JSValue::Float(epoch_float) = epoch_value {
                                    if epoch_float.is_finite() {
                                        return Ok(JSValue::Date(*epoch_float as i64));
                                    }
                                }
                            }
                        }
                        SET_TYPE => {
                            if let Some(JSValue::Array(values)) = object.get(SET_VALUES_KEY) {
                                return Ok(JSValue::Set(values.clone()));
                            }
                        }
                        BIGINT_TYPE => {
                            if let Some(entry) = object.get(BIGINT_VALUE_KEY) {
                                return match entry {
                                    JSValue::String(value) => {
                                        let parsed = BigInt::parse_bytes(value.as_bytes(), 10)
                                            .ok_or_else(|| {
                                                de::Error::custom(format!(
                                                    "Invalid BigInt literal '{}'",
                                                    value
                                                ))
                                            })?;
                                        Ok(JSValue::BigInt(parsed))
                                    }
                                    JSValue::Int(i) => Ok(JSValue::BigInt(BigInt::from(*i))),
                                    other => Err(de::Error::custom(format!(
                                        "Invalid BigInt payload: expected string, got {:?}",
                                        other
                                    ))),
                                };
                            }
                        }
                        JS_STREAM_TYPE => {
                            if let Some(entry) = object.get(STREAM_ID_KEY) {
                                let id = match entry {
                                    JSValue::Int(v) if *v >= 0 => *v as u32,
                                    JSValue::Float(f) if f.is_finite() && *f >= 0.0 => *f as u32,
                                    other => {
                                        return Err(de::Error::custom(format!(
                                            "Invalid JsStream id payload: {:?}",
                                            other
                                        )))
                                    }
                                };
                                return Ok(JSValue::JsStream { id });
                            }
                        }
                        PY_STREAM_TYPE => {
                            if let Some(entry) = object.get(STREAM_ID_KEY) {
                                let id = match entry {
                                    JSValue::Int(v) if *v >= 0 => *v as u32,
                                    JSValue::Float(f) if f.is_finite() && *f >= 0.0 => *f as u32,
                                    other => {
                                        return Err(de::Error::custom(format!(
                                            "Invalid PyStream id payload: {:?}",
                                            other
                                        )))
                                    }
                                };
                                return Ok(JSValue::PyStream { id });
                            }
                        }
                        _ => {}
                    }
                }

                if let Some(tag_value) = tag {
                    object.insert(TYPE_TAG.to_string(), JSValue::String(tag_value));
                }

                Ok(JSValue::Object(object))
            }
        }

        deserializer.deserialize_any(JSValueVisitor)
    }
}

/// Tracks depth and size limits during JavaScript value conversion.
///
/// This is used to enforce limits while traversing V8 values to prevent
/// excessive memory usage and stack overflow.
pub struct LimitTracker {
    max_depth: usize,
    max_bytes: usize,
    current_depth: usize,
    current_bytes: usize,
}

impl LimitTracker {
    /// Create a new limit tracker with the specified limits.
    pub fn new(max_depth: usize, max_bytes: usize) -> Self {
        Self {
            max_depth,
            max_bytes,
            current_depth: 0,
            current_bytes: 0,
        }
    }

    /// Enter a new depth level.
    ///
    /// Returns an error if the depth limit is exceeded, or if this thread has
    /// a recorded stack anchor ([`record_stack_anchor`]) and recursing this
    /// deep has consumed more than [`STACK_HEADROOM_BYTES`] of real stack
    /// since that anchor. The depth counter alone cannot catch this: a caller
    /// is free to configure `max_serialization_depth` far higher than V8's
    /// own stack budget can actually sustain, and without this check that
    /// configuration corrupts the process instead of raising a Python
    /// exception. On a thread with no anchor recorded (e.g. a Python caller
    /// thread running `python_to_js_value` directly) this check is a no-op,
    /// exactly as before.
    pub fn enter(&mut self) -> RuntimeResult<()> {
        self.current_depth = self.current_depth.saturating_add(1);
        if self.current_depth > self.max_depth {
            return Err(RuntimeError::internal(depth_limit_message(self.max_depth)));
        }
        if let Some(used) = stack_used_since_anchor() {
            if used > STACK_HEADROOM_BYTES {
                return Err(RuntimeError::internal(format!(
                    "stack headroom exhausted at depth {}: {used} bytes used since \
                     the runtime thread's stack anchor (limit {STACK_HEADROOM_BYTES} \
                     bytes) -- this is a real V8/native stack limit, not \
                     `max_serialization_depth`; the value being converted is too \
                     deeply nested to serialize safely regardless of that setting",
                    self.current_depth
                )));
            }
        }
        Ok(())
    }

    /// Exit a depth level.
    pub fn exit(&mut self) {
        self.current_depth = self.current_depth.saturating_sub(1);
    }

    /// The configured byte ceiling, for the one caller that needs to reject a
    /// single oversized value by its own size rather than by the running
    /// total.
    pub fn max_bytes(&self) -> usize {
        self.max_bytes
    }

    /// Add to the byte count.
    ///
    /// Returns an error if the size limit is exceeded.
    pub fn add_bytes(&mut self, bytes: usize) -> RuntimeResult<()> {
        // Saturating: an "unlimited" tracker (`usize::MAX`) plus a large
        // payload would otherwise overflow and panic in a debug build.
        self.current_bytes = self.current_bytes.saturating_add(bytes);
        if self.current_bytes > self.max_bytes {
            return Err(RuntimeError::internal(byte_limit_message(
                self.current_bytes,
                self.max_bytes,
            )));
        }
        Ok(())
    }
}

/// The user-facing message for a depth rejection.
///
/// It names `max_serialization_depth` on purpose: the review of v0.2.0 found
/// that a bare "Depth exceeded maximum limit of 100" gives the reader no way
/// to discover that the limit is a knob they own, so a tunable limit reads as
/// a hard wall. Every depth rejection on either direction of the boundary
/// goes through here.
pub fn depth_limit_message(max_depth: usize) -> String {
    format!(
        "Serialization depth exceeded the configured limit of {max_depth} \
         (RuntimeConfig(max_serialization_depth=...))"
    )
}

/// The user-facing message for a byte rejection. Names
/// `max_serialization_bytes` for the same reason as [`depth_limit_message`].
pub fn byte_limit_message(current_bytes: usize, max_bytes: usize) -> String {
    format!(
        "Serialization size ({current_bytes} bytes) exceeded the configured limit of \
         {max_bytes} bytes (RuntimeConfig(max_serialization_bytes=...)); see \
         docs/guides/advanced/arrow-ipc-dataframes.md for transferring large payloads"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_js_value_creation() {
        // Test that we can create various JSValue types
        let _null = JSValue::Null;
        let _bool = JSValue::Bool(true);
        let _int = JSValue::Int(42);
        let _float = JSValue::Float(2.5);
        let _string = JSValue::String("hello".to_string());
        let _array = JSValue::Array(vec![JSValue::Int(1), JSValue::Int(2)]);
        let mut map = IndexMap::new();
        map.insert("key".to_string(), JSValue::String("value".to_string()));
        let _object = JSValue::Object(map);
    }

    #[test]
    fn test_js_value_special_floats() {
        let nan = JSValue::Float(f64::NAN);
        let inf = JSValue::Float(f64::INFINITY);
        let neg_inf = JSValue::Float(f64::NEG_INFINITY);

        // Verify they can be created without panicking
        assert!(matches!(nan, JSValue::Float(_)));
        assert!(matches!(inf, JSValue::Float(_)));
        assert!(matches!(neg_inf, JSValue::Float(_)));
    }

    #[test]
    fn test_limit_tracker_basic() {
        let mut tracker = LimitTracker::new(10, 1000);

        assert!(tracker.enter().is_ok());
        assert!(tracker.add_bytes(100).is_ok());
        tracker.exit();
    }

    #[test]
    fn test_limit_tracker_depth_exceeded() {
        let mut tracker = LimitTracker::new(3, 1000);

        assert!(tracker.enter().is_ok()); // depth 1
        assert!(tracker.enter().is_ok()); // depth 2
        assert!(tracker.enter().is_ok()); // depth 3
        assert!(tracker.enter().is_err()); // depth 4 - should fail
    }

    #[test]
    fn test_limit_tracker_size_exceeded() {
        let mut tracker = LimitTracker::new(10, 100);

        assert!(tracker.add_bytes(50).is_ok());
        assert!(tracker.add_bytes(40).is_ok());
        assert!(tracker.add_bytes(20).is_err()); // Total 110 - should fail
    }
}
