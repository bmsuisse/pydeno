//! Streaming registries bridging JavaScript `ReadableStream`s and Python async iterables.

use crate::runtime::conversion::python_to_js_value;
use crate::runtime::error::{RuntimeError, RuntimeResult};
use crate::runtime::js_value::{JSValue, SerializationLimits};
use deno_core::v8;
use indexmap::IndexMap;
use pyo3::exceptions::PyStopAsyncIteration;
use pyo3::prelude::*;
use pyo3_async_runtimes::TaskLocals;
use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use tokio::sync::Mutex as AsyncMutex;

type PyStreamReleaseCallback = Arc<dyn Fn(u32) + Send + Sync + 'static>;
type PyStreamReleaseListeners = Arc<Mutex<Vec<PyStreamReleaseCallback>>>;

const STREAM_CHUNK_TYPE: &str = "StreamChunk";
const STREAM_CHUNK_DONE_KEY: &str = "done";
const STREAM_CHUNK_VALUE_KEY: &str = "value";

/// Streaming usage counters (active/total per side, bytes per direction).
#[derive(Debug, Default, Clone)]
pub struct StreamStatsSnapshot {
    pub active_js_streams: u64,
    pub active_py_streams: u64,
    pub total_js_streams: u64,
    pub total_py_streams: u64,
    pub bytes_streamed_js_to_py: u64,
    pub bytes_streamed_py_to_js: u64,
}

impl StreamStatsSnapshot {
    pub fn merge(&mut self, other: &StreamStatsSnapshot) {
        macro_rules! add {
            ($($f:ident),*) => { $(self.$f = self.$f.saturating_add(other.$f);)* };
        }
        add!(
            active_js_streams,
            active_py_streams,
            total_js_streams,
            total_py_streams,
            bytes_streamed_js_to_py,
            bytes_streamed_py_to_js
        );
    }
}

/// A chunk pulled from a JS or Python stream, shaped like
/// `ReadableStreamDefaultReader.read()`'s result.
#[derive(Debug, Clone)]
pub struct StreamChunk {
    pub done: bool,
    pub value: Option<JSValue>,
}

impl StreamChunk {
    pub fn to_js_value(&self) -> JSValue {
        let mut map = IndexMap::new();
        map.insert(
            "__pydeno_type".to_string(),
            JSValue::String(STREAM_CHUNK_TYPE.to_string()),
        );
        map.insert(STREAM_CHUNK_DONE_KEY.to_string(), JSValue::Bool(self.done));
        if let Some(value) = &self.value {
            map.insert(STREAM_CHUNK_VALUE_KEY.to_string(), value.clone());
        }
        JSValue::Object(map)
    }

    pub fn from_js_value(payload: JSValue) -> RuntimeResult<Self> {
        let JSValue::Object(mut map) = payload else {
            return Err(RuntimeError::internal(format!(
                "Unexpected chunk payload: {:?}",
                payload
            )));
        };
        let tagged = matches!(
            map.shift_remove("__pydeno_type"),
            Some(JSValue::String(tag)) if tag == STREAM_CHUNK_TYPE
        );
        // Tagged chunks require a bool; untagged ones also accept "true"/"false".
        let done = match (tagged, map.shift_remove(STREAM_CHUNK_DONE_KEY)) {
            (_, Some(JSValue::Bool(flag))) => flag,
            (true, other) => {
                return Err(RuntimeError::internal(format!(
                    "Stream chunk missing done flag: {:?}",
                    other
                )))
            }
            (false, Some(JSValue::String(text))) if text == "true" => true,
            (false, Some(JSValue::String(text))) if text == "false" => false,
            (false, Some(other)) => {
                return Err(RuntimeError::internal(format!(
                    "Invalid done field for stream chunk: {:?}",
                    other
                )))
            }
            (false, None) => return Err(RuntimeError::internal("Stream chunk missing done field")),
        };
        let value = map.shift_remove(STREAM_CHUNK_VALUE_KEY);
        Ok(StreamChunk { done, value })
    }
}

#[derive(Default)]
struct JsStreamStats {
    active: u64,
    total: u64,
    bytes: u64,
}

impl JsStreamStats {
    fn snapshot(&self) -> StreamStatsSnapshot {
        StreamStatsSnapshot {
            active_js_streams: self.active,
            total_js_streams: self.total,
            bytes_streamed_js_to_py: self.bytes,
            ..StreamStatsSnapshot::default()
        }
    }
}

struct JsStreamEntry {
    stream: v8::Global<v8::Value>,
    reader: Option<v8::Global<v8::Object>>,
    chunks: u64,
    transferred_bytes: u64,
}

/// Call `obj[name]()` with the given error messages for each failure step
/// (allocating the key, missing property, not callable, threw).
fn call_js_method<'s>(
    scope: &mut v8::PinScope<'s, '_>,
    obj: v8::Local<'s, v8::Object>,
    name: &str,
    [alloc_err, missing_err, not_fn_err, threw_err]: [&'static str; 4],
) -> RuntimeResult<v8::Local<'s, v8::Value>> {
    let key = v8::String::new(scope, name).ok_or_else(|| RuntimeError::internal(alloc_err))?;
    let value = obj
        .get(scope, key.into())
        .ok_or_else(|| RuntimeError::internal(missing_err))?;
    let func = v8::Local::<v8::Function>::try_from(value)
        .map_err(|_| RuntimeError::internal(not_fn_err))?;
    func.call(scope, obj.into(), &[])
        .ok_or_else(|| RuntimeError::internal(threw_err))
}

/// Registry of live JS `ReadableStream` handles (and their readers) consumed
/// from Python.
#[derive(Default)]
pub struct JsStreamRegistry {
    entries: RefCell<HashMap<u32, JsStreamEntry>>,
    next_id: Cell<u32>,
    stats: RefCell<JsStreamStats>,
}

impl JsStreamRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register_stream(
        &self,
        scope: &mut v8::PinScope<'_, '_>,
        stream_value: v8::Local<'_, v8::Value>,
    ) -> u32 {
        let id = self.next_id.get();
        self.next_id.set(id.wrapping_add(1));
        let entry = JsStreamEntry {
            stream: v8::Global::new(scope, stream_value),
            reader: None,
            chunks: 0,
            transferred_bytes: 0,
        };
        self.entries.borrow_mut().insert(id, entry);
        let mut stats = self.stats.borrow_mut();
        stats.active = stats.active.saturating_add(1);
        stats.total = stats.total.saturating_add(1);
        id
    }

    pub fn release(&self, stream_id: u32) {
        if self.entries.borrow_mut().remove(&stream_id).is_some() {
            let mut stats = self.stats.borrow_mut();
            stats.active = stats.active.saturating_sub(1);
        }
    }

    pub fn stats_snapshot(&self) -> StreamStatsSnapshot {
        self.stats.borrow().snapshot()
    }

    pub fn ensure_reader<'s>(
        &self,
        scope: &mut v8::PinScope<'s, '_>,
        stream_id: u32,
    ) -> RuntimeResult<v8::Local<'s, v8::Object>> {
        let mut entries = self.entries.borrow_mut();
        let entry = entries
            .get_mut(&stream_id)
            .ok_or_else(|| RuntimeError::internal("Unknown stream id"))?;
        if let Some(existing) = &entry.reader {
            return Ok(v8::Local::new(scope, existing));
        }
        let stream_local: v8::Local<v8::Value> = v8::Local::new(scope, &entry.stream);
        let stream_obj = stream_local
            .to_object(scope)
            .ok_or_else(|| RuntimeError::internal("ReadableStream is not an object"))?;
        let reader_value = call_js_method(
            scope,
            stream_obj,
            "getReader",
            [
                "Failed to allocate getReader string",
                "ReadableStream.getReader missing",
                "getReader is not a function",
                "getReader threw",
            ],
        )?;
        let reader_obj = reader_value
            .to_object(scope)
            .ok_or_else(|| RuntimeError::internal("getReader did not return object"))?;
        entry.reader = Some(v8::Global::new(scope, reader_obj));
        Ok(reader_obj)
    }

    pub fn start_read(
        &self,
        scope: &mut v8::PinScope<'_, '_>,
        stream_id: u32,
    ) -> RuntimeResult<v8::Global<v8::Promise>> {
        let reader = self.ensure_reader(scope, stream_id)?;
        let promise_value = call_js_method(
            scope,
            reader,
            "read",
            [
                "Failed to allocate read key",
                "Reader.read missing",
                "Reader.read is not callable",
                "Reader.read threw",
            ],
        )?;
        let promise = v8::Local::<v8::Promise>::try_from(promise_value)
            .map_err(|_| RuntimeError::internal("Reader.read must return a promise"))?;
        Ok(v8::Global::new(scope, promise))
    }

    pub fn update_stats_after_chunk(&self, stream_id: u32, chunk: &StreamChunk) {
        let mut entries = self.entries.borrow_mut();
        if let Some(entry) = entries.get_mut(&stream_id) {
            entry.chunks = entry.chunks.saturating_add(1);
            if let Some(JSValue::Bytes(bytes)) = &chunk.value {
                entry.transferred_bytes =
                    entry.transferred_bytes.saturating_add(bytes.len() as u64);
                let mut stats = self.stats.borrow_mut();
                stats.bytes = stats.bytes.saturating_add(bytes.len() as u64);
            }
        }
    }
}

/// Registry of Python async iterables exposed to JavaScript as
/// `ReadableStream`s. Clones share state (handle and runtime thread).
#[derive(Clone)]
pub struct PyStreamRegistry {
    entries: Arc<Mutex<HashMap<u32, Arc<PyStreamEntry>>>>,
    next_id: Arc<AtomicU32>,
    active: Arc<AtomicU64>,
    total: Arc<AtomicU64>,
    /// Bytes transferred from Python to JavaScript.
    bytes: Arc<AtomicU64>,
    release_listeners: PyStreamReleaseListeners,
    serialization_limits: SerializationLimits,
}

impl PyStreamRegistry {
    pub fn new(serialization_limits: SerializationLimits) -> Self {
        Self {
            entries: Arc::new(Mutex::new(HashMap::new())),
            next_id: Arc::new(AtomicU32::new(1)),
            active: Arc::new(AtomicU64::new(0)),
            total: Arc::new(AtomicU64::new(0)),
            bytes: Arc::new(AtomicU64::new(0)),
            release_listeners: Arc::new(Mutex::new(Vec::new())),
            serialization_limits,
        }
    }

    pub fn add_release_listener<F>(&self, listener: F)
    where
        F: Fn(u32) + Send + Sync + 'static,
    {
        self.release_listeners
            .lock()
            .unwrap()
            .push(Arc::new(listener));
    }

    fn notify_release_listeners(&self, stream_id: u32) {
        // Clone out so listeners run without holding the lock.
        let listeners = self.release_listeners.lock().unwrap().clone();
        for listener in listeners {
            listener(stream_id);
        }
    }

    pub fn register_iterable(
        &self,
        iterable: Py<PyAny>,
        task_locals: TaskLocals,
    ) -> RuntimeResult<u32> {
        let stream_id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let entry = Arc::new(PyStreamEntry {
            iterable,
            iterator: AsyncMutex::new(None),
            task_locals,
            closed: AtomicBool::new(false),
            serialization_limits: self.serialization_limits,
        });
        self.entries.lock().unwrap().insert(stream_id, entry);
        self.active.fetch_add(1, Ordering::Relaxed);
        self.total.fetch_add(1, Ordering::Relaxed);
        Ok(stream_id)
    }

    pub async fn pull_next(&self, stream_id: u32) -> RuntimeResult<StreamChunk> {
        let entry = self
            .entries
            .lock()
            .unwrap()
            .get(&stream_id)
            .cloned()
            .ok_or_else(|| RuntimeError::internal("Unknown Python stream id"))?;

        let chunk = entry.next_chunk().await?;
        if let Some(JSValue::Bytes(bytes)) = &chunk.value {
            self.bytes.fetch_add(bytes.len() as u64, Ordering::Relaxed);
        }
        if chunk.done {
            self.release(stream_id);
        }
        Ok(chunk)
    }

    pub async fn cancel(&self, stream_id: u32) -> RuntimeResult<()> {
        if let Some(entry) = self.remove_entry(stream_id) {
            entry.cancel().await?;
        }
        Ok(())
    }

    pub fn release(&self, stream_id: u32) {
        let _ = self.remove_entry(stream_id);
    }

    fn remove_entry(&self, stream_id: u32) -> Option<Arc<PyStreamEntry>> {
        let removed = self.entries.lock().unwrap().remove(&stream_id);
        if removed.is_some() {
            self.active.fetch_sub(1, Ordering::Relaxed);
            self.notify_release_listeners(stream_id);
        }
        removed
    }

    pub fn stats_snapshot(&self) -> StreamStatsSnapshot {
        StreamStatsSnapshot {
            active_py_streams: self.active.load(Ordering::Relaxed),
            total_py_streams: self.total.load(Ordering::Relaxed),
            bytes_streamed_py_to_js: self.bytes.load(Ordering::Relaxed),
            ..StreamStatsSnapshot::default()
        }
    }
}

struct PyStreamEntry {
    iterable: Py<PyAny>,
    iterator: AsyncMutex<Option<Py<PyAny>>>,
    task_locals: TaskLocals,
    closed: AtomicBool,
    serialization_limits: SerializationLimits,
}

fn is_stop_async_iteration(err: &PyErr) -> bool {
    Python::attach(|py| err.is_instance_of::<PyStopAsyncIteration>(py))
}

impl PyStreamEntry {
    async fn ensure_iterator(&self) -> RuntimeResult<Py<PyAny>> {
        let mut guard = self.iterator.lock().await;
        if let Some(it) = guard.as_ref() {
            return Python::attach(|py| Ok(it.clone_ref(py)));
        }
        let iterator = Python::attach(|py| {
            self.iterable
                .bind(py)
                .call_method0(pyo3::intern!(py, "__aiter__"))
                .map(|obj| obj.into_any().unbind())
                .map_err(|err| RuntimeError::internal(format!("Failed to get __aiter__: {err}")))
        })?;
        Python::attach(|py| {
            *guard = Some(iterator.clone_ref(py));
        });
        Ok(iterator)
    }

    async fn next_chunk(&self) -> RuntimeResult<StreamChunk> {
        let iterator = self.ensure_iterator().await?;
        let future = Python::attach(|py| {
            let iterator_bound = iterator.bind(py);
            let awaitable = iterator_bound
                .call_method0(pyo3::intern!(py, "__anext__"))
                .map_err(|err| RuntimeError::internal(format!("__anext__ failed: {err}")))?;
            pyo3_async_runtimes::into_future_with_locals(&self.task_locals, awaitable)
                .map_err(|err| RuntimeError::internal(format!("Failed to await __anext__: {err}")))
        })?;

        match future.await {
            Ok(value) => {
                let js_value = Python::attach(|py| {
                    python_to_js_value(value.into_bound(py), &self.serialization_limits).map_err(
                        |err| RuntimeError::internal(format!("Chunk conversion failed: {err}")),
                    )
                })?;
                Ok(StreamChunk {
                    done: false,
                    value: Some(js_value),
                })
            }
            Err(err) if is_stop_async_iteration(&err) => Ok(StreamChunk {
                done: true,
                value: None,
            }),
            Err(err) => Err(RuntimeError::internal(format!(
                "Python stream errored: {err}"
            ))),
        }
    }

    async fn cancel(&self) -> RuntimeResult<()> {
        if self.closed.swap(true, Ordering::SeqCst) {
            return Ok(());
        }
        let iterator_opt = {
            let guard = self.iterator.lock().await;
            Python::attach(|py| guard.as_ref().map(|it| it.clone_ref(py)))
        };
        let Some(iterator) = iterator_opt else {
            return Ok(());
        };

        let future = Python::attach(|py| {
            let iterator_bound = iterator.bind(py);
            match iterator_bound.getattr(pyo3::intern!(py, "aclose")) {
                Ok(aclose) => {
                    let awaitable = aclose
                        .call0()
                        .map_err(|err| RuntimeError::internal(format!("aclose() failed: {err}")))?;
                    pyo3_async_runtimes::into_future_with_locals(&self.task_locals, awaitable)
                        .map_err(|err| {
                            RuntimeError::internal(format!("Failed to await aclose(): {err}"))
                        })
                        .map(Some)
                }
                Err(_) => Ok(None),
            }
        })?;

        match future {
            Some(fut) => match fut.await {
                Err(err) if !is_stop_async_iteration(&err) => {
                    Err(RuntimeError::internal(format!("aclose() errored: {err}")))
                }
                _ => Ok(()),
            },
            None => Ok(()),
        }
    }
}
