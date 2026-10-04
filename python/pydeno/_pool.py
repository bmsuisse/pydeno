"""`SessionPool`: live `AsyncAgentSandbox` sessions per owner, persisted to a pluggable async store.

A multi-user service needs more than `dump`/`load`: one live session per (owner, session id),
eviction (idle TTL, a global LRU cap, a per-owner cap), journals in a shared store so another
process can pick a session up, and protection against *rollback* (putting an older journal back
in the store would restore tool budget the guest has already spent).

How it fits together:

* `get(owner, session_id)` leases the session: it returns the live one, or restores it from the
  store by replay, or starts a fresh one. A session has at most one lease; a second `get` waits
  for it (up to ``acquire_timeout``) or fails with `SessionBusy`.
* `release(owner, session_id)` dumps the journal, signed and bound to
  ``associated_data=f"{owner}:{session_id}:{counter}"`` with the counter incremented, writes it
  with the TTL, and records the counter under its own key. A journal whose counter is below the
  recorded one is refused with `StaleJournal`.
* A dead worker (crashed, killed, timed out) is noticed on `release` (nothing is written) and on
  `get` (the session is restored from the last stored journal).
* A background task evicts idle sessions (journals are already stored, so nothing is lost).
"""

from __future__ import annotations

import abc
import asyncio
import collections
import collections.abc
import contextlib
import re
import struct
import time
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from ._agent import DEFAULT_MAX_JOURNAL_BYTES, JournalError, _seal_journal
from ._aio_agent import AsyncAgentSandbox

__all__ = [
    "InMemoryJournalStore",
    "JournalStore",
    "JournalTooLarge",
    "PoolFull",
    "SessionBusy",
    "SessionPool",
    "StaleJournal",
]

DEFAULT_TTL = 3600.0
DEFAULT_MAX_SESSIONS = 1000
DEFAULT_ACQUIRE_TIMEOUT = 30.0
DEFAULT_EVICTION_INTERVAL = 5.0

# Stored value: magic, counter (8 bytes, big-endian), signed journal.
_ENVELOPE = b"pydeno-session1\x00"
_COUNTER = struct.Struct(">Q")
# An owner or session id: no ':' (it separates the parts of the associated data, so "a:b" + "c"
# and "a" + "b:c" must not both be possible), no control characters, bounded.
_ID = re.compile(r"[^:\x00-\x1f\x7f]{1,256}")


class SessionBusy(RuntimeError):
    """The session is leased by another task (or pool call) and did not become free in time."""


class PoolFull(RuntimeError):
    """``max_sessions`` or ``max_per_owner`` is reached and every counted session is leased."""


class StaleJournal(JournalError):
    """The stored journal's counter is older than the newest one recorded: a rollback."""


class JournalTooLarge(JournalError):
    """The session's journal outgrew ``max_journal_bytes``; its state was dropped."""


# ---------------------------------------------------------------------------
# stores
# ---------------------------------------------------------------------------


class JournalStore(abc.ABC):
    """Where `SessionPool` keeps journals and counters: an async key-value store of bytes.

    Implement three coroutines over Redis, Valkey, a database or anything else. Keys are short
    strings; values are bytes up to about ``max_journal_bytes``. ``ttl`` is in seconds (``None``:
    no expiry). The store holds signed, not encrypted, data: journals contain the guest's code and
    the tool results, so protect the store as you would those.
    """

    @abc.abstractmethod
    async def get(self, key: str) -> bytes | None:
        """The value, or None if absent or expired."""

    @abc.abstractmethod
    async def set(self, key: str, value: bytes, *, ttl: float | None) -> None:
        """Store `value`, replacing any previous one, expiring after `ttl` seconds."""

    @abc.abstractmethod
    async def delete(self, key: str) -> None:
        """Remove the key (no error if absent)."""


class InMemoryJournalStore(JournalStore):
    """A process-local `JournalStore` (a dict with expiry), for tests and single-process use."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[bytes, float | None]] = {}

    async def get(self, key: str) -> bytes | None:
        item = self._data.get(key)
        if item is None:
            return None
        value, expires = item
        if expires is not None and time.monotonic() >= expires:
            del self._data[key]
            return None
        return value

    async def set(self, key: str, value: bytes, *, ttl: float | None) -> None:
        if not isinstance(value, (bytes, bytearray)):
            raise TypeError("value must be bytes")
        expires = None if ttl is None else time.monotonic() + ttl
        self._data[key] = (bytes(value), expires)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)


# ---------------------------------------------------------------------------
# the pool
# ---------------------------------------------------------------------------


class _Entry:
    __slots__ = (
        "barrier",
        "counter",
        "gone",
        "last_used",
        "lock",
        "owner",
        "sandbox",
        "session_id",
        "waiters",
    )

    def __init__(self, owner: str, session_id: str) -> None:
        self.owner = owner
        self.session_id = session_id
        self.lock = asyncio.Lock()
        self.sandbox: AsyncAgentSandbox | None = None
        self.counter = 0
        self.last_used = time.monotonic()
        self.waiters = 0
        self.gone = False  # evicted or dropped: a waiter that wakes up must start over
        # Held by `drop` while it works: a `get` waits on it instead of restoring (from a store
        # `drop` is busy emptying) a session that is being dropped.
        self.barrier = False

    def idle(self) -> bool:
        return not self.lock.locked() and self.waiters == 0


class _Default:
    pass


_DEFAULT: Any = _Default()

Tools = Mapping[str, Any] | collections.abc.Sequence[Any]
ToolsFactory = Callable[[str, str], Tools]
# Taken from the journal when a session is restored, so only given to new sessions.
_NEW_ONLY = frozenset({"max_result_bytes"})


class SessionPool:
    """Live `AsyncAgentSandbox` sessions keyed by (owner, session id), persisted to a store.

    Args:
        store: A `JournalStore` (`InMemoryJournalStore`, or your Redis/Valkey adapter).
        key: HMAC key for the journals (at least 16 bytes). Every process sharing the store needs
            the same key.
        tools: The tools of every session, as `AgentSandbox` takes them (``name -> callable``,
            ``name -> SchemaTool``, or a list of schema tools), or ``(owner, session_id) -> tools``
            to give each owner its own (closures over the owner's credentials, say). The names
            must not change for a session's life: a stored journal only loads with the same names.
        tools_catalog: A lazy catalog of `SchemaTool`s for every session (see `AgentSandbox`).
        ttl: Seconds a stored journal lives after its last write, and (unless ``idle_timeout`` says
            otherwise) how long an unleased live session is kept.
        max_sessions: Live sessions in this process, at most. A new one evicts the least recently
            used unleased session; if all are leased, `PoolFull`.
        max_per_owner: Live sessions per owner, at most (None: no cap), evicting likewise.
        max_journal_bytes: Cap on a session's journal. A session that outgrows it has its live and
            stored state dropped on `release`, which raises `JournalTooLarge`.
        max_tool_calls, namespace: As for `AgentSandbox`, for new sessions (a restored session
            takes them from its journal).
        idle_timeout: Seconds an unleased live session is kept (default: ``ttl``).
        acquire_timeout: Seconds `get` waits for a leased session to be released before raising
            `SessionBusy` (0: never wait; None: wait indefinitely).
        eviction_interval: Seconds between background eviction sweeps.
        counter_ttl: Expiry of the rollback counters (default None: they never expire; they are a
            few bytes each). A counter that expires stops protecting its session.
        key_prefix: Prepended to every store key.
        **sandbox_options: Passed to every `AsyncAgentSandbox` (``timeout``, ``max_pause``,
            ``max_memory``, ``sandbox``, ``redact_host_errors``, ``handler_executor``, ...).

    The pool belongs to the event loop it is first used on. Use ``async with SessionPool(...)`` or
    call `close()`; sessions are released with `release`, or use ``async with pool.session(...)``.
    """

    def __init__(
        self,
        store: JournalStore,
        key: bytes,
        tools: Tools | ToolsFactory,
        *,
        tools_catalog: Tools | None = None,
        ttl: float | None = DEFAULT_TTL,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        max_per_owner: int | None = None,
        max_journal_bytes: int = DEFAULT_MAX_JOURNAL_BYTES,
        max_tool_calls: int | None = None,
        namespace: str | None = None,
        idle_timeout: float | None = _DEFAULT,
        acquire_timeout: float | None = DEFAULT_ACQUIRE_TIMEOUT,
        eviction_interval: float = DEFAULT_EVICTION_INTERVAL,
        counter_ttl: float | None = None,
        key_prefix: str = "pydeno:session:",
        **sandbox_options: Any,
    ) -> None:
        if not isinstance(store, JournalStore):
            raise TypeError("store must be a JournalStore")
        if not isinstance(key, (bytes, bytearray)) or len(key) < 16:
            raise ValueError("key must be at least 16 bytes")
        if not (callable(tools) or _static_tools(tools)):
            raise TypeError(
                "tools must be a mapping, a list of schema tools, or a "
                "(owner, session_id) -> tools function"
            )
        if not isinstance(max_sessions, int) or max_sessions < 1:
            raise ValueError("max_sessions must be a positive int")
        if max_per_owner is not None and (
            not isinstance(max_per_owner, int) or max_per_owner < 1
        ):
            raise ValueError("max_per_owner must be a positive int or None")
        if not isinstance(max_journal_bytes, int) or max_journal_bytes <= 0:
            raise ValueError("max_journal_bytes must be a positive int")
        for name, value in (("ttl", ttl), ("counter_ttl", counter_ttl)):
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive or None")
        if eviction_interval <= 0:
            raise ValueError("eviction_interval must be positive")
        owned = {"clock", "random_seed", "max_journal_bytes"} & sandbox_options.keys()
        if owned:
            raise TypeError(f"SessionPool sets {sorted(owned)} itself")
        self._store = store
        self._key = bytes(key)
        self._tools = tools
        self._catalog = tools_catalog
        self._ttl = ttl
        self._idle_timeout = ttl if idle_timeout is _DEFAULT else idle_timeout
        self._max_sessions = max_sessions
        self._max_per_owner = max_per_owner
        self._max_journal_bytes = max_journal_bytes
        self._max_tool_calls = max_tool_calls
        self._namespace = namespace
        self._acquire_timeout = acquire_timeout
        self._interval = eviction_interval
        self._counter_ttl = counter_ttl
        self._prefix = key_prefix
        self._options = sandbox_options
        # Validate the sandbox options now, not at the first `get` (constructing starts nothing).
        if _static_tools(tools):
            AsyncAgentSandbox(tools, **self._new_options())
        self._entries: collections.OrderedDict[tuple[str, str], _Entry] = (
            collections.OrderedDict()
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sweeper: asyncio.Task[None] | None = None
        self._closing: set[asyncio.Task[None]] = set()
        self._closed = False

    # -- public API ----------------------------------------------------------

    async def get(
        self, owner: str, session_id: str, *, timeout: float | None = _DEFAULT
    ) -> AsyncAgentSandbox:
        """Lease the session: the live one, the one restored from the store, or a fresh one.

        Release it with `release` (or `drop`). While leased, another `get` of it waits up to
        `timeout` (default: the pool's ``acquire_timeout``) and then raises `SessionBusy`.
        Raises `StaleJournal` for a rolled-back journal, `JournalError` / `ReplayDivergence` for
        one that cannot be restored (`drop` the session to start over), `PoolFull` when no room
        can be made."""
        self._check_open()
        _check_id(owner, "owner")
        _check_id(session_id, "session_id")
        self._bind_loop()
        wait = self._acquire_timeout if timeout is _DEFAULT else timeout
        k = (owner, session_id)
        while True:
            entry = self._entries.get(k)
            if entry is None:
                if await self._make_room(owner):
                    continue  # evicting awaited: look again
                entry = self._entries[k] = _Entry(owner, session_id)
            self._entries.move_to_end(k)
            entry.waiters += 1
            try:
                await _acquire(entry.lock, wait, owner, session_id)
            finally:
                entry.waiters -= 1
            if entry.gone:
                entry.lock.release()
                continue
            break
        try:
            sandbox = entry.sandbox
            if sandbox is not None and sandbox.is_closed():
                # Crashed, killed or timed out since its last lease. Store what it still holds
                # (its last good state, and what a lost run spent), then restore from that.
                entry.sandbox = None
                self._close_later(sandbox)
                await self._persist(entry, sandbox)
            if entry.sandbox is None:
                entry.sandbox = await self._restore(entry)
        except BaseException:
            if entry.sandbox is None:
                self._forget(entry)
            entry.lock.release()
            raise
        entry.last_used = time.monotonic()
        return entry.sandbox

    async def release(self, owner: str, session_id: str) -> None:
        """Persist the leased session's journal and end the lease.

        The journal is signed with ``associated_data=f"{owner}:{session_id}:{counter}"`` under
        the next counter and written with the TTL. If the worker is gone (a crash, a timeout, a
        cancelled run), the journal written is the one as of the last good run plus a ``lost``
        record charging the tool calls the lost run made, and the next `get` restores that state
        on a fresh worker. If the journal outgrew
        ``max_journal_bytes``, the session's live and stored state is dropped and
        `JournalTooLarge` is raised. The lease ends either way. A session that was dropped while
        leased is released without effect, unless it has been leased again since: this ends
        whatever lease the session has, so prefer ``async with pool.session(...)``, which releases
        only its own."""
        await self._release(owner, session_id, None)

    async def _release(
        self, owner: str, session_id: str, leased: AsyncAgentSandbox | None
    ) -> None:
        """`release`; with `leased`, only if the session is still the one that lease got (a
        session dropped while leased may have been leased afresh by someone else since)."""
        _check_id(owner, "owner")
        _check_id(session_id, "session_id")
        entry = self._entries.get((owner, session_id))
        if entry is None or entry.barrier:
            return  # dropped (or the pool closed) while leased: nothing to persist
        if leased is not None and entry.sandbox is not leased:
            return  # dropped while leased, and leased again since: not ours to release
        if not entry.lock.locked():
            raise RuntimeError(f"session {owner}:{session_id} is not leased")
        try:
            if entry.gone:
                return
            sandbox = entry.sandbox
            if sandbox is None:
                return
            await self._persist(entry, sandbox)
            if sandbox.is_closed():
                entry.sandbox = None
                self._close_later(sandbox)
            entry.last_used = time.monotonic()
        finally:
            entry.lock.release()

    async def _persist(self, entry: _Entry, sandbox: AsyncAgentSandbox) -> None:
        """Dump `sandbox` under the next counter and store it (the lease is held)."""
        owner, session_id = entry.owner, entry.session_id
        counter = entry.counter + 1
        try:
            blob = await sandbox.dump(
                self._key, associated_data=_bound(owner, session_id, counter)
            )
        except JournalError as exc:
            await self._drop_state(entry, sandbox, counter)
            raise JournalTooLarge(
                f"session {owner}:{session_id} outgrew max_journal_bytes="
                f"{self._max_journal_bytes}; its state was dropped, its spent tool budget "
                f"kept ({exc})"
            ) from None
        await self._store.set(
            self._journal_key(owner, session_id),
            _ENVELOPE + _COUNTER.pack(counter) + blob,
            ttl=self._ttl,
        )
        await self._store.set(
            self._counter_key(owner, session_id),
            str(counter).encode(),
            ttl=self._counter_ttl,
        )
        entry.counter = counter

    async def drop(self, owner: str, session_id: str) -> None:
        """Forget the session: close its worker (even if leased: a run in progress is killed),
        delete its stored journal and advance its counter, so no earlier journal of it can be
        loaded again. A later `get` starts a fresh session (with a fresh tool budget)."""
        self._check_open()
        _check_id(owner, "owner")
        _check_id(session_id, "session_id")
        k = (owner, session_id)
        # Before the first await: a barrier entry takes the session's place, so a `get` arriving
        # while the store is being emptied waits for it instead of restoring the old journal.
        barrier = _Entry(owner, session_id)
        barrier.barrier = True
        await barrier.lock.acquire()  # uncontended: never suspends
        entry = self._entries.get(k)
        self._entries[k] = barrier
        sandbox = None
        if entry is not None:
            entry.gone = True
            sandbox, entry.sandbox = entry.sandbox, None
        try:
            if (
                sandbox is not None
            ):  # first: it is out of the map, nothing else will close it
                await sandbox.close()
            counter = await self._stored_counter(owner, session_id)
            if entry is not None:
                counter = max(counter, entry.counter)
            await self._store.delete(self._journal_key(owner, session_id))
            await self._store.set(
                self._counter_key(owner, session_id),
                str(counter + 1).encode(),
                ttl=self._counter_ttl,
            )
        finally:
            self._forget(barrier)
            barrier.lock.release()

    @contextlib.asynccontextmanager
    async def session(
        self, owner: str, session_id: str, *, timeout: float | None = _DEFAULT
    ) -> AsyncIterator[AsyncAgentSandbox]:
        """``async with pool.session(owner, sid) as sb:`` -- `get`, then `release` on exit
        (also on an exception: a JavaScript error leaves the session valid, and a dead worker is
        simply not persisted)."""
        sandbox = await self.get(owner, session_id, timeout=timeout)
        try:
            yield sandbox
        finally:
            await self._release(owner, session_id, sandbox)

    async def evict_idle(self) -> int:
        """One eviction sweep (the background task runs this every ``eviction_interval``):
        close unleased sessions idle past ``idle_timeout`` or with a dead worker, then trim to
        ``max_sessions``. Returns how many were evicted."""
        now = time.monotonic()
        victims = [
            e
            for e in self._entries.values()
            if e.idle()
            and (
                (e.sandbox is not None and e.sandbox.is_closed())
                or (
                    self._idle_timeout is not None
                    and now - e.last_used >= self._idle_timeout
                )
            )
        ]
        excess = len(self._entries) - len(victims) - self._max_sessions
        if excess > 0:
            spare = [e for e in self._entries.values() if e.idle() and e not in victims]
            victims.extend(spare[:excess])  # oldest first: the dict is in LRU order
        for entry in victims:
            self._evict(entry)
        if victims:
            await asyncio.sleep(0)
        return len(victims)

    def stats(self) -> dict[str, int]:
        """Live sessions, leased sessions and tasks waiting for a lease."""
        entries = list(self._entries.values())
        return {
            "live": sum(1 for e in entries if e.sandbox is not None),
            "leased": sum(1 for e in entries if e.lock.locked()),
            "waiting": sum(e.waiters for e in entries),
        }

    def __len__(self) -> int:
        return len(self._entries)

    async def close(self) -> None:
        """Stop eviction and close every live session (leased ones too, killing a run in
        progress). Journals already stored stay in the store. Idempotent."""
        self._closed = True
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sweeper
            self._sweeper = None
        entries = list(self._entries.values())
        self._entries.clear()
        sandboxes = []
        for entry in entries:
            entry.gone = True
            if entry.sandbox is not None:
                sandboxes.append(entry.sandbox)
                entry.sandbox = None
        await asyncio.gather(
            *(s.close() for s in sandboxes), *self._closing, return_exceptions=True
        )

    async def __aenter__(self) -> SessionPool:
        self._check_open()
        self._bind_loop()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- internals -----------------------------------------------------------

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("the SessionPool is closed")

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif loop is not self._loop:
            raise RuntimeError(
                "a SessionPool belongs to the event loop it was first used on"
            )
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = loop.create_task(
                self._sweep_forever(), name="pydeno-session-pool"
            )

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.evict_idle()
            except Exception:  # noqa: BLE001, S112 - a sweep must not end the sweeper
                continue

    def _journal_key(self, owner: str, session_id: str) -> str:
        return f"{self._prefix}{owner}:{session_id}:journal"

    def _counter_key(self, owner: str, session_id: str) -> str:
        return f"{self._prefix}{owner}:{session_id}:counter"

    def _tools_for(self, owner: str, session_id: str) -> Tools:
        if _static_tools(self._tools):
            return self._tools  # type: ignore[return-value]
        return self._tools(owner, session_id)  # type: ignore[operator]

    def _new_options(self) -> dict[str, Any]:
        return {
            **self._options,
            "max_tool_calls": self._max_tool_calls,
            "namespace": self._namespace,
            "tools_catalog": self._catalog,
            "max_journal_bytes": self._max_journal_bytes,
        }

    async def _stored_counter(self, owner: str, session_id: str) -> int:
        raw = await self._store.get(self._counter_key(owner, session_id))
        if raw is None:
            return 0
        if not isinstance(raw, (bytes, bytearray)) or not bytes(raw).isdigit():
            raise JournalError(f"malformed rollback counter for {owner}:{session_id}")
        return int(raw)

    async def _restore(self, entry: _Entry) -> AsyncAgentSandbox:
        owner, session_id = entry.owner, entry.session_id
        tools = self._tools_for(owner, session_id)
        floor = max(entry.counter, await self._stored_counter(owner, session_id))
        raw = await self._store.get(self._journal_key(owner, session_id))
        if raw is None:
            entry.counter = floor
            return await AsyncAgentSandbox.create(tools, **self._new_options())
        raw = bytes(raw)
        head = len(_ENVELOPE) + _COUNTER.size
        if len(raw) < head or not raw.startswith(_ENVELOPE):
            raise JournalError(f"malformed stored journal for {owner}:{session_id}")
        (counter,) = _COUNTER.unpack_from(raw, len(_ENVELOPE))
        if counter < floor:
            raise StaleJournal(
                f"the stored journal of {owner}:{session_id} has counter {counter}, older than "
                f"the newest recorded ({floor}): refusing a rollback"
            )
        # The counter in the envelope is only a claim; the signature binds the real one. (It may
        # be above the recorded counter if a writer died between writing the journal and the
        # counter: that journal is genuine and newer, so it is accepted.)
        options = {k: v for k, v in self._options.items() if k not in _NEW_ONLY}
        sandbox = await AsyncAgentSandbox.load(
            raw[head:],
            self._key,
            tools,
            tools_catalog=self._catalog,
            max_journal_bytes=self._max_journal_bytes,
            associated_data=_bound(owner, session_id, counter),
            **options,
        )
        entry.counter = counter
        return sandbox

    async def _drop_state(
        self, entry: _Entry, sandbox: AsyncAgentSandbox, counter: int
    ) -> None:
        """The journal is over its cap: close the session and store, under the next counter (so
        no earlier journal loads again), a journal with no state that charges every tool call
        the session made. The next `get` restores a fresh session with that budget spent: a
        guest that grows its own journal (every tool answer is in it) cannot win back its
        budget that way."""
        entry.sandbox = None
        owner, session_id = entry.owner, entry.session_id
        self._forget(entry)
        config, records = sandbox._spent_journal("JournalTooLarge")  # noqa: SLF001
        blob = _seal_journal(
            config, records, self._key, _bound(owner, session_id, counter)
        )
        with contextlib.suppress(Exception):
            await sandbox.close()
        await self._store.set(
            self._journal_key(owner, session_id),
            _ENVELOPE + _COUNTER.pack(counter) + blob,
            ttl=self._ttl,
        )
        await self._store.set(
            self._counter_key(owner, session_id),
            str(counter).encode(),
            ttl=self._counter_ttl,
        )
        entry.counter = counter

    def _forget(self, entry: _Entry) -> None:
        entry.gone = True
        k = (entry.owner, entry.session_id)
        if self._entries.get(k) is entry:
            del self._entries[k]

    def _evict(self, entry: _Entry) -> None:
        """Remove an unleased entry now (synchronously, so no `get` can lease it meanwhile) and
        close its worker in the background."""
        self._forget(entry)
        if entry.sandbox is not None:
            sandbox, entry.sandbox = entry.sandbox, None
            self._close_later(sandbox)

    def _close_later(self, sandbox: AsyncAgentSandbox) -> None:
        task = asyncio.get_running_loop().create_task(_quiet_close(sandbox))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def _make_room(self, owner: str) -> bool:
        """Before adding a session: evict for the per-owner and global caps. True if anything
        was evicted (the caller looks again: the map may have changed)."""
        evicted = False
        if self._max_per_owner is not None:
            mine = [e for e in self._entries.values() if e.owner == owner]
            if len(mine) >= self._max_per_owner:
                victim = next((e for e in mine if e.idle()), None)
                if victim is None:
                    raise PoolFull(
                        f"owner {owner!r} has {len(mine)} sessions (max_per_owner="
                        f"{self._max_per_owner}), all leased"
                    )
                self._evict(victim)
                evicted = True
        if len(self._entries) >= self._max_sessions:
            victim = next((e for e in self._entries.values() if e.idle()), None)
            if victim is None:
                raise PoolFull(
                    f"{len(self._entries)} sessions (max_sessions={self._max_sessions}), "
                    "all leased"
                )
            self._evict(victim)
            evicted = True
        if evicted:
            await asyncio.sleep(0)
        return evicted


def _static_tools(tools: Any) -> bool:
    return isinstance(tools, Mapping) or (
        isinstance(tools, collections.abc.Sequence)
        and not isinstance(tools, (str, bytes))
    )


def _check_id(value: Any, what: str) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(
            f"{what} must be a string of 1-256 characters without ':' or control characters"
        )


def _bound(owner: str, session_id: str, counter: int) -> bytes:
    return f"{owner}:{session_id}:{counter}".encode()


async def _acquire(
    lock: asyncio.Lock, wait: float | None, owner: str, session_id: str
) -> None:
    if not lock.locked():
        await lock.acquire()
        return
    if wait is not None and wait <= 0:
        raise SessionBusy(f"session {owner}:{session_id} is leased")
    try:
        await asyncio.wait_for(lock.acquire(), wait)
    except asyncio.TimeoutError:
        raise SessionBusy(
            f"session {owner}:{session_id} stayed leased for {wait:g}s"
        ) from None


async def _quiet_close(sandbox: AsyncAgentSandbox) -> None:
    with contextlib.suppress(Exception):
        await sandbox.close()
