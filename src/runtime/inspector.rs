//! DevTools inspector support.
//!
//! The HTTP/WebSocket server (`server`, which pulls in `hyper`, `hyper-util`,
//! `fastwebsockets` and tokio's `net` feature) is compiled only with the
//! `inspector` cargo feature, on by default. Without it the types below still
//! exist, so `InspectorConfig` and `inspector_endpoints()` keep their shape,
//! but creating a runtime with an inspector configured fails with
//! [`INSPECTOR_UNAVAILABLE`].

use serde::Serialize;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

#[cfg(feature = "inspector")]
mod server;
#[cfg(feature = "inspector")]
pub use server::{InspectorRegistration, InspectorRegistrationParams, InspectorServer};

/// The error a runtime creation reports when an inspector is configured on a
/// build without the `inspector` feature.
#[cfg(not(feature = "inspector"))]
pub const INSPECTOR_UNAVAILABLE: &str = "pydeno was built without inspector support (cargo \
     feature `inspector` is off), so RuntimeConfig(inspector=...) cannot be used; install the \
     standard wheel or rebuild with the feature enabled";

/// Metadata shared back to Python exposing debugger entry-points.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct InspectorMetadata {
    pub id: String,
    #[serde(rename = "webSocketDebuggerUrl")]
    pub websocket_url: String,
    pub devtools_frontend_url: String,
    pub title: String,
    pub description: String,
    #[serde(rename = "url")]
    pub target_url: String,
    pub favicon_url: String,
    #[serde(skip_serializing)]
    pub host: String,
    #[serde(rename = "type")]
    pub target_type: String,
}

#[derive(Clone, Default)]
pub struct InspectorConnectionState {
    connected: Arc<AtomicBool>,
}

impl InspectorConnectionState {
    pub fn mark_connected(&self) {
        self.connected.store(true, Ordering::SeqCst);
    }

    pub fn is_connected(&self) -> bool {
        self.connected.load(Ordering::SeqCst)
    }
}
