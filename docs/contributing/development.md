# Development Guide

## Prerequisites

!!! warning "Non-macOS Platforms"
    Building on Linux and Windows requires compiling `rusty_v8` from source, which can take 30+ minutes and requires additional dependencies (Python, Clang/LLVM, etc.). See the [rusty_v8 build documentation](https://github.com/denoland/rusty_v8?tab=readme-ov-file#build-v8-from-source) for platform-specific requirements.

- **Python**: 3.10 or higher
- **Rust**: Latest stable toolchain (install via [rustup](https://rust-lang.org/tools/install/))
- **uv**: Fast Python package manager (install via [uv docs](https://docs.astral.sh/uv/getting-started/installation/))
- **Make**: Build automation tool (usually pre-installed on macOS/Linux)

## Quick Start

1. **Clone**: `git clone https://github.com/bmsuisse/pydeno.git && cd pydeno`
2. **Install**: `make install` - Installs all Python dependencies using `uv`
3. **Build**: `make build-dev` - Compiles Rust code using [maturin](https://www.maturin.rs/)
4. **Test**: `make test` (or `make test-quiet` for less output)

## Common Development Tasks

- **Format code**: `make format` - Auto-format both Python and Rust code
- **Lint code**: `make lint` - Check code style without making changes
- **Fix linting**: `make lint-python-fix` - Auto-fix Python linting issues
- **Build docs**: `make docs` - Build the documentation site
- **Serve docs**: `make docs-serve` - Serve docs locally at http://127.0.0.1:8000 with live reload
- **Run CI locally**: `make all` - Run the full CI pipeline (format, build, lint, test)
- **Clean artifacts**: `make clean` - Remove build artifacts and caches

## Development Workflow

1. **Create a feature branch**: `git checkout -b feature/my-feature`
2. **Make changes**: Edit Python code in `python/pydeno/` or Rust code in `src/`
3. **Rebuild**: Run `make build-dev` after Rust changes
4. **Test**: Run `make test` to verify your changes
5. **Format and lint**: Run `make format` and `make lint`
6. **Commit**: Make commits with clear messages
7. **Run full CI**: Run `make all` before pushing
8. **Push and create PR**: Push your branch and open a pull request

## Project Structure

```
python/pydeno/        # Python API and bindings
src/                 # Rust core implementation
    lib.rs          # PyO3 module definition
    runtime/        # V8 runtime implementation
tests/              # Python test suite
docs/               # MkDocs documentation
examples/           # Usage examples
Makefile            # Development automation
pyproject.toml      # Python project configuration
```

## Tips

- **Use `make help`** to see all available Make targets
- **Development builds are faster** but production builds (`make build-prod`) are optimized for performance
- **Pre-commit hooks** run automatically after `make install` to catch issues early
- **Run tests frequently** to catch regressions quickly
- **Check the Makefile** for additional tasks and customizations

## See Also

- See [Architecture](architecture.md) for implementation details
- Check existing [issues](https://github.com/bmsuisse/pydeno/issues) or open a new one

## Release artifact checks

The `Publish to PyPI` workflow validates the artifacts built in that run before publishing:

- Linux wheels on native x86-64 and ARM64, for CPython 3.10–3.14. Each job records the installed
  wheel's SHA-256 and requires successful collection, a complete JUnit report, at least 700 tests,
  zero skips, and no more than the 10 documented expected failures.
- The hostile-guest metric battery on CPython 3.12 for both architectures. The gate checks the
  reported metric is exactly zero; the metric script's exit status alone does not prove this.
- The full suite inside a root Linux container on both architectures, including tests that need
  to exercise privilege dropping. Three additional container profiles test unavailable Landlock,
  unavailable seccomp, and neither layer, using the isolation and escape suites. Each containment
  job requires at least 500 selected tests. Containers share the runner's kernel; this does not
  establish coverage of every supported kernel or production security policy.

Collection logs, JUnit reports, wheel hashes and container pytest logs are retained as workflow
artifacts, including on failure. The container runner bounds collection to 180 seconds. It never
uses a Python source overlay in release jobs.

A manual workflow run or a pull request changing this workflow or its report/container helpers
exercises these gates without publishing. Only the `release: published` event can reach the
publishing job, after all required jobs succeed. macOS and Windows artifacts are built here;
these added release gates specifically cover Linux. Their broader platform tests remain in the
`Platforms` workflow.
