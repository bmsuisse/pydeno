//! The V8 startup snapshot `build.rs` makes, and when a runtime starts from it.
//!
//! It is made under the flags of a default worker, so only a worker started with exactly those
//! uses it (see `STARTUP_SNAPSHOT_V8_FLAGS`).
//!
//! It is plain `deno_core` plus two built-in prototypes the bridge refuses to bind onto
//! (`js/segmenter_prototypes.js`), so a runtime does not spend about 7 ms getting them, nearly all of that
//! ICU start-up. Nothing in it comes from the host or from a guest.

use pyo3::prelude::*;

include!("startup_snapshot_flags.in");

/// What `build.rs` made; empty when it could not (see there).
static BUILTIN: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/startup.snap"));

/// The snapshot a runtime without one of its own starts from, if it may use this one.
///
/// Not when the build has none, not when this process gave V8 flags other than
/// `STARTUP_SNAPSHOT_V8_FLAGS` (V8 only accepts a snapshot under the flags it was made with, and
/// the in-process `Runtime` gives none), and not with `PYDENO_STARTUP_SNAPSHOT=0`, which exists so a
/// comparison or a bug report can switch it off.
pub(crate) fn builtin() -> Option<&'static [u8]> {
    if BUILTIN.is_empty()
        || !super::v8_flags::flags_are(STARTUP_SNAPSHOT_V8_FLAGS)
        || std::env::var_os("PYDENO_STARTUP_SNAPSHOT").is_some_and(|v| v == "0")
    {
        return None;
    }
    Some(BUILTIN)
}

/// Size in bytes of the snapshot a new runtime would start from, 0 if it would start without.
#[pyfunction]
pub fn _startup_snapshot_bytes() -> usize {
    builtin().map_or(0, <[u8]>::len)
}

/// The V8 flags a process must have given V8 for the built-in snapshot to be used.
#[pyfunction]
pub fn _startup_snapshot_flags() -> Vec<String> {
    STARTUP_SNAPSHOT_V8_FLAGS
        .iter()
        .map(|flag| flag.to_string())
        .collect()
}
