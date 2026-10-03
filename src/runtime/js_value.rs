//! `JSValue`: lossless JavaScript value representation plus conversion limits.

use crate::runtime::error::{RuntimeError, RuntimeResult};
use indexmap::IndexMap;
use num_bigint::BigInt;
use serde::ser::SerializeMap;
use serde::{Deserialize, Serialize};
use serde_bytes::Bytes;

/// Default serialization depth / byte limits.
pub const MAX_JS_DEPTH: usize = 100;
pub const MAX_JS_BYTES: usize = 10 * 1024 * 1024;

/// Stack reserved for every thread that may run V8 or the recursive `JSValue`
/// converters (Rust's 2 MiB default is too small for unoptimized frames at
/// [`MAX_JS_DEPTH`]). Only a lazily committed reservation; V8's own ~984 KB
/// limit is the real bound, enforced by [`LimitTracker::enter`].
///
/// `python_to_js_value` recurses on the *calling* Python thread (it needs the
/// GIL), so this does not cover it: e.g. a 512 KB macOS `threading.Thread`
/// handles ~900 levels, ~9x the default depth
/// (`tests/test_thread_stack_size.py`). Raising `max_serialization_depth` past
/// that and converting from a small-stack thread is unsafe.
pub const RUNTIME_THREAD_STACK_SIZE: usize = 16 * 1024 * 1024;

/// Stack budget below the runtime thread's anchor before [`LimitTracker::enter`]
/// refuses to recurse. Approximates V8's native stack limit, whose overflow
/// aborts the process (`Check failed: IsOnCentralStack()`) instead of raising.
///
/// Checked unconditionally, so in a debug build (~29.5 KB/frame) it trips at
/// depth ~22, below the default `max_serialization_depth`; release builds
/// (~0.9 KB/frame) trip at ~743. Raising it reproduces the V8 abort, and
/// `set_stack_limit` cannot move V8's boundary (process-global flag). Measured
/// from the thread anchor, so frames already on the stack count against it.
/// See `tests/test_serialization_headroom.py`.
const STACK_HEADROOM_BYTES: usize = 640 * 1024;

#[cfg(debug_assertions)]
const PROFILE_LABEL: &str = "unoptimized (debug_assertions on)";
#[cfg(not(debug_assertions))]
const PROFILE_LABEL: &str = "optimized";

#[cfg(debug_assertions)]
const PROFILE_CEILING: &str = "around depth 22 -- below the default \
                               `max_serialization_depth` of 100";
#[cfg(not(debug_assertions))]
const PROFILE_CEILING: &str = "around depth 743 -- far past the default \
                               `max_serialization_depth` of 100";

thread_local! {
    /// Runtime thread's stack anchor; `None` elsewhere, which disables the
    /// headroom check (e.g. on Python caller threads).
    static STACK_ANCHOR: std::cell::Cell<Option<usize>> = const { std::cell::Cell::new(None) };
}

/// Record the current thread's stack anchor for [`LimitTracker::enter`]. Call
/// once near the top of the runtime thread, before `JsRuntime::new`.
pub fn record_stack_anchor() {
    let local_probe: u8 = 0;
    let addr = &local_probe as *const u8 as usize;
    STACK_ANCHOR.with(|cell| cell.set(Some(addr)));
}

/// Stack bytes used since this thread's anchor (stack grows down), or `None`.
fn stack_used_since_anchor() -> Option<usize> {
    STACK_ANCHOR.with(|cell| {
        cell.get().map(|anchor| {
            let here_probe: u8 = 0;
            anchor.saturating_sub(&here_probe as *const u8 as usize)
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

/// JavaScript value that round-trips accurately (NaN, ±Infinity, BigInt,
/// Date, ...). Serde impls are manual: `Function` cannot be serialized.
#[derive(Clone, Debug, PartialEq)]
pub enum JSValue {
    Undefined,
    Null,
    Bool(bool),
    /// Integer within i64 range.
    Int(i64),
    BigInt(BigInt),
    /// Float, including NaN and ±Infinity.
    Float(f64),
    String(String),
    /// Uint8Array / ArrayBuffer.
    Bytes(Vec<u8>),
    Array(Vec<JSValue>),
    /// Object in insertion order.
    Object(IndexMap<String, JSValue>),
    /// Epoch milliseconds, UTC.
    Date(i64),
    /// Set in insertion order.
    Set(Vec<JSValue>),
    /// Function proxied via registry id.
    Function {
        id: u32,
    },
    /// ReadableStream proxied via the runtime stream registry.
    JsStream {
        id: u32,
    },
    /// Python async iterable placeholder forwarded into JavaScript.
    PyStream {
        id: u32,
    },
}

/// Serialize `{TYPE_TAG: tag}` plus an optional payload entry.
fn serialize_tagged<S: serde::Serializer, V: Serialize + ?Sized>(
    serializer: S,
    tag: &str,
    entry: Option<(&str, &V)>,
) -> Result<S::Ok, S::Error> {
    let mut map = serializer.serialize_map(Some(1 + entry.is_some() as usize))?;
    map.serialize_entry(TYPE_TAG, tag)?;
    if let Some((key, value)) = entry {
        map.serialize_entry(key, value)?;
    }
    map.end()
}

impl Serialize for JSValue {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        use serde::ser::Error;
        match self {
            JSValue::Undefined => serialize_tagged::<S, ()>(serializer, UNDEFINED_TYPE, None),
            JSValue::Null => serializer.serialize_none(),
            JSValue::Bool(b) => serializer.serialize_bool(*b),
            JSValue::Int(i) => serializer.serialize_i64(*i),
            JSValue::BigInt(bigint) => serialize_tagged(
                serializer,
                BIGINT_TYPE,
                Some((BIGINT_VALUE_KEY, &bigint.to_str_radix(10))),
            ),
            JSValue::Float(f) => serializer.serialize_f64(*f),
            JSValue::String(s) => serializer.serialize_str(s),
            JSValue::Bytes(bytes) => Bytes::new(bytes).serialize(serializer),
            JSValue::Array(arr) => arr.serialize(serializer),
            JSValue::Object(obj) => obj.serialize(serializer),
            JSValue::Date(epoch_ms) => {
                serialize_tagged(serializer, DATE_TYPE, Some((DATE_EPOCH_KEY, epoch_ms)))
            }
            JSValue::Set(values) => {
                serialize_tagged(serializer, SET_TYPE, Some((SET_VALUES_KEY, values)))
            }
            JSValue::Function { id } => Err(Error::custom(format!(
                "Cannot serialize JSValue::Function (id: {}). Functions must be called, not serialized.",
                id
            ))),
            JSValue::JsStream { id } => {
                serialize_tagged(serializer, JS_STREAM_TYPE, Some((STREAM_ID_KEY, id)))
            }
            JSValue::PyStream { id } => {
                serialize_tagged(serializer, PY_STREAM_TYPE, Some((STREAM_ID_KEY, id)))
            }
        }
    }
}

impl<'de> Deserialize<'de> for JSValue {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        use serde::de;

        struct JSValueVisitor;

        /// Decode the `id` of a tagged stream placeholder.
        fn stream_id<E: de::Error>(kind: &str, entry: &JSValue) -> Result<u32, E> {
            match entry {
                JSValue::Int(v) if *v >= 0 => Ok(*v as u32),
                JSValue::Float(f) if f.is_finite() && *f >= 0.0 => Ok(*f as u32),
                other => Err(E::custom(format!("Invalid {kind} id payload: {:?}", other))),
            }
        }

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
                Ok(i64::try_from(value).map_or(JSValue::Float(value as f64), JSValue::Int))
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
                    match value {
                        JSValue::String(tag_value) if key == TYPE_TAG => tag = Some(tag_value),
                        value => {
                            object.insert(key, value);
                        }
                    }
                }

                // A recognised tag with a malformed payload falls through to a
                // plain object that keeps the tag.
                if let Some(tag_value) = tag {
                    match (tag_value.as_str(), object.get(tag_payload_key(&tag_value))) {
                        (UNDEFINED_TYPE, _) => return Ok(JSValue::Undefined),
                        (DATE_TYPE, Some(JSValue::Int(epoch_ms))) => {
                            return Ok(JSValue::Date(*epoch_ms))
                        }
                        (DATE_TYPE, Some(JSValue::Float(f))) if f.is_finite() => {
                            return Ok(JSValue::Date(*f as i64))
                        }
                        (SET_TYPE, Some(JSValue::Array(values))) => {
                            return Ok(JSValue::Set(values.clone()))
                        }
                        (BIGINT_TYPE, Some(JSValue::String(value))) => {
                            return BigInt::parse_bytes(value.as_bytes(), 10)
                                .map(JSValue::BigInt)
                                .ok_or_else(|| {
                                    de::Error::custom(format!("Invalid BigInt literal '{}'", value))
                                })
                        }
                        (BIGINT_TYPE, Some(JSValue::Int(i))) => {
                            return Ok(JSValue::BigInt(BigInt::from(*i)))
                        }
                        (BIGINT_TYPE, Some(other)) => {
                            return Err(de::Error::custom(format!(
                                "Invalid BigInt payload: expected string, got {:?}",
                                other
                            )))
                        }
                        (JS_STREAM_TYPE, Some(entry)) => {
                            return Ok(JSValue::JsStream {
                                id: stream_id("JsStream", entry)?,
                            })
                        }
                        (PY_STREAM_TYPE, Some(entry)) => {
                            return Ok(JSValue::PyStream {
                                id: stream_id("PyStream", entry)?,
                            })
                        }
                        _ => {}
                    }
                    object.insert(TYPE_TAG.to_string(), JSValue::String(tag_value));
                }

                Ok(JSValue::Object(object))
            }
        }

        deserializer.deserialize_any(JSValueVisitor)
    }
}

/// Key holding a tagged value's payload.
fn tag_payload_key(tag: &str) -> &'static str {
    match tag {
        DATE_TYPE => DATE_EPOCH_KEY,
        SET_TYPE => SET_VALUES_KEY,
        BIGINT_TYPE => BIGINT_VALUE_KEY,
        _ => STREAM_ID_KEY,
    }
}

/// Enforces depth, byte and stack-headroom limits during value conversion.
pub struct LimitTracker {
    max_depth: usize,
    max_bytes: usize,
    current_depth: usize,
    current_bytes: usize,
}

impl LimitTracker {
    pub fn new(max_depth: usize, max_bytes: usize) -> Self {
        Self {
            max_depth,
            max_bytes,
            current_depth: 0,
            current_bytes: 0,
        }
    }

    /// Enter a depth level. Errors past `max_depth`, or when more than
    /// [`STACK_HEADROOM_BYTES`] of stack were used since this thread's anchor
    /// (a no-op on threads without one).
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
                     bytes). This is V8's own native stack limit, not \
                     `max_serialization_depth` -- raising that setting cannot lift \
                     it. How deep you can go depends on the build profile, because \
                     a serializer frame is ~33x larger unoptimized: this pydeno was \
                     built {PROFILE_LABEL}, where the ceiling is {PROFILE_CEILING}. \
                     Released wheels are optimized builds; if you are seeing this \
                     well below `max_serialization_depth` on ordinary data, you are \
                     running a debug build of pydeno, not hitting a limit its users \
                     see",
                    self.current_depth
                )));
            }
        }
        Ok(())
    }

    pub fn exit(&mut self) {
        self.current_depth = self.current_depth.saturating_sub(1);
    }

    /// The configured byte ceiling, for rejecting a single oversized value.
    pub fn max_bytes(&self) -> usize {
        self.max_bytes
    }

    /// Add to the byte count; errors past `max_bytes`. Saturating so an
    /// "unlimited" (`usize::MAX`) tracker cannot overflow.
    pub fn add_bytes(&mut self, bytes: usize) -> RuntimeResult<()> {
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

/// User-facing depth rejection (both directions); names the config knob so the
/// limit reads as tunable.
pub fn depth_limit_message(max_depth: usize) -> String {
    format!(
        "Serialization depth exceeded the configured limit of {max_depth} \
         (RuntimeConfig(max_serialization_depth=...))"
    )
}

/// User-facing byte rejection; see [`depth_limit_message`].
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
    fn test_limit_tracker_basic() {
        let mut tracker = LimitTracker::new(10, 1000);
        assert!(tracker.enter().is_ok());
        assert!(tracker.add_bytes(100).is_ok());
        tracker.exit();
    }

    #[test]
    fn test_limit_tracker_depth_exceeded() {
        let mut tracker = LimitTracker::new(3, 1000);
        for _ in 0..3 {
            assert!(tracker.enter().is_ok());
        }
        assert!(tracker.enter().is_err());
    }

    #[test]
    fn test_limit_tracker_size_exceeded() {
        let mut tracker = LimitTracker::new(10, 100);
        assert!(tracker.add_bytes(50).is_ok());
        assert!(tracker.add_bytes(40).is_ok());
        assert!(tracker.add_bytes(20).is_err());
    }
}
