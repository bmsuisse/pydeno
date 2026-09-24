//! The runtime thread's main loop: drive the event loop, poll the active job,
//! handle commands, and park when there is nothing to do.

use super::core::{runtime_error_indicates_termination, RuntimeCoreState};
use super::jobs::{
    call_function_async_job, eval_async_job, resume_function_call_job, stream_read_job,
    EvalModuleAsyncJob, Responder, RuntimeJob,
};
use super::RuntimeCommand;
use crate::runtime::error::{RuntimeError, RuntimeResult};
use deno_core::PollEventLoopOptions;
use std::collections::VecDeque;
use std::sync::Arc;
use std::task::Poll;
use std::time::{Duration, Instant};
use tokio::sync::mpsc;

/// Safety-net re-poll interval, used only while async work is in flight.
///
/// Every wake the loop needs is otherwise exact (the waker, a command, or the
/// active job's deadline via `pending_work_wait`); this bounds any progress
/// that is *not* signalled through the waker, turning a potential permanent
/// hang into at most 1ms of latency. Termination does not depend on it:
/// `TerminationController::request` signals the waker.
const PENDING_WORK_TICK: Duration = Duration::from_millis(1);

/// Waker handed to `poll_event_loop` so deno_core can signal progress and the
/// loop can park instead of spin. `Notify` stores a permit, so a wake landing
/// mid-poll is not lost.
struct DispatcherWaker(Arc<tokio::sync::Notify>);

impl futures::task::ArcWake for DispatcherWaker {
    fn wake_by_ref(arc_self: &Arc<Self>) {
        arc_self.0.notify_one();
    }
}

pub(super) struct RuntimeDispatcher {
    core: RuntimeCoreState,
    cmd_rx: mpsc::UnboundedReceiver<RuntimeCommand>,
    pending_jobs: VecDeque<Box<dyn RuntimeJob>>,
    active_job: Option<Box<dyn RuntimeJob>>,
    /// Gives a freshly activated job one un-waited iteration: its first poll
    /// starts the work and returns `Pending`, and the promise it now waits on
    /// is often already resolved with nothing left to signal the waker.
    job_just_activated: bool,
}

impl RuntimeDispatcher {
    pub(super) fn new(
        core: RuntimeCoreState,
        cmd_rx: mpsc::UnboundedReceiver<RuntimeCommand>,
    ) -> Self {
        Self {
            core,
            cmd_rx,
            pending_jobs: VecDeque::new(),
            active_job: None,
            job_just_activated: false,
        }
    }

    /// How long to park with work in flight: the tick, clamped to the active
    /// job's deadline so its timeout fires on time (`ZERO` if already past).
    fn pending_work_wait(&self) -> Duration {
        let mut wait = PENDING_WORK_TICK;
        if let Some(deadline) = self.active_job.as_ref().and_then(|job| job.deadline()) {
            wait = wait.min(deadline.saturating_duration_since(Instant::now()));
        }
        wait
    }

    pub(super) async fn run(&mut self) {
        // Owned by the `TerminationController` so an off-thread `terminate()`
        // wakes the same loop.
        let notify = self.core.termination.dispatcher_wake();
        let waker = futures::task::waker(Arc::new(DispatcherWaker(notify.clone())));

        loop {
            // 1. Drive the JS event loop one non-blocking step.
            let mut cx = std::task::Context::from_waker(&waker);
            let poll_opts = PollEventLoopOptions {
                wait_for_inspector: false,
            };

            // With no job holding a deadline, still bound this step by
            // `execution_timeout`: work can outlive the job that queued it
            // (a self-requeuing microtask) and would otherwise hang the
            // dispatcher. See tests/test_dispatcher_step_timeout.py.
            let step_watchdog = if self.active_job.is_none() {
                self.core
                    .arm_execution_watchdog("Event loop step exceeded execution_timeout")
            } else {
                None
            };

            let poll_result = self.core.js_runtime.poll_event_loop(&mut cx, poll_opts);

            if let Some(watchdog) = step_watchdog {
                let (fired, duration) = self.core.resolve_watchdog(watchdog);
                if fired {
                    log::warn!(
                        "Event loop step exceeded execution_timeout ({}ms) with no job \
                         active -- terminated a runaway async chain (e.g. a \
                         self-requeuing microtask/promise) that outlived the job \
                         that queued it",
                        duration.as_millis()
                    );
                }
            }

            // `Ready` means nothing is outstanding; `Pending` means the waker
            // will be signalled on progress.
            let event_loop_drained = match poll_result {
                Poll::Ready(Err(err)) => {
                    let runtime_err = self.core.translate_core_error(err);
                    // A termination error is left for the job's own poll to report.
                    if !runtime_error_indicates_termination(&runtime_err) {
                        let runtime_err_debug = format!("{runtime_err:?}");
                        if self.active_job.is_some() {
                            log::error!("Unexpected event loop error: {runtime_err_debug}");
                            self.complete_active_job(Err(runtime_err));
                        } else {
                            log::error!(
                                "JavaScript event loop failed without an active job: {runtime_err_debug}"
                            );
                        }
                        continue;
                    }
                    true
                }
                Poll::Ready(Ok(())) => true,
                Poll::Pending => false,
            };

            // 2. Check whether the active job is complete.
            if let Some(job) = &mut self.active_job {
                if let Poll::Ready(result) = job.poll(&mut self.core) {
                    self.complete_active_job(result);
                }
            }

            // 2b. Honour an off-thread termination request. `terminate_execution`
            // cannot reach a runtime parked on a pending promise (it only trips
            // when V8 next enters JS), so the flag must be checked here. This
            // cannot help a thread wedged inside a host call; that is what
            // `RuntimeHandle`'s force-kill escalation is for.
            if self.core.should_reject_new_work() && self.has_work() {
                log::debug!("Observed off-thread termination request; aborting in-flight work");
                let termination_error = self.core.terminated_error();
                self.cancel_all_jobs(termination_error);
                if let Err(err) = self.core.finalize_termination() {
                    log::warn!("Failed to finalize termination after abort: {err}");
                }
                break;
            }

            // 3. Park until something happens: idle waits only for a command;
            // pending also wakes on the waker or the bounded tick.
            if std::mem::take(&mut self.job_just_activated) {
                continue;
            }

            let idle =
                event_loop_drained && self.active_job.is_none() && self.pending_jobs.is_empty();
            let wait = self.pending_work_wait();
            let should_exit = tokio::select! {
                biased; // Prefer new commands

                cmd = self.cmd_rx.recv() => match cmd {
                    Some(cmd) => self.handle_command(cmd),
                    None => self.handle_channel_closed(),
                },
                // Progress on outstanding work (or, when idle, a stray wake --
                // honouring it costs one iteration and never sleeps through work).
                _ = notify.notified() => false,
                // Job deadline, or the safety net -- see `PENDING_WORK_TICK`.
                _ = tokio::time::sleep(wait), if !idle => false,
            };

            if should_exit {
                break;
            }
        }
    }

    /// Record, finish and clear the active job, then activate the next one.
    fn complete_active_job(&mut self, result: RuntimeResult<crate::runtime::js_value::JSValue>) {
        if let Some(job) = self.active_job.take() {
            self.core
                .stats_state
                .record(job.kind(), job.start_time().elapsed());
            job.finish(&mut self.core, result);
            // Drop stale event loop references.
            self.core.clear_task_locals();
            if let Some(next_job) = self.pending_jobs.pop_front() {
                self.job_just_activated = true;
                self.active_job = Some(next_job);
            }
        }
    }

    /// Activate `job` if the runtime is free, otherwise queue it. Only
    /// activation sets `job_just_activated`.
    fn submit_job(&mut self, job: Box<dyn RuntimeJob>) {
        if self.active_job.is_none() {
            self.job_just_activated = true;
            self.active_job = Some(job);
        } else {
            self.pending_jobs.push_back(job);
        }
    }

    fn has_work(&self) -> bool {
        self.active_job.is_some() || !self.pending_jobs.is_empty()
    }

    /// Fail the active job and every queued job with `error`.
    fn cancel_all_jobs(&mut self, error: RuntimeError) {
        if let Some(job) = self.active_job.take() {
            log::debug!("Cancelling active job");
            job.finish(&mut self.core, Err(error.clone()));
        }
        let pending_count = self.pending_jobs.len();
        if pending_count > 0 {
            log::debug!("Cancelling {pending_count} pending jobs");
        }
        while let Some(job) = self.pending_jobs.pop_front() {
            job.finish(&mut self.core, Err(error.clone()));
        }
        self.core.clear_task_locals();
    }

    /// Run `f` unless the runtime is terminating.
    fn if_alive<T>(
        &mut self,
        f: impl FnOnce(&mut RuntimeCoreState) -> RuntimeResult<T>,
    ) -> RuntimeResult<T> {
        if self.core.should_reject_new_work() {
            Err(self.core.terminated_error())
        } else {
            f(&mut self.core)
        }
    }

    /// Hand back `responder` if a new async job may start; otherwise answer it.
    fn admit_async(&mut self, responder: Responder) -> Option<Responder> {
        match self.core.admit() {
            Ok(()) => Some(responder),
            Err(err) => {
                let _ = responder.send(Err(err));
                None
            }
        }
    }

    /// Handle a command; returns true if the dispatcher should exit.
    fn handle_command(&mut self, cmd: RuntimeCommand) -> bool {
        match cmd {
            RuntimeCommand::Eval { code, responder } => {
                let result = self.core.admit().and_then(|()| {
                    let wd = self
                        .core
                        .arm_execution_watchdog("Synchronous evaluation timed out");
                    self.core
                        .run_timed(wd, "Sync evaluation", |core| core.eval_sync(&code))
                });
                let _ = responder.send(result);
            }
            RuntimeCommand::EvalModule {
                specifier,
                responder,
            } => {
                let result = self.core.admit().and_then(|()| {
                    let wd = self
                        .core
                        .arm_execution_watchdog("Synchronous module evaluation timed out");
                    self.core.run_timed(wd, "Sync module evaluation", |core| {
                        core.eval_module_sync(&specifier)
                    })
                });
                let _ = responder.send(result);
            }
            RuntimeCommand::CallFunctionSync {
                fn_id,
                args,
                timeout_ms,
                responder,
            } => {
                let result = self.core.admit().and_then(|()| {
                    // Armed on the *effective* timeout, so a per-call timeout
                    // works without a runtime-wide `execution_timeout`.
                    let timeout = self.core.effective_timeout_ms(timeout_ms);
                    let wd = self
                        .core
                        .arm_watchdog(timeout, "Synchronous function call timed out");
                    self.core.run_timed(wd, "Sync function call", |core| {
                        core.call_function_sync(fn_id, args, timeout_ms)
                    })
                });
                let _ = responder.send(result);
            }
            RuntimeCommand::EvalAsync {
                code,
                timeout_ms,
                task_locals,
                responder,
            } => {
                if let Some(responder) = self.admit_async(responder) {
                    let timeout = self.core.effective_timeout_ms(timeout_ms);
                    let wd = self
                        .core
                        .arm_watchdog(timeout, "Asynchronous evaluation timed out");
                    let job = eval_async_job(code, timeout, task_locals, responder, wd);
                    self.submit_job(Box::new(job));
                }
            }
            RuntimeCommand::EvalModuleAsync {
                specifier,
                timeout_ms,
                task_locals,
                responder,
            } => {
                if let Some(responder) = self.admit_async(responder) {
                    let timeout = self.core.effective_timeout_ms(timeout_ms);
                    let wd = self
                        .core
                        .arm_watchdog(timeout, "Asynchronous module evaluation timed out");
                    let job =
                        EvalModuleAsyncJob::new(specifier, timeout, task_locals, responder, wd);
                    self.submit_job(Box::new(job));
                }
            }
            RuntimeCommand::CallFunctionAsync {
                fn_id,
                args,
                timeout_ms,
                task_locals,
                responder,
            } => {
                if let Some(responder) = self.admit_async(responder) {
                    let job = call_function_async_job(
                        fn_id,
                        args,
                        timeout_ms,
                        task_locals,
                        responder,
                        &self.core,
                    );
                    self.submit_job(Box::new(job));
                }
            }
            RuntimeCommand::ResumeFunctionCall {
                call_id,
                task_locals,
                responder,
            } => {
                if let Some(responder) = self.admit_async(responder) {
                    match self.core.take_pending_call(call_id) {
                        Ok(pending) => {
                            let job = resume_function_call_job(
                                pending,
                                task_locals,
                                responder,
                                &self.core,
                            );
                            self.submit_job(Box::new(job));
                        }
                        Err(err) => {
                            let _ = responder.send(Err(err));
                        }
                    }
                }
            }
            RuntimeCommand::StreamRead {
                stream_id,
                responder,
            } => {
                if self.core.should_reject_new_work() {
                    let _ = responder.send(Err(self.core.terminated_error()));
                } else {
                    self.submit_job(Box::new(stream_read_job(stream_id, responder)));
                }
            }
            RuntimeCommand::RegisterPythonOp {
                name,
                mode,
                handler,
                responder,
            } => {
                let _ = responder
                    .send(self.if_alive(|core| core.register_python_op(name, mode, handler)));
            }
            RuntimeCommand::SetPythonOpExposure {
                op_id,
                exposed,
                responder,
            } => {
                let _ = responder
                    .send(self.if_alive(|core| Ok(core.set_python_op_exposure(op_id, exposed))));
            }
            RuntimeCommand::SetModuleResolver { handler, responder } => {
                let _ = responder.send(self.if_alive(|core| {
                    core.module_loader.set_resolver(handler);
                    core.sync_loader_task_locals();
                    Ok(())
                }));
            }
            RuntimeCommand::SetModuleLoader { handler, responder } => {
                let _ = responder.send(self.if_alive(|core| {
                    core.module_loader.set_loader(handler);
                    core.sync_loader_task_locals();
                    Ok(())
                }));
            }
            RuntimeCommand::AddStaticModule {
                name,
                source,
                responder,
            } => {
                let _ = responder.send(self.if_alive(|core| {
                    core.module_loader.add_static_module(name, source);
                    Ok(())
                }));
            }
            RuntimeCommand::BindObject {
                name,
                properties,
                responder,
            } => {
                let _ = responder.send(self.if_alive(|core| core.bind_object(name, properties)));
            }
            RuntimeCommand::ReleaseFunction { fn_id, responder } => {
                let _ = responder.send(self.if_alive(|core| core.release_function(fn_id)));
            }
            RuntimeCommand::StreamRelease {
                stream_id,
                responder,
            } => {
                let _ = responder.send(self.core.release_js_stream(stream_id));
            }
            RuntimeCommand::StreamCancel {
                stream_id,
                responder,
            } => {
                let _ = responder.send(self.core.cancel_js_stream(stream_id));
            }
            RuntimeCommand::GetStats { responder } => {
                let _ = responder.send(self.core.collect_stats());
            }
            RuntimeCommand::Terminate { responder } => {
                let termination_error = self.core.terminated_error();
                self.cancel_all_jobs(termination_error);
                let _ = responder.send(self.core.finalize_termination());
                self.cmd_rx.close();
                return true;
            }
            RuntimeCommand::Shutdown { responder } => {
                let leaked_count = self.core.conv.fn_registry.borrow().len();
                if leaked_count > 0 {
                    log::warn!(
                        "Function handles not released before shutdown: {leaked_count} leaked"
                    );
                }
                self.core.conv.fn_registry.borrow_mut().clear();
                self.core.clear_task_locals();
                let _ = responder.send(());
                self.cmd_rx.close();
                return true;
            }
        }
        false
    }

    /// Handle the command channel closing without an explicit shutdown request.
    fn handle_channel_closed(&mut self) -> bool {
        log::warn!("Command channel closed without explicit shutdown - cleaning up");
        self.core
            .termination
            .ensure_reason("Command channel closed unexpectedly");
        let termination_error = self.core.terminated_error();
        self.cancel_all_jobs(termination_error);
        if let Err(err) = self.core.finalize_termination() {
            log::warn!(
                "Failed to finalize termination after command channel closed: {}",
                err
            );
        }
        true
    }
}
