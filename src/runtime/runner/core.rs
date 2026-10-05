//! [`RuntimeCoreState`]: the V8 isolate and everything that lives beside it on
//! the runtime thread.

use super::convert::{
    capture_stream_prototype, caught_call_error, global_helper, CallError, Converter,
};
use super::jobs::Responder;
use super::termination::{TerminationController, Watchdog, WatchdogToken};
use super::FunctionCallResult;
use crate::runtime::config::RuntimeConfig;
use crate::runtime::error::{JsExceptionDetails, RuntimeError, RuntimeResult};
use crate::runtime::handle::BoundObjectProperty;
use crate::runtime::inspector::{InspectorConnectionState, InspectorMetadata};
#[cfg(feature = "inspector")]
use crate::runtime::inspector::{
    InspectorRegistration, InspectorRegistrationParams, InspectorServer,
};
use crate::runtime::js_value::{JSValue, RuntimeOwner, SerializationLimits};
use crate::runtime::loader::PythonModuleLoader;
use crate::runtime::ops::{
    python_extension, BufferBudget, GlobalTaskLocals, OpToken, PythonOpMode, PythonOpRegistry,
    SharedBuffers,
};
use crate::runtime::stats::{
    ActivitySummary, HeapSnapshot, RuntimeCallKind, RuntimeStatsSnapshot, RuntimeStatsState,
};
use crate::runtime::stream::{JsStreamRegistry, PyStreamRegistry};
use deno_core::error::{CoreError, JsError};
use deno_core::stats::RuntimeActivityStatsFilter;
use deno_core::{v8, JsRuntime, ModuleId, ModuleSpecifier, PollEventLoopOptions, RuntimeOptions};
use indexmap::IndexMap;
use pyo3_async_runtimes::TaskLocals;
use std::cell::RefCell;
use std::collections::HashMap;
use std::ptr::NonNull;
use std::rc::Rc;
use std::time::{Duration, Instant};

/// Minimum heap headroom granted when V8 nears its limit, so it can unwind
/// with an exception instead of aborting the process.
const NEAR_HEAP_LIMIT_MIN_HEADROOM_BYTES: usize = 1024 * 1024; // 1 MiB

/// Stubs out `console` when `enable_console=False` (deno_core enables it by default).
const DISABLE_CONSOLE_JS: &str = r#"
(() => {
    const noop = () => {};
    const stub = new Proxy(Object.create(null), { get: () => noop });
    const existing = globalThis.console;
    if (typeof existing === "object" && existing !== null) {
        for (const key of Reflect.ownKeys(existing)) {
            try { existing[key] = noop; } catch (_) {} // ignore non-writable properties
        }
        return;
    }
    globalThis.console = stub;
})();
"#;

/// Routes console output to the `on_console` op; `__OP_ID__` / `__PASSTHROUGH__`
/// are substituted at install time.
const CONSOLE_CAPTURE_JS: &str = r#"
(() => {
  const OP_ID = __OP_ID__;
  const PASSTHROUGH = __PASSTHROUGH__;
  const forward = globalThis.__host_op_sync__;
  const previous = globalThis.console;
  const target = {};
  for (const level of ["log", "info", "warn", "error", "debug", "trace"]) {
    const prior =
      PASSTHROUGH &&
      previous !== null &&
      typeof previous === "object" &&
      typeof previous[level] === "function"
        ? previous[level].bind(previous)
        : null;
    target[level] = (...args) => {
      try {
        forward(OP_ID, level, args);
      } catch (_) {
        // Unrepresentable arguments degrade to strings; a console call must
        // never throw into (or break) the script that made it.
        try {
          forward(
            OP_ID,
            level,
            args.map((a) => {
              try {
                return String(a);
              } catch (_) {
                return "[unrepresentable]";
              }
            }),
          );
        } catch (_) {}
      }
      if (prior !== null) {
        prior(...args);
      }
    };
  }
  globalThis.console = target;
})();
"#;

/// A JS promise returned by a sync function call, parked until resumed.
pub(super) struct PendingFunctionCall {
    pub(super) promise: v8::Global<v8::Promise>,
    pub(super) start_time: Instant,
    pub(super) deadline: Option<Instant>,
    pub(super) timeout_ms: Option<u64>,
}

/// Startup snapshot bytes leaked to `'static` for V8 and reclaimed on drop.
struct OwnedSnapshot {
    data: Option<Box<[u8]>>,
    leaked_ptr: Option<NonNull<[u8]>>,
}

impl OwnedSnapshot {
    fn new(bytes: Vec<u8>) -> Self {
        Self {
            data: Some(bytes.into_boxed_slice()),
            leaked_ptr: None,
        }
    }

    fn as_static(&mut self) -> &'static [u8] {
        if let Some(ptr) = self.leaked_ptr {
            // SAFETY: pointer remains valid until Drop reconstructs the box.
            return unsafe { ptr.as_ref() };
        }

        let boxed = self
            .data
            .take()
            .expect("OwnedSnapshot bytes already leaked");
        let leaked: &'static mut [u8] = Box::leak(boxed);
        self.leaked_ptr = Some(NonNull::from(&mut *leaked));
        leaked
    }
}

impl Drop for OwnedSnapshot {
    fn drop(&mut self) {
        if let Some(ptr) = self.leaked_ptr.take() {
            // SAFETY: pointer came from Box::leak and has not been reclaimed yet.
            unsafe {
                let _ = Box::from_raw(ptr.as_ptr());
            }
        }
    }
}

#[cfg(feature = "inspector")]
struct InspectorRuntimeState {
    _server: InspectorServer,
    registration: InspectorRegistration,
    wait_for_connection: bool,
    break_on_next_statement: bool,
    has_waited: bool,
    connection_state: InspectorConnectionState,
}

/// Never constructed: a build without the `inspector` feature refuses an
/// inspector config before the runtime exists.
#[cfg(not(feature = "inspector"))]
enum InspectorRuntimeState {}

/// Parse an absolute specifier, or resolve a bare one against `pydeno://runtime/`.
fn module_specifier(specifier: &str) -> RuntimeResult<ModuleSpecifier> {
    if specifier.contains(':') || specifier.starts_with('/') {
        return ModuleSpecifier::parse(specifier).map_err(|e| {
            RuntimeError::internal(format!("Invalid module specifier '{}': {}", specifier, e))
        });
    }
    let base = ModuleSpecifier::parse("pydeno://runtime/")
        .map_err(|e| RuntimeError::internal(format!("Failed to create base URL: {}", e)))?;
    base.join(specifier).map_err(|e| {
        RuntimeError::internal(format!(
            "Failed to resolve module specifier '{}': {}",
            specifier, e
        ))
    })
}

fn js_error(err: Box<JsError>) -> RuntimeError {
    RuntimeError::javascript(JsExceptionDetails::from_js_error(*err))
}

/// The V8 isolate and all runtime data, owned by the dispatcher so jobs can be
/// polled without `RefCell` borrows.
pub(super) struct RuntimeCoreState {
    pub(super) js_runtime: JsRuntime,
    registry: PythonOpRegistry,
    pub(super) module_loader: Rc<PythonModuleLoader>,
    pub(super) task_locals: Option<TaskLocals>,
    execution_timeout: Option<Duration>,
    pub(super) conv: Converter,
    pending_calls: RefCell<HashMap<u64, PendingFunctionCall>>,
    next_pending_call_id: RefCell<u64>,
    pub(super) stats_state: RuntimeStatsState,
    pub(super) termination: TerminationController,
    /// Joined when this state drops.
    pub(super) watchdog: Watchdog,
    terminated: bool,
    inspector_state: Option<InspectorRuntimeState>,
    #[allow(dead_code)]
    startup_snapshot: Option<OwnedSnapshot>,
    pub(super) py_stream_registry: PyStreamRegistry,
    /// A module evaluation was abandoned (timed out, terminated, or stuck on a top-level `await`
    /// that will never settle). deno_core keeps it pending for good and reports it as a stalled
    /// top-level await on every later event-loop poll; once this is set, that report is about the
    /// abandoned module, not about the work in hand.
    pub(super) abandoned_module_evaluation: bool,
    /// The main module this runtime loaded (deno_core allows one), and whether its evaluation was
    /// cut short. Loading it again then fails inside deno_core with an opaque error; this lets
    /// `load_module` say why without reading deno_core's message.
    main_module: Option<String>,
    main_module_incomplete: bool,
}

impl RuntimeCoreState {
    pub(super) fn new(config: RuntimeConfig) -> RuntimeResult<Self> {
        crate::runtime::v8_flags::mark_v8_started();
        let registry = PythonOpRegistry::new();
        let extension = python_extension(registry.clone());
        let module_loader = Rc::new(PythonModuleLoader::new());

        let RuntimeConfig {
            max_heap_size,
            initial_heap_size,
            max_buffer_bytes,
            execution_timeout,
            bootstrap_script,
            enable_console,
            on_console,
            inspector,
            snapshot,
            max_serialization_depth,
            max_serialization_bytes,
            // Consumed by `RuntimeHandle`, which does the waiting.
            force_kill_grace: _,
        } = config;

        if initial_heap_size.is_some() && max_heap_size.is_none() {
            return Err(RuntimeError::internal(
                "initial_heap_size requires max_heap_size to be set as well",
            ));
        }
        if let (Some(initial), Some(max)) = (initial_heap_size, max_heap_size) {
            if initial > max {
                return Err(RuntimeError::internal(format!(
                    "initial_heap_size ({}) cannot exceed max_heap_size ({})",
                    initial, max
                )));
            }
        }
        let mut create_params = max_heap_size.map(|max| {
            v8::CreateParams::default().heap_limits(initial_heap_size.unwrap_or(0), max)
        });
        // ArrayBuffer storage is off the JS heap, so `max_heap_size` never
        // counts it; it has its own opt-in budget. The budget is shared with the
        // bridge (through OpState below), which charges the resizable buffers V8
        // allocates past this allocator.
        let buffer_budget = max_buffer_bytes.map(crate::runtime::capped_allocator::Budget::new);
        if let Some(budget) = &buffer_budget {
            create_params = Some(
                create_params
                    .unwrap_or_default()
                    .array_buffer_allocator(crate::runtime::capped_allocator::new(budget.clone())),
            );
        }

        // The owner is fixed here, once per runtime: every conversion for this runtime (op
        // results, stream chunks and, through the handle, the host's own calls) carries it.
        let serialization_limits =
            SerializationLimits::new(max_serialization_depth, max_serialization_bytes)
                .with_owner(RuntimeOwner::fresh());

        let mut snapshot_source = snapshot.map(OwnedSnapshot::new);
        let startup_snapshot = snapshot_source.as_mut().map(|source| source.as_static());

        #[cfg(not(feature = "inspector"))]
        if inspector.is_some() {
            return Err(RuntimeError::internal(
                crate::runtime::inspector::INSPECTOR_UNAVAILABLE,
            ));
        }
        let inspector_enabled = inspector.is_some();
        let mut js_runtime = JsRuntime::new(RuntimeOptions {
            extensions: vec![extension],
            create_params,
            module_loader: Some(module_loader.clone()),
            inspector: inspector_enabled,
            is_main: true,
            startup_snapshot,
            ..Default::default()
        });

        let py_stream_registry = PyStreamRegistry::new(serialization_limits);
        {
            let op_state = js_runtime.op_state();
            let mut op_state = op_state.borrow_mut();
            op_state.put(py_stream_registry.clone());
            // Must precede *any* script: the sync op path reads the limits
            // from OpState, and console capture / bootstrap logging call ops.
            op_state.put(serialization_limits);
            let tracked = SharedBuffers::default();
            if buffer_budget.is_some() {
                // A fixed-length allocation V8 refuses on our budget retries after a GC: let
                // it find the bytes of collected resizable buffers released first. Only a weak
                // reference crosses into the thread-local, so the hook can never reach V8
                // handles after OpState (and with it the isolate) is gone.
                let weak = Rc::downgrade(&tracked);
                crate::runtime::capped_allocator::set_thread_sweeper(Box::new(move |budget| {
                    if let Some(table) = weak.upgrade() {
                        if let Ok(mut table) = table.try_borrow_mut() {
                            table.sweep(budget);
                        }
                    }
                }));
            }
            op_state.put(BufferBudget(buffer_budget.clone()));
            op_state.put(tracked);
        }

        if inspector_enabled {
            js_runtime.maybe_init_inspector();
        }

        if enable_console == Some(false) {
            js_runtime
                .execute_script("<disable_console>", DISABLE_CONSOLE_JS.to_string())
                .map_err(js_error)?;
        }

        // After the disable stub (the callback still sees output) and before
        // the bootstrap script (its output is captured too).
        if let Some(callback) = on_console {
            let op_id = registry.register(
                "__pydeno_console__".to_string(),
                PythonOpMode::Sync,
                callback.0,
            );
            // The shim closes over the token, so exposing it here is the bind step.
            registry.expose(op_id);
            let script = CONSOLE_CAPTURE_JS
                .replace("__OP_ID__", &op_id.to_string())
                .replace(
                    "__PASSTHROUGH__",
                    &(enable_console == Some(true)).to_string(),
                );
            js_runtime
                .execute_script("<console_capture>", script)
                .map_err(js_error)?;
        }

        if let Some(script) = bootstrap_script {
            js_runtime
                .execute_script("<bootstrap>", script)
                .map_err(js_error)?;
        }

        let termination = TerminationController::new(js_runtime.v8_isolate().thread_safe_handle());
        let watchdog = Watchdog::spawn(termination.clone())?;

        if let Some(heap_limit_bytes) = max_heap_size {
            let termination = termination.clone();
            let budget = buffer_budget.clone();
            js_runtime.add_near_heap_limit_callback(move |current_limit, initial_limit| {
                // V8 also invokes this as the last resort of a *failed external backing-store
                // allocation* (an ArrayBuffer the budget refused), with the JS heap nowhere near
                // its limit. Terminating then turned a catchable RangeError into a runtime that
                // answered every later command with "Heap limit exceeded". The allocator flags
                // its refusal right before V8 gets here; taking the flag means "not the heap":
                // the limit goes back unchanged and V8 fails that allocation as it should. A
                // heap that really is at its limit never sets the flag and terminates as before.
                if budget.as_ref().is_some_and(|b| b.take_refusal()) {
                    return current_limit;
                }
                if termination.request_with_reason("Heap limit exceeded") {
                    log::error!(
                        "V8 isolate is nearing its heap limit; terminating execution \
                         (configured_heap_limit={heap_limit_bytes}, \
                         current_heap_limit={current_limit}, \
                         initial_heap_limit={initial_limit})"
                    );
                }
                termination.terminate_execution();
                let extra_headroom = initial_limit
                    .max(heap_limit_bytes / 8)
                    .max(NEAR_HEAP_LIMIT_MIN_HEADROOM_BYTES);
                current_limit.saturating_add(extra_headroom)
            });
        }

        #[cfg(not(feature = "inspector"))]
        let inspector_state: Option<InspectorRuntimeState> = None;
        #[cfg(feature = "inspector")]
        let inspector_state = match inspector {
            Some(cfg) => {
                let connection_state = InspectorConnectionState::default();
                let server = InspectorServer::bind(cfg.socket_addr(), "pydeno").map_err(|err| {
                    RuntimeError::internal(format!("Failed to start inspector server: {err}"))
                })?;
                let registration = server
                    .register_runtime(
                        js_runtime.inspector(),
                        InspectorRegistrationParams {
                            target_url: cfg.target_url.clone(),
                            display_name: cfg.display_name.clone(),
                            wait_for_connection: cfg.wait_for_connection,
                        },
                        connection_state.clone(),
                    )
                    .map_err(|err| {
                        RuntimeError::internal(format!("Failed to register inspector: {err}"))
                    })?;
                Some(InspectorRuntimeState {
                    _server: server,
                    registration,
                    wait_for_connection: cfg.wait_for_connection,
                    break_on_next_statement: cfg.break_on_next_statement,
                    has_waited: false,
                    connection_state,
                })
            }
            None => None,
        };

        let stream_prototype = {
            deno_core::scope!(scope, js_runtime);
            capture_stream_prototype(scope).map(Rc::new)
        };

        Ok(Self {
            js_runtime,
            registry,
            module_loader,
            task_locals: None,
            execution_timeout,
            conv: Converter {
                fn_registry: Default::default(),
                next_fn_id: Default::default(),
                limits: serialization_limits,
                streams: Rc::new(JsStreamRegistry::new()),
                stream_prototype,
            },
            pending_calls: Default::default(),
            next_pending_call_id: Default::default(),
            stats_state: RuntimeStatsState::default(),
            termination,
            watchdog,
            terminated: false,
            inspector_state,
            startup_snapshot: snapshot_source,
            py_stream_registry,
            abandoned_module_evaluation: false,
            main_module: None,
            main_module_incomplete: false,
        })
    }

    #[cfg(not(feature = "inspector"))]
    pub(super) fn inspector_info(&self) -> Option<(InspectorMetadata, InspectorConnectionState)> {
        self.inspector_state.as_ref().map(|state| match *state {})
    }

    #[cfg(not(feature = "inspector"))]
    pub(super) fn ensure_inspector_ready(&mut self) -> RuntimeResult<()> {
        if let Some(state) = &self.inspector_state {
            match *state {}
        }
        Ok(())
    }

    #[cfg(feature = "inspector")]
    pub(super) fn inspector_info(&self) -> Option<(InspectorMetadata, InspectorConnectionState)> {
        self.inspector_state.as_ref().map(|state| {
            (
                state.registration.metadata().clone(),
                state.connection_state.clone(),
            )
        })
    }

    #[cfg(feature = "inspector")]
    pub(super) fn ensure_inspector_ready(&mut self) -> RuntimeResult<()> {
        if let Some(state) = self.inspector_state.as_mut() {
            if state.has_waited {
                return Ok(());
            }
            let inspector = self.js_runtime.inspector();
            if state.break_on_next_statement {
                inspector.wait_for_session_and_break_on_next_statement();
            } else if state.wait_for_connection {
                inspector.wait_for_session();
            }
            state.has_waited = true;
        }
        Ok(())
    }

    pub(super) fn should_reject_new_work(&self) -> bool {
        self.terminated || self.termination.is_requested()
    }

    pub(super) fn terminated_error(&self) -> RuntimeError {
        self.termination.terminated_error()
    }

    /// Admission check for a new command: not terminated, inspector ready.
    pub(super) fn admit(&mut self) -> RuntimeResult<()> {
        if self.should_reject_new_work() {
            return Err(self.terminated_error());
        }
        self.ensure_inspector_ready()
    }

    /// Publish (or with `None`, clear) the caller's Python task locals to
    /// everything on this thread that can re-enter them.
    pub(super) fn set_task_locals(&mut self, locals: Option<TaskLocals>) {
        match &locals {
            Some(locals) => self.module_loader.set_task_locals(locals.clone()),
            None => self.module_loader.clear_task_locals(),
        }
        self.js_runtime
            .op_state()
            .borrow_mut()
            .put(GlobalTaskLocals(locals.clone()));
        self.task_locals = locals;
    }

    /// Hand the current task locals to a freshly installed resolver/loader.
    pub(super) fn sync_loader_task_locals(&self) {
        if let Some(locals) = &self.task_locals {
            self.module_loader.set_task_locals(locals.clone());
        }
    }

    pub(super) fn clear_task_locals(&mut self) {
        self.set_task_locals(None);
    }

    pub(super) fn effective_timeout_ms(&self, timeout_ms: Option<u64>) -> Option<u64> {
        timeout_ms.or_else(|| {
            self.execution_timeout
                .map(|d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
        })
    }

    fn store_pending_call(&self, call: PendingFunctionCall) -> u64 {
        let mut next_id = self.next_pending_call_id.borrow_mut();
        let call_id = *next_id;
        *next_id = next_id.wrapping_add(1);
        self.pending_calls.borrow_mut().insert(call_id, call);
        call_id
    }

    pub(super) fn take_pending_call(&self, call_id: u64) -> RuntimeResult<PendingFunctionCall> {
        self.pending_calls
            .borrow_mut()
            .remove(&call_id)
            .ok_or_else(|| {
                RuntimeError::internal(format!("Pending function call {} not found", call_id))
            })
    }

    /// Arm the watchdog for `timeout_ms`, if any.
    pub(super) fn arm_watchdog(
        &self,
        timeout_ms: Option<u64>,
        reason: &str,
    ) -> Option<WatchdogToken> {
        timeout_ms.map(|ms| self.watchdog.arm(Duration::from_millis(ms), reason))
    }

    /// Arm the watchdog for the runtime-wide `execution_timeout`, if any.
    pub(super) fn arm_execution_watchdog(&self, reason: &str) -> Option<WatchdogToken> {
        self.execution_timeout
            .map(|duration| self.watchdog.arm(duration, reason))
    }

    /// Disarm `watchdog`, cancelling the termination it caused if it fired.
    /// Returns `(fired, armed duration)`.
    pub(super) fn resolve_watchdog(&mut self, watchdog: WatchdogToken) -> (bool, Duration) {
        let duration = watchdog.duration;
        let fired = self.watchdog.disarm(watchdog);
        if fired {
            self.cancel_pending_termination();
        }
        (fired, duration)
    }

    /// Clear a termination request that has done its job, so it cannot latch
    /// and kill the next, unrelated, call. A no-op when none is pending.
    pub(super) fn cancel_pending_termination(&mut self) {
        let _ = self.js_runtime.v8_isolate().cancel_terminate_execution();
        self.termination.clear_handled_reason();
    }

    /// Resolve `watchdog` and, if it fired, turn the call's outcome into a timeout.
    pub(super) fn apply_watchdog_result<T: CallOutcome>(
        &mut self,
        result: RuntimeResult<T>,
        watchdog: Option<WatchdogToken>,
        context: &str,
    ) -> RuntimeResult<T> {
        if let Some(watchdog) = watchdog {
            let (fired, duration) = self.resolve_watchdog(watchdog);
            if fired {
                // The deadline passed, whatever the call produced meanwhile: a guest error whose
                // conversion ran into the deadline (a looping `cause` getter, say) is reported as
                // the timeout it is, not as the guest's error. A value converted meanwhile is
                // dropped, so its handles go too.
                if let Ok(outcome) = &result {
                    self.discard(outcome);
                }
                let message = format!("{context} timed out after {}ms", duration.as_millis());
                return Err(RuntimeError::timeout(message));
            }
        }
        result
    }

    /// Run `f` under an execution-timeout watchdog and map a firing to a timeout.
    pub(super) fn run_timed<T: CallOutcome>(
        &mut self,
        watchdog: Option<WatchdogToken>,
        context: &str,
        f: impl FnOnce(&mut Self) -> RuntimeResult<T>,
    ) -> RuntimeResult<T> {
        let result = f(self);
        self.apply_watchdog_result(result, watchdog, context)
    }

    /// Release the function and stream handles in an outcome nobody will receive.
    pub(super) fn discard(&self, outcome: &impl CallOutcome) {
        if let Some(value) = outcome.value() {
            self.conv.release_handles(value);
        }
    }

    /// Answer an async call; if its caller stopped waiting, release the handles in the value.
    pub(super) fn send_result(&self, responder: Responder, result: RuntimeResult<JSValue>) {
        if let Err(Ok(value)) = responder.send(result) {
            self.discard(&value);
        }
    }

    /// Answer a sync call; if its caller stopped waiting, release the handles in the value.
    pub(super) fn send_sync_result<T: CallOutcome>(
        &self,
        responder: std::sync::mpsc::Sender<RuntimeResult<T>>,
        result: RuntimeResult<T>,
    ) {
        if let Err(std::sync::mpsc::SendError(Ok(outcome))) = responder.send(result) {
            self.discard(&outcome);
        }
    }

    pub(super) fn finalize_termination(&mut self) -> RuntimeResult<()> {
        if self.terminated {
            return Ok(());
        }
        // `false` just means no termination was pending.
        let _ = self.js_runtime.v8_isolate().cancel_terminate_execution();
        self.conv.fn_registry.borrow_mut().clear();
        self.pending_calls.borrow_mut().clear();
        self.termination.mark_terminated();
        self.terminated = true;
        Ok(())
    }

    pub(super) fn translate_js_error(&mut self, err: JsError) -> RuntimeError {
        self.translate_js_details(JsExceptionDetails::from_js_error(err))
    }

    /// The error for a `func.call` that V8 terminated mid-flight. V8 reports a
    /// *null* exception (`Uncaught null`), which would not be recognised as a
    /// termination; name it the way deno_core names a terminated
    /// `execute_script`, so function calls classify exactly like `eval`.
    fn terminated_call_error(&mut self) -> RuntimeError {
        self.translate_js_details(JsExceptionDetails {
            name: Some("Error".to_string()),
            message: Some("execution terminated".to_string()),
            stack: Some("Error: execution terminated".to_string()),
            frames: Vec::new(),
        })
    }

    pub(super) fn translate_call_error(&mut self, err: CallError) -> RuntimeError {
        match err {
            CallError::Runtime(err) => err,
            CallError::Js(js_error) => self.translate_js_error(*js_error),
            CallError::Terminated => self.terminated_call_error(),
        }
    }

    fn translate_js_details(&mut self, details: JsExceptionDetails) -> RuntimeError {
        if self.should_reject_new_work() && js_error_indicates_termination(&details) {
            let _ = self.finalize_termination();
            self.terminated_error()
        } else {
            RuntimeError::javascript(details)
        }
    }

    pub(super) fn translate_core_error(&mut self, err: CoreError) -> RuntimeError {
        let runtime_error = RuntimeError::from(err);
        if self.should_reject_new_work() && runtime_error_indicates_termination(&runtime_error) {
            let _ = self.finalize_termination();
            self.terminated_error()
        } else {
            runtime_error
        }
    }

    pub(super) fn register_python_op(
        &self,
        name: String,
        mode: PythonOpMode,
        handler: pyo3::Py<pyo3::PyAny>,
    ) -> RuntimeResult<OpToken> {
        Ok(self.registry.register(name, mode, handler))
    }

    /// Expose (`true`) or revoke (`false`) one op capability.
    pub(super) fn set_python_op_exposure(&self, op_id: OpToken, exposed: bool) -> bool {
        if exposed {
            self.registry.expose(op_id)
        } else {
            self.registry.revoke(op_id)
        }
    }

    pub(super) fn bind_object(
        &mut self,
        name: String,
        properties: Vec<BoundObjectProperty>,
    ) -> RuntimeResult<()> {
        let mut op_tokens: Vec<OpToken> = Vec::new();
        let assignments = properties
            .into_iter()
            .map(|entry| {
                let str_value = |s: &str| JSValue::String(s.to_string());
                let mut obj = IndexMap::new();
                match entry {
                    BoundObjectProperty::Value { key, value } => {
                        obj.insert("key".to_string(), JSValue::String(key));
                        obj.insert("kind".to_string(), str_value("value"));
                        obj.insert("value".to_string(), value);
                    }
                    BoundObjectProperty::Op { key, op_id, mode } => {
                        op_tokens.push(op_id);
                        let mode = match mode {
                            PythonOpMode::Async => "async",
                            PythonOpMode::Sync => "sync",
                        };
                        obj.insert("key".to_string(), JSValue::String(key));
                        obj.insert("kind".to_string(), str_value("op"));
                        obj.insert("op_id".to_string(), JSValue::Float(op_id as f64));
                        obj.insert("mode".to_string(), str_value(mode));
                    }
                }
                JSValue::Object(obj)
            })
            .collect();

        let registry = self.registry.clone();
        let conv = self.conv.clone();
        deno_core::scope!(scope, self.js_runtime);
        v8::tc_scope!(let try_catch, scope);
        let (global, helper_fn) = global_helper(try_catch, "__pydeno_bind_object")?;
        let global_name = v8::String::new(try_catch, &name)
            .ok_or_else(|| RuntimeError::internal("Failed to allocate target name"))?;
        let assignments = conv.to_v8(try_catch, &JSValue::Array(assignments))?;

        if helper_fn
            .call(try_catch, global.into(), &[global_name.into(), assignments])
            .is_some()
        {
            // Expose only once the binding is installed in the guest's scope; a
            // helper that threw leaves the handlers registered but unreachable.
            for token in op_tokens {
                registry.expose(token);
            }
            return Ok(());
        }
        // Refused: the handlers were never reachable; drop them rather than keep them for the
        // runtime's lifetime.
        for token in op_tokens {
            registry.revoke(token);
        }
        match try_catch.exception() {
            Some(exception) => Err(js_error(JsError::from_v8_exception(try_catch, exception))),
            None => Err(RuntimeError::internal(
                "__pydeno_bind_object invocation failed",
            )),
        }
    }

    /// Run a synchronous entry point, recording its duration (errors included).
    fn with_timing<T>(
        &mut self,
        kind: RuntimeCallKind,
        f: impl FnOnce(&mut Self) -> RuntimeResult<T>,
    ) -> RuntimeResult<T> {
        let start = Instant::now();
        let result = f(self);
        self.stats_state.record(kind, start.elapsed());
        result
    }

    /// Convert a global handle to a [`JSValue`].
    pub(super) fn global_to_js_value<T>(&mut self, value: &v8::Global<T>) -> RuntimeResult<JSValue>
    where
        for<'s> v8::Local<'s, T>: Into<v8::Local<'s, v8::Value>>,
    {
        let conv = self.conv.clone();
        deno_core::scope!(scope, self.js_runtime);
        let local = v8::Local::new(scope, value);
        conv.to_js_value(scope, local.into())
    }

    /// Drain microtasks the call queued, still inside its own watchdog window,
    /// so a runaway microtask is bounded by (and reported against) this call
    /// rather than hanging some later, untimed one. See
    /// tests/test_microtask_timeout.py.
    fn drain_microtasks(&mut self) {
        self.js_runtime.v8_isolate().perform_microtask_checkpoint();
    }

    pub(super) fn eval_sync(&mut self, code: &str) -> RuntimeResult<JSValue> {
        self.with_timing(RuntimeCallKind::EvalSync, |this| {
            let global_value = this
                .js_runtime
                .execute_script("<eval>", code.to_string())
                .map_err(|err| this.translate_js_error(*err))?;
            this.drain_microtasks();
            this.global_to_js_value(&global_value)
        })
    }

    /// Load `specifier` as the main module (loading is blocking in deno_core).
    pub(super) fn load_module(&mut self, specifier: &str) -> RuntimeResult<ModuleId> {
        let module_specifier = module_specifier(specifier)?;
        let resolved = module_specifier.to_string();
        let again_after_cut_short =
            self.main_module_incomplete && self.main_module.as_deref() == Some(resolved.as_str());
        let loaded =
            futures::executor::block_on(self.js_runtime.load_main_es_module(&module_specifier));
        match loaded {
            Ok(module_id) => {
                if self.main_module.is_none() {
                    self.main_module = Some(resolved);
                }
                Ok(module_id)
            }
            Err(_) if again_after_cut_short => Err(RuntimeError::internal(format!(
                "Failed to load module '{specifier}': its earlier evaluation did not complete \
                 (it timed out or was terminated), so it cannot be evaluated again in this runtime"
            ))),
            Err(e) => Err(RuntimeError::internal(format!(
                "Failed to load module '{}': {}",
                specifier, e
            ))),
        }
    }

    pub(super) fn module_namespace(&mut self, module_id: ModuleId) -> RuntimeResult<JSValue> {
        let namespace = self
            .js_runtime
            .get_module_namespace(module_id)
            .map_err(|e| {
                RuntimeError::internal(format!("Failed to get module namespace: {}", e))
            })?;
        self.global_to_js_value(&namespace)
    }

    pub(super) fn eval_module_sync(&mut self, specifier: &str) -> RuntimeResult<JSValue> {
        self.with_timing(RuntimeCallKind::EvalModuleSync, |this| {
            let module_id = this.load_module(specifier)?;
            let mut receiver = Box::pin(this.js_runtime.mod_evaluate(module_id));
            let result = match futures::executor::block_on(
                this.js_runtime
                    .run_event_loop(PollEventLoopOptions::default()),
            ) {
                Ok(()) => futures::executor::block_on(receiver.as_mut())
                    .map_err(|err| this.translate_core_error(err)),
                Err(err) => {
                    let err = this.translate_core_error(err);
                    // deno_core reports a stalled top-level `await` once nothing else is left to
                    // run; it may be an earlier, abandoned evaluation's. If this module has
                    // settled, its own outcome stands.
                    let mut cx = std::task::Context::from_waker(futures::task::noop_waker_ref());
                    match std::future::Future::poll(receiver.as_mut(), &mut cx) {
                        std::task::Poll::Ready(settled) if is_stalled_module_evaluation(&err) => {
                            settled.map_err(|err| this.translate_core_error(err))
                        }
                        _ => Err(err),
                    }
                }
            };
            if let Err(err) = &result {
                this.note_module_evaluation_failure(err);
            }
            result?;
            // A bare top-level `queueMicrotask` is not on the path the event
            // loop waited for; drain it like `eval_sync` does.
            this.drain_microtasks();
            this.module_namespace(module_id)
        })
    }

    /// Record what a main-module evaluation that ended in `err` leaves behind.
    pub(super) fn note_module_evaluation_failure(&mut self, err: &RuntimeError) {
        if leaves_module_evaluation_pending(err) {
            self.abandoned_module_evaluation = true;
            self.main_module_incomplete = true;
        }
    }

    pub(super) fn call_function_sync(
        &mut self,
        fn_id: u32,
        args: Vec<JSValue>,
        timeout_ms: Option<u64>,
    ) -> RuntimeResult<FunctionCallResult> {
        self.with_timing(RuntimeCallKind::CallFunctionSync, |this| {
            this.invoke_function_sync(fn_id, args, timeout_ms)
        })
    }

    fn invoke_function_sync(
        &mut self,
        fn_id: u32,
        args: Vec<JSValue>,
        timeout_ms: Option<u64>,
    ) -> RuntimeResult<FunctionCallResult> {
        if !self.conv.fn_registry.borrow().contains_key(&fn_id) {
            return Err(RuntimeError::internal(format!(
                "Function ID {} not found",
                fn_id
            )));
        }

        let start_time = Instant::now();
        let effective_timeout = self.effective_timeout_ms(timeout_ms);
        let deadline = effective_timeout.map(|ms| start_time + Duration::from_millis(ms));
        let conv = self.conv.clone();

        enum Outcome {
            Immediate(JSValue),
            Pending(v8::Global<v8::Promise>),
        }

        let outcome: Result<Outcome, CallError> = (|| {
            deno_core::scope!(scope, self.js_runtime);
            v8::tc_scope!(let try_catch, scope);

            let Some(result_value) = conv.call_stored(try_catch, fn_id, &args)? else {
                return Err(caught_call_error!(try_catch));
            };
            try_catch.perform_microtask_checkpoint();
            // A continuation the function queued spun in the checkpoint and was
            // terminated there; report it rather than park a dead promise.
            if try_catch.is_execution_terminating() {
                return Err(CallError::Terminated);
            }

            let value = match v8::Local::<v8::Promise>::try_from(result_value) {
                Ok(promise) => match promise.state() {
                    v8::PromiseState::Pending => {
                        return Ok(Outcome::Pending(v8::Global::new(try_catch, promise)))
                    }
                    v8::PromiseState::Fulfilled => promise.result(try_catch),
                    v8::PromiseState::Rejected => {
                        let exception = promise.result(try_catch);
                        return Err(CallError::Js(JsError::from_v8_exception(
                            try_catch, exception,
                        )));
                    }
                },
                Err(_) => result_value,
            };
            Ok(Outcome::Immediate(conv.to_js_value(try_catch, value)?))
        })();

        match outcome {
            Ok(Outcome::Immediate(value)) => Ok(FunctionCallResult::Immediate(value)),
            Ok(Outcome::Pending(promise)) => Ok(FunctionCallResult::Pending {
                call_id: self.store_pending_call(PendingFunctionCall {
                    promise,
                    start_time,
                    deadline,
                    timeout_ms: effective_timeout,
                }),
            }),
            Err(err) => Err(self.translate_call_error(err)),
        }
    }

    /// Remove a function from the registry, freeing its V8 global handle.
    pub(super) fn release_function(&mut self, fn_id: u32) -> RuntimeResult<()> {
        if self.conv.fn_registry.borrow_mut().remove(&fn_id).is_none() {
            log::debug!("Attempted to release unknown function id {}", fn_id);
        }
        Ok(())
    }

    pub(super) fn release_js_stream(&self, stream_id: u32) -> RuntimeResult<()> {
        self.conv.streams.release(stream_id);
        Ok(())
    }

    pub(super) fn cancel_js_stream(&mut self, stream_id: u32) -> RuntimeResult<()> {
        let streams = self.conv.streams.clone();
        {
            deno_core::scope!(scope, self.js_runtime);
            if let Ok(reader) = streams.ensure_reader(scope, stream_id) {
                let cancel_fn = v8::String::new(scope, "cancel")
                    .and_then(|key| reader.get(scope, key.into()))
                    .and_then(|value| v8::Local::<v8::Function>::try_from(value).ok());
                if let Some(cancel_fn) = cancel_fn {
                    let _ = cancel_fn.call(scope, reader.into(), &[]);
                }
            }
        }
        streams.release(stream_id);
        Ok(())
    }

    pub(super) fn collect_stats(&mut self) -> RuntimeResult<RuntimeStatsSnapshot> {
        let stats = self.js_runtime.v8_isolate().get_heap_statistics();
        let heap = HeapSnapshot {
            heap_total_bytes: stats.total_heap_size() as u64,
            heap_used_bytes: stats.used_heap_size() as u64,
            external_memory_bytes: stats.external_memory() as u64,
            physical_total_bytes: stats.total_physical_size() as u64,
        };
        let activity = ActivitySummary::from_snapshot(
            self.js_runtime
                .runtime_activity_stats_factory()
                .capture(&RuntimeActivityStatsFilter::all())
                .dump(),
        );
        let mut streams = self.conv.streams.stats_snapshot();
        streams.merge(&self.py_stream_registry.stats_snapshot());
        let mut snapshot =
            RuntimeStatsSnapshot::new(heap, self.stats_state.snapshot(), activity, streams);
        snapshot.function_handles = self.conv.fn_registry.borrow().len() as u64;
        Ok(snapshot)
    }
}

/// A call's successful outcome: the value it carries, whose handles must be released if the
/// outcome is dropped instead of delivered.
pub(super) trait CallOutcome {
    fn value(&self) -> Option<&JSValue>;
}

impl CallOutcome for JSValue {
    fn value(&self) -> Option<&JSValue> {
        Some(self)
    }
}

impl CallOutcome for FunctionCallResult {
    fn value(&self) -> Option<&JSValue> {
        match self {
            FunctionCallResult::Immediate(value) => Some(value),
            FunctionCallResult::Pending { .. } => None,
        }
    }
}

/// V8's text for a module evaluation stuck on a top-level `await`.
const STALLED_TOP_LEVEL_AWAIT: &str = "Top-level await promise never resolved";

/// Whether `err` is deno_core's report that a module evaluation is stuck on a top-level `await`
/// with nothing left that could settle it.
///
/// Decided from the error's shape, not its text alone, so a guest cannot pass its own rejection
/// off as one: deno_core builds the report from a V8 message (not a thrown value), so it has no
/// error name, exactly V8's text, and the one frame of the stalled `await`. A guest `Error` with
/// that message carries its name; a non-`Error` thrown value comes back as "Uncaught ...". The
/// deadlock variant is a deno_core error, never a JavaScript one.
pub(super) fn is_stalled_module_evaluation(err: &RuntimeError) -> bool {
    match err {
        RuntimeError::JavaScript(details) => {
            details.name.is_none()
                && details.message.as_deref() == Some(STALLED_TOP_LEVEL_AWAIT)
                && details.frames.len() == 1
        }
        other => other
            .to_string()
            .contains("Module evaluation is still pending after multiple event loop iterations"),
    }
}

/// Whether a module evaluation that ended in `err` may stay pending in deno_core: it was cut
/// short (timeout, termination) or is stuck on a top-level `await`. A module that threw or failed
/// to resolve has settled and leaves nothing behind.
pub(super) fn leaves_module_evaluation_pending(err: &RuntimeError) -> bool {
    matches!(err, RuntimeError::Timeout { .. })
        || runtime_error_indicates_termination(err)
        || is_stalled_module_evaluation(err)
}

pub(super) fn runtime_error_indicates_termination(err: &RuntimeError) -> bool {
    match err {
        RuntimeError::JavaScript(details) => js_error_indicates_termination(details),
        RuntimeError::Timeout { context } | RuntimeError::Internal { context } => {
            context.contains("execution terminated")
        }
        RuntimeError::Terminated { .. } | RuntimeError::ForceKilled { .. } => true,
    }
}

fn js_error_indicates_termination(details: &JsExceptionDetails) -> bool {
    let needle = "execution terminated";
    details
        .message
        .as_deref()
        .is_some_and(|msg| msg.contains(needle))
        || details.summary().contains(needle)
}
