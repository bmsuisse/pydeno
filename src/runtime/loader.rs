//! Module loader that delegates resolution and loading to Python callables.

use deno_core::{
    ModuleLoadOptions, ModuleLoadReferrer, ModuleLoader, ModuleSource, ModuleSourceCode,
    ModuleSpecifier, ModuleType, RequestedModuleType,
};
use deno_error::JsErrorBox;
use futures::FutureExt;
use pyo3::prelude::*;
use pyo3_async_runtimes::TaskLocals;
use std::cell::RefCell;

/// Internal state for the Python module loader.
#[derive(Default)]
struct LoaderInner {
    /// Pre-registered static modules (name -> source).
    static_modules: std::collections::HashMap<String, String>,
    /// Optional Python resolver callable.
    resolver: Option<Py<PyAny>>,
    /// Optional Python loader callable.
    loader: Option<Py<PyAny>>,
}

/// Module loader that delegates resolution and loading to Python callables.
///
/// Implements `deno_core::ModuleLoader` to enable custom module resolution and loading
/// logic written in Python. Supports static modules registered at runtime and dynamic
/// resolution via Python callbacks.
pub struct PythonModuleLoader {
    /// Internal state (static modules, resolver/loader callbacks).
    inner: RefCell<LoaderInner>,
    /// Asyncio task locals for async module loading.
    task_locals: RefCell<Option<TaskLocals>>,
}

impl PythonModuleLoader {
    /// Create a new Python module loader.
    pub fn new() -> Self {
        Self {
            inner: RefCell::default(),
            task_locals: RefCell::default(),
        }
    }

    /// Set a Python resolver callable for custom module resolution.
    ///
    /// The resolver receives `(specifier, referrer)` and returns a resolved URL string
    /// or `None` to fall back to static modules.
    pub fn set_resolver(&self, resolver: Py<PyAny>) {
        self.inner.borrow_mut().resolver = Some(resolver);
    }

    /// Set a Python loader callable for fetching module source.
    ///
    /// The loader receives a resolved specifier and returns the module source code.
    pub fn set_loader(&self, loader: Py<PyAny>) {
        self.inner.borrow_mut().loader = Some(loader);
    }

    /// Register a static module with pre-defined source.
    ///
    /// Static modules are resolved using `pydeno://static/<name>` URLs and do not
    /// require a custom loader.
    pub fn add_static_module(&self, name: String, source: String) {
        self.inner.borrow_mut().static_modules.insert(name, source);
    }

    /// Set asyncio task locals for async module loading.
    ///
    /// Required when using async module loaders to provide the event loop context.
    pub fn set_task_locals(&self, task_locals: TaskLocals) {
        *self.task_locals.borrow_mut() = Some(task_locals);
    }

    /// Clear the asyncio task locals.
    pub fn clear_task_locals(&self) {
        *self.task_locals.borrow_mut() = None;
    }

    /// Resolve a registered static module (optionally `static:`-prefixed) to
    /// its `pydeno://static/<name>` URL; `None` if not registered.
    fn resolve_static(&self, specifier: &str) -> Option<Result<ModuleSpecifier, JsErrorBox>> {
        let bare = specifier.strip_prefix("static:").unwrap_or(specifier);
        self.inner
            .borrow()
            .static_modules
            .contains_key(bare)
            .then(|| parse_url(&format!("pydeno://static/{bare}")))
    }

    fn module_type_from_request(requested: &RequestedModuleType) -> ModuleType {
        match requested {
            RequestedModuleType::Json => ModuleType::Json,
            RequestedModuleType::Text => ModuleType::Text,
            RequestedModuleType::Bytes => ModuleType::Bytes,
            RequestedModuleType::Other(ty) => ModuleType::Other(ty.clone()),
            _ => ModuleType::JavaScript,
        }
    }
}

fn parse_url(url: &str) -> Result<ModuleSpecifier, JsErrorBox> {
    ModuleSpecifier::parse(url).map_err(|e| JsErrorBox::new("URIError", e.to_string()))
}

/// Call the Python loader; returns its result and whether it is a coroutine.
fn call_loader(loader: &Py<PyAny>, specifier: &str) -> Result<(Py<PyAny>, bool), JsErrorBox> {
    Python::attach(|py| {
        let result = loader
            .bind(py)
            .call1((specifier,))
            .map_err(|e| JsErrorBox::generic(format!("Failed to call module loader: {e}")))?;
        let inspect = py
            .import("inspect")
            .map_err(|e| JsErrorBox::generic(format!("Failed to import inspect module: {e}")))?;
        let is_coroutine = inspect
            .call_method1("iscoroutine", (&result,))
            .map_err(|e| {
                JsErrorBox::generic(format!("Failed to check if result is coroutine: {e}"))
            })?
            .extract::<bool>()
            .map_err(|e| {
                JsErrorBox::generic(format!(
                    "Failed to extract boolean from iscoroutine check: {e}"
                ))
            })?;
        Ok((result.unbind(), is_coroutine))
    })
}

impl ModuleLoader for PythonModuleLoader {
    /// Resolve via the Python resolver if set (`None`/`""` falls back to static
    /// modules), otherwise only static modules are allowed.
    fn resolve(
        &self,
        specifier: &str,
        referrer: &str,
        _kind: deno_core::ResolutionKind,
    ) -> Result<deno_core::url::Url, JsErrorBox> {
        let specifier = specifier
            .strip_prefix("pydeno://runtime/")
            .unwrap_or(specifier);
        let static_or_deny = |why: &str| {
            self.resolve_static(specifier).unwrap_or_else(|| {
                Err(JsErrorBox::generic(format!(
                    "Module resolution denied for {specifier}. {why}"
                )))
            })
        };

        let inner = self.inner.borrow();
        let Some(resolver) = &inner.resolver else {
            return static_or_deny("Did you call add_static_module()?");
        };
        Python::attach(|py| {
            let result = resolver.call1(py, (specifier, referrer)).map_err(|e| {
                JsErrorBox::generic(format!("Module resolution failed for {specifier}: {e}"))
            })?;
            if result.is_none(py) {
                return static_or_deny("Resolver returned None and no static module was found.");
            }
            let resolved = result.extract::<String>(py).map_err(|e| {
                JsErrorBox::type_error(format!("Module resolver must return a string or None: {e}"))
            })?;
            if resolved.is_empty() {
                return static_or_deny(
                    "Resolver returned empty string and no static module was found.",
                );
            }
            parse_url(&resolved)
        })
    }

    /// Load static modules from the registry, else via the Python loader (sync
    /// or, when task locals are set, async); error if no loader is set.
    fn load(
        &self,
        module_specifier: &ModuleSpecifier,
        _maybe_referrer: Option<&ModuleLoadReferrer>,
        options: ModuleLoadOptions,
    ) -> deno_core::ModuleLoadResponse {
        let specifier = module_specifier.as_str();
        let module_type = Self::module_type_from_request(&options.requested_module_type);
        let inner = self.inner.borrow();

        if let Some(source) = specifier
            .strip_prefix("pydeno://static/")
            .and_then(|name| inner.static_modules.get(name))
        {
            let module = ModuleSource::new(
                module_type,
                ModuleSourceCode::String(source.clone().into()),
                module_specifier,
                None,
            );
            return deno_core::ModuleLoadResponse::Async(async move { Ok(module) }.boxed_local());
        }

        let loader = inner
            .loader
            .as_ref()
            .map(|l| Python::attach(|py| l.clone_ref(py)));
        drop(inner);

        let Some(loader) = loader else {
            return deno_core::ModuleLoadResponse::Sync(Err(JsErrorBox::generic(format!(
                "Module loading denied for {specifier}. Did you call set_module_loader()?"
            ))));
        };
        let task_locals = self.task_locals.borrow().clone();
        let specifier = specifier.to_string();
        let module_specifier = module_specifier.clone();

        deno_core::ModuleLoadResponse::Async(Box::pin(async move {
            let (result, is_coroutine) = call_loader(&loader, &specifier)?;
            let source_obj = match (&task_locals, is_coroutine) {
                (_, false) => result,
                (Some(locals), true) => {
                    let py_future = Python::attach(|py| {
                        pyo3_async_runtimes::into_future_with_locals(locals, result.into_bound(py))
                            .map_err(|e| {
                                JsErrorBox::generic(format!("Failed to create async future: {e}"))
                            })
                    })?;
                    py_future
                        .await
                        .map_err(|e| JsErrorBox::generic(format!("Module loader failed: {e}")))?
                }
                (None, true) => {
                    // Close the coroutine to prevent a "never awaited" warning.
                    Python::attach(|py| {
                        let _ = result.call_method0(py, "close");
                    });
                    return Err(JsErrorBox::type_error(
                        "An async module loader cannot be used with a synchronous evaluation (eval_module). Use eval_module_async() instead.",
                    ));
                }
            };
            let source: String = Python::attach(|py| {
                source_obj.extract(py).map_err(|e| {
                    JsErrorBox::type_error(format!("Module loader must return a string: {e}"))
                })
            })?;
            Ok(ModuleSource::new(
                module_type,
                ModuleSourceCode::String(source.into()),
                &module_specifier,
                None,
            ))
        }))
    }
}
