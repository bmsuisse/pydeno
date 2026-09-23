//! V8 value <-> [`JSValue`] conversion, and calls into stored JS functions.

use crate::runtime::error::{RuntimeError, RuntimeResult};
use crate::runtime::js_value::{JSValue, LimitTracker, SerializationLimits};
use crate::runtime::stream::JsStreamRegistry;
use deno_core::error::JsError;
use deno_core::v8;
use indexmap::IndexMap;
use num_bigint::{BigInt, Sign};
use std::cell::RefCell;
use std::collections::HashMap;
use std::ptr;
use std::rc::Rc;

/// A JS function handed to Python, with the receiver it was read from (for `this`).
pub(super) struct StoredFunction {
    pub(super) function: v8::Global<v8::Function>,
    pub(super) receiver: Option<v8::Global<v8::Value>>,
}

pub(super) type FnRegistry = Rc<RefCell<HashMap<u32, StoredFunction>>>;

/// Why a call into a stored function produced no value.
pub(super) enum CallError {
    Runtime(RuntimeError),
    /// Boxed: `JsError` carries the whole stack (clippy::result_large_err).
    Js(Box<JsError>),
    /// V8 terminated the call; see `RuntimeCoreState::terminated_call_error`.
    Terminated,
}

impl From<RuntimeError> for CallError {
    fn from(err: RuntimeError) -> Self {
        Self::Runtime(err)
    }
}

/// Turn a failed `func.call` inside a `tc_scope!` into a [`CallError`].
macro_rules! caught_call_error {
    ($try_catch:expr) => {
        if $try_catch.has_terminated() {
            CallError::Terminated
        } else {
            match $try_catch.exception() {
                Some(exception) => CallError::Js(JsError::from_v8_exception($try_catch, exception)),
                None => CallError::Runtime(RuntimeError::internal(
                    "Function call failed with no exception",
                )),
            }
        }
    };
}
pub(super) use caught_call_error;

/// Everything needed to marshal values across the V8 boundary; cheap to clone.
#[derive(Clone)]
pub(super) struct Converter {
    pub(super) fn_registry: FnRegistry,
    pub(super) next_fn_id: Rc<RefCell<u32>>,
    pub(super) limits: SerializationLimits,
    pub(super) streams: Rc<JsStreamRegistry>,
}

fn circular_check<'s>(
    seen: &mut Vec<v8::Local<'s, v8::Object>>,
    obj: v8::Local<'s, v8::Object>,
) -> RuntimeResult<()> {
    if seen
        .iter()
        .any(|ancestor| ancestor.strict_equals(obj.into()))
    {
        return Err(RuntimeError::internal(
            "Cannot serialize circular reference",
        ));
    }
    seen.push(obj);
    Ok(())
}

pub(super) fn is_readable_stream(
    scope: &mut v8::PinScope<'_, '_>,
    value: v8::Local<'_, v8::Value>,
) -> bool {
    if !value.is_object() {
        return false;
    }
    let Some(key) = v8::String::new(scope, "ReadableStream") else {
        return false;
    };
    let global = scope.get_current_context().global(scope);
    let Some(ctor) = global
        .get(scope, key.into())
        .and_then(|value| v8::Local::<v8::Function>::try_from(value).ok())
    else {
        return false;
    };
    value.instance_of(scope, ctor.into()).unwrap_or_default()
}

/// Look up the global JS helper function `name` (installed by the ops bootstrap).
pub(super) fn global_helper<'s>(
    scope: &mut v8::PinScope<'s, '_>,
    name: &str,
) -> RuntimeResult<(v8::Local<'s, v8::Object>, v8::Local<'s, v8::Function>)> {
    let global = scope.get_current_context().global(scope);
    let key = v8::String::new(scope, name)
        .ok_or_else(|| RuntimeError::internal("Failed to allocate helper key"))?;
    let value = global
        .get(scope, key.into())
        .ok_or_else(|| RuntimeError::internal(format!("Missing {name} helper")))?;
    let func = v8::Local::<v8::Function>::try_from(value)
        .map_err(|_| RuntimeError::internal(format!("{name} is not callable")))?;
    Ok((global, func))
}

impl Converter {
    /// Look up `fn_id`, convert `args` and call it with its captured receiver
    /// (or the global). `None` means it threw or was terminated; run this
    /// inside a `tc_scope!` and use [`caught_call_error!`] to tell which.
    pub(super) fn call_stored<'s>(
        &self,
        scope: &mut v8::PinScope<'s, '_>,
        fn_id: u32,
        args: &[JSValue],
    ) -> RuntimeResult<Option<v8::Local<'s, v8::Value>>> {
        let (func, receiver) = {
            let registry = self.fn_registry.borrow();
            let stored = registry.get(&fn_id).ok_or_else(|| {
                RuntimeError::internal(format!("Function ID {} not found", fn_id))
            })?;
            let func = v8::Local::new(scope, &stored.function);
            let receiver = stored.receiver.as_ref().map(|r| v8::Local::new(scope, r));
            (func, receiver)
        };
        let v8_args = args
            .iter()
            .map(|arg| self.to_v8(scope, arg))
            .collect::<RuntimeResult<Vec<_>>>()?;
        let receiver = receiver.unwrap_or_else(|| scope.get_current_context().global(scope).into());
        Ok(func.call(scope, receiver, &v8_args))
    }

    /// Convert a V8 value to JSValue with circular reference detection and limits enforced.
    pub(super) fn to_js_value<'s>(
        &self,
        scope: &mut v8::PinScope<'s, '_>,
        value: v8::Local<'s, v8::Value>,
    ) -> RuntimeResult<JSValue> {
        let mut tracker = LimitTracker::new(self.limits.max_depth, self.limits.max_bytes);
        self.to_js_value_inner(scope, value, &mut Vec::new(), &mut tracker, None)
    }

    /// Recursive converter. `seen` is the current path of container objects,
    /// so a cycle is "this object is its own ancestor" (checked with
    /// `strict_equals`; identity hashes can collide and reject acyclic input).
    fn to_js_value_inner<'s>(
        &self,
        scope: &mut v8::PinScope<'s, '_>,
        value: v8::Local<'s, v8::Value>,
        seen: &mut Vec<v8::Local<'s, v8::Object>>,
        tracker: &mut LimitTracker,
        receiver: Option<v8::Global<v8::Value>>,
    ) -> RuntimeResult<JSValue> {
        tracker.enter()?;

        let result = if value.is_undefined() {
            tracker.add_bytes(0)?;
            Ok(JSValue::Undefined)
        } else if value.is_null() {
            tracker.add_bytes(4)?;
            Ok(JSValue::Null)
        } else if value.is_boolean() {
            tracker.add_bytes(5)?; // "false" (worst case)
            Ok(JSValue::Bool(value.boolean_value(scope)))
        } else if value.is_number() {
            let num_val = value
                .to_number(scope)
                .ok_or_else(|| RuntimeError::internal("Failed to convert value to number"))?
                .value();
            // NaN/±Infinity and non-integral values stay floats.
            if num_val.is_finite() && num_val.fract() == 0.0 && num_val as i64 as f64 == num_val {
                tracker.add_bytes(20)?;
                Ok(JSValue::Int(num_val as i64))
            } else {
                tracker.add_bytes(24)?;
                Ok(JSValue::Float(num_val))
            }
        } else if value.is_big_int() {
            let bigint = v8::Local::<v8::BigInt>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast to BigInt"))?;
            let (int_value, lossless) = bigint.i64_value();
            if lossless {
                tracker.add_bytes(20)?;
                Ok(JSValue::Int(int_value))
            } else {
                let string = bigint
                    .to_string(scope)
                    .ok_or_else(|| RuntimeError::internal("Failed to stringify BigInt"))?
                    .to_rust_string_lossy(scope);
                let parsed = BigInt::parse_bytes(string.as_bytes(), 10)
                    .ok_or_else(|| RuntimeError::internal("Failed to parse BigInt literal"))?;
                tracker.add_bytes(string.len())?;
                Ok(JSValue::BigInt(parsed))
            }
        } else if value.is_string() {
            let rust_str = value
                .to_string(scope)
                .ok_or_else(|| RuntimeError::internal("Failed to convert string"))?
                .to_rust_string_lossy(scope);
            tracker.add_bytes(rust_str.len())?;
            Ok(JSValue::String(rust_str))
        } else if value.is_function() {
            let func = v8::Local::<v8::Function>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast to function"))?;
            let function = v8::Global::new(scope, func);
            let mut next_id = self.next_fn_id.borrow_mut();
            let fn_id = *next_id;
            *next_id += 1;
            self.fn_registry
                .borrow_mut()
                .insert(fn_id, StoredFunction { function, receiver });
            tracker.add_bytes(8)?; // ID size
            Ok(JSValue::Function { id: fn_id })
        } else if value.is_symbol() {
            Err(RuntimeError::internal("Cannot serialize V8 symbol"))
        } else if value.is_uint8_array() {
            let typed_array = v8::Local::<v8::Uint8Array>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast to Uint8Array"))?;
            let length = typed_array.byte_length();
            tracker.add_bytes(length)?;
            let mut buffer = vec![0u8; length];
            let view: v8::Local<v8::ArrayBufferView> = typed_array.into();
            view.copy_contents(&mut buffer);
            Ok(JSValue::Bytes(buffer))
        } else if value.is_array_buffer() {
            let array_buffer = v8::Local::<v8::ArrayBuffer>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast to ArrayBuffer"))?;
            let length = array_buffer.byte_length();
            tracker.add_bytes(length)?;
            let mut buffer = vec![0u8; length];
            if length > 0 {
                if let Some(data_ptr) = array_buffer.data() {
                    unsafe {
                        ptr::copy_nonoverlapping(
                            data_ptr.as_ptr() as *const u8,
                            buffer.as_mut_ptr(),
                            length,
                        );
                    }
                }
            }
            Ok(JSValue::Bytes(buffer))
        } else if value.is_array() {
            let obj = v8::Local::<v8::Object>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast array to object"))?;
            circular_check(seen, obj)?;

            let array = v8::Local::<v8::Array>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast to array"))?;
            let len = array.length() as usize;

            // Meter the guest-controlled length *before* reserving or walking:
            // holes convert to `Undefined` (0 bytes), so `a[4294967294] = 1`
            // would otherwise allocate unboundedly. Clamp the capacity hint too.
            tracker.add_bytes(16)?;
            tracker.add_bytes(len.saturating_mul(size_of::<usize>()))?;

            const ARRAY_CAPACITY_HINT_CAP: usize = 4096;
            let mut items = Vec::with_capacity(len.min(ARRAY_CAPACITY_HINT_CAP));
            for i in 0..len {
                let item = array.get_index(scope, i as u32).ok_or_else(|| {
                    RuntimeError::internal(format!("Failed to get array index {}", i))
                })?;
                items.push(self.to_js_value_inner(scope, item, seen, tracker, None)?);
            }

            seen.pop();
            Ok(JSValue::Array(items))
        } else if value.is_set() {
            let obj = v8::Local::<v8::Object>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast set to object"))?;
            circular_check(seen, obj)?;

            let set = v8::Local::<v8::Set>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast to Set"))?;
            let entries = set.as_array(scope);
            let len = entries.length() as usize;

            tracker.add_bytes(24)?;
            tracker.add_bytes(len.saturating_mul(size_of::<usize>()))?;

            let mut values = Vec::with_capacity(len);
            for index in 0..len {
                let element = entries
                    .get_index(scope, index as u32)
                    .ok_or_else(|| RuntimeError::internal("Failed to get Set entry"))?;
                values.push(self.to_js_value_inner(scope, element, seen, tracker, None)?);
            }

            seen.pop();
            Ok(JSValue::Set(values))
        } else if value.is_date() {
            let date = v8::Local::<v8::Date>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast to Date"))?;
            let epoch_ms = date.value_of();
            if !epoch_ms.is_finite() || epoch_ms < i64::MIN as f64 || epoch_ms > i64::MAX as f64 {
                return Err(RuntimeError::internal("Date value out of range"));
            }
            tracker.add_bytes(16)?;
            Ok(JSValue::Date(epoch_ms.round() as i64))
        } else if value.is_object() && is_readable_stream(scope, value) {
            let stream_id = self.streams.register_stream(scope, value);
            tracker.add_bytes(size_of::<u32>())?;
            Ok(JSValue::JsStream { id: stream_id })
        } else if value.is_object() {
            let obj = v8::Local::<v8::Object>::try_from(value)
                .map_err(|_| RuntimeError::internal("Failed to cast to object"))?;
            circular_check(seen, obj)?;

            let prop_names = obj
                .get_own_property_names(scope, v8::GetPropertyNamesArgs::default())
                .ok_or_else(|| RuntimeError::internal("Failed to get property names"))?;

            let mut map = IndexMap::new();
            for i in 0..prop_names.length() {
                let key = prop_names
                    .get_index(scope, i)
                    .ok_or_else(|| RuntimeError::internal("Failed to get property name"))?;
                let key_str = key
                    .to_string(scope)
                    .ok_or_else(|| RuntimeError::internal("Failed to convert key to string"))?
                    .to_rust_string_lossy(scope);

                let val = obj.get(scope, key).ok_or_else(|| {
                    RuntimeError::internal(format!("Failed to get property '{}'", key_str))
                })?;

                // A method keeps its object as `this`.
                let receiver = val.is_function().then(|| {
                    let obj_as_value: v8::Local<v8::Value> = obj.into();
                    v8::Global::new(scope, obj_as_value)
                });

                tracker.add_bytes(key_str.len())?;
                let converted = self.to_js_value_inner(scope, val, seen, tracker, receiver)?;
                map.insert(key_str, converted);
            }

            seen.pop();
            Ok(JSValue::Object(map))
        } else {
            let rust_str = value
                .to_string(scope)
                .ok_or_else(|| RuntimeError::internal("Failed to convert value to string"))?
                .to_rust_string_lossy(scope);
            tracker.add_bytes(rust_str.len())?;
            Ok(JSValue::String(rust_str))
        };

        tracker.exit();
        result
    }

    pub(super) fn to_v8<'s>(
        &self,
        scope: &mut v8::PinScope<'s, '_>,
        value: &JSValue,
    ) -> RuntimeResult<v8::Local<'s, v8::Value>> {
        Ok(match value {
            JSValue::Undefined => v8::undefined(scope).into(),
            JSValue::Null => v8::null(scope).into(),
            JSValue::Bool(b) => v8::Boolean::new(scope, *b).into(),
            JSValue::Int(i) => v8::Number::new(scope, *i as f64).into(),
            JSValue::BigInt(bigint) => {
                let (sign, bytes) = bigint.to_bytes_le();
                let words: Vec<u64> = bytes
                    .chunks(8)
                    .map(|chunk| {
                        let mut buf = [0u8; 8];
                        buf[..chunk.len()].copy_from_slice(chunk);
                        u64::from_le_bytes(buf)
                    })
                    .collect();
                v8::BigInt::new_from_words(scope, matches!(sign, Sign::Minus), &words)
                    .ok_or_else(|| RuntimeError::internal("Failed to create BigInt"))?
                    .into()
            }
            JSValue::Float(f) => v8::Number::new(scope, *f).into(),
            JSValue::String(s) => v8::String::new(scope, s)
                .ok_or_else(|| RuntimeError::internal("Failed to allocate string"))?
                .into(),
            JSValue::Bytes(bytes) => {
                let backing = v8::ArrayBuffer::new_backing_store_from_vec(bytes.clone());
                let buffer = v8::ArrayBuffer::with_backing_store(scope, &backing.make_shared());
                v8::Uint8Array::new(scope, buffer, 0, bytes.len())
                    .ok_or_else(|| RuntimeError::internal("Failed to create Uint8Array"))?
                    .into()
            }
            JSValue::Array(items) => {
                let array = v8::Array::new(scope, items.len() as i32);
                for (index, item) in items.iter().enumerate() {
                    let v8_value = self.to_v8(scope, item)?;
                    array
                        .set_index(scope, index as u32, v8_value)
                        .ok_or_else(|| RuntimeError::internal("Failed to set array element"))?;
                }
                array.into()
            }
            JSValue::Set(values) => {
                let set = v8::Set::new(scope);
                for value in values {
                    let v8_value = self.to_v8(scope, value)?;
                    set.add(scope, v8_value);
                }
                set.into()
            }
            JSValue::Object(map) => {
                let object = v8::Object::new(scope);
                for (key, val) in map.iter() {
                    let key_str = v8::String::new(scope, key).ok_or_else(|| {
                        RuntimeError::internal(format!("Failed to allocate key '{key}'"))
                    })?;
                    let v8_value = self.to_v8(scope, val)?;
                    object.set(scope, key_str.into(), v8_value).ok_or_else(|| {
                        RuntimeError::internal(format!("Failed to set property '{key}'"))
                    })?;
                }
                object.into()
            }
            JSValue::Date(epoch_ms) => v8::Date::new(scope, *epoch_ms as f64)
                .ok_or_else(|| RuntimeError::internal("Failed to create Date"))?
                .into(),
            JSValue::Function { id } => {
                let registry = self.fn_registry.borrow();
                let stored = registry.get(id).ok_or_else(|| {
                    RuntimeError::internal(format!("Function ID {} not found in args", id))
                })?;
                v8::Local::new(scope, &stored.function).into()
            }
            JSValue::PyStream { id } => {
                let (global, helper_fn) = global_helper(scope, "__pydeno_from_py_stream")?;
                let id_value = v8::Number::new(scope, *id as f64);
                helper_fn
                    .call(scope, global.into(), &[id_value.into()])
                    .ok_or_else(|| {
                        RuntimeError::internal("__pydeno_from_py_stream invocation failed")
                    })?
            }
            JSValue::JsStream { .. } => {
                return Err(RuntimeError::internal(
                    "JsStream values cannot be sent back into JavaScript",
                ))
            }
        })
    }
}
