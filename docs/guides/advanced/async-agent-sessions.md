# Async agent sessions and the session pool

Two layers on top of [`AsyncIsolatedRuntime`](async.md) for services that drive many
[agent sessions](../agent-sessions.md) from one asyncio event loop:

- **`AsyncAgentSandbox`**: `AgentSandbox` with coroutine methods and no thread per session.
- **`SessionPool`**: live sessions per (owner, session id), persisted to a shared async store, with
  eviction, rollback protection and crash recovery.

## `AsyncAgentSandbox`

```python
from pydeno import AsyncAgentSandbox, ToolCall

async def search(query: str) -> list[str]: ...      # tools may be async ...
def lookup(sku: str) -> dict: ...                   # ... or plain (they run on a thread pool)

async with AsyncAgentSandbox({"search": search, "lookup": lookup}, max_tool_calls=20) as sb:
    print(await sb.run("const hits = await search('lamps'); return hits.length"))

    step = await sb.start("return await lookup('A-1')")     # pause at the tool call
    if isinstance(step, ToolCall):
        blob = await sb.dump(key, associated_data=b"tenant-42")
        step = await sb.resume(step, {"price": 10})

restored = await AsyncAgentSandbox.load(blob, key, tools, associated_data=b"tenant-42")
```

Everything `AgentSandbox` does, with the same arguments, limits, errors and journal format
(a journal dumped by one class loads in the other):

| `AgentSandbox` | `AsyncAgentSandbox` |
|---|---|
| `AgentSandbox(tools, ...)` (starts the worker) | `AsyncAgentSandbox(tools, ...)` validates only; `async with` or `await AsyncAgentSandbox.create(tools, ...)` starts it |
| `run`, `start`, `resume`, `dump` | the same names, as coroutines |
| `AgentSandbox.load(...)` | `await AsyncAgentSandbox.load(...)` |
| `with ...:` / `close()` | `async with ...:` / `await close()` |
| one event-loop thread per session | none: shims, run task and caller share the caller's loop |

- **Tools.** A coroutine function is awaited on the loop. A plain function runs on the shared handler
  pool of `AsyncIsolatedRuntime` (or the `handler_executor=` you pass), with the caller's
  contextvars, so a blocking tool never stalls the loop.
- **Nothing blocks the loop.** Worker start-up, resource sampling and reaping happen off the loop
  (see [the async runtime](async.md)); journals over 1 MiB are signed and parsed on a thread.
- **One task at a time.** A second concurrent `run`/`start`/`resume`/`dump` on the same session, or a
  tool calling back into its own session, raises `RuntimeError` ("busy"). Use `SessionPool` to
  serialise callers instead.
- A session belongs to the loop it was started on.

### Cancellation

Cancelling the task that awaits `start`, `resume` or `run` **SIGKILLs the worker before the
`CancelledError` propagates** and closes the session; the loop's supervisor reaps the process within
one tick (50 ms). This holds while the guest is computing and while it is paused at a tool call that
`run()` is answering (a slow async tool, a human approval). The reason is the same as for
`AsyncIsolatedRuntime`: V8 cannot be interrupted mid-command, and a half-finished command must not
answer the next one.

A session that is paused with nobody awaiting it (after `start` returned a `ToolCall`) is released
by `await sb.close()` or by leaving the `async with` block, also when that block is left because
the task holding it was cancelled. Closing a session that is running or paused kills its worker at
once instead of asking it to exit. Pending tool calls are cancelled, so no task is left waiting.
`max_pause` still applies: a session nobody resumes gives its worker back after `max_pause` seconds.

After a cancellation, `dump()` raises `JournalError` (the interrupted run cannot be replayed). Dump,
or `SessionPool.release`, after each step you want to be able to return to.

A synchronous tool that is running on a thread when its run is cancelled keeps that thread until it
returns (threads cannot be interrupted). The worker is killed regardless.

### Cost per session

Measured on an Apple M2 (8 cores) with 200 sessions in one loop, each paused at a tool call
(`tests/test_aio_agent.py::TestScale` runs the same scenario):

| | per session | 200 sessions |
|---|---|---|
| Worker process (resident memory as the OS reports it, which counts shared pages) | ~36 MiB median, ~46 MiB max | |
| Parent Python heap (tracemalloc) | ~150 KiB | ~30 MiB |
| Parent threads | **0** | +3 in total (the shared metrics, io and handler pools) |
| Asyncio tasks | 1 while a run is in progress, plus 1 per unanswered tool call | |
| Start-up | ~55 ms CPU; at most `cpu_count // 2` in flight per loop | ~5 s for 200 |

With `AgentSandbox` the same 200 sessions need 200 event-loop threads plus each
`IsolatedRuntime`'s threads. Memory is dominated by the worker processes: plan the host for
`max_memory` (default 1 GiB per worker) times the number of live sessions, or cap them with a pool.

## `SessionPool`

```python
from pydeno import SessionPool, InMemoryJournalStore

pool = SessionPool(
    InMemoryJournalStore(),        # or your Redis/Valkey adapter, see below
    key=journal_key,               # >= 16 bytes, the same in every process
    tools=tools,                   # or (owner, session_id) -> tools
    ttl=3600,                      # journals expire an hour after their last write
    max_sessions=200,              # live sessions in this process (LRU eviction)
    max_per_owner=3,               # live sessions per owner
    max_journal_bytes=1 << 20,
    max_tool_calls=50,
    timeout=10, max_memory=256 << 20,   # anything else goes to AsyncAgentSandbox
)

async def handle(user_id: str, chat_id: str, code: str):
    async with pool.session(user_id, chat_id) as sb:      # get ... release
        return await sb.run(code)

await pool.close()                 # or: async with SessionPool(...) as pool:
```

- **`await pool.get(owner, session_id)`** leases the session: the live one if there is one,
  otherwise it is restored from the store by replay, otherwise a fresh session starts.
- **`await pool.release(owner, session_id)`** dumps the journal, stores it with the TTL and ends the
  lease. The session stays live (no replay on the next `get`) until it is evicted.
- **`await pool.drop(owner, session_id)`** closes the session (killing a run in progress, even
  under someone else's lease), deletes its journal and advances its counter.
- **`pool.session(owner, session_id)`** is `get` + `release` as an `async with` block. The release
  also happens when the block raises: a JavaScript error leaves the session valid, and a session
  whose worker died is not persisted.
- Owner and session ids are strings of up to 256 characters without `:` or control characters.

### Concurrency: serialised, or rejected with `SessionBusy`

A session has at most one lease. A second `get` of a leased session **waits** for the release, up
to `acquire_timeout` (default 30 s, per call `get(..., timeout=)`), then raises `SessionBusy`.
`acquire_timeout=0` rejects at once instead of waiting. Different sessions run concurrently.

This holds within one pool. Two processes sharing a store do not lock each other out: route each
owner to one process (sticky sessions), or put a lock in front of the pool.

### Rollback protection

Every `release` signs the journal with
`associated_data=f"{owner}:{session_id}:{counter}"` under the next counter, and writes two keys:

| key | value | expiry |
|---|---|---|
| `{prefix}{owner}:{session_id}:journal` | magic, counter (8 bytes), signed journal | `ttl` |
| `{prefix}{owner}:{session_id}:counter` | the counter, in decimal | `counter_ttl` (default: never) |

On restore, a journal whose counter is below the recorded one raises `StaleJournal`: putting an
older journal back in the store (to restore tool budget the guest has spent since) does not work.
Changing the counter in the envelope breaks the signature, and moving a journal under another
owner's or session's key fails the same way. A journal *above* the recorded counter is genuine (only
the key holder can sign one) and is accepted: that is a process that died between writing the
journal and writing the counter. `drop` advances the counter so that no earlier journal of the
session loads again.

What this cannot stop: someone who can write to the store can roll back **both** keys together.
Keep the counters where the journals' writers cannot reach them if that is in your threat model
(any store will do; the pool only needs `get`/`set`/`delete`), and do not let `counter_ttl`
expire before the journals do.

### Size cap

`max_journal_bytes` caps each session's journal. A session that outgrows it is not stored
half-way: `release` closes it, deletes its stored journal, advances its counter and raises
`JournalTooLarge` (a `JournalError`). The next `get` starts a fresh session. Keep long-lived state
out of the journal by keeping runs short, or raise the cap.

### Crash safety

A worker that crashed, hit a hard timeout or `max_memory`, or was killed from outside is noticed:

- by `release`, which then writes nothing (the last good journal stays in the store);
- by the next `get`, which restores the session from that journal on a fresh worker;
- by the background sweep, which evicts it.

What a session did after its last `release` is lost when its worker dies, by design: the journal
is the unit of durability. Release after every turn you want to keep.

### Eviction

A background task (every `eviction_interval`, default 5 s) evicts unleased sessions that have
been idle for `idle_timeout` (default: `ttl`) or whose worker is dead. Adding a session beyond
`max_sessions` or `max_per_owner` evicts the least recently used unleased session first; if every
counted session is leased, `get` raises `PoolFull`. Eviction only closes the worker: the journal was
stored on release, so the next `get` restores the session. Leased sessions are never evicted.
`await pool.evict_idle()` runs one sweep by hand; `pool.stats()` reports live, leased and waiting
counts.

### Stores

`JournalStore` is three coroutines. `InMemoryJournalStore` is a dict with expiry, for tests and
single-process services. For Redis or Valkey (with `redis.asyncio`, `valkey.asyncio`, or any client
speaking the protocol), an adapter is a few lines; pydeno does not depend on a client:

```python
from pydeno import JournalStore

class RedisJournalStore(JournalStore):
    def __init__(self, client):            # e.g. redis.asyncio.Redis.from_url("redis://...")
        self.client = client

    async def get(self, key: str) -> bytes | None:
        return await self.client.get(key)  # bytes (do not use decode_responses=True)

    async def set(self, key: str, value: bytes, *, ttl: float | None) -> None:
        if ttl is None:
            await self.client.set(key, value)
        else:
            await self.client.set(key, value, px=max(1, int(ttl * 1000)))

    async def delete(self, key: str) -> None:
        await self.client.delete(key)
```

Journals are signed, not encrypted: they hold the guest's code and every tool result. Protect the
store accordingly (TLS, authentication, a key prefix per environment via `key_prefix=`).

## API

```python
AsyncAgentSandbox(tools, *, max_tool_calls=None, namespace=None, clock=None, random_seed=None,
                  timeout=30.0, max_pause=600.0, max_journal_bytes=8 MiB,
                  **async_isolated_runtime_options)
await AsyncAgentSandbox.create(tools, **options) -> AsyncAgentSandbox
await sb.run(code) / await sb.start(code) / await sb.resume(step, value | error=exc)
await sb.dump(key, *, associated_data=b"") -> bytes
await AsyncAgentSandbox.load(blob, key, tools, *, max_journal_bytes=8 MiB, associated_data=b"", **options)
await sb.close(); sb.is_closed(); sb.pending; sb.worker_pid
sb.calls_made, sb.calls_remaining, sb.clock, sb.random_seed, sb.describe_tools(), sb.typescript_stubs()

SessionPool(store, key, tools, *, ttl=3600, max_sessions=1000, max_per_owner=None,
            max_journal_bytes=8 MiB, max_tool_calls=None, namespace=None, idle_timeout=ttl,
            acquire_timeout=30.0, eviction_interval=5.0, counter_ttl=None,
            key_prefix="pydeno:session:", **sandbox_options)
await pool.get(owner, session_id, *, timeout=acquire_timeout) -> AsyncAgentSandbox
await pool.release(owner, session_id)
await pool.drop(owner, session_id)
async with pool.session(owner, session_id) as sb: ...
await pool.evict_idle() -> int; pool.stats(); await pool.close()

class JournalStore: async get(key) / async set(key, value, *, ttl) / async delete(key)
InMemoryJournalStore()
SessionBusy, PoolFull (RuntimeError); StaleJournal, JournalTooLarge (JournalError)
```
