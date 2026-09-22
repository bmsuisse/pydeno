//! Conversion helpers between Python objects and JSValue/serde_json values.

use crate::runtime::js_value::{byte_limit_message, JSValue, LimitTracker, SerializationLimits};
use crate::runtime::python::{runtime_error_to_py, PyStreamSource};
use indexmap::IndexMap;
use num_bigint::BigInt;
use pyo3::conversion::IntoPyObject;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyDateTime, PyDict, PyFloat, PyFrozenSet, PyFrozenSetMethods,
    PyInt, PyList, PyMemoryView, PySet, PySetMethods, PyString,
};
use std::collections::HashSet;

const TYPE_TAG: &str = "__pydeno_type";
const UNDEFINED_TYPE: &str = "Undefined";
const DATE_TYPE: &str = "Date";
const DATE_EPOCH_KEY: &str = "epoch_ms";
const SET_TYPE: &str = "Set";
const SET_VALUES_KEY: &str = "values";
const BIGINT_TYPE: &str = "BigInt";
const BIGINT_VALUE_KEY: &str = "value";

/// Convert a JSValue into a Python object.
///
/// This is the new primary conversion function that supports native JavaScript values
/// including NaN and ±Infinity without sentinel strings.
///
/// For Function variants, a RuntimeHandle must be provided to create JsFunction proxies.
///
/// This entry point applies **no** depth or size limits, because its callers
/// (the eval-result path) have already been metered while reading the value
/// out of V8. Anything converting *guest-supplied* input -- i.e. op arguments
/// -- must use [`js_value_to_python_tracked`] instead, so the configured
/// limits actually bind inbound. See the module-level note on symmetry.
pub(crate) fn js_value_to_python(
    py: Python<'_>,
    value: &JSValue,
    handle: Option<&super::handle::RuntimeHandle>,
) -> PyResult<Py<PyAny>> {
    let mut unlimited = LimitTracker::new(usize::MAX, usize::MAX);
    js_value_to_python_tracked(py, value, handle, &mut unlimited)
}

/// Convert a JSValue into a Python object against a caller-supplied
/// [`LimitTracker`], so several conversions can share one *aggregate* budget.
///
/// This is the inbound (JS -> Python) mirror of
/// [`python_to_js_value_tracked`]. Both halves of the boundary must meter, and
/// both must meter *aggregately*:
///
/// - Until v0.2.0 the inbound path passed no tracker at all, so a guest could
///   hand a host tool four 9 MB strings in one call -- 37.7 MB measured
///   against a configured 10 MB cap -- and a 10-deep object against a
///   configured depth of 3. The identical payload was correctly refused as an
///   `eval` *result*, which made the limit bind only the trusted party.
/// - A fresh tracker per argument would reintroduce the per-argument budget
///   bug that `python_to_js_value_tracked` was written to fix, so the ops pass
///   one tracker across the whole argument list.
pub(crate) fn js_value_to_python_tracked(
    py: Python<'_>,
    value: &JSValue,
    handle: Option<&super::handle::RuntimeHandle>,
    tracker: &mut LimitTracker,
) -> PyResult<Py<PyAny>> {
    tracker.enter().map_err(runtime_error_to_py)?;
    let result = js_value_to_python_inner(py, value, handle, tracker);
    tracker.exit();
    result
}

fn js_value_to_python_inner(
    py: Python<'_>,
    value: &JSValue,
    handle: Option<&super::handle::RuntimeHandle>,
    tracker: &mut LimitTracker,
) -> PyResult<Py<PyAny>> {
    let add_bytes = |bytes: usize, tracker: &mut LimitTracker| {
        tracker.add_bytes(bytes).map_err(runtime_error_to_py)
    };

    match value {
        JSValue::Undefined => super::python::get_js_undefined(py).map(Into::into),
        JSValue::Null => {
            add_bytes(4, tracker)?;
            Ok(py.None())
        }
        JSValue::Bool(b) => {
            add_bytes(1, tracker)?;
            Ok(PyBool::new(py, *b).to_owned().into_any().unbind())
        }
        JSValue::Int(i) => {
            add_bytes(size_of::<i64>(), tracker)?;
            Ok(PyInt::new(py, *i).into_any().unbind())
        }
        JSValue::BigInt(bigint) => {
            let (_, magnitude) = bigint.to_bytes_le();
            add_bytes(magnitude.len(), tracker)?;
            Ok(bigint.clone().into_pyobject(py)?.into_any().unbind())
        }
        JSValue::Float(f) => {
            add_bytes(size_of::<f64>(), tracker)?;
            Ok(PyFloat::new(py, *f).into_any().unbind())
        }
        JSValue::String(s) => {
            add_bytes(s.len(), tracker)?;
            add_bytes(16, tracker)?;
            Ok(PyString::new(py, s).into_any().unbind())
        }
        JSValue::Bytes(bytes) => {
            add_bytes(bytes.len(), tracker)?;
            Ok(PyBytes::new(py, bytes).into_any().unbind())
        }
        JSValue::Array(items) => {
            add_bytes(16, tracker)?;
            add_bytes(items.len().saturating_mul(size_of::<usize>()), tracker)?;
            let list = PyList::empty(py);
            for item in items {
                list.append(js_value_to_python_tracked(py, item, handle, tracker)?)?;
            }
            Ok(list.into_any().unbind())
        }
        JSValue::Set(items) => {
            add_bytes(24, tracker)?;
            add_bytes(items.len().saturating_mul(size_of::<usize>()), tracker)?;
            let py_set = PySet::empty(py)?;
            for item in items {
                py_set.add(js_value_to_python_tracked(py, item, handle, tracker)?)?;
            }
            Ok(py_set.into_any().unbind())
        }
        JSValue::Object(map) => {
            add_bytes(24, tracker)?;
            add_bytes(map.len().saturating_mul(size_of::<usize>() * 2), tracker)?;
            if let Some(JSValue::String(tag)) = map.get(TYPE_TAG) {
                match tag.as_str() {
                    UNDEFINED_TYPE => {
                        return super::python::get_js_undefined(py).map(Into::into);
                    }
                    DATE_TYPE => {
                        if let Some(epoch_value) = map.get(DATE_EPOCH_KEY) {
                            let epoch_ms = match epoch_value {
                                JSValue::Int(i) => *i,
                                JSValue::Float(f) if f.is_finite() => *f as i64,
                                _ => {
                                    return Err(PyRuntimeError::new_err(
                                        "Invalid epoch_ms payload for Date",
                                    ))
                                }
                            };
                            let datetime = py.import("datetime")?;
                            let datetime_cls = datetime.getattr("datetime")?;
                            let timezone = datetime.getattr("timezone")?;
                            let utc = timezone.getattr("utc")?;
                            let seconds = epoch_ms as f64 / 1000.0;
                            let py_dt = datetime_cls
                                .call_method1("fromtimestamp", (seconds, utc))?
                                .into_any()
                                .unbind();
                            return Ok(py_dt);
                        }
                    }
                    SET_TYPE => {
                        if let Some(JSValue::Array(values)) = map.get(SET_VALUES_KEY) {
                            let py_set = PySet::empty(py)?;
                            for item in values {
                                py_set
                                    .add(js_value_to_python_tracked(py, item, handle, tracker)?)?;
                            }
                            return Ok(py_set.into_any().unbind());
                        }
                    }
                    BIGINT_TYPE => {
                        if let Some(JSValue::String(text)) = map.get(BIGINT_VALUE_KEY) {
                            let value =
                                BigInt::parse_bytes(text.as_bytes(), 10).ok_or_else(|| {
                                    PyRuntimeError::new_err(
                                        "Invalid BigInt payload from JavaScript",
                                    )
                                })?;
                            let obj = value.into_pyobject(py)?;
                            return Ok(obj.into_any().unbind());
                        }
                    }
                    _ => {}
                }
            }
            let dict = PyDict::new(py);
            for (key, val) in map {
                add_bytes(key.len(), tracker)?;
                add_bytes(8, tracker)?;
                dict.set_item(key, js_value_to_python_tracked(py, val, handle, tracker)?)?;
            }
            Ok(dict.into_any().unbind())
        }
        JSValue::Date(epoch_ms) => {
            add_bytes(16, tracker)?;
            let datetime = py.import("datetime")?;
            let datetime_cls = datetime.getattr("datetime")?;
            let timezone = datetime.getattr("timezone")?;
            let utc = timezone.getattr("utc")?;
            let seconds = *epoch_ms as f64 / 1000.0;
            let py_dt = datetime_cls.call_method1("fromtimestamp", (seconds, utc))?;
            Ok(py_dt.into_any().unbind())
        }
        JSValue::Function { id } => {
            add_bytes(8, tracker)?;
            // Create JsFunction proxy
            let handle = handle.ok_or_else(|| {
                PyRuntimeError::new_err("RuntimeHandle required to convert JSValue::Function")
            })?;

            let js_fn = super::python::JsFunction::new(
                py,
                handle.clone(),
                *id,
                handle.serialization_limits(),
            )?;
            Ok(js_fn.into_any())
        }
        JSValue::JsStream { id } => {
            add_bytes(8, tracker)?;
            let handle = handle.ok_or_else(|| {
                PyRuntimeError::new_err("RuntimeHandle required to convert JSValue::JsStream")
            })?;
            let js_stream = super::python::JsStream::new(py, handle.clone(), *id)?;
            Ok(js_stream.into_any())
        }
        JSValue::PyStream { .. } => Err(PyRuntimeError::new_err(
            "PyStream placeholders cannot be materialized on the Python side",
        )),
    }
}

/// Convert a Python object into a JSValue.
///
/// This is used by the ops system to convert Python handler arguments to JSValue.
pub(crate) fn python_to_js_value(
    obj: Bound<'_, PyAny>,
    limits: &SerializationLimits,
) -> PyResult<JSValue> {
    let mut tracker = LimitTracker::new(limits.max_depth, limits.max_bytes);
    python_to_js_value_tracked(obj, &mut tracker)
}

/// Convert a Python object into a JSValue against a caller-supplied
/// [`LimitTracker`], so several conversions can share one *aggregate* budget.
///
/// Needed because `max_serialization_bytes` is meant to cap what a single call
/// transfers, not what each of its arguments transfers independently: a fresh
/// tracker per argument lets N arguments each just under the limit through, for
/// N times the intended budget.
///
/// Cycle detection (`seen`) stays per-object on purpose -- it is scoped to one
/// value's own traversal, and sharing it across arguments would misreport the
/// same object legitimately passed twice as a cycle.
pub(crate) fn python_to_js_value_tracked(
    obj: Bound<'_, PyAny>,
    tracker: &mut LimitTracker,
) -> PyResult<JSValue> {
    let mut seen: HashSet<usize> = HashSet::new();
    python_to_js_value_internal(obj, &mut seen, tracker)
}

fn python_to_js_value_internal(
    obj: Bound<'_, PyAny>,
    seen: &mut HashSet<usize>,
    tracker: &mut LimitTracker,
) -> PyResult<JSValue> {
    // Depth is the tracker's job and only the tracker's job. This function
    // used to also thread an explicit `depth` parameter and check it against
    // `limits.max_depth` -- two counters for one limit, through six recursive
    // call sites. The tracker increments *before* comparing, so it always
    // rejected one level earlier than the parameter did and the parameter's
    // check was unreachable.
    tracker.enter().map_err(runtime_error_to_py)?;

    let add_bytes = |bytes: usize, tracker: &mut LimitTracker| {
        tracker.add_bytes(bytes).map_err(runtime_error_to_py)
    };

    let py = obj.py();

    let result = if obj.is_none() {
        add_bytes(4, tracker)?;
        Ok(JSValue::Null)
    } else if obj.extract::<PyRef<super::python::JsUndefined>>().is_ok() {
        add_bytes(0, tracker)?;
        Ok(JSValue::Undefined)
    } else if let Ok(stream) = obj.extract::<PyRef<PyStreamSource>>() {
        add_bytes(size_of::<u32>(), tracker)?;
        let stream_id = stream.stream_id_for_transfer()?;
        Ok(JSValue::PyStream { id: stream_id })
    } else if let Ok(py_bytes) = obj.cast::<PyBytes>() {
        let data = py_bytes.as_bytes();
        add_bytes(data.len(), tracker)?;
        Ok(JSValue::Bytes(data.to_vec()))
    } else if let Ok(py_bytearray) = obj.cast::<PyByteArray>() {
        let data = unsafe { py_bytearray.as_bytes() };
        add_bytes(data.len(), tracker)?;
        Ok(JSValue::Bytes(data.to_vec()))
    } else if let Ok(memory_view) = obj.cast::<PyMemoryView>() {
        let bytes_obj = memory_view.call_method0(pyo3::intern!(py, "tobytes"))?;
        let data: Vec<u8> = bytes_obj.extract()?;
        add_bytes(data.len(), tracker)?;
        Ok(JSValue::Bytes(data))
    } else if let Ok(list) = obj.cast::<PyList>() {
        let ptr = list.as_ptr() as usize;
        if !seen.insert(ptr) {
            return Err(PyRuntimeError::new_err(
                "Circular reference detected while converting Python list",
            ));
        }

        add_bytes(16, tracker)?;
        add_bytes(list.len().saturating_mul(size_of::<usize>()), tracker)?;

        let mut items = Vec::with_capacity(list.len());
        for item in list.iter() {
            items.push(python_to_js_value_internal(item, seen, tracker)?);
        }
        seen.remove(&ptr);
        Ok(JSValue::Array(items))
    } else if let Ok(dict) = obj.cast::<PyDict>() {
        let ptr = dict.as_ptr() as usize;
        if !seen.insert(ptr) {
            return Err(PyRuntimeError::new_err(
                "Circular reference detected while converting Python dict",
            ));
        }

        add_bytes(24, tracker)?;
        add_bytes(dict.len().saturating_mul(size_of::<usize>() * 2), tracker)?;

        let mut map = IndexMap::with_capacity(dict.len());
        for (key, value) in dict.iter() {
            let key_str = key.extract::<String>()?;
            add_bytes(key_str.len(), tracker)?;
            add_bytes(8, tracker)?;
            map.insert(key_str, python_to_js_value_internal(value, seen, tracker)?);
        }
        seen.remove(&ptr);
        Ok(JSValue::Object(map))
    } else if let Ok(py_set) = obj.cast::<PySet>() {
        let ptr = py_set.as_ptr() as usize;
        if !seen.insert(ptr) {
            return Err(PyRuntimeError::new_err(
                "Circular reference detected while converting Python set",
            ));
        }

        add_bytes(24, tracker)?;
        add_bytes(py_set.len().saturating_mul(size_of::<usize>()), tracker)?;

        let mut items = Vec::with_capacity(py_set.len());
        for item in py_set.iter() {
            items.push(python_to_js_value_internal(item, seen, tracker)?);
        }
        seen.remove(&ptr);
        Ok(JSValue::Set(items))
    } else if let Ok(py_frozenset) = obj.cast::<PyFrozenSet>() {
        let ptr = py_frozenset.as_ptr() as usize;
        if !seen.insert(ptr) {
            return Err(PyRuntimeError::new_err(
                "Circular reference detected while converting Python frozenset",
            ));
        }

        add_bytes(24, tracker)?;
        add_bytes(
            py_frozenset.len().saturating_mul(size_of::<usize>()),
            tracker,
        )?;

        let mut items = Vec::with_capacity(py_frozenset.len());
        for item in py_frozenset.iter() {
            items.push(python_to_js_value_internal(item, seen, tracker)?);
        }
        seen.remove(&ptr);
        Ok(JSValue::Set(items))
    } else if let Ok(py_datetime) = obj.cast::<PyDateTime>() {
        add_bytes(16, tracker)?;
        let datetime_mod = py.import(pyo3::intern!(py, "datetime"))?;
        let timezone = datetime_mod.getattr(pyo3::intern!(py, "timezone"))?;
        let utc = timezone.getattr(pyo3::intern!(py, "utc"))?;

        let dt_any = py_datetime.clone().into_any();
        let offset = dt_any.call_method0(pyo3::intern!(py, "utcoffset"))?;
        let normalized = if offset.is_none() {
            let kwargs = PyDict::new(py);
            kwargs.set_item(pyo3::intern!(py, "tzinfo"), utc)?;
            dt_any.call_method(pyo3::intern!(py, "replace"), (), Some(&kwargs))?
        } else {
            dt_any.call_method1(pyo3::intern!(py, "astimezone"), (utc,))?
        };

        let timestamp = normalized
            .call_method0(pyo3::intern!(py, "timestamp"))?
            .extract::<f64>()?;
        if !timestamp.is_finite() {
            return Err(PyRuntimeError::new_err(
                "datetime.timestamp returned non-finite value",
            ));
        }
        let epoch_ms = timestamp * 1000.0;
        if !epoch_ms.is_finite() || epoch_ms < i64::MIN as f64 || epoch_ms > i64::MAX as f64 {
            return Err(PyRuntimeError::new_err(
                "Datetime value out of range for JavaScript Date",
            ));
        }
        Ok(JSValue::Date(epoch_ms.round() as i64))
    } else if let Ok(b) = obj.extract::<bool>() {
        add_bytes(1, tracker)?;
        Ok(JSValue::Bool(b))
    } else if let Ok(i) = obj.extract::<i64>() {
        add_bytes(size_of::<i64>(), tracker)?;
        Ok(JSValue::Int(i))
    } else if let Ok(bigint) = obj.extract::<BigInt>() {
        let (_, magnitude) = bigint.to_bytes_le();
        add_bytes(magnitude.len(), tracker)?;
        Ok(JSValue::BigInt(bigint))
    } else if let Ok(f) = obj.extract::<f64>() {
        add_bytes(size_of::<f64>(), tracker)?;
        Ok(JSValue::Float(f))
    } else if let Ok(s) = obj.extract::<String>() {
        if s.len() > tracker.max_bytes() {
            return Err(PyRuntimeError::new_err(byte_limit_message(
                s.len(),
                tracker.max_bytes(),
            )));
        }
        add_bytes(s.len(), tracker)?;
        add_bytes(16, tracker)?;
        Ok(JSValue::String(s))
    } else if let Ok(js_fn) = obj.extract::<PyRef<super::python::JsFunction>>() {
        // JsFunction proxy - extract the function ID for round-trip
        // This validates that the function is not closed and runtime is alive
        let id = js_fn.function_id_for_transfer()?;
        add_bytes(8, tracker)?;
        Ok(JSValue::Function { id })
    } else {
        Err(PyRuntimeError::new_err(
            "Unsupported Python type for JSValue conversion",
        ))
    };

    tracker.exit();
    result
}
