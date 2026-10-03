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

Format: ``MAGIC (13 bytes) | HMAC-SHA256 (32 bytes) | snapshot``. The MAC covers the magic and the
payload, so it also binds the format version. This protects integrity and provenance, not
secrecy: the snapshot itself is not encrypted.
"""

from __future__ import annotations

import hashlib
import hmac

__all__ = ["SnapshotAuthenticationError", "sign_snapshot", "verify_snapshot"]

_MAGIC = b"pydeno-snap1\x00"
_MAC_LEN = hashlib.sha256().digest_size
_MIN_KEY_BYTES = 16


class SnapshotAuthenticationError(ValueError):
    """The snapshot is not authentic: malformed, tampered with, or signed with another key."""


def _key(key: bytes) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) < _MIN_KEY_BYTES:
        raise ValueError(f"key must be at least {_MIN_KEY_BYTES} bytes")
    return bytes(key)


def sign_snapshot(snapshot: bytes, key: bytes) -> bytes:
    """Wrap `snapshot` with an HMAC-SHA256 tag made with `key`."""
    if not isinstance(snapshot, (bytes, bytearray, memoryview)):
        raise TypeError("snapshot must be bytes")
    payload = bytes(snapshot)
    mac = hmac.new(_key(key), _MAGIC + payload, hashlib.sha256).digest()
    return _MAGIC + mac + payload


def verify_snapshot(blob: bytes, key: bytes) -> bytes:
    """Return the snapshot inside `blob` if, and only if, it was signed with `key`.

    Raises `SnapshotAuthenticationError` for anything else, without ever returning bytes that
    failed the check.
    """
    secret = _key(key)
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise TypeError("blob must be bytes")
    data = bytes(blob)
    if len(data) < len(_MAGIC) + _MAC_LEN or not data.startswith(_MAGIC):
        raise SnapshotAuthenticationError("not a signed pydeno snapshot")
    mac = data[len(_MAGIC) : len(_MAGIC) + _MAC_LEN]
    payload = data[len(_MAGIC) + _MAC_LEN :]
    expected = hmac.new(secret, _MAGIC + payload, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise SnapshotAuthenticationError(
            "snapshot authentication failed (tampered, or signed with a different key)"
        )
    return payload
