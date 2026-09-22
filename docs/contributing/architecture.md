# Architecture Overview

!!! note "Advanced Technical Content"
    This section is intended for contributors, library developers, and users who need deep technical understanding of pydeno's internals. For general usage, see the [Concepts](../concepts/runtime.md) section.

## Multi-Layer Design

pydeno is built with three distinct layers that communicate via well-defined boundaries:

```mermaid
graph TB
    subgraph "Layer 1: Python API"
        PYRT[Python Runtime class]
    end

    subgraph "Layer 2: Rust-Python Bridge"
        BRIDGE[src/lib.rs<br/>PyO3 bindings]
        PYRT_RS[src/runtime/python.rs]
    end

    subgraph "Layer 3: Rust Core"
        HANDLE[src/runtime/handle.rs<br/>RuntimeHandle]

        subgraph "Runtime Thread"
            TOKIO[Tokio Runtime<br/><i>single-threaded event loop</i>]

            subgraph "Inside Event Loop"
                DISPATCHER[RuntimeDispatcher]
                CORE[RuntimeCoreState]
                DENO[deno_core::JsRuntime]
                V8[V8 Isolate]
            end
        end
    end

    PYRT --> BRIDGE
    BRIDGE --> PYRT_RS
    PYRT_RS --> HANDLE
    HANDLE -->|spawns thread| TOKIO
    TOKIO -->|block_on| DISPATCHER
    DISPATCHER -->|owns| CORE
    CORE -->|contains| DENO
    DENO -->|wraps| V8
```

### Layer 1: Python API

**Location**: `python/pydeno/__init__.py`

The user-facing Python interface that provides:

- Convenience functions (`pydeno.eval()`, `pydeno.eval_async()`)
- High-level abstractions over the Rust runtime
- Pythonic API design

### Layer 2: Rust-Python Bridge

**Location**: `src/lib.rs`, `src/runtime/python.rs`

PyO3-based bindings that:

- Expose Rust types to Python (e.g., `Runtime`, `RuntimeConfig`, `JsFunction`)
- Handle Python GIL management
- Convert between Python and Rust types
- Define Python exceptions (`PyRuntimeError`)

Key classes:

- `Runtime` - Python wrapper around `RuntimeHandle`
- `JsFunction` - Python proxy for JavaScript functions
- `JsStream` - Python wrapper for JavaScript async iterators
- `JsUndefined` - Sentinel for JavaScript `undefined`

### Layer 3: Rust Core

**Location**: `src/runtime/`

The core JavaScript execution engine built on `deno_core` and V8:

- **RuntimeHandle** (`handle.rs`) - Thread-safe handle for communicating with runtime
- **RuntimeDispatcher** (`runner.rs`) - Multiplexes command processing with async job execution
- **RuntimeCoreState** (`runner.rs`) - Owns the V8 isolate and runtime state
- **RuntimeConfig** (`config.rs`) - Configuration options
- **PythonOpRegistry** (`ops.rs`) - Host function registry for the
  Python-JavaScript bridge. Handlers are addressed by unguessable capability
  tokens and dispatched only after a bind step exposed them, so guest JS can
  invoke exactly the ops it was given and nothing else; see the module docs
  for the reasoning.
- **PythonModuleLoader** (`loader.rs`) - Module resolution and loading
- **Context** (`context.rs`) - Isolated execution contexts

## Key Components

### RuntimeHandle

**Purpose**: Thread-safe communication between Python thread and runtime thread

```rust
pub struct RuntimeHandle {
    sender: mpsc::UnboundedSender<HostCommand>,
    shutdown_state: Arc<Mutex<ShutdownState>>,
    // ...
}
```

**Key characteristics**:

- Clone-safe (uses `Arc` internally)
- Sends commands via async channel (`mpsc::UnboundedSender`)
- Does NOT auto-shutdown on drop (explicit `close()` required)
- Thread-safe via `Arc<Mutex>` for shutdown state

**Lifetime**: The handle does not own the runtime thread. You can clone handles and they'll all point to the same runtime. The runtime only shuts down when `close()` is explicitly called.

### RuntimeCoreState

**Purpose**: Owns the V8 isolate and processes commands on the runtime thread

```rust
struct RuntimeCoreState {
    js_runtime: JsRuntime,
    registry: PythonOpRegistry,
    module_loader: Rc<PythonModuleLoader>,
    fn_registry: Rc<RefCell<HashMap<u32, StoredFunction>>>,
    termination: TerminationController,
    // ...
}
```

**Key characteristics**:

- Lives on the dedicated runtime thread
- NOT Send or Sync (V8 isolates are single-threaded)
- Owned by `RuntimeDispatcher` which processes `RuntimeCommand` messages from handle
- Uses `deno_core::JsRuntime` directly (not wrapped in a custom struct)
- Handles promise polling with microtask checkpoints via async jobs

**Thread model**: Each `RuntimeCoreState` runs on its own OS thread with:

- V8 isolate (single-threaded JavaScript execution)
- Tokio single-threaded runtime (for async operations)
- Command receiver loop managed by `RuntimeDispatcher`

### Command Flow

The communication between Python and JavaScript follows this pattern:

```mermaid
sequenceDiagram
    participant Python
    participant Handle as RuntimeHandle
    participant Channel as Command Channel
    participant Tokio as Tokio Runtime
    participant Dispatcher as RuntimeDispatcher
    participant Core as RuntimeCoreState
    participant V8 as V8 Isolate

    Python->>Handle: runtime.eval("2 + 2")
    Handle->>Channel: Send RuntimeCommand::Eval
    Channel->>Tokio: Runtime thread event loop
    Tokio->>Dispatcher: Receive command
    Dispatcher->>Core: Process sync command
    Core->>V8: Compile and execute script
    V8-->>Core: Return result
    Core-->>Dispatcher: Return result
    Dispatcher-->>Channel: Send result via responder
    Channel-->>Handle: Receive result
    Handle-->>Python: Return converted value
```

**Command types** (defined in `RuntimeCommand` enum):

- `Eval` / `EvalAsync` - Execute JavaScript code
- `BindObject` - Register Python object
- `CallFunctionSync` / `CallFunctionAsync` - Invoke JavaScript function
- `RegisterPythonOp` - Register Python function as JavaScript op
- `SetModuleResolver` / `SetModuleLoader` - Configure module system
- `AddStaticModule` - Register static module
- `GetStats` - Query runtime statistics
- `Terminate` / `Shutdown` - Close runtime

## Thread Safety Guarantees

### V8 Isolate Constraints

V8 isolates are **NOT thread-safe**:

- Cannot be moved between threads (not `Send`)
- Cannot be accessed from multiple threads (not `Sync`)
- All V8 operations must happen on the thread that created the isolate

pydeno handles this by:

1. Creating each isolate on a dedicated thread
2. Never moving the isolate
3. Using message passing for cross-thread communication

### RuntimeHandle Safety

At the Rust level, `RuntimeHandle` **IS thread-safe**:

- Uses `Arc` for shared ownership
- Commands sent via thread-safe channel
- Can be cloned and used from multiple threads
- Shutdown protected by `Arc<Mutex<ShutdownState>>`

However, the **Python `Runtime` class is NOT thread-safe**:

- Marked as `unsendable` in PyO3 (cannot be sent across threads)
- Uses `RefCell` internally (not thread-safe)
- Cannot be passed to another Python thread
- Each thread must create its own `Runtime` instance

This design choice prevents accidental misuse from Python while allowing the underlying Rust implementation to be efficient.

### GIL Release

Python's Global Interpreter Lock (GIL) is **released** during:

- JavaScript execution (`eval`, `eval_async`)
- Waiting for command responses
- Module evaluation

This enables true parallelism: Python threads can run while JavaScript executes.

## Data Flow and Type Conversion

### Python → JavaScript

1. Python object → `python_to_js_value()` → `JSValue` (Rust)
2. `JSValue` → `serde_json::to_value()` → `serde_json::Value`
3. Pass to `deno_core` → Converted to V8 value
4. V8 value available in JavaScript

### JavaScript → Python

1. V8 value → `deno_core` extraction → `serde_json::Value`
2. `serde_json::Value` → `js_value_to_python()` → Python object

### Special Cases

**Undefined**: JavaScript `undefined` uses a sentinel (`pydeno.undefined`) because Python has no native equivalent.

**Binary data**: 
- JS `Uint8Array`/`ArrayBuffer` → Python `bytes`
- Python `bytes`/`bytearray`/`memoryview` → JS `Uint8Array`

**Dates**: JS `Date` ↔ Python `datetime` (UTC normalized)

**Sets**: JS `Set` ↔ Python `set`

**BigInt**: JS `BigInt` ↔ Python `int` (arbitrary precision)

**Functions**: JS functions become `JsFunction` proxies in Python that send `CallFunction` commands when invoked.

## Ops System

The ops system allows Python functions to be called from JavaScript:

```mermaid
sequenceDiagram
    participant JS as JavaScript
    participant V8
    participant Op as Op Callback (Rust)
    participant Python

    JS->>V8: __host_op_sync__(opId, args)
    V8->>Op: Invoke registered op
    Op->>Python: Call Python function
    Python-->>Op: Return result
    Op-->>V8: Serialize result
    V8-->>JS: Return value
```

**Key features**:

- Capability-addressed: an op is reachable only by holding its token
- Sync and async variants
- JSON serialization for arguments and results
- Automatic error propagation

### There is no permission model, and none is missing

Through 0.2.x this document, and `CLAUDE.md`, described ops as
"permission-based (ops require specific permissions)" and claimed a runtime
had to be "granted those permissions via `RuntimeConfig`". None of that
existed: `grep -rni permission src/` returned zero hits and `RuntimeConfig`
had no permission field. The v0.2.0 review called this the most load-bearing
doc/code divergence in the repo, precisely because it is a *security* claim —
a reader who believes ops are permission-gated will not look for what is
actually gating them.

What actually gates them, as of 0.2.1, is two layers that are stronger than
a permission list would have been, because they are unforgeable rather than
checked:

1. **Unguessable capability tokens.** `PythonOpRegistry::register` draws an op
   token uniformly from `1..2^53` via a CSPRNG. Holding the token *is* the
   capability; there is no name to ask for and no list to consult. Before
   this, op ids were sequential integers and `__host_op_sync__(0, ...)`
   reached handlers the guest had never been given.
2. **An allowlist established at bind time.** Registering an op does not make
   it callable — `expose` does, and the bind paths call it only after the
   binding is actually installed in the guest's scope. An op whose bind failed
   half-way, or one registered for host-side use, is not dispatchable at all.
   `revoke` reverses both, which is what makes `Runtime.revoke_op` and
   `ToolBridge.detach` real revocations.

Scoping on top of that is `ToolBridge`: a namespace, a total call budget
enforced in the shim the token resolves to, and a construction-time name
check. Two bridges on one `Runtime` are two capability sets, which was not
true before the token work.

What is deliberately *not* here: there is no ambient authority to gate in the
first place. A `Runtime` ships no filesystem, no network, no process and no
timer access — a guest can only reach what a host explicitly bound. A
permission model is the right shape for a runtime that grants capabilities by
default and then restricts them; this one grants nothing by default, so the
useful question is "what did you bind?", not "what did you permit?". See
`src/runtime/ops.rs`'s module header for the full rationale, and
`tests/test_known_escape_techniques.py::TestOverpatch` for the tests that pin
the absent surfaces as absent.

## Performance Considerations

### Memory Management

- Each runtime has its own V8 heap (configured via `max_heap_size`)
- Serialization limits prevent OOM attacks (`max_depth`, `max_bytes`)
- Circular reference detection during conversion
- Automatic garbage collection by V8

### Promise Polling

Async evaluation (`eval_async`) polls promises by:

1. Checking if result is a promise
2. Running microtask checkpoint
3. Yielding to Tokio runtime
4. Repeating until promise settles or timeout

This can be CPU-intensive for long-running promises.

### Command Channel Overhead

Each Python API call involves:

- Serialization (Python → Rust)
- Channel send/receive
- Deserialization (Rust → V8)
- Reverse path for results

For tight loops, batch operations when possible.

## Module System Architecture

The module system handles JavaScript `import` statements through a multi-stage resolution and loading pipeline:

```mermaid
sequenceDiagram
    participant JS as JavaScript Code
    participant V8
    participant Resolver as Custom Resolver
    participant Static as Static Module Registry
    participant Loader as Custom Loader
    participant Core as RuntimeCoreState

    JS->>V8: import 'my-module'
    V8->>Core: Resolve module specifier

    Core->>Resolver: resolve('my-module', referrer)
    alt Custom resolver returns specifier
        Resolver-->>Core: 'custom://my-module'
        Core->>Loader: load('custom://my-module')
        Loader-->>Core: Module source code
    else Resolver returns None
        Resolver-->>Core: None
        Core->>Static: lookup('my-module')
        alt Found in static registry
            Static-->>Core: Module source code
        else Not found
            Static-->>Core: Error: Module not found
        end
    end

    Core->>V8: Compile and instantiate module
    V8-->>JS: Module namespace object
```

**Static modules**:

- Registered in `ModuleLoader` before evaluation via `add_static_module()`
- Resolved synchronously from in-memory map
- Checked after custom resolver returns `None`

**Custom loaders**:

- **Resolver**: Maps specifier → resolved specifier (e.g., `"math"` → `"file:///math.js"`)
- **Loader**: Loads module source code asynchronously (can fetch from network, database, etc.)
- Supports dynamic imports and top-level await

**Resolution order**:

1. Custom resolver (if set)
2. Static module registry (if resolver returns `None`)
3. Error if no match found


## References

- [deno_core documentation](https://docs.rs/deno_core/)
- [V8 Documentation](https://v8.dev/docs)
- [PyO3 guide](https://pyo3.rs/)

## Adding a disclosed sandbox escape to the regression suite

When a new JavaScript or sandbox escape technique is publicly disclosed and
triaged, add it to `tests/test_known_escape_techniques.py` as a **new test
class** — not a new loose test file. That file is a deliberately growing
catalogue rather than a one-time audit, and keeping every entry in one place
with the same metadata is what makes it reviewable.

Each class carries a fixed docstring template:

```python
class TestTechniqueName:
    """One-line summary of the technique.

    Source:      <URL or CVE>
    Disclosed:   <YYYY-MM>
    Root cause:  <the *class* of mistake, not the specific payload>
    Relevance:   <why pydeno's design does or does not avoid it>
    Status:      not-applicable | mitigated | mitigated-by-design | VULNERABLE
    """
```

Two conventions matter more than the template:

- **Test the root-cause class, not the payload.** A specific payload stops
  working for uninteresting reasons (a V8 version bump, a renamed global).
  The design property it attacked is what has to keep holding, so assert
  that.
- **`not-applicable` still gets assertions.** If a technique cannot apply
  because the surface does not exist — pydeno ships no filesystem and no
  network by default — pin the *absence* of that surface anyway. That turns
  "we don't have that feature" into a tripwire that fails the moment someone
  adds the capability without a permission model, which is exactly when the
  technique becomes relevant. `TestOverpatch` is the worked example.

Prefer testing through the public Python API, since that is the surface a
user is actually exposed to.
