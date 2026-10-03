//! Snapshot builder bindings surfaced as `pydeno.SnapshotBuilder`.
use crate::runtime::snapshot::{SnapshotBuilder, SnapshotBuilderConfig};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use super::error::runtime_error_to_py;

#[pyclass(name = "SnapshotBuilder", module = "pydeno", unsendable)]
pub struct SnapshotBuilderPy {
    builder: std::cell::RefCell<Option<SnapshotBuilder>>,
}

#[pymethods]
impl SnapshotBuilderPy {
    #[new]
    #[pyo3(signature = (bootstrap = None, enable_console = Some(false)))]
    fn new(bootstrap: Option<String>, enable_console: Option<bool>) -> PyResult<Self> {
        let config = SnapshotBuilderConfig {
            bootstrap_script: bootstrap,
            enable_console,
        };
        let builder = SnapshotBuilder::new(config).map_err(runtime_error_to_py)?;
        Ok(Self {
            builder: std::cell::RefCell::new(Some(builder)),
        })
    }

    fn execute_script(&self, name: &str, source: &str) -> PyResult<()> {
        self.builder
            .borrow_mut()
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("Snapshot already built"))?
            .execute_script(name, source)
            .map_err(runtime_error_to_py)
    }

    fn build(&self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        let builder = self
            .builder
            .borrow_mut()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("Snapshot already built"))?;
        let bytes = builder.build().map_err(runtime_error_to_py)?;
        Ok(PyBytes::new(py, &bytes).into())
    }
}
