//! Python op registry and deno_core integration.
//!
//! This module exposes two ops (`op_peno_call_python_sync` and
//! `op_peno_call_python_async`) that bridge JavaScript calls into Python
//! handlers. Python handlers are registered dynamically at runtime and
//! addressed by a **capability token**.
//!
//! # The op boundary is a capability, not an ambient name
//!
//! Until v0.2.0 an op id was a small integer allocated sequentially from
//! zero, and dispatch resolved any registered id. That made the registry an
//! *ambient* capability: guest JS could call `__host_op_sync__(0, ...)` and
//! reach a handler that had never been put in its scope, so `ToolBridge`'s
//! namespacing was decorative and its call budget survived only because the
//! budgeted shim happened to be the thing registered. Two `ToolBridge`es with
//! different trust levels on one `Runtime` were one trust level.
//!
//! Two changes make the boundary real, and they are deliberately layered:
//!
//! 1. **Unguessable tokens.** [`PythonOpRegistry::register`] draws a token
//!    uniformly at random from `1..2^53` (a CSPRNG via `uuid::Uuid::new_v4`;
//!    `2^53` because the token crosses the ABI as a JS number and must be
//!    exactly representable). Knowing the token *is* the capability. Guessing
//!    one is ~2^53 work, and sequential enumeration -- the actual escape the
//!    review demonstrated -- finds nothing.
//! 2. **An explicit allowlist established at bind time.** Registering an op
//!    does not make it callable; [`PythonOpRegistry::expose`] does, and the
//!    bind paths call it only *after* the binding has actually been installed
//!    in the guest's scope. So an op whose binding failed half-way, or one
//!    registered for host-side use only, is not dispatchable at all.
//!
//! [`PythonOpRegistry::revoke`] reverses both, which is what makes
//! `Runtime.revoke_op` (and `ToolBridge.detach`) a real revocation rather
//! than a hidden global that still works.
//!
//! # Nothing that describes host internals crosses into guest JS
//!
//! Op ids resolve to handler *names* on the host side only. Guest-visible
//! failures are name-free and, for anything that would describe a host data
//! structure, a single opaque string ([`OPAQUE_INTERNAL_ERROR`]) with the
//! detail logged host-side. `deno_core`'s own argument-deserialization errors
//! (`serde_v8 error: ...`) are scrubbed in the bridge JS, which is the only
//! chokepoint above them -- see `sanitizeHostError` in the bridge source.

use crate::runtime::conversion::{js_value_to_python_tracked, python_to_js_value};
use crate::runtime::js_value::{JSValue, LimitTracker, SerializationLimits};
use crate::runtime::stream::PyStreamRegistry;
use deno_core::ascii_str;
use deno_core::op2;
use deno_core::Extension;
use deno_core::ExtensionFileSource;
use deno_core::OpState;
use deno_error::JsErrorBox;
use pyo3::prelude::*;
use pyo3::types::PyTuple;
use pyo3_async_runtimes::TaskLocals;
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};

/// Execution mode for Python op handlers.
///
/// Determines whether the Python callable is synchronous or asynchronous.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PythonOpMode {
    /// Synchronous Python function (returns value directly).
    Sync,
    /// Asynchronous Python function (returns awaitable/coroutine).
    Async,
}

/// An op capability token: an unguessable identifier for one registered
/// Python handler.
///
/// It crosses the JS ABI as a plain number, so it is bounded by
/// [`MAX_OP_TOKEN`] to stay exactly representable as an IEEE-754 double.
pub type OpToken = u64;

/// Largest value an [`OpToken`] may take: `2^53 - 1`, the last integer a JS
/// number represents exactly.
pub const MAX_OP_TOKEN: OpToken = (1 << 53) - 1;

/// Draw a fresh capability token from a CSPRNG.
///
/// `uuid` v4 is already a dependency and is documented to use the OS entropy
/// source, so this avoids adding `rand` for eight bytes.
fn random_op_token() -> OpToken {
    loop {
        let bytes = *uuid::Uuid::new_v4().as_bytes();
        let raw = u64::from_le_bytes(bytes[..8].try_into().expect("8 bytes"));
        let token = raw & MAX_OP_TOKEN;
        // Zero is excluded so that a forged `__host_op_sync__(0, ...)` -- the
        // escape the v0.2.0 review demonstrated -- can never be a live token.
        if token != 0 {
            return token;
        }
    }
}

/// Guest-facing message for any failure that would otherwise describe a host
/// data structure by name (`OpState`, `PyStreamRegistry`, ...). The real
/// cause is logged host-side; the guest cannot act on it either way.
pub const OPAQUE_INTERNAL_ERROR: &str = "Host call failed: internal runtime error";

/// Guest-facing message for a token that is not a live, exposed capability --
/// unknown, never exposed, revoked, or malformed. Deliberately uniform, so it
/// reveals neither the existence nor the name of anything.
const UNKNOWN_OP_ERROR: &str = "Unknown host op";

fn opaque_internal_error(detail: &str) -> JsErrorBox {
    log::error!("peno internal op failure: {detail}");
    JsErrorBox::type_error(OPAQUE_INTERNAL_ERROR)
}

fn unknown_op_error() -> JsErrorBox {
    JsErrorBox::type_error(UNKNOWN_OP_ERROR)
}

/// Validate the raw number guest JS passed as an op token.
fn token_from_js(raw: f64) -> Result<OpToken, JsErrorBox> {
    if !raw.is_finite() || raw.fract() != 0.0 || raw < 1.0 || raw > MAX_OP_TOKEN as f64 {
        return Err(unknown_op_error());
    }
    Ok(raw as OpToken)
}

/// Metadata for a registered Python op.
///
/// Stores the handler callable and its execution mode for op invocations from JavaScript.
pub struct PythonOpEntry {
    /// Unguessable capability token for this op.
    pub id: OpToken,
    /// Human-readable op name.
    pub name: String,
    /// Sync or async execution mode.
    pub mode: PythonOpMode,
    /// Python callable (function or bound method).
    pub handler: Py<PyAny>,
}

/// Global asyncio task locals for all async ops in this runtime.
#[derive(Clone)]
pub struct GlobalTaskLocals(pub Option<TaskLocals>);

impl Clone for PythonOpEntry {
    fn clone(&self) -> Self {
        Python::attach(|py| Self {
            id: self.id,
            name: self.name.clone(),
            mode: self.mode,
            handler: self.handler.clone_ref(py),
        })
    }
}

#[derive(Default)]
struct PythonOpRegistryInner {
    handlers: Mutex<HashMap<OpToken, PythonOpEntry>>,
    /// Tokens that a bind step has actually installed in the guest's scope.
    /// Dispatch consults this, not `handlers`.
    exposed: Mutex<HashSet<OpToken>>,
}

/// Thread-safe registry of Python operations.
///
/// Manages dynamic registration and lookup of Python callables that can be invoked
/// from JavaScript via `op_peno_call_python_sync` and `op_peno_call_python_async`.
#[derive(Clone, Default)]
pub struct PythonOpRegistry {
    inner: Arc<PythonOpRegistryInner>,
}

impl PythonOpRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Register a Python callable as an op and return its capability token.
    ///
    /// The op is **not** callable from JavaScript yet: dispatch is gated on
    /// [`Self::expose`], which the bind paths call once the binding is
    /// actually installed in the guest's scope.
    pub fn register(&self, name: String, mode: PythonOpMode, handler: Py<PyAny>) -> OpToken {
        let mut handlers = self.inner.handlers.lock().unwrap();
        let mut id = random_op_token();
        while handlers.contains_key(&id) {
            id = random_op_token();
        }
        handlers.insert(
            id,
            PythonOpEntry {
                id,
                name,
                mode,
                handler,
            },
        );
        id
    }

    /// Make a registered op dispatchable from JavaScript.
    ///
    /// Returns `false` if the token is not registered (or was revoked), in
    /// which case nothing is exposed.
    pub fn expose(&self, id: OpToken) -> bool {
        if !self.inner.handlers.lock().unwrap().contains_key(&id) {
            return false;
        }
        self.inner.exposed.lock().unwrap().insert(id);
        true
    }

    /// Revoke a capability: drop the handler and its exposure.
    ///
    /// Returns `false` if the token was not registered. After this, guest JS
    /// calling the token gets the same uniform "Unknown host op" it would get
    /// for any other value, so a global left behind by `bind_function`
    /// becomes inert rather than quietly still working.
    pub fn revoke(&self, id: OpToken) -> bool {
        self.inner.exposed.lock().unwrap().remove(&id);
        self.inner.handlers.lock().unwrap().remove(&id).is_some()
    }

    /// Resolve a token to its handler, but only if a bind step exposed it.
    pub fn get_exposed(&self, id: OpToken) -> Option<PythonOpEntry> {
        if !self.inner.exposed.lock().unwrap().contains(&id) {
            return None;
        }
        self.inner.handlers.lock().unwrap().get(&id).cloned()
    }
}

fn lookup_entry(op_state: &mut OpState, raw_token: f64) -> Result<PythonOpEntry, JsErrorBox> {
    let registry = op_state
        .try_borrow::<PythonOpRegistry>()
        .ok_or_else(|| opaque_internal_error("PythonOpRegistry missing from OpState"))?
        .clone();

    // One uniform failure for "malformed", "unknown", "not exposed" and
    // "revoked": distinguishing them would let a guest enumerate the
    // registry, which is exactly what this design removes.
    registry
        .get_exposed(token_from_js(raw_token)?)
        .ok_or_else(unknown_op_error)
}

/// Convert one call's guest-supplied arguments to Python against a single
/// shared budget.
///
/// One tracker for the whole list, not one per argument: `max_serialization_bytes`
/// caps what a *call* transfers. See [`js_value_to_python_tracked`].
fn convert_args(
    py: Python<'_>,
    args: &[JSValue],
    limits: &SerializationLimits,
) -> Result<Vec<Py<PyAny>>, JsErrorBox> {
    let mut tracker = LimitTracker::new(limits.max_depth, limits.max_bytes);
    args.iter()
        .map(|arg| js_value_to_python_tracked(py, arg, None, &mut tracker).map_err(map_pyerr))
        .collect()
}

/// Marker byte prefixed onto op error messages that carry a Python exception
/// class name, so the JS bridge shim can restore it onto `error.name`.
///
/// Why a marker instead of `JsErrorBox::new(class, message)`: `deno_core`
/// routes op errors through `to_v8_error` -> the JS `Deno.core.buildCustomError`
/// callback, which looks the class up in an `errorMap` of *registered* classes
/// (`Error`, `RangeError`, `TypeError`, ...). An unregistered class name -- and
/// every Python exception class is unregistered -- makes `buildCustomError`
/// return `undefined`, so guest JS ends up catching a literal `undefined`
/// rather than an `Error`. Verified empirically against `deno_core` 0.409, not
/// just read off its source. So the class name travels inside the message of a
/// plain `TypeError`, and `restoreHostError` in the bridge JS lifts it back out
/// onto a real `Error` with `name`/`message` set -- the same shape
/// `build_js_exception` (src/runtime/python/error.rs) already gives Python.
///
/// U+0001 cannot appear in a Python class name, and is effectively invisible if
/// a message ever escapes without being un-marked; what follows it is still the
/// readable `"ClassName: message"` form.
pub(crate) const HOST_ERROR_MARKER: char = '\u{1}';

/// Convert a Python exception raised inside a host callback into a JS-visible
/// error, preserving the exception's class name (see [`HOST_ERROR_MARKER`]).
fn map_pyerr(err: PyErr) -> JsErrorBox {
    let (class, message) = Python::attach(|py| {
        let class = err
            .get_type(py)
            .name()
            .map(|name| name.to_string())
            .unwrap_or_else(|_| "Error".to_string());
        (class, err.value(py).to_string())
    });
    JsErrorBox::type_error(format!("{HOST_ERROR_MARKER}{class}: {message}"))
}

/// Deno op for synchronously calling a Python handler from JavaScript.
///
/// Looks up the registered Python op by ID, serializes JS arguments to Python,
/// invokes the handler, and returns the result as a JSValue.
#[op2]
#[serde]
fn op_peno_call_python_sync(
    state: &mut OpState,
    op_id: f64,
    #[serde] args: Vec<JSValue>,
) -> Result<JSValue, JsErrorBox> {
    let entry = lookup_entry(state, op_id)?;
    if entry.mode != PythonOpMode::Sync {
        // Name-free on purpose: `Op {name} is not synchronous` let a guest
        // loop over ids and harvest the names of host tools it was never given.
        return Err(JsErrorBox::type_error("Host op is not synchronous"));
    }

    let serialization_limits = *state
        .try_borrow::<SerializationLimits>()
        .ok_or_else(|| opaque_internal_error("SerializationLimits missing from OpState"))?;

    Python::attach(|py| -> Result<JSValue, JsErrorBox> {
        let py_args = convert_args(py, &args, &serialization_limits)?;
        let py_args_tuple = PyTuple::new(py, py_args).map_err(map_pyerr)?;
        let result = entry
            .handler
            .call(py, py_args_tuple, None)
            .map_err(map_pyerr)?;
        python_to_js_value(result.into_bound(py), &serialization_limits).map_err(map_pyerr)
    })
}

/// Deno op for asynchronously calling a Python handler from JavaScript.
///
/// Returns a future that polls the Python coroutine using the asyncio event loop
/// from the TaskLocals stored in OpState. This enables Python async functions to
/// be awaited from JavaScript.
#[op2(async(deferred))]
#[serde]
fn op_peno_call_python_async(
    state: &mut OpState,
    op_id: f64,
    #[serde] args: Vec<JSValue>,
) -> Result<impl std::future::Future<Output = Result<JSValue, JsErrorBox>>, JsErrorBox> {
    let entry = lookup_entry(state, op_id)?;
    if entry.mode != PythonOpMode::Async {
        // Name-free: see the sync op.
        return Err(JsErrorBox::type_error("Host op is not asynchronous"));
    }

    // Get global task locals from OpState
    let global_locals = state
        .try_borrow::<GlobalTaskLocals>()
        .ok_or_else(|| opaque_internal_error("GlobalTaskLocals missing from OpState"))?
        .clone();

    let serialization_limits = *state
        .try_borrow::<SerializationLimits>()
        .ok_or_else(|| opaque_internal_error("SerializationLimits missing from OpState"))?;

    let coroutine = Python::attach(|py| -> Result<Py<PyAny>, JsErrorBox> {
        let py_args = convert_args(py, &args, &serialization_limits)?;
        let py_args_tuple = PyTuple::new(py, py_args).map_err(map_pyerr)?;
        let awaitable = entry
            .handler
            .call(py, py_args_tuple, None)
            .map_err(map_pyerr)?;

        // Validate that we have task locals with a running event loop
        let _locals = global_locals.0.as_ref().ok_or_else(|| {
            JsErrorBox::type_error(
                "Async op requires asyncio context. Call eval_async() first to establish context.",
            )
        })?;

        Ok(awaitable)
    })?;

    // Use into_future_with_locals to properly await the Python coroutine
    // This allows the Rust future to be suspended and resumed, enabling re-entrance
    let task_locals = global_locals
        .0
        .ok_or_else(|| opaque_internal_error("TaskLocals not available for async op"))?;

    Ok(async move {
        // Convert the coroutine future using into_future_with_locals
        // This must be done with GIL acquired
        let future = Python::attach(|py| {
            let bound_coroutine = coroutine.bind(py).clone();
            pyo3_async_runtimes::into_future_with_locals(&task_locals, bound_coroutine)
        })
        .map_err(|err| opaque_internal_error(&format!("Python coroutine setup failed: {err}")))?;

        // This error *is* the exception the awaited Python tool raised, so it
        // goes through map_pyerr like the sync path -- wrapping it in a
        // "Python coroutine failed: ..." string here is what previously made
        // async tools lose their exception class while sync tools kept it.
        let result = future.await.map_err(map_pyerr)?;

        Python::attach(|py| {
            python_to_js_value(result.into_bound(py), &serialization_limits).map_err(map_pyerr)
        })
    })
}

#[op2(async(deferred), fast)]
#[serde]
fn op_peno_stream_pull_py(
    state: &mut OpState,
    #[smi] stream_id: u32,
) -> Result<impl std::future::Future<Output = Result<JSValue, JsErrorBox>>, JsErrorBox> {
    let registry = state
        .try_borrow::<PyStreamRegistry>()
        .ok_or_else(|| opaque_internal_error("PyStreamRegistry missing from OpState"))?
        .clone();

    Ok(async move {
        registry
            .pull_next(stream_id)
            .await
            .map(|chunk| chunk.to_js_value())
            .map_err(|err| JsErrorBox::type_error(err.to_string()))
    })
}

#[op2(async(deferred), fast)]
fn op_peno_stream_cancel_py(
    state: &mut OpState,
    #[smi] stream_id: u32,
) -> Result<impl std::future::Future<Output = Result<(), JsErrorBox>>, JsErrorBox> {
    let registry = state
        .try_borrow::<PyStreamRegistry>()
        .ok_or_else(|| opaque_internal_error("PyStreamRegistry missing from OpState"))?
        .clone();

    Ok(async move {
        registry
            .cancel(stream_id)
            .await
            .map_err(|err| JsErrorBox::type_error(err.to_string()))
    })
}

/// Build the `deno_core::Extension` that wires the Python op registry into the runtime.
pub fn python_extension(registry: PythonOpRegistry) -> Extension {
    let bridge_code = ascii_str!(
        r#"(function (globalThis) {
  const { ops } = Deno.core;

  // Delete every guest-reachable deno_core scaffolding global after caching
  // `ops`. `Deno` and `__bootstrap` both expose `core.ops`, which is a raw,
  // unmetered, untimed call surface into the host process (e.g.
  // `op_print` writes straight to the host's stdout/stderr, bypassing peno's
  // own I/O entirely). `__infra` is deno_core's other bootstrap scaffolding
  // global; it isn't always present, but is deleted defensively since a
  // deno_core bump could start installing it unconditionally. See
  // tests/test_guest_globals.py, which pins the exact guest-visible global
  // surface so a future deno_core bump adding a new global fails loudly here
  // instead of silently reopening this escape.
  delete globalThis.Deno;
  delete globalThis.__bootstrap;
  delete globalThis.__infra;

  // Install `key` as a plain own data property.
  //
  // Plain assignment (`obj[key] = value`) is wrong here: for key "__proto__"
  // it invokes the inherited __proto__ *setter*, which replaces the object's
  // prototype instead of storing data. A Python dict {"__proto__": {...}}
  // therefore lost the key entirely and silently changed the prototype of the
  // object guest JS received -- so host data became inherited behaviour, and a
  // tool returning parsed untrusted JSON could alter method lookup
  // (toString, hasOwnProperty, ...) on the object the sandbox sees.
  // defineProperty never consults setters, so __proto__ round-trips as data.
  // Found by the hypothesis fuzz suite (tests/test_fuzz_eval_boundary.py).
  function setOwn(target, key, value) {
    Object.defineProperty(target, key, {
      value,
      writable: true,
      enumerable: true,
      configurable: true,
    });
  }

  // A JS function has no Python representation, and `serde_v8` turns one
  // into an object with no own enumerable properties -- so before v0.2.1 a
  // callback argument reached the host tool as `{}`, bare or nested, with no
  // error. Silent truncation at a trust boundary is the worst failure mode
  // available: a tool expecting `onProgress` got an empty options object and
  // failed somewhere unrelated, or worse, carried on.
  //
  // So refuse it here, where the argument path is still known, rather than
  // registering the function and handing the tool a live `JsFunction`.
  // Supporting it would need a documented answer for what a held reference
  // means after the call that supplied it has returned (who owns it, when it
  // is released, what it does once the runtime is torn down) -- that is a
  // feature with a lifetime contract, not a bug fix, and `{}` is not the
  // status quo worth preserving while it is designed.
  function rejectFunction(path) {
    throw new TypeError(
      "Cannot pass a JavaScript function to a host tool (at " +
        path +
        "). Host tools receive data, not callables; call the tool with a " +
        "plain value, or have the tool return a value the guest applies itself."
    );
  }

  function prepare(value, path) {
    const at = path === undefined ? "argument" : path;
    if (typeof value === "function") {
      rejectFunction(at);
    }
    // A Symbol has no Python representation either, and it must not be
    // handed down to `serde_v8`: that panics ("unknown ValueType for
    // v8::Value") inside the op, which aborts the *host process* -- guest JS
    // must never be able to do that. Refuse it here, where a throw is just a
    // catchable JS error.
    if (typeof value === "symbol") {
      throw new TypeError(
        "Cannot pass a JavaScript Symbol to a host tool (at " +
          at +
          "). Symbols have no Python representation; pass its description as " +
          "a string instead."
      );
    }
    if (value === undefined || value === null) {
      return value;
    }
    if (ArrayBuffer.isView(value)) {
      return value;
    }
    if (Array.isArray(value)) {
      return value.map((entry, index) => prepare(entry, at + "[" + index + "]"));
    }
    if (value instanceof Date) {
      return { __peno_type: "Date", epoch_ms: value.valueOf() };
    }
    if (value instanceof Set) {
      return {
        __peno_type: "Set",
        values: Array.from(value, (entry, index) =>
          prepare(entry, at + ".<set item " + index + ">")
        ),
      };
    }
    if (typeof value === "bigint") {
      return { __peno_type: "BigInt", value: value.toString() };
    }
    if (typeof value === "object") {
      const result = {};
      for (const [key, val] of Object.entries(value)) {
        setOwn(result, key, prepare(val, at + "." + key));
      }
      return result;
    }
    return value;
  }

  function prepareArgs(args) {
    return args.map((value, index) => prepare(value, "args[" + index + "]"));
  }

  function reviveStreamChunk(entry) {
    if (!entry || entry.__peno_type !== "StreamChunk") {
      return entry;
    }
    return {
      done: Boolean(entry.done),
      value: entry.value === undefined ? undefined : revive(entry.value),
    };
  }

  function revive(value) {
    if (value && typeof value === "object") {
      if (ArrayBuffer.isView(value)) {
        return value;
      }
      if (Array.isArray(value)) {
        return value.map(revive);
      }
      const tag = value.__peno_type;
      switch (tag) {
        case "Undefined":
          return undefined;
        case "Date":
          return new Date(value.epoch_ms);
        case "Set": {
          const set = new Set();
          if (Array.isArray(value.values)) {
            for (const entry of value.values) {
              set.add(revive(entry));
            }
          }
          return set;
        }
        case "BigInt":
          return BigInt(value.value);
        case "PyStream":
          if (typeof globalThis.__peno_from_py_stream === "function") {
            return globalThis.__peno_from_py_stream(value.id);
          }
          return value;
        default: {
          const result = {};
          for (const [key, val] of Object.entries(value)) {
            setOwn(result, key, revive(val));
          }
          return result;
        }
      }
    }
    return value;
  }

  // Host callbacks report a raised Python exception as a TypeError whose
  // message is "\u0001ClassName: message" (see HOST_ERROR_MARKER in ops.rs).
  // Rebuild it as an Error carrying the Python class name on `.name`, so guest
  // JS can `catch (e)` and branch on `e.name` -- and so an uncaught one reaches
  // Python with the same `.name`/`.message` shape build_js_exception produces.
  const HOST_ERROR_MARKER = "\u0001";
  function restoreHostError(err) {
    if (!err || typeof err.message !== "string") {
      return err;
    }
    if (err.message.charCodeAt(0) !== 1) {
      return err;
    }
    const body = err.message.slice(HOST_ERROR_MARKER.length);
    const split = body.indexOf(": ");
    if (split < 0) {
      // No class/message separator: strip the marker, keep the text as-is.
      err.message = body;
      return err;
    }
    const restored = new Error(body.slice(split + 2));
    restored.name = body.slice(0, split);
    // Strip this shim's own frames so the stack points at the guest's call
    // site rather than at python_bridge.js -- the same thing deno_core's
    // buildCustomError does with ErrorCaptureStackTrace.
    if (typeof Error.captureStackTrace === "function") {
      Error.captureStackTrace(restored, restoreHostError);
    }
    return restored;
  }

  // Internal-implementation strings must not cross into guest JS. peno's own
  // messages are already name-free and opaque (see OPAQUE_INTERNAL_ERROR in
  // ops.rs), but `deno_core` builds its own errors *above* the op body -- an
  // argument list it cannot deserialize yields `serde_v8 error: recursion
  // limit exceeded`, naming a dependency crate to sandboxed code. This shim
  // is the only chokepoint above that, so the scrub happens here.
  //
  // These markers name host-side machinery and are useless to a guest, which
  // cannot act on any of them. A host exception whose own message contains
  // one is masked too, which is the intended trade.
  const INTERNAL_MARKERS = [
    "serde_v8",
    "deno_core",
    "JsErrorBox",
    "OpState",
    "PythonOpRegistry",
    "PyStreamRegistry",
    "GlobalTaskLocals",
    "TaskLocals",
    "SerializationLimits",
  ];
  const OPAQUE_INTERNAL = "Host call failed: internal runtime error";
  function sanitizeHostError(err) {
    if (!err || typeof err.message !== "string") {
      return err;
    }
    for (const marker of INTERNAL_MARKERS) {
      if (err.message.indexOf(marker) !== -1) {
        const opaque = new TypeError(OPAQUE_INTERNAL);
        if (typeof Error.captureStackTrace === "function") {
          Error.captureStackTrace(opaque, sanitizeHostError);
        }
        return opaque;
      }
    }
    return err;
  }

  globalThis.__penoCallSync = function (opId, ...args) {
    const prepared = prepareArgs(args);
    try {
      return revive(ops.op_peno_call_python_sync(opId, prepared));
    } catch (err) {
      throw sanitizeHostError(restoreHostError(err));
    }
  };
  globalThis.__penoCallAsync = function (opId, ...args) {
    const prepared = prepareArgs(args);
    return ops.op_peno_call_python_async(opId, prepared).then(revive, (err) => {
      throw sanitizeHostError(restoreHostError(err));
    });
  };
  globalThis.__host_op_sync__ = globalThis.__penoCallSync;
  globalThis.__host_op_async__ = function (opId, ...args) {
    return globalThis.__penoCallAsync(opId, ...args);
  };
  globalThis.__peno_bind_object = function (globalName, assignments) {
    if (typeof globalName !== "string" || !Array.isArray(assignments)) {
      return;
    }
    const target = globalThis[globalName] ?? (globalThis[globalName] = {});
    for (const entry of assignments) {
      if (!entry || typeof entry !== "object" || typeof entry.key !== "string") {
        continue;
      }
      if (entry.kind === "op") {
        const bridge =
          entry.mode === "async"
            ? globalThis.__host_op_async__
            : globalThis.__host_op_sync__;
        if (typeof bridge !== "function" || typeof entry.op_id !== "number") {
          continue;
        }
        setOwn(target, entry.key, (...args) => bridge(entry.op_id, ...args));
      } else if (entry.kind === "value") {
        setOwn(target, entry.key, entry.value);
      }
    }
  };

  if (typeof globalThis.ReadableStream !== "function") {
    // Note: This minimal polyfill does not implement backpressure or BYOB readers.
    class PenoReadableStream {
      constructor(underlying = {}) {
        this._queue = [];
        this._closed = false;
        this._errored = false;
        this._error = undefined;
        this._pulling = false;
        this._underlying = underlying;
        this._controller = {
          enqueue: (value) => {
            if (this._closed) {
              return;
            }
            this._queue.push(value);
          },
          close: () => {
            this._closed = true;
          },
          error: (reason) => {
            this._errored = true;
            this._error =
              reason instanceof Error
                ? reason
                : new Error(String(reason ?? "ReadableStream error"));
            this._closed = true;
          },
        };
        if (typeof underlying.start === "function") {
          underlying.start(this._controller);
        }
      }

      async _maybePull() {
        if (this._closed || this._pulling) {
          return;
        }
        if (typeof this._underlying.pull === "function") {
          this._pulling = true;
          try {
            await this._underlying.pull(this._controller);
          } finally {
            this._pulling = false;
          }
        }
      }

      getReader() {
        const stream = this;
        return {
          async read() {
            if (stream._queue.length === 0 && !stream._closed) {
              await stream._maybePull();
            }
            if (stream._queue.length > 0) {
              const value = stream._queue.shift();
              return { done: false, value };
            }
            if (stream._errored) {
              throw stream._error || new Error("ReadableStream error");
            }
            return { done: true, value: undefined };
          },
          async cancel(reason) {
            stream._closed = true;
            if (typeof stream._underlying.cancel === "function") {
              await stream._underlying.cancel(reason);
            }
          },
        };
      }
    }
    globalThis.ReadableStream = PenoReadableStream;
  }

  globalThis.__peno_from_py_stream = function (id) {
    return new ReadableStream({
      async pull(controller) {
        let raw;
        try {
          raw = await ops.op_peno_stream_pull_py(id);
        } catch (err) {
          throw sanitizeHostError(restoreHostError(err));
        }
        const chunk = reviveStreamChunk(raw);
        if (chunk.done) {
          controller.close();
          return;
        }
        controller.enqueue(chunk.value);
      },
      cancel(reason) {
        return ops.op_peno_stream_cancel_py(id);
      },
    });
  };
})(globalThis);"#
    );

    let registry_for_state = registry.clone();

    Extension {
        name: "peno_python",
        ops: std::borrow::Cow::Owned(vec![
            op_peno_call_python_sync(),
            op_peno_call_python_async(),
            op_peno_stream_pull_py(),
            op_peno_stream_cancel_py(),
        ]),
        js_files: std::borrow::Cow::Owned(vec![ExtensionFileSource::new(
            "ext:peno/python_bridge.js",
            bridge_code,
        )]),
        op_state_fn: Some(Box::new(move |state| {
            state.put::<PythonOpRegistry>(registry_for_state.clone());
            state.put::<GlobalTaskLocals>(GlobalTaskLocals(None));
        })),
        ..Default::default()
    }
}
