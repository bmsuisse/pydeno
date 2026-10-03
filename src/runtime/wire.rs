//! The value codec of the `IsolatedRuntime` wire protocol, in Rust.
//!
//! The decoding half of the value codec: tag rules, budgets and the hash-flood check. Encoding
//! lives in `wire_json.rs`, fused with writing the bytes. The readable specification of both is
//! `tests/wire_reference.py`; moving a structured result across the boundary walks every node
//! once here instead of twice in Python.
//!
//! The reference and this must agree exactly, errors included: `tests/test_wire_native.py` runs
//! them against each other. Typed values (`int`, bytes, datetimes) call back into Python's own `int`, `base64` and
//! `datetime`, so their edge cases (underscores in digits, ISO formats, base64 padding) cannot
//! drift from the reference.
//!
//! A hostile peer controls every byte this decodes, so the budgets are the same: nodes, depth, and
//! the hash-collision flood check for sets and dictionaries.

use std::cell::Cell;
use std::collections::HashMap;

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PySet, PySlice, PyString};

pyo3::create_exception!(_pydeno, WireNativeError, PyException);

const SAFE_INT: i64 = 1 << 53;
const MAX_HASH_REPEATS: usize = 16;

pub(crate) fn fail<T>(message: impl Into<String>) -> PyResult<T> {
    Err(WireNativeError::new_err(message.into()))
}

// ---------------------------------------------------------------------------------------------
// decode
// ---------------------------------------------------------------------------------------------

pub(crate) struct Decoder<'py> {
    pub(crate) py: Python<'py>,
    pub(crate) max_depth: usize,
    nodes_left: Cell<i64>,
    undefined: Bound<'py, PyAny>,
    base64: Bound<'py, PyModule>,
    datetime: Bound<'py, PyAny>,
}

enum Collection {
    Set,
    Dict,
}

impl Collection {
    fn unhashable(&self) -> &'static str {
        match self {
            Collection::Set => "unhashable set member",
            Collection::Dict => "unhashable dict key",
        }
    }
}

impl<'py> Decoder<'py> {
    pub(crate) fn new(py: Python<'py>, max_depth: usize, max_nodes: i64) -> PyResult<Self> {
        Ok(Decoder {
            py,
            max_depth,
            nodes_left: Cell::new(max_nodes),
            undefined: super::python::get_js_undefined(py)?
                .into_bound(py)
                .into_any(),
            base64: py.import("base64")?,
            datetime: py.import("datetime")?.getattr("datetime")?,
        })
    }

    /// Count one node against the budget.
    pub(crate) fn spend(&self) -> PyResult<()> {
        self.nodes_left.set(self.nodes_left.get() - 1);
        if self.nodes_left.get() < 0 {
            return fail("value has too many nodes");
        }
        Ok(())
    }

    /// How many distinct values may share one hash inside a set or a non-string-keyed dict. A
    /// hostile peer can pick integers that all hash alike, which makes building the set quadratic.
    fn reject_hash_flood(&self, values: &[Bound<'py, PyAny>], kind: &Collection) -> PyResult<()> {
        if values.len() <= MAX_HASH_REPEATS {
            return Ok(());
        }
        let mut seen: HashMap<isize, usize> = HashMap::new();
        for value in values {
            let h = value.hash().or_else(|_| fail(kind.unhashable()))?;
            let count = seen.entry(h).or_insert(0);
            *count += 1;
            if *count > MAX_HASH_REPEATS {
                return fail("too many values in a collection share a hash");
            }
        }
        Ok(())
    }

    pub(crate) fn decode(
        &self,
        node: &Bound<'py, PyAny>,
        depth: usize,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = self.py;
        self.spend()?;
        if depth > self.max_depth {
            return fail("value nested too deeply");
        }
        if node.is_none() || node.is_instance_of::<PyBool>() || node.is_instance_of::<PyString>() {
            return Ok(node.clone());
        }
        if node.is_instance_of::<PyInt>() {
            // A plain JSON number past 2**53 is never what the encoder sends (it tags those), and
            // accepting it would let a peer smuggle in integers chosen to collide in a hash table.
            return match node.extract::<i64>() {
                Ok(n) if (-SAFE_INT..=SAFE_INT).contains(&n) => Ok(node.clone()),
                _ => fail("integer outside the safe range must be tagged"),
            };
        }
        if node.is_instance_of::<PyFloat>() {
            return Ok(node.clone());
        }
        if let Ok(list) = node.cast::<PyList>() {
            let out = PyList::empty(py);
            for item in list.iter() {
                out.append(self.decode(&item, depth + 1)?)?;
            }
            return Ok(out.into_any());
        }
        let Ok(dict) = node.cast::<PyDict>() else {
            return fail("unexpected JSON node");
        };
        let Some(tag) = dict.get_item("$")? else {
            let out = PyDict::new(py);
            for (k, v) in dict.iter() {
                out.set_item(k, self.decode(&v, depth + 1)?)?;
            }
            return Ok(out.into_any());
        };
        let Ok(tag) = tag.cast_into::<PyString>() else {
            return fail("malformed tagged value");
        };
        let name = tag.to_string_lossy().into_owned();
        if name == "u" && dict.len() == 1 {
            return Ok(self.undefined.clone());
        }
        let Some(payload) = (if dict.len() == 2 {
            dict.get_item("v")?
        } else {
            None
        }) else {
            return fail("malformed tagged value");
        };
        self.decode_tagged(&tag, &name, &payload, depth)
    }

    fn decode_tagged(
        &self,
        tag: &Bound<'py, PyString>,
        name: &str,
        payload: &Bound<'py, PyAny>,
        depth: usize,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = self.py;
        let text = payload.cast::<PyString>().ok();
        match name {
            "int" => {
                let Some(text) = text.filter(|t| t.len().map(|n| n <= 4096).unwrap_or(false))
                else {
                    return fail("bad int payload");
                };
                py.get_type::<PyInt>()
                    .call1((text,))
                    .or_else(|_| fail("bad int payload"))
            }
            "f" => {
                let value = match text.map(|t| t.to_string_lossy().into_owned()).as_deref() {
                    Some("nan") => f64::NAN,
                    Some("inf") => f64::INFINITY,
                    Some("-inf") => f64::NEG_INFINITY,
                    Some("-0") => -0.0,
                    _ => return fail("bad float payload"),
                };
                Ok(PyFloat::new(py, value).into_any())
            }
            "b" => {
                let Some(text) = text else {
                    return fail("bad bytes payload");
                };
                let kwargs = PyDict::new(py);
                kwargs.set_item("validate", true)?;
                self.base64
                    .call_method("b64decode", (text,), Some(&kwargs))
                    .or_else(|_| fail("bad bytes payload"))
            }
            "dt" => {
                let Some(text) = text.filter(|t| t.len().map(|n| n <= 64).unwrap_or(false)) else {
                    return fail("bad datetime payload");
                };
                self.datetime
                    .call_method1("fromisoformat", (text,))
                    .or_else(|_| fail("bad datetime payload"))
            }
            "set" => {
                let Ok(items) = payload.cast::<PyList>() else {
                    return fail("bad set payload");
                };
                let mut members = Vec::with_capacity(items.len());
                for item in items.iter() {
                    members.push(self.decode(&item, depth + 1)?);
                }
                self.reject_hash_flood(&members, &Collection::Set)?;
                PySet::new(py, &members)
                    .map(|s| s.into_any())
                    .or_else(|_| fail(Collection::Set.unhashable()))
            }
            "d" => {
                let Ok(items) = payload.cast::<PyList>() else {
                    return fail("bad dict payload");
                };
                let mut keys = Vec::with_capacity(items.len());
                let mut values = Vec::with_capacity(items.len());
                for pair in items.iter() {
                    let Ok(pair) = pair.cast_into::<PyList>() else {
                        return fail("bad dict entry");
                    };
                    if pair.len() != 2 {
                        return fail("bad dict entry");
                    }
                    keys.push(self.decode(&pair.get_item(0)?, depth + 1)?);
                    values.push(self.decode(&pair.get_item(1)?, depth + 1)?);
                }
                self.reject_hash_flood(&keys, &Collection::Dict)?;
                let out = PyDict::new(py);
                for (k, v) in keys.iter().zip(values.iter()) {
                    out.set_item(k, v)
                        .or_else(|_| fail(Collection::Dict.unhashable()))?;
                }
                Ok(out.into_any())
            }
            _ => {
                let head = tag.get_item(PySlice::new(py, 0, 32, 1))?;
                fail(format!("unknown tag {}", head.repr()?))
            }
        }
    }
}

/// Decode several values under ONE shared node budget, so a peer cannot multiply the node limit by
/// the number of arguments. Mirrors `_wire.decode_values`.
#[pyfunction]
pub fn _wire_decode_values<'py>(
    nodes: &Bound<'py, PyList>,
    max_nodes: i64,
    max_depth: usize,
) -> PyResult<Bound<'py, PyList>> {
    let py = nodes.py();
    let decoder = Decoder::new(py, max_depth, max_nodes)?;
    let out = PyList::empty(py);
    for node in nodes.iter() {
        out.append(decoder.decode(&node, 0)?)?;
    }
    Ok(out)
}
