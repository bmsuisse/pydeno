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

/// Convert a JSValue into a Python object (`handle` is needed for functions and
/// streams). Applies **no** limits: eval results were already metered when read
/// out of V8. Guest-supplied input (op arguments) must use
/// [`js_value_to_python_tracked`].
pub(crate) fn js_value_to_python(
    py: Python<'_>,
    value: &JSValue,
    handle: Option<&super::handle::RuntimeHandle>,
) -> PyResult<Py<PyAny>> {
    let mut unlimited = LimitTracker::new(usize::MAX, usize::MAX);
    js_value_to_python_tracked(py, value, handle, &mut unlimited)
}

/// Convert a JSValue into a Python object against a caller-supplied
/// [`LimitTracker`]; ops share one tracker across all arguments so the limits
/// bind the whole call, not each argument (mirror of
/// [`python_to_js_value_tracked`]).
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
            js_items_to_pyset(py, items, handle, tracker)
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
                            return epoch_ms_to_datetime(py, epoch_ms);
                        }
                    }
                    SET_TYPE => {
                        if let Some(JSValue::Array(values)) = map.get(SET_VALUES_KEY) {
                            return js_items_to_pyset(py, values, handle, tracker);
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
            epoch_ms_to_datetime(py, *epoch_ms)
        }
        JSValue::Function { id } => {
            add_bytes(8, tracker)?;
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

fn epoch_ms_to_datetime(py: Python<'_>, epoch_ms: i64) -> PyResult<Py<PyAny>> {
    let datetime = py.import("datetime")?;
    let utc = datetime.getattr("timezone")?.getattr("utc")?;
    let seconds = epoch_ms as f64 / 1000.0;
    Ok(datetime
        .getattr("datetime")?
        .call_method1("fromtimestamp", (seconds, utc))?
        .unbind())
}

fn js_items_to_pyset(
    py: Python<'_>,
    items: &[JSValue],
    handle: Option<&super::handle::RuntimeHandle>,
    tracker: &mut LimitTracker,
) -> PyResult<Py<PyAny>> {
    let py_set = PySet::empty(py)?;
    for item in items {
        py_set.add(js_value_to_python_tracked(py, item, handle, tracker)?)?;
    }
    Ok(py_set.into_any().unbind())
}

/// Convert a Python object into a JSValue under `limits`.
pub(crate) fn python_to_js_value(
    obj: Bound<'_, PyAny>,
    limits: &SerializationLimits,
) -> PyResult<JSValue> {
    let mut tracker = LimitTracker::new(limits.max_depth, limits.max_bytes);
    python_to_js_value_tracked(obj, &mut tracker)
}

/// Convert a Python object into a JSValue against a caller-supplied
/// [`LimitTracker`], so all arguments of one call share one byte budget. Cycle
/// detection stays per value: the same object passed twice is not a cycle.
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
        py_items_to_js(&obj, "list", 16, list.len(), list.iter(), seen, tracker).map(JSValue::Array)
    } else if let Ok(dict) = obj.cast::<PyDict>() {
        let ptr = enter_container(&obj, "dict", seen)?;
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
    } else if let Ok(set) = obj.cast::<PySet>() {
        py_items_to_js(&obj, "set", 24, set.len(), set.iter(), seen, tracker).map(JSValue::Set)
    } else if let Ok(set) = obj.cast::<PyFrozenSet>() {
        py_items_to_js(&obj, "frozenset", 24, set.len(), set.iter(), seen, tracker)
            .map(JSValue::Set)
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
        // Validates the function is open and its runtime alive.
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

/// Mark a container as being visited; errors if it is already on the path.
fn enter_container(
    obj: &Bound<'_, PyAny>,
    kind: &str,
    seen: &mut HashSet<usize>,
) -> PyResult<usize> {
    let ptr = obj.as_ptr() as usize;
    if !seen.insert(ptr) {
        return Err(PyRuntimeError::new_err(format!(
            "Circular reference detected while converting Python {kind}"
        )));
    }
    Ok(ptr)
}

/// Convert the items of a list/set/frozenset, metering `header` bytes plus one
/// pointer per item.
fn py_items_to_js<'py>(
    obj: &Bound<'py, PyAny>,
    kind: &str,
    header: usize,
    len: usize,
    iter: impl Iterator<Item = Bound<'py, PyAny>>,
    seen: &mut HashSet<usize>,
    tracker: &mut LimitTracker,
) -> PyResult<Vec<JSValue>> {
    let ptr = enter_container(obj, kind, seen)?;
    let meter =
        |tracker: &mut LimitTracker, bytes| tracker.add_bytes(bytes).map_err(runtime_error_to_py);
    meter(tracker, header)?;
    meter(tracker, len.saturating_mul(size_of::<usize>()))?;
    let mut items = Vec::with_capacity(len);
    for item in iter {
        items.push(python_to_js_value_internal(item, seen, tracker)?);
    }
    seen.remove(&ptr);
    Ok(items)
}
