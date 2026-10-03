# Supply-chain security

pydeno ships a Rust extension (deno_core, V8, PyO3, about 270 crates in `Cargo.lock`) and a Python
package that has **no runtime Python dependencies**. The dev, docs and example groups in
`pyproject.toml` pull a further ~150 packages into `uv.lock`; those never reach a wheel.

`.github/workflows/security.yml` runs on pull requests that touch `Cargo.*`, `pyproject.toml`,
`uv.lock`, `deny.toml` or a workflow, on every push to `main`, and weekly (new advisories appear
without any change here).

## What is scanned

| Tool | Scans | Fails the job on | Policy |
|---|---|---|---|
| `cargo-deny` | `Cargo.lock` | RustSec vulnerabilities and unsound crates, disallowed licences, wildcard version requirements, non-crates.io or git sources. Duplicate versions and yanked crates only warn. | `deny.toml` |
| `cargo-audit` | `Cargo.lock` | RustSec vulnerabilities. Unmaintained and yanked crates only warn. | `ignore:` input in the workflow, mirrored from `deny.toml` |
| `pip-audit` | `uv.lock`, exported | any known PyPI vulnerability | none |
| `osv-scanner` | `Cargo.lock` and `uv.lock` | **never** (`continue-on-error`) | none |

`cargo-deny` and `cargo-audit` read the same RustSec database; running both is cheap and catches
tool-specific differences. `osv-scanner` is an advisory cross-check because it cannot read the
reasoned ignores in `deny.toml` (that needs an `osv-scanner.toml`), so accepted findings would keep
it permanently red. Read its log for GHSA and PYSEC ids the other tools do not index.

## Running locally

All commands are read-only. None modifies `Cargo.lock` or `uv.lock`.

```bash
# Rust. Install once; use a throwaway target dir so the repo's target/ is untouched.
CARGO_TARGET_DIR=/tmp/cargo-tools cargo install --locked cargo-deny cargo-audit
cargo deny check                 # advisories, licenses, bans, sources
cargo deny check advisories      # one section at a time
cargo audit

# Python. Export from the lock file, then audit the export.
uv export --frozen --all-groups --no-emit-project --no-hashes -o /tmp/requirements.txt
uvx pip-audit -r /tmp/requirements.txt --no-deps --disable-pip

# Both ecosystems (brew install osv-scanner)
osv-scanner scan -L Cargo.lock -L uv.lock
```

## Handling a finding

1. **Is it reachable?** Read the advisory, then grep `src/` and `python/` for the affected API.
   Most findings in a V8 embedder's dependency tree concern code paths pydeno never calls.
2. **Can we fix it?** Prefer `cargo update -p <crate>` (Rust) or `uv lock --upgrade-package <pkg>`
   (Python) when a patched version is semver-compatible. A fix that needs a major bump of
   `pyo3`, `deno_core` or `v8` goes through the upgrade path below.
3. **If it must wait,** add an entry to `[advisories].ignore` in `deny.toml` with `reason = ...` and a
   comment saying why it is not reachable and **what event removes the entry**. Mirror the id into
   the `ignore:` input of the `cargo-audit` job. An ignore with no removal condition is a bug.
4. **Never** loosen `[licenses]`, `[sources]` or `unknown-git` to make a build pass without a
   written reason in the same change. `r-efi` is licensed `MIT OR Apache-2.0 OR LGPL-2.1-or-later`
   and passes on the MIT alternative; LGPL stays off the allow list.
5. Python findings in dev-only groups are a developer-machine risk, not a wheel risk, but still
   upgrade them: `uv lock --upgrade-package <pkg>`, review the diff, commit.

## State at the time of writing

* Excepted in `deny.toml`: `RUSTSEC-2026-0176` and `RUSTSEC-2026-0177` (both `pyo3` 0.27.2). The
  fix is `pyo3 >= 0.29`, but `pyo3-async-runtimes` 0.27 pins `pyo3` 0.27. pydeno calls neither
  `nth`/`nth_back` on list/tuple iterators nor `PyCFunction::new_closure`. Remove both once
  `pyo3-async-runtimes` releases for `pyo3 >= 0.29`.
* Not excepted, shown as warnings: `paste` is unmaintained (`RUSTSEC-2024-0436`, a build-time proc
  macro pulled in by the `v8` crate), and `yoke-derive` 0.8.3 is yanked
  (`cargo update -p yoke-derive` fixes it).
* `pip-audit` reports `pyjwt` 2.14.0 (`PYSEC-2026-4141`, fixed in 2.15.0), reached only through
  `mcp` in the `examples` group. The job fails until `uv.lock` is refreshed.

## The V8 and deno_core upgrade path

Be honest about the limit of this tooling: **none of these scanners sees V8 vulnerabilities.** V8 is
a prebuilt static library fetched by the `v8` crate's build script; RustSec and OSV track the Rust
crate, not the Chromium V8 CVEs inside it. A V8 security fix reaches pydeno only when `deno_core`
moves to a `v8` crate built on a newer V8, and we then bump `deno_core` and rebuild.

That gap is what `.github/workflows/engine-watch.yml` and `scripts/check_engine.py` cover: weekly,
they compare the V8 crate in `Cargo.lock` with what the newest `deno_core` depends on, and fail
only when an upgrade would raise the V8 version. Newer `deno_core` is not automatically newer V8
(0.410 and later moved to the `deno_v8` facade, which has resolved to an older pre-release), which
is why the check asks the V8 question and not "is there a newer `deno_core`".

When the watch fires:

1. Bump `deno_core` (and `deno_error` if needed) in `Cargo.toml`, then `cargo update -p deno_core`.
2. Re-run `cargo deny check`, `cargo audit` and the full test suite. `deno_core` is the largest
   source of churn in this tree and its API changes between minors.
3. Re-check the `[profile.dev.package."*"] overflow-checks` workaround in `Cargo.toml`; it exists
   only because of a `deno_core` bug and should be removed once that is fixed.
4. For untrusted code, remember the other mitigation: `IsolatedRuntime` runs V8 `--jitless` in an
   OS-sandboxed worker process, so a V8 bug in the window before an upgrade lands is contained
   rather than fatal. See the architecture page.
