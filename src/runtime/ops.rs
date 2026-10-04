//! Python op registry and deno_core integration.
//!
//! Two ops (`op_pydeno_call_python_sync`/`_async`) bridge JS calls into Python
//! handlers addressed by an unguessable **capability token**, not an ambient id:
//! [`PythonOpRegistry::register`] draws a CSPRNG token (so enumeration finds
//! nothing) and dispatch additionally requires [`PythonOpRegistry::expose`],
//! which bind paths call only once the binding is installed in the guest's scope.
//! [`PythonOpRegistry::revoke`] reverses both, making `Runtime.revoke_op` real.
//!
//! Guest-visible failures never name host internals: they are uniform or the
//! opaque [`OPAQUE_INTERNAL_ERROR`] (detail logged host-side); `deno_core`'s own
//! `serde_v8` errors are scrubbed by `sanitizeHostError` in the bridge JS.

use crate::runtime::conversion::{js_value_to_python_tracked, python_to_js_value};
use crate::runtime::js_value::{JSValue, LimitTracker, SerializationLimits};
use crate::runtime::stream::PyStreamRegistry;
use deno_core::ascii_str;
use deno_core::op2;
use deno_core::v8;
use deno_core::Extension;
use deno_core::ExtensionFileSource;
use deno_core::OpState;
use deno_error::JsErrorBox;
use pyo3::prelude::*;
use pyo3::types::PyTuple;
use pyo3_async_runtimes::TaskLocals;
use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::rc::Rc;
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

/// An op capability token. Crosses the JS ABI as a number, so it is bounded by
/// [`MAX_OP_TOKEN`] to stay exactly representable as a double.
pub type OpToken = u64;

/// `2^53 - 1`, the largest integer a JS number represents exactly.
pub const MAX_OP_TOKEN: OpToken = (1 << 53) - 1;

/// Draw a fresh token from a CSPRNG (`uuid` v4 uses OS entropy; avoids `rand`).
fn random_op_token() -> OpToken {
    loop {
        let bytes = *uuid::Uuid::new_v4().as_bytes();
        let raw = u64::from_le_bytes(bytes[..8].try_into().expect("8 bytes"));
        let token = raw & MAX_OP_TOKEN;
        // Zero is excluded so a forged `__host_op_sync__(0, ...)` is never live.
        if token != 0 {
            return token;
        }
    }
}

/// Guest-facing message for any failure that would otherwise name a host data
/// structure; the real cause is logged host-side.
pub const OPAQUE_INTERNAL_ERROR: &str = "Host call failed: internal runtime error";

/// Uniform message for any token that is not a live, exposed capability, so it
/// reveals neither the existence nor the name of anything.
const UNKNOWN_OP_ERROR: &str = "Unknown host op";

fn opaque_internal_error(detail: &str) -> JsErrorBox {
    log::error!("pydeno internal op failure: {detail}");
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

/// Clone a `T` out of `OpState`, failing opaquely if it is missing.
fn state_get<T: Clone + 'static>(state: &OpState, name: &str) -> Result<T, JsErrorBox> {
    state
        .try_borrow::<T>()
        .cloned()
        .ok_or_else(|| opaque_internal_error(&format!("{name} missing from OpState")))
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

/// The `max_buffer_bytes` budget the ArrayBuffer allocator charges to (`None`: no cap). The
/// bridge charges resizable buffers to it, which V8 allocates past the embedder's allocator.
#[derive(Clone, Default)]
pub struct BufferBudget(pub Option<Arc<crate::runtime::capped_allocator::Budget>>);

/// The resizable buffers the bridge has charged, by the id stored on each as a private symbol:
/// a weak handle and the bytes it currently holds. Weak, so a buffer the guest dropped is found
/// collected at the next sweep and gives its bytes back; the sweep runs when a charge would
/// exceed the cap, after a forced GC, so churn through short-lived resizable buffers does not
/// exhaust the budget inside one synchronous run.
pub struct ResizableBuffers {
    next_id: u64,
    entries: HashMap<u64, TrackedBuffer>,
    /// Table size at which the next cheap sweep (no GC: drop the entries whose handle V8 has
    /// emptied) runs. Doubles with the live count, so the table never holds more than about
    /// twice the live buffers, and tiny buffers that never touch the cap cannot grow it without
    /// bound.
    sweep_at: usize,
}

/// A charged buffer: V8's weak handle to it and the bytes it holds of the budget.
type TrackedBuffer = (v8::Weak<v8::Object>, usize);

/// The table as OpState holds it: shared with the allocator's refusal path (see
/// `capped_allocator::set_thread_sweeper`), which runs on the same isolate thread.
pub type SharedBuffers = Rc<RefCell<ResizableBuffers>>;

/// First cheap sweep after this many entries.
const SWEEP_START: usize = 1024;
/// Hard bound on tracked resizable buffers. A guest keeping this many alive (each one also a
/// JS object on its heap) is refused the next, with the same RangeError as an exhausted cap.
const MAX_TRACKED: usize = 1 << 22;

impl Default for ResizableBuffers {
    fn default() -> Self {
        Self {
            next_id: 0,
            entries: HashMap::new(),
            sweep_at: SWEEP_START,
        }
    }
}

impl ResizableBuffers {
    /// Drop the entries whose buffer V8 has collected, giving their bytes back. No GC here:
    /// callers that need one run it first. Cost is linear in the table, so it runs on a cap hit,
    /// on a table that doubled, and when the allocator refuses a fixed-length buffer.
    pub fn sweep(&mut self, budget: &crate::runtime::capped_allocator::Budget) {
        self.entries.retain(|_, (handle, bytes)| {
            if handle.is_empty() {
                budget.release(*bytes);
                false
            } else {
                true
            }
        });
        self.sweep_at = (self.entries.len() * 2).max(SWEEP_START);
    }
}

/// Private-symbol key for the id a charged buffer carries; invisible to JavaScript.
const BUFFER_ID_KEY: &str = "pydeno.buffer#id";

/// Whole non-negative byte counts only; anything else the bridge could be tricked into passing
/// (`NaN`, a negative, a fraction) is refused rather than rounded.
fn byte_count(raw: f64) -> Option<usize> {
    (raw.is_finite() && raw >= 0.0 && raw.fract() == 0.0 && raw <= usize::MAX as f64)
        .then_some(raw as usize)
}

extern "C" {
    // POSIX; on every platform pydeno builds for. No crate needed for one call.
    fn getpagesize() -> std::ffi::c_int;
}

/// The OS page size, read once.
fn page_size() -> usize {
    static PAGE: std::sync::OnceLock<usize> = std::sync::OnceLock::new();
    *PAGE.get_or_init(|| {
        // SAFETY: `getpagesize` takes no arguments and only reads a constant.
        let size = unsafe { getpagesize() };
        usize::try_from(size)
            .ok()
            .filter(|s| *s > 0)
            .unwrap_or(4096)
    })
}

/// What a resizable backing store of `bytes` really commits: V8 maps and commits it in whole
/// pages, so a one-byte buffer holds a page of RSS. Charging the raw `byteLength` let 40,000
/// one-byte buffers (40 KB charged) commit hundreds of MB. Zero stays zero (nothing committed).
fn committed_pages(bytes: usize) -> usize {
    if bytes == 0 {
        return 0;
    }
    let page = page_size();
    bytes.div_ceil(page).saturating_mul(page)
}

/// Set the bytes `buffer` (a resizable ArrayBuffer or growable SharedArrayBuffer) holds of the
/// `max_buffer_bytes` budget: the growth is reserved (after a GC and sweep if the cap is hit),
/// a shrink is released, zero forgets the buffer. `false` when the cap would be exceeded; `true`
/// when there is no cap.
#[op2(nofast, reentrant)]
fn op_pydeno_buffer_charge<'s>(
    scope: &mut v8::PinScope<'s, '_>,
    state: Rc<RefCell<OpState>>,
    buffer: v8::Local<'s, v8::Value>,
    bytes: f64,
) -> bool {
    let Some(budget) = state
        .borrow()
        .try_borrow::<BufferBudget>()
        .and_then(|b| b.0.clone())
    else {
        return true;
    };
    let (Some(bytes), Ok(buffer)) = (byte_count(bytes), v8::Local::<v8::Object>::try_from(buffer))
    else {
        return false;
    };
    // The budget counts what the process commits, so every charge, release and table entry is
    // in whole pages; the bridge keeps talking in byteLengths.
    let bytes = committed_pages(bytes);
    let Some(key_name) = v8::String::new(scope, BUFFER_ID_KEY) else {
        return false;
    };
    let key = v8::Private::for_api(scope, Some(key_name));
    let known = buffer
        .get_private(scope, key)
        .filter(|value| value.is_number())
        .and_then(|value| value.number_value(scope))
        .filter(|id| *id >= 1.0 && id.fract() == 0.0)
        .map(|id| id as u64);

    // The table is borrowed in short scopes only: a GC (`low_memory_notification`) must never
    // run while it is held, and the allocator's refusal path borrows it too.
    let Some(table) = state.borrow().try_borrow::<SharedBuffers>().cloned() else {
        return false;
    };
    let known = known.filter(|id| table.borrow().entries.contains_key(id));
    let id = match known {
        Some(id) => id,
        None => {
            let mut full = {
                let mut tracked = table.borrow_mut();
                if tracked.entries.len() >= tracked.sweep_at {
                    tracked.sweep(&budget);
                }
                tracked.entries.len() >= MAX_TRACKED
            };
            if full {
                // Collect before refusing: the table may be full of buffers the guest dropped.
                scope.low_memory_notification();
                let mut tracked = table.borrow_mut();
                tracked.sweep(&budget);
                full = tracked.entries.len() >= MAX_TRACKED;
            }
            if full {
                return false;
            }
            let id = {
                let mut tracked = table.borrow_mut();
                // Never reused: ids stay exact as doubles far past any count a runtime reaches.
                tracked.next_id += 1;
                tracked.next_id
            };
            let tag = v8::Number::new(scope, id as f64);
            buffer.set_private(scope, key, tag.into());
            let handle = v8::Weak::new(scope, buffer);
            table.borrow_mut().entries.insert(id, (handle, 0));
            id
        }
    };
    let current = table.borrow().entries.get(&id).map_or(0, |entry| entry.1);
    if bytes > current {
        let growth = bytes - current;
        if !budget.reserve(growth) {
            // Over the cap: collect first. A resizable buffer the guest dropped still holds
            // its bytes here until its handle is seen emptied.
            scope.low_memory_notification();
            table.borrow_mut().sweep(&budget);
            if !budget.reserve(growth) {
                return false;
            }
        }
    } else {
        budget.release(current - bytes);
    }
    let mut tracked = table.borrow_mut();
    if bytes == 0 {
        tracked.entries.remove(&id);
    } else if let Some(entry) = tracked.entries.get_mut(&id) {
        entry.1 = bytes;
    }
    true
}

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
    /// Tokens a bind step actually installed in the guest's scope; dispatch
    /// consults this, not `handlers`.
    exposed: Mutex<HashSet<OpToken>>,
}

/// Thread-safe registry of Python operations.
///
/// Manages dynamic registration and lookup of Python callables that can be invoked
/// from JavaScript via `op_pydeno_call_python_sync` and `op_pydeno_call_python_async`.
#[derive(Clone, Default)]
pub struct PythonOpRegistry {
    inner: Arc<PythonOpRegistryInner>,
}

impl PythonOpRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Register a Python callable and return its capability token. It is not
    /// callable from JS until [`Self::expose`].
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

    /// Make a registered op dispatchable; `false` (and no-op) if not registered.
    pub fn expose(&self, id: OpToken) -> bool {
        if !self.inner.handlers.lock().unwrap().contains_key(&id) {
            return false;
        }
        self.inner.exposed.lock().unwrap().insert(id);
        true
    }

    /// Revoke a capability (handler and exposure); `false` if not registered.
    /// A leftover guest global then gets the uniform "Unknown host op".
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

/// Resolve `raw_token` to an exposed entry of the expected `mode`.
///
/// Malformed, unknown, unexposed and revoked all fail identically so a guest
/// cannot enumerate the registry; the mode error is name-free for the same reason.
fn lookup_entry(
    state: &OpState,
    raw_token: f64,
    mode: PythonOpMode,
    mode_error: &'static str,
) -> Result<PythonOpEntry, JsErrorBox> {
    let registry = state_get::<PythonOpRegistry>(state, "PythonOpRegistry")?;
    let entry = registry
        .get_exposed(token_from_js(raw_token)?)
        .ok_or_else(unknown_op_error)?;
    if entry.mode != mode {
        return Err(JsErrorBox::type_error(mode_error));
    }
    Ok(entry)
}

/// Convert the call's arguments against one shared budget (so
/// `max_serialization_bytes` caps a whole call) and invoke the handler.
fn call_handler(
    py: Python<'_>,
    entry: &PythonOpEntry,
    args: &[JSValue],
    limits: &SerializationLimits,
) -> Result<Py<PyAny>, JsErrorBox> {
    let mut tracker = LimitTracker::new(limits.max_depth, limits.max_bytes);
    let py_args = args
        .iter()
        .map(|arg| js_value_to_python_tracked(py, arg, None, &mut tracker).map_err(map_pyerr))
        .collect::<Result<Vec<_>, _>>()?;
    let py_args = PyTuple::new(py, py_args).map_err(map_pyerr)?;
    entry.handler.call(py, py_args, None).map_err(map_pyerr)
}

/// Marker prefixed onto op error messages carrying a Python exception class,
/// which `restoreHostError` in the bridge JS lifts back onto `error.name`.
///
/// `JsErrorBox::new(class, ..)` can't be used: `buildCustomError` returns
/// `undefined` for unregistered classes (verified on deno_core 0.409). U+0001
/// cannot appear in a class name and is invisible if it ever escapes.
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

fn to_js(result: Py<PyAny>, limits: &SerializationLimits) -> Result<JSValue, JsErrorBox> {
    Python::attach(|py| python_to_js_value(result.into_bound(py), limits).map_err(map_pyerr))
}

/// Synchronously call a Python handler from JavaScript.
#[op2]
#[serde]
fn op_pydeno_call_python_sync(
    state: &mut OpState,
    op_id: f64,
    #[serde] args: Vec<JSValue>,
) -> Result<JSValue, JsErrorBox> {
    let entry = lookup_entry(
        state,
        op_id,
        PythonOpMode::Sync,
        "Host op is not synchronous",
    )?;
    let limits = state_get::<SerializationLimits>(state, "SerializationLimits")?;
    // One GIL acquisition for the call and the result conversion.
    Python::attach(|py| {
        let result = call_handler(py, &entry, &args, &limits)?;
        python_to_js_value(result.into_bound(py), &limits).map_err(map_pyerr)
    })
}

/// Asynchronously call a Python handler from JavaScript, awaiting the returned
/// coroutine on the asyncio loop from the `GlobalTaskLocals` in `OpState`.
#[op2(async(deferred))]
#[serde]
fn op_pydeno_call_python_async(
    state: &mut OpState,
    op_id: f64,
    #[serde] args: Vec<JSValue>,
) -> Result<impl std::future::Future<Output = Result<JSValue, JsErrorBox>>, JsErrorBox> {
    let entry = lookup_entry(
        state,
        op_id,
        PythonOpMode::Async,
        "Host op is not asynchronous",
    )?;
    let global_locals = state_get::<GlobalTaskLocals>(state, "GlobalTaskLocals")?;
    let limits = state_get::<SerializationLimits>(state, "SerializationLimits")?;

    let (coroutine, task_locals) = Python::attach(|py| {
        let awaitable = call_handler(py, &entry, &args, &limits)?;
        let locals = global_locals.0.ok_or_else(|| {
            JsErrorBox::type_error(
                "Async op requires asyncio context. Call eval_async() first to establish context.",
            )
        })?;
        Ok::<_, JsErrorBox>((awaitable, locals))
    })?;

    Ok(async move {
        let future = Python::attach(|py| {
            pyo3_async_runtimes::into_future_with_locals(&task_locals, coroutine.into_bound(py))
        })
        .map_err(|err| opaque_internal_error(&format!("Python coroutine setup failed: {err}")))?;
        // This error is the tool's own exception: keep its class like the sync path.
        let result = future.await.map_err(map_pyerr)?;
        to_js(result, &limits)
    })
}

#[op2(async(deferred), fast)]
#[serde]
fn op_pydeno_stream_pull_py(
    state: &mut OpState,
    #[smi] stream_id: u32,
) -> Result<impl std::future::Future<Output = Result<JSValue, JsErrorBox>>, JsErrorBox> {
    let registry = state_get::<PyStreamRegistry>(state, "PyStreamRegistry")?;
    Ok(async move {
        registry
            .pull_next(stream_id)
            .await
            .map(|chunk| chunk.to_js_value())
            .map_err(|err| JsErrorBox::type_error(err.to_string()))
    })
}

#[op2(async(deferred), fast)]
fn op_pydeno_stream_cancel_py(
    state: &mut OpState,
    #[smi] stream_id: u32,
) -> Result<impl std::future::Future<Output = Result<(), JsErrorBox>>, JsErrorBox> {
    let registry = state_get::<PyStreamRegistry>(state, "PyStreamRegistry")?;
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
  "use strict";
  const { ops } = Deno.core;

  // Everything below runs while the guest holds values it fully controls, so the bridge must not
  // look anything up on a guest-reachable object at call time: a guest that replaces
  // `Date.prototype.valueOf`, `Array.prototype.map` or `Array.prototype[Symbol.iterator]` could
  // otherwise hand a Symbol (or anything else the converter does not expect) to the Rust side,
  // which aborts the process. These are captured once, before any guest code exists, and
  // `uncurry` binds each to the intrinsic itself (not to `.call` as the guest may have left it).
  const uncurry = Function.prototype.call.bind.bind(Function.prototype.call);
  const ArrayIsArray = Array.isArray;
  const IsView = ArrayBuffer.isView;
  const ObjectEntries = Object.entries;
  const DefineProperty = Object.defineProperty;
  const GetOwnPropertyDescriptor = Object.getOwnPropertyDescriptor;
  const GetPrototypeOf = Object.getPrototypeOf;
  const IsExtensible = Object.isExtensible;
  const HasOwn = Object.hasOwn;
  const ObjectPrototype = Object.prototype;
  const IsProxy = Deno.core.isProxy;
  const OwnKeys = Reflect.ownKeys;
  const WeakSetAdd = uncurry(WeakSet.prototype.add);
  const WeakSetHas = uncurry(WeakSet.prototype.has);
  const DateValueOf = uncurry(Date.prototype.valueOf);
  const BigIntToString = uncurry(BigInt.prototype.toString);
  const SetForEach = uncurry(Set.prototype.forEach);
  const SetAdd = uncurry(Set.prototype.add);
  const PromiseThen = uncurry(Promise.prototype.then);
  const DateCtor = Date;
  const SetCtor = Set;
  const BigIntCtor = BigInt;
  const TypeErrorCtor = TypeError;
  // deno_core reads this registered symbol from every thrown error (`errorAdditionalPropertyKeys`).
  const ErrorAdditionalPropertyKeys = Symbol.for("errorAdditionalPropertyKeys");
  const RangeErrorCtor = RangeError;
  // A host-bound function's arguments are copied before they cross: cap the work and the depth
  // up front, so one call cannot make the copy itself the denial of service.
  const MAX_ARG_NODES = 1000000;
  const MAX_ARG_DEPTH = 128;
  let argNodes = 0;

  // `Deno`/`__bootstrap` expose raw, unmetered `core.ops` into the host;
  // `__infra` is deleted defensively. tests/test_guest_globals.py pins this.
  delete globalThis.Deno;
  delete globalThis.__bootstrap;
  delete globalThis.__infra;

  // Install `key` as own data: plain assignment of "__proto__" would invoke
  // the setter and swap the prototype (tests/test_fuzz_eval_boundary.py).
  // The descriptor has no prototype, so `Object.prototype.get` (or `.set`, `.writable`...) planted
  // by the guest cannot turn it into something else.
  function setOwn(target, key, value) {
    DefineProperty(target, key, {
      __proto__: null,
      value,
      writable: true,
      enumerable: true,
      configurable: true,
    });
  }

  // `serde_v8` would silently turn a function into `{}`; refuse it loudly
  // instead (passing live callables would need a lifetime contract).
  function rejectFunction(path) {
    throw new TypeError(
      "Cannot pass a JavaScript function to a host tool (at " +
        path +
        "). Host tools receive data, not callables; call the tool with a " +
        "plain value, or have the tool return a value the guest applies itself."
    );
  }

  function prepare(value, path, depth) {
    const at = path === undefined ? "argument" : path;
    const level = depth === undefined ? 0 : depth;
    if (typeof value === "function") {
      rejectFunction(at);
    }
    // A Symbol reaching `serde_v8` panics and aborts the host process.
    if (typeof value === "symbol") {
      throw new TypeErrorCtor(
        "Cannot pass a JavaScript Symbol to a host tool (at " +
          at +
          "). Symbols have no Python representation; pass its description as " +
          "a string instead."
      );
    }
    // Counted before the null check: `new Array(2 ** 32 - 1)` is four billion `undefined`s, and each
    // one must cost a node, or the cap never trips.
    if (++argNodes > MAX_ARG_NODES) {
      throw new RangeErrorCtor("Host tool argument is too large (more than " + MAX_ARG_NODES + " values)");
    }
    if (value === undefined || value === null) {
      return value;
    }
    if (level > MAX_ARG_DEPTH) {
      throw new RangeErrorCtor("Host tool argument is nested too deeply (at " + at + ")");
    }
    if (IsView(value)) {
      return value;
    }
    if (ArrayIsArray(value)) {
      const length = value.length;
      if (length > MAX_ARG_NODES) {
        throw new RangeErrorCtor("Host tool argument is too large (an array of " + length + " entries)");
      }
      const out = [];
      for (let index = 0; index < length; index++) {
        setOwn(out, index, prepare(value[index], at + "[" + index + "]", level + 1));
      }
      return out;
    }
    if (value instanceof Date) {
      return { __pydeno_type: "Date", epoch_ms: DateValueOf(value) };
    }
    if (value instanceof Set) {
      const values = [];
      SetForEach(value, (entry) => {
        const at2 = at + ".<set item " + values.length + ">";
        setOwn(values, values.length, prepare(entry, at2, level + 1));
      });
      return { __pydeno_type: "Set", values };
    }
    if (typeof value === "bigint") {
      return { __pydeno_type: "BigInt", value: BigIntToString(value) };
    }
    if (typeof value === "object") {
      const result = {};
      // Indexed loops, not `for...of` or destructuring: those go through
      // `Array.prototype[Symbol.iterator]`, which the guest can replace.
      const entries = ObjectEntries(value);
      for (let index = 0; index < entries.length; index++) {
        const entry = entries[index];
        const key = entry[0];
        setOwn(result, key, prepare(entry[1], at + "." + key, level + 1));
      }
      return result;
    }
    return value;
  }

  function prepareArgs(args) {
    argNodes = 0;
    const out = [];
    for (let index = 0; index < args.length; index++) {
      setOwn(out, index, prepare(args[index], "args[" + index + "]", 0));
    }
    return out;
  }

  function reviveStreamChunk(entry) {
    if (!entry || entry.__pydeno_type !== "StreamChunk") {
      return entry;
    }
    return {
      done: Boolean(entry.done),
      value: entry.value === undefined ? undefined : revive(entry.value),
    };
  }

  // Read an own property only: a key the host value lacks must not fall through to a getter the
  // guest planted on `Object.prototype`.
  function own(value, key) {
    return HasOwn(value, key) ? value[key] : undefined;
  }

  // Rebuild a host result as guest values. Same rule as `prepare`: captured intrinsics and indexed
  // loops only, so a guest that replaced `Array.prototype.map`, `Object.entries`, the array
  // iterator or the global `Date`/`Set`/`BigInt` does not get to run during the rebuild.
  function revive(value) {
    if (value && typeof value === "object") {
      if (IsView(value)) {
        return value;
      }
      if (ArrayIsArray(value)) {
        const length = value.length;
        const out = [];
        for (let index = 0; index < length; index++) {
          setOwn(out, index, revive(value[index]));
        }
        return out;
      }
      switch (own(value, "__pydeno_type")) {
        case "Undefined":
          return undefined;
        case "Date":
          return new DateCtor(own(value, "epoch_ms"));
        case "Set": {
          const set = new SetCtor();
          const values = own(value, "values");
          if (ArrayIsArray(values)) {
            for (let index = 0; index < values.length; index++) {
              SetAdd(set, revive(values[index]));
            }
          }
          return set;
        }
        case "BigInt":
          return BigIntCtor(own(value, "value"));
        case "PyStream":
          return fromPyStream(own(value, "id"));
        default: {
          const result = {};
          const entries = ObjectEntries(value);
          for (let index = 0; index < entries.length; index++) {
            const entry = entries[index];
            setOwn(result, entry[0], revive(entry[1]));
          }
          return result;
        }
      }
    }
    return value;
  }

  // Rebuild "\u0001ClassName: message" (HOST_ERROR_MARKER in ops.rs) as an
  // Error whose `.name` is the Python exception class.
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
    // Point the stack at the guest's call site, not this shim.
    if (typeof Error.captureStackTrace === "function") {
      Error.captureStackTrace(restored, restoreHostError);
    }
    return restored;
  }

  // `deno_core` builds errors above the op body (e.g. `serde_v8 error: ...`);
  // this shim is the only chokepoint to mask host-internal names from guests.
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

  // `args` is always an array built by a rest parameter, never spread: spreading goes through
  // `Array.prototype[Symbol.iterator]`, which the guest can replace.
  function callSync(opId, args) {
    const prepared = prepareArgs(args);
    try {
      return revive(ops.op_pydeno_call_python_sync(opId, prepared));
    } catch (err) {
      throw sanitizeHostError(restoreHostError(err));
    }
  }
  function callAsync(opId, args) {
    const prepared = prepareArgs(args);
    return PromiseThen(ops.op_pydeno_call_python_async(opId, prepared), revive, (err) => {
      throw sanitizeHostError(restoreHostError(err));
    });
  }
  function hostFunction(opId, mode) {
    return mode === "async"
      ? (...args) => callAsync(opId, args)
      : (...args) => callSync(opId, args);
  }

  globalThis.__pydenoCallSync = function (opId, ...args) {
    return callSync(opId, args);
  };
  globalThis.__pydenoCallAsync = function (opId, ...args) {
    return callAsync(opId, args);
  };
  globalThis.__host_op_sync__ = globalThis.__pydenoCallSync;
  globalThis.__host_op_async__ = globalThis.__pydenoCallAsync;

  // The bind helpers below run on behalf of the host, often long after guest code has had the
  // run of the global object. The host exposes the op tokens only if a helper returns normally,
  // so each helper must either install every binding where the guest will look for it, or throw:
  // never return having installed nothing (a Proxy namespace that swallows `defineProperty`, an
  // accessor that hands back a throwaway object, a read-only global that ignores the assignment).
  // And they run no guest code: no `for...of`, no plain assignment that could hit a setter, no
  // lookups on a namespace that could be a Proxy.
  // The host converts the error it gets back with deno_core's `JsError::from_v8_exception`. Its
  // ordinary [[Get]]s on the error are: `name` and `message` (serde), `cause`, `stack`,
  // `Symbol.for("errorAdditionalPropertyKeys")` (then each key it lists, and that value's
  // `toString()`), and, in the AggregateError check, `constructor` and that constructor's `name`,
  // repeated up the prototype chain (and `errors` when the name says AggregateError). Every one of
  // those is an own data property here, so no read reaches a getter the guest planted on a
  // prototype or constructor -- no deadline covers a bind, so a looping getter would block the
  // host. `constructor` is `null`: deno_core stops the AggregateError walk as soon as it is not an
  // object, before it looks at any prototype. `stack` is plain text, so it is never formatted (no
  // guest `Error.prepareStackTrace`, no CallSite frames to read). The remaining steps
  // (`is_instance_of_error`, the V8 message) walk prototypes natively without calling JavaScript.
  function refuseBind(name, why) {
    const message = "Cannot bind '" + name + "': " + why;
    const error = new TypeErrorCtor(message);
    const fields = [
      "name", "TypeError",
      "message", message,
      "constructor", null,
      "cause", undefined,
      "stack", "TypeError: " + message,
      ErrorAdditionalPropertyKeys, undefined,
    ];
    for (let index = 0; index < fields.length; index += 2) {
      DefineProperty(error, fields[index], {
        __proto__: null,
        value: fields[index + 1],
        writable: true,
        configurable: true,
      });
    }
    throw error;
  }

  // Define `globalThis[name] = value` as an own data property, never through a setter.
  function installGlobal(name, value) {
    const desc = GetOwnPropertyDescriptor(globalThis, name);
    if (desc === undefined) {
      if (!IsExtensible(globalThis)) {
        refuseBind(name, "the global object is not extensible");
      }
      DefineProperty(globalThis, name, {
        __proto__: null,
        value,
        writable: true,
        enumerable: true,
        configurable: true,
      });
      return;
    }
    if (!HasOwn(desc, "value")) {
      refuseBind(name, "globalThis." + name + " is already a getter/setter property");
    }
    if (!desc.writable) {
      refuseBind(name, "globalThis." + name + " is already a read-only property");
    }
    // Writable (a `var`, say, which is also non-configurable): replace the value only.
    DefineProperty(globalThis, name, { __proto__: null, value });
  }

  // The object a namespace binding goes on: a fresh one, or an existing *plain* object -- not a
  // Proxy, not an accessor, not a function or class instance, not frozen. Anything else could
  // run guest code during the install or discard it, so it is refused rather than guessed at.
  // Every built-in object a guest could point a namespace at (`Object.prototype`, `Math`,
  // `Array.prototype`, `%IteratorPrototype%`...): installing host tools on one of those would put
  // them on every object, or on a shared built-in. Collected once, before any guest code, from the
  // *standard* global names only: this script also runs after a host snapshot is restored, and
  // the objects that snapshot's bootstrap put on the global object are the host's own namespaces,
  // not built-ins. From each standard global: the value, its own object-valued properties (a
  // constructor's `prototype`, `Intl.Collator`...) and theirs (`Intl.Collator.prototype`,
  // `Array.prototype[Symbol.unscopables]`), plus the prototypes reachable only through instances.
  const STANDARD_GLOBALS = [
    "AggregateError", "Array", "ArrayBuffer", "AsyncDisposableStack", "Atomics", "BigInt",
    "BigInt64Array", "BigUint64Array", "Boolean", "DataView", "Date", "DisposableStack", "Error",
    "EvalError", "FinalizationRegistry", "Float16Array", "Float32Array", "Float64Array",
    "Function", "Int16Array", "Int32Array", "Int8Array", "Intl", "Iterator", "JSON", "Map",
    "Math", "Number", "Object", "Promise", "Proxy", "RangeError", "ReferenceError", "Reflect",
    "RegExp", "Set", "SharedArrayBuffer", "String", "SuppressedError", "Symbol", "SyntaxError",
    "Temporal", "TypeError", "URIError", "Uint16Array", "Uint32Array", "Uint8Array",
    "Uint8ClampedArray", "WeakMap", "WeakRef", "WeakSet", "WebAssembly", "console",
  ];
  const INTRINSICS = new WeakSet();
  function markIntrinsic(value, depth) {
    if (value === null || (typeof value !== "object" && typeof value !== "function")) {
      return;
    }
    if (WeakSetHas(INTRINSICS, value)) {
      return;
    }
    WeakSetAdd(INTRINSICS, value);
    if (depth === 0) {
      return;
    }
    const keys = OwnKeys(value);
    for (let index = 0; index < keys.length; index++) {
      const desc = GetOwnPropertyDescriptor(value, keys[index]);
      if (desc !== undefined && HasOwn(desc, "value")) {
        markIntrinsic(desc.value, depth - 1);
      }
    }
  }
  markIntrinsic(globalThis, 0);
  for (let index = 0; index < STANDARD_GLOBALS.length; index++) {
    const desc = GetOwnPropertyDescriptor(globalThis, STANDARD_GLOBALS[index]);
    if (desc !== undefined && HasOwn(desc, "value")) {
      markIntrinsic(desc.value, 2);
    }
  }
  // `CallSite.prototype`, only handed out to `Error.prepareStackTrace`.
  function callSitePrototype() {
    const saved = GetOwnPropertyDescriptor(Error, "prepareStackTrace");
    let frames;
    try {
      DefineProperty(Error, "prepareStackTrace", {
        __proto__: null,
        value: (error, callSites) => callSites,
        writable: true,
        configurable: true,
      });
      frames = new Error().stack;
    } finally {
      if (saved === undefined) {
        delete Error.prepareStackTrace;
      } else {
        DefineProperty(Error, "prepareStackTrace", saved);
      }
    }
    return ArrayIsArray(frames) && frames.length > 0 ? GetPrototypeOf(frames[0]) : null;
  }
  const iteratorHelper = [][Symbol.iterator]();
  for (const hidden of [
    callSitePrototype(),
    typeof Intl === "object" && typeof Intl.Segmenter === "function"
      ? GetPrototypeOf(new Intl.Segmenter().segment("a"))
      : null,
    typeof Intl === "object" && typeof Intl.Segmenter === "function"
      ? GetPrototypeOf(new Intl.Segmenter().segment("a")[Symbol.iterator]())
      : null,
    typeof iteratorHelper.map === "function" ? GetPrototypeOf(iteratorHelper.map((x) => x)) : null,
    typeof WebAssembly === "object" && typeof WebAssembly.Module === "function"
      ? GetPrototypeOf(WebAssembly.Module.prototype)
      : null,
    typeof Iterator === "function" && typeof Iterator.from === "function"
      ? GetPrototypeOf(Iterator.from({ next() { return { done: true }; } }))
      : null,
    GetPrototypeOf([][Symbol.iterator]()),
    GetPrototypeOf(GetPrototypeOf([][Symbol.iterator]())),
    GetPrototypeOf(new Map()[Symbol.iterator]()),
    GetPrototypeOf(new Set()[Symbol.iterator]()),
    GetPrototypeOf(""[Symbol.iterator]()),
    GetPrototypeOf(/x/[Symbol.matchAll]("")),
    GetPrototypeOf(Uint8Array),
    GetPrototypeOf(Uint8Array.prototype),
    GetPrototypeOf(function* () {}),
    GetPrototypeOf(function* () {}).prototype,
    GetPrototypeOf(async function () {}),
    GetPrototypeOf(async function* () {}),
    GetPrototypeOf(async function* () {}).prototype,
    GetPrototypeOf(GetPrototypeOf(async function* () {}).prototype),
  ]) {
    markIntrinsic(hidden, 1);
  }

  function namespaceFor(name, keys) {
    const desc = GetOwnPropertyDescriptor(globalThis, name);
    if (desc === undefined) {
      return undefined;
    }
    const where = "globalThis." + name;
    if (!HasOwn(desc, "value")) {
      refuseBind(name, where + " is already a getter/setter property, not a plain object");
    }
    const target = desc.value;
    if (target === null || typeof target !== "object" || IsProxy(target)) {
      refuseBind(name, where + " already exists and is not a plain object");
    }
    if (WeakSetHas(INTRINSICS, target)) {
      refuseBind(name, where + " is a built-in object, not a namespace of its own");
    }
    const proto = GetPrototypeOf(target);
    if (proto !== ObjectPrototype && proto !== null) {
      refuseBind(name, where + " already exists and is not a plain object (it has a prototype)");
    }
    if (!IsExtensible(target)) {
      refuseBind(name, where + " is frozen, sealed or otherwise not extensible");
    }
    for (let index = 0; index < keys.length; index++) {
      const existing = GetOwnPropertyDescriptor(target, keys[index]);
      if (existing !== undefined && !existing.configurable) {
        refuseBind(name, where + "." + keys[index] + " already exists and is non-configurable");
      }
    }
    return target;
  }

  globalThis.__pydeno_bind_object = function (globalName, assignments) {
    if (typeof globalName !== "string" || !ArrayIsArray(assignments)) {
      throw new TypeErrorCtor("__pydeno_bind_object: invalid arguments");
    }
    // Validate and build everything first, so a refusal installs nothing. `setOwn`, not
    // `keys[index] = ...`: an index setter on `Array.prototype` would intercept the assignment.
    const keys = [];
    const values = [];
    for (let index = 0; index < assignments.length; index++) {
      const entry = assignments[index];
      if (!entry || typeof entry !== "object" || typeof entry.key !== "string") {
        throw new TypeErrorCtor("__pydeno_bind_object: invalid assignment");
      }
      if (entry.kind === "op" && typeof entry.op_id === "number") {
        setOwn(values, index, hostFunction(entry.op_id, entry.mode));
      } else if (entry.kind === "value") {
        setOwn(values, index, entry.value);
      } else {
        throw new TypeErrorCtor("__pydeno_bind_object: invalid assignment");
      }
      setOwn(keys, index, entry.key);
    }
    let target = namespaceFor(globalName, keys);
    if (target === undefined) {
      target = {};
      installGlobal(globalName, target);
    }
    for (let index = 0; index < keys.length; index++) {
      setOwn(target, keys[index], values[index]);
    }
  };

  globalThis.__pydeno_bind_function = function (name, opId, mode) {
    if (typeof name !== "string" || typeof opId !== "number") {
      throw new TypeErrorCtor("__pydeno_bind_function: invalid arguments");
    }
    installGlobal(name, hostFunction(opId, mode));
  };

  // `max_buffer_bytes` is enforced by the embedder's ArrayBuffer allocator, which V8 consults for
  // fixed-length buffers only. A *resizable* ArrayBuffer (`new ArrayBuffer(n, {maxByteLength})`),
  // a growable SharedArrayBuffer, and the copy `transfer()` makes of a resizable buffer come from
  // V8's page allocator instead, so a guest could commit gigabytes under a cap of a few hundred
  // megabytes and turn the promised catchable RangeError into the memory kill. Their *committed*
  // bytes (`byteLength`, what `resize`/`grow` can touch) are charged to the same budget here, at
  // the only places that size changes, through one op that keys each buffer by a private symbol
  // and holds it weakly (ops.rs `op_pydeno_buffer_charge`): a dropped buffer gives its bytes back
  // the next time the cap is hit. Fixed-length buffers, including the fixed copy
  // `transferToFixedLength` makes, still go through the allocator and are never charged twice.
  // Every length is converted to a number exactly once and V8 is handed that number, so the size
  // charged is the size V8 commits: a `valueOf` that answers differently on a second call changes
  // nothing.
  {
    const Charge = ops.op_pydeno_buffer_charge;
    const ReflectConstruct = Reflect.construct;
    const MathTrunc = Math.trunc;
    const allocationFailed = () => new RangeErrorCtor("Array buffer allocation failed");

    function guard(name, growName, isAB) {
      const Native = globalThis[name];
      if (typeof Native !== "function") {
        return;
      }
      const Prototype = Native.prototype;
      const ByteLength = uncurry(GetOwnPropertyDescriptor(Prototype, "byteLength").get);
      const Resizable = uncurry(
        GetOwnPropertyDescriptor(Prototype, isAB ? "resizable" : "growable").get
      );
      const NativeGrow = uncurry(Prototype[growName]);

      // `buffer` is resizable; set its committed size to `newLength`, charging the growth first.
      function grow(buffer, newLength) {
        const n = +newLength;
        const before = ByteLength(buffer);
        const want = MathTrunc(n);
        if (want > before) {
          if (!Charge(buffer, want)) {
            throw allocationFailed();
          }
          try {
            NativeGrow(buffer, n);
          } catch (err) {
            Charge(buffer, before);
            throw err;
          }
        } else {
          NativeGrow(buffer, n); // a shrink, a no-op, or V8's own error for a bad length
          Charge(buffer, ByteLength(buffer));
        }
      }

      const Patched = function (length, options) {
        if (new.target === undefined) {
          throw new TypeErrorCtor("Constructor " + name + " requires 'new'");
        }
        const target = new.target === Patched ? Native : new.target;
        // Read once; V8 gets exactly this value, not a second look at a guest getter.
        const max =
          options !== null && typeof options === "object" ? options.maxByteLength : undefined;
        if (max === undefined) {
          return ReflectConstruct(Native, [length], target); // fixed: the allocator charges it
        }
        // Born empty and grown to size: the bytes are charged before V8 commits them, and the
        // result is the same zero-filled buffer (`length` over `maxByteLength` is still a
        // RangeError, from the growth).
        const buffer = ReflectConstruct(Native, [0, { __proto__: null, maxByteLength: max }], target);
        grow(buffer, length);
        return buffer;
      };
      // Same statics (`isView`, `Symbol.species`, `length`, `name`) and the same prototype object,
      // so instances, subclasses and `instanceof` are unchanged; only the construction path differs.
      const keys = OwnKeys(Native);
      for (let index = 0; index < keys.length; index++) {
        if (keys[index] !== "prototype") {
          DefineProperty(Patched, keys[index], GetOwnPropertyDescriptor(Native, keys[index]));
        }
      }
      DefineProperty(Patched, "prototype", {
        __proto__: null,
        value: Prototype,
        writable: false,
        enumerable: false,
        configurable: false,
      });
      DefineProperty(Prototype, "constructor", {
        __proto__: null,
        value: Patched,
        writable: true,
        enumerable: false,
        configurable: true,
      });

      const methods = {
        __proto__: null,
        [growName](newLength) {
          if (!Resizable(this)) {
            return NativeGrow(this, newLength); // V8 says what is wrong with it
          }
          grow(this, newLength);
        },
      };
      if (isAB) {
        // `transfer` keeps the source's resizability, so its copy of a resizable buffer is one
        // more buffer the allocator never sees; `transferToFixedLength` hands the copy to the
        // allocator. Either way the detached source gives its bytes back.
        const NativeTransfer = uncurry(Prototype.transfer);
        const NativeTransferFixed = uncurry(Prototype.transferToFixedLength);
        const moved = (native, buffer, newLength, resizableCopy) => {
          if (!Resizable(buffer)) {
            return native(buffer, newLength); // fixed source: the allocator charges the copy
          }
          const n = newLength === undefined ? undefined : +newLength;
          const before = ByteLength(buffer);
          const want = n === undefined ? before : MathTrunc(n);
          if (resizableCopy && want > before && !Charge(buffer, want)) {
            throw allocationFailed(); // room for the copy's growth, before anything moves
          }
          let copy;
          try {
            copy = native(buffer, n);
          } catch (err) {
            Charge(buffer, before);
            throw err;
          }
          Charge(buffer, 0); // detached
          if (resizableCopy && !Charge(copy, ByteLength(copy))) {
            // Unreachable (the source just gave back at least this much), kept as a guard.
            NativeTransferFixed(copy, 0);
            throw allocationFailed();
          }
          return copy;
        };
        methods.transfer = function transfer(newLength) {
          return moved(NativeTransfer, this, newLength, true);
        };
        methods.transferToFixedLength = function transferToFixedLength(newLength) {
          return moved(NativeTransferFixed, this, newLength, false);
        };
      }
      const names = OwnKeys(methods);
      for (let index = 0; index < names.length; index++) {
        DefineProperty(Prototype, names[index], {
          __proto__: null,
          value: methods[names[index]],
          writable: true,
          enumerable: false,
          configurable: true,
        });
      }

      DefineProperty(globalThis, name, {
        __proto__: null,
        value: Patched,
        writable: true,
        enumerable: false,
        configurable: true,
      });
      // As much a built-in as the constructor it stands in for (the namespace check above).
      markIntrinsic(Patched, 1);
    }

    guard("ArrayBuffer", "resize", true);
    guard("SharedArrayBuffer", "grow", false);
  }

  if (typeof globalThis.ReadableStream !== "function") {
    // Note: This minimal polyfill does not implement backpressure or BYOB readers.
    //
    // Its state lives in a WeakMap, not in `this._queue = ...` properties: an assignment to the
    // instance would run a setter the guest planted on `ReadableStream.prototype`, and the bridge
    // constructs these streams inside a host bind. The state holders have no prototype for the
    // same reason.
    const streamState = new WeakMap();
    const StateGet = uncurry(WeakMap.prototype.get);
    const StateSet = uncurry(WeakMap.prototype.set);
    const stateOf = (stream) => {
      const state = StateGet(streamState, stream);
      if (state === undefined) {
        throw new TypeErrorCtor("not a ReadableStream");
      }
      return state;
    };
    const maybePull = async (state) => {
      if (state.closed || state.pulling) {
        return;
      }
      if (typeof state.underlying.pull === "function") {
        state.pulling = true;
        try {
          await state.underlying.pull(state.controller);
        } finally {
          state.pulling = false;
        }
      }
    };
    class PydenoReadableStream {
      constructor(underlying = {}) {
        const state = {
          __proto__: null,
          queue: [],
          closed: false,
          errored: false,
          error: undefined,
          pulling: false,
          underlying,
          controller: undefined,
        };
        state.controller = {
          enqueue: (value) => {
            if (state.closed) {
              return;
            }
            state.queue.push(value);
          },
          close: () => {
            state.closed = true;
          },
          error: (reason) => {
            state.errored = true;
            state.error =
              reason instanceof Error
                ? reason
                : new Error(String(reason ?? "ReadableStream error"));
            state.closed = true;
          },
        };
        StateSet(streamState, this, state);
        if (typeof underlying.start === "function") {
          underlying.start(state.controller);
        }
      }

      getReader() {
        const state = stateOf(this);
        return {
          async read() {
            if (state.queue.length === 0 && !state.closed) {
              await maybePull(state);
            }
            if (state.queue.length > 0) {
              const value = state.queue.shift();
              return { done: false, value };
            }
            if (state.errored) {
              throw state.error || new Error("ReadableStream error");
            }
            return { done: true, value: undefined };
          },
          async cancel(reason) {
            state.closed = true;
            if (typeof state.underlying.cancel === "function") {
              await state.underlying.cancel(reason);
            }
          },
        };
      }
    }
    globalThis.ReadableStream = PydenoReadableStream;
  }

  // Captured after the polyfill: a guest that later replaces `globalThis.ReadableStream` must not
  // get its constructor called (with the stream id and the pull closure) when the host hands a
  // Python stream to JavaScript, which can happen inside a bind.
  const ReadableStreamCtor = globalThis.ReadableStream;
  // Defined after the built-in scan above, and just as much a built-in for the namespace check.
  markIntrinsic(ReadableStreamCtor, 1);

  // Called by `revive` directly and by the Rust converter through the fixed global below. The
  // underlying source has no prototype, so the constructor's reads of `start`, `type` or
  // `autoAllocateChunkSize` cannot reach a getter the guest planted on `Object.prototype`.
  function fromPyStream(id) {
    return new ReadableStreamCtor({
      __proto__: null,
      async pull(controller) {
        let raw;
        try {
          raw = await ops.op_pydeno_stream_pull_py(id);
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
        return ops.op_pydeno_stream_cancel_py(id);
      },
    });
  }
  globalThis.__pydeno_from_py_stream = fromPyStream;

  // Library code (or a guest) that assigns to one of these would silently reroute every bound
  // host function, since they look the bridge up by name at call time, and the Rust side looks up
  // `__pydeno_bind_object` and `__pydeno_from_py_stream` on the live global. Fixed in place and
  // hidden from enumeration; last, so that every helper above already exists.
  const FIXED_GLOBALS = [
    "__pydenoCallSync",
    "__pydenoCallAsync",
    "__host_op_sync__",
    "__host_op_async__",
    "__pydeno_bind_object",
    "__pydeno_bind_function",
    "__pydeno_from_py_stream",
  ];
  for (let index = 0; index < FIXED_GLOBALS.length; index++) {
    DefineProperty(globalThis, FIXED_GLOBALS[index], {
      writable: false,
      configurable: false,
      enumerable: false,
    });
  }
})(globalThis);"#
    );

    Extension {
        name: "pydeno_python",
        ops: std::borrow::Cow::Owned(vec![
            op_pydeno_call_python_sync(),
            op_pydeno_call_python_async(),
            op_pydeno_stream_pull_py(),
            op_pydeno_stream_cancel_py(),
            op_pydeno_buffer_charge(),
        ]),
        js_files: std::borrow::Cow::Owned(vec![ExtensionFileSource::new(
            "ext:pydeno/python_bridge.js",
            bridge_code,
        )]),
        op_state_fn: Some(Box::new(move |state| {
            state.put::<PythonOpRegistry>(registry.clone());
            state.put::<GlobalTaskLocals>(GlobalTaskLocals(None));
        })),
        ..Default::default()
    }
}
