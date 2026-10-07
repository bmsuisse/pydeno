"""Authenticate V8 startup snapshots before they are loaded.

A snapshot is a serialized V8 heap, and V8 deserialises it without checking that it is
well-formed: bytes an attacker controls can crash the process or worse. Monty documents the same
trust requirement for its snapshots ("must be unmodified output from a trusted producer ...
a checksum supplied alongside untrusted bytes is not authentication"). So the check has to be a
MAC under a key the attacker does not have, made when the snapshot is built and verified before
`RuntimeConfig(snapshot=...)` ever sees the bytes:

    blob = pydeno.sign_snapshot(SnapshotBuilder()....create_snapshot(), key)
    ...
    config = RuntimeConfig(snapshot=pydeno.verify_snapshot(blob, key))

Format: ``MAGIC (13 bytes) | version length (1 byte) | version | HMAC-SHA256 (32 bytes) | snapshot``.
The MAC covers the magic, the version and the payload. The version is the engine build identity
(pydeno version, target triple and V8 version, compiled into the extension) that made
the snapshot: a V8 heap is only valid for the exact engine build that wrote it, and V8 answers a
snapshot from another build by aborting the process, so an authentic snapshot from a different
build is refused here, before V8 sees it. This protects integrity and provenance, not secrecy:
the snapshot itself is not encrypted.
"""

from __future__ import annotations

# PEP 810 (Python 3.15): these stdlib modules are loaded on first use, not at import. A plain
# list, so it is inert on 3.10-3.14. Never list what the isolation worker imports before it
# applies its sandbox (`_worker`, `_sandbox`, `_wire`, `_wasm`, `_awaitable`): a lazy import
# there would run after the sandbox closed the filesystem.
__lazy_modules__ = ["hashlib"]

import hashlib
import hmac

__all__ = ["SnapshotAuthenticationError", "sign_snapshot", "verify_snapshot"]

_MAGIC = b"pydeno-snap2\x00"
_MAC_LEN = hashlib.sha256().digest_size
_MIN_KEY_BYTES = 16
_MAX_ENGINE_LEN = 255  # one length byte in the blob


def _engine_version() -> bytes:
    """The engine build this process runs, as compiled into the extension: crate version, target
    triple and V8 version. Raises when it cannot be read: a fallback would let two unrelated
    builds accept each other's snapshots and journals."""
    try:
        from ._pydeno import _build_identity

        identity = _build_identity().encode("ascii")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"cannot identify this pydeno engine build: {exc}") from exc
    if not identity or len(identity) > _MAX_ENGINE_LEN:
        raise RuntimeError(
            "cannot identify this pydeno engine build: unusable identity"
        )
    return identity


class SnapshotAuthenticationError(ValueError):
    """The snapshot is not authentic: malformed, tampered with, or signed with another key."""


def _key(key: bytes) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) < _MIN_KEY_BYTES:
        raise ValueError(f"key must be at least {_MIN_KEY_BYTES} bytes")
    return bytes(key)


def sign_snapshot(snapshot: bytes, key: bytes) -> bytes:
    """Wrap `snapshot` with an HMAC-SHA256 tag made with `key`, bound to this engine build."""
    if not isinstance(snapshot, (bytes, bytearray, memoryview)):
        raise TypeError("snapshot must be bytes")
    payload = bytes(snapshot)
    engine = _engine_version()
    head = _MAGIC + bytes([len(engine)]) + engine
    mac = hmac.new(_key(key), head + payload, hashlib.sha256).digest()
    return head + mac + payload


def verify_snapshot(blob: bytes, key: bytes) -> bytes:
    """Return the snapshot inside `blob` if, and only if, it was signed with `key` by this pydeno
    release.

    Raises `SnapshotAuthenticationError` for anything else, without ever returning bytes that
    failed the check.
    """
    secret = _key(key)
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise TypeError("blob must be bytes")
    data = bytes(blob)
    if len(data) < len(_MAGIC) + 1 or not data.startswith(_MAGIC):
        raise SnapshotAuthenticationError("not a signed pydeno snapshot")
    version_len = data[len(_MAGIC)]
    head_end = len(_MAGIC) + 1 + version_len
    if len(data) < head_end + _MAC_LEN:
        raise SnapshotAuthenticationError("not a signed pydeno snapshot")
    head = data[:head_end]
    mac = data[head_end : head_end + _MAC_LEN]
    payload = data[head_end + _MAC_LEN :]
    expected = hmac.new(secret, head + payload, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise SnapshotAuthenticationError(
            "snapshot authentication failed (tampered, or signed with a different key)"
        )
    made_by = head[len(_MAGIC) + 1 :]
    current = _engine_version()
    if made_by != current:
        raise SnapshotAuthenticationError(
            f"snapshot was made by pydeno {made_by.decode(errors='replace')!r} but this is "
            f"{current.decode(errors='replace')!r}: a V8 snapshot is only valid for the "
            "engine build that wrote it, so build it again with this release"
        )
    return payload
