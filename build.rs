//! Builds the V8 startup snapshot every worker starts from (`src/runtime/startup_snapshot.rs`).
//!
//! The snapshot is plain `deno_core` plus one script, `src/runtime/js/segmenter_prototypes.js`, whose result
//! is parked on the global object for the bridge to pick up. It holds nothing from the host and
//! nothing a guest can influence: it is made here, at build time, from files in this repository.
//!
//! A snapshot is only valid for the V8 and the V8 flags it was made with, so it is made on the machine that builds
//! the wheel and only when that machine is the target. When it cannot be made (cross compiling,
//! `PYDENO_NO_STARTUP_SNAPSHOT=1`, or V8 failing here) an empty file is written and the runtime
//! starts the way it always did.

use std::path::PathBuf;

include!("src/runtime/startup_snapshot_flags.in");

const SEGMENTER_PROTOTYPES: &str = "src/runtime/js/segmenter_prototypes.js";

fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed={SEGMENTER_PROTOTYPES}");
    println!("cargo:rerun-if-changed=src/runtime/startup_snapshot_flags.in");
    println!("cargo:rerun-if-env-changed=PYDENO_NO_STARTUP_SNAPSHOT");

    let out = PathBuf::from(std::env::var("OUT_DIR").expect("OUT_DIR")).join("startup.snap");
    let native = std::env::var("HOST").ok() == std::env::var("TARGET").ok();
    let wanted = std::env::var_os("PYDENO_NO_STARTUP_SNAPSHOT").is_none();
    let bytes = if native && wanted {
        snapshot()
    } else {
        Vec::new()
    };
    if bytes.is_empty() {
        println!("cargo:warning=pydeno: building without a V8 startup snapshot");
    }
    std::fs::write(out, bytes).expect("write the startup snapshot");
}

fn snapshot() -> Vec<u8> {
    use deno_core::{JsRuntimeForSnapshot, RuntimeOptions};

    let script =
        std::fs::read_to_string(SEGMENTER_PROTOTYPES).expect("read segmenter_prototypes.js");
    // An isolate made for a snapshot registers itself with the current tokio handle.
    let tokio_rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .expect("tokio runtime");
    let _enter = tokio_rt.enter();
    let mut runtime = JsRuntimeForSnapshot::try_new(RuntimeOptions {
        is_main: true,
        ..Default::default()
    })
    .expect("snapshot runtime");
    runtime
        .execute_script(
            "<pydeno_segmenter_prototypes>",
            format!(
                "Object.defineProperty(globalThis, '__pydeno_segmenter_prototypes', \
                 {{ value: {script}, configurable: true, writable: true, enumerable: false }});"
            ),
        )
        .expect("collect the Segmenter prototypes");
    runtime.snapshot().into_vec()
}
