fn main() {
    // The target triple is only known to cargo while it builds; `_build_identity` reports it.
    println!(
        "cargo:rustc-env=PYDENO_BUILD_TARGET={}",
        std::env::var("TARGET").expect("cargo sets TARGET for build scripts")
    );
    println!("cargo:rerun-if-changed=build.rs");
}
