"""Signed snapshots: V8 deserialises snapshot bytes without validating them, so authenticity has
to be proven *before* the bytes reach `RuntimeConfig(snapshot=...)`."""

from __future__ import annotations

import pytest

from pydeno import (
    Runtime,
    RuntimeConfig,
    SnapshotAuthenticationError,
    SnapshotBuilder,
    sign_snapshot,
    verify_snapshot,
)

KEY = b"0123456789abcdef-test-key"
OTHER_KEY = b"fedcba9876543210-other-key"
PAYLOAD = bytes(range(256)) * 8


class TestRoundTrip:
    def test_verify_returns_exactly_what_was_signed(self) -> None:
        assert verify_snapshot(sign_snapshot(PAYLOAD, KEY), KEY) == PAYLOAD

    @pytest.mark.parametrize(
        "payload",
        [b"", b"x", b"\x00" * 100, bytes(range(256)), b"A" * 100_000],
        ids=["empty", "one-byte", "100-nulls", "all-256-byte-values", "100k-bytes"],
    )
    def test_any_payload_round_trips(self, payload: bytes) -> None:
        assert verify_snapshot(sign_snapshot(payload, KEY), KEY) == payload

    def test_accepts_bytearray_and_memoryview(self) -> None:
        signed = sign_snapshot(bytearray(PAYLOAD), KEY)
        assert verify_snapshot(memoryview(signed), KEY) == PAYLOAD
        assert verify_snapshot(bytearray(signed), KEY) == PAYLOAD

    def test_signing_is_deterministic(self) -> None:
        assert sign_snapshot(PAYLOAD, KEY) == sign_snapshot(PAYLOAD, KEY)

    def test_different_keys_make_different_tags(self) -> None:
        assert sign_snapshot(PAYLOAD, KEY) != sign_snapshot(PAYLOAD, OTHER_KEY)


class TestTamperingIsRejected:
    def test_the_wrong_key(self) -> None:
        with pytest.raises(SnapshotAuthenticationError):
            verify_snapshot(sign_snapshot(PAYLOAD, KEY), OTHER_KEY)

    @pytest.mark.parametrize(
        "region",
        [
            "magic",
            "mac-first",
            "mac-last",
            "payload-first",
            "payload-last",
            "payload-middle",
        ],
    )
    def test_flipping_a_bit_anywhere(self, region: str) -> None:
        signed = bytearray(sign_snapshot(PAYLOAD, KEY))
        magic, mac = 13, 32
        index = {
            "magic": 0,
            "mac-first": magic,
            "mac-last": magic + mac - 1,
            "payload-first": magic + mac,
            "payload-last": len(signed) - 1,
            "payload-middle": magic + mac + len(PAYLOAD) // 2,
        }[region]
        signed[index] ^= 0x01
        with pytest.raises(SnapshotAuthenticationError):
            verify_snapshot(bytes(signed), KEY)

    def test_every_single_bit_flip_in_a_small_snapshot(self) -> None:
        signed = sign_snapshot(b"tiny snapshot", KEY)
        for i in range(len(signed)):
            for bit in range(8):
                mutated = bytearray(signed)
                mutated[i] ^= 1 << bit
                with pytest.raises(SnapshotAuthenticationError):
                    verify_snapshot(bytes(mutated), KEY)

    def test_truncation_at_every_length(self) -> None:
        signed = sign_snapshot(b"tiny snapshot", KEY)
        for n in range(len(signed)):
            with pytest.raises(SnapshotAuthenticationError):
                verify_snapshot(signed[:n], KEY)

    def test_extension_is_rejected(self) -> None:
        with pytest.raises(SnapshotAuthenticationError):
            verify_snapshot(sign_snapshot(PAYLOAD, KEY) + b"\x00", KEY)

    def test_an_unsigned_snapshot_is_rejected(self) -> None:
        with pytest.raises(SnapshotAuthenticationError, match="not a signed"):
            verify_snapshot(PAYLOAD, KEY)

    def test_a_checksum_next_to_the_bytes_is_not_authentication(self) -> None:
        import hashlib

        forged = b"pydeno-snap1\x00" + hashlib.sha256(b"evil").digest() + b"evil"
        with pytest.raises(SnapshotAuthenticationError):
            verify_snapshot(forged, KEY)

    def test_a_payload_cannot_be_moved_under_another_tag(self) -> None:
        a = sign_snapshot(b"payload A", KEY)
        b = sign_snapshot(b"payload B", KEY)
        cut = len(b) - len(b"payload B")
        spliced = b[:cut] + a[cut:]  # B's header and tag, A's payload
        with pytest.raises(SnapshotAuthenticationError):
            verify_snapshot(spliced, KEY)

    def test_empty_blob(self) -> None:
        with pytest.raises(SnapshotAuthenticationError):
            verify_snapshot(b"", KEY)


class TestInputValidation:
    @pytest.mark.parametrize(
        "key",
        [
            b"",
            b"short",
            b"15-bytes-xxxxxx",
            "a string key of enough length",
            None,
            12345,
        ],
    )
    def test_weak_or_wrongly_typed_keys_are_refused(self, key: object) -> None:
        with pytest.raises(ValueError, match="key"):
            sign_snapshot(PAYLOAD, key)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="key"):
            verify_snapshot(sign_snapshot(PAYLOAD, KEY), key)  # type: ignore[arg-type]

    def test_the_minimum_key_length_is_accepted(self) -> None:
        key = b"x" * 16
        assert verify_snapshot(sign_snapshot(PAYLOAD, key), key) == PAYLOAD

    @pytest.mark.parametrize("bad", ["text", 5, None, [1, 2]])
    def test_non_bytes_snapshots_are_refused(self, bad: object) -> None:
        with pytest.raises(TypeError):
            sign_snapshot(bad, KEY)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            verify_snapshot(bad, KEY)  # type: ignore[arg-type]

    def test_the_error_is_a_value_error(self) -> None:
        assert issubclass(SnapshotAuthenticationError, ValueError)


class TestWithRealSnapshots:
    def _snapshot(self) -> bytes:
        builder = SnapshotBuilder()
        builder.execute_script("lib.js", "globalThis.lib = { version: '1.0' };")
        return builder.build()

    def test_a_signed_snapshot_loads_after_verification(self) -> None:
        blob = sign_snapshot(self._snapshot(), KEY)
        runtime = Runtime(RuntimeConfig(snapshot=verify_snapshot(blob, KEY)))
        assert runtime.eval("lib.version") == "1.0"

    def test_a_tampered_snapshot_never_reaches_v8(self) -> None:
        blob = bytearray(sign_snapshot(self._snapshot(), KEY))
        blob[len(blob) // 2] ^= 0xFF
        with pytest.raises(SnapshotAuthenticationError):
            # the point: this raises *here*, in Python, instead of V8 deserialising
            # attacker-shaped bytes
            RuntimeConfig(snapshot=verify_snapshot(bytes(blob), KEY))

    def test_a_snapshot_signed_by_someone_else_is_refused(self) -> None:
        blob = sign_snapshot(self._snapshot(), OTHER_KEY)
        with pytest.raises(SnapshotAuthenticationError):
            verify_snapshot(blob, KEY)


class TestEngineVersionIsBound:
    """An authentic snapshot from another release must be refused before V8 sees it: V8 answers a
    snapshot from a different build by aborting the process."""

    def test_a_snapshot_signed_by_another_release_is_refused(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from pydeno import _snapshot_auth as auth

        key = b"k" * 32
        monkeypatch.setattr(auth, "_engine_version", lambda: b"0.0.1-older")
        blob = auth.sign_snapshot(b"payload", key)
        monkeypatch.undo()
        with pytest.raises(auth.SnapshotAuthenticationError, match="made by pydeno"):
            auth.verify_snapshot(blob, key)

    def test_the_same_release_round_trips(self) -> None:
        from pydeno import _snapshot_auth as auth

        key = b"k" * 32
        assert (
            auth.verify_snapshot(auth.sign_snapshot(b"payload", key), key) == b"payload"
        )

    def test_the_version_cannot_be_edited_without_the_key(self) -> None:
        from pydeno import _snapshot_auth as auth

        key = b"k" * 32
        blob = bytearray(auth.sign_snapshot(b"payload", key))
        # flip a byte of the recorded version: the MAC covers it, so this is tampering
        blob[len(auth._MAGIC) + 1] ^= 1  # noqa: SLF001
        with pytest.raises(auth.SnapshotAuthenticationError):
            auth.verify_snapshot(bytes(blob), key)

    def test_the_old_format_is_refused(self) -> None:
        from pydeno import _snapshot_auth as auth

        with pytest.raises(auth.SnapshotAuthenticationError):
            auth.verify_snapshot(b"pydeno-snap1\x00" + b"\x00" * 64, b"k" * 32)


class TestBuildIdentity:
    """The engine tag is compiled into the extension, and never falls back to a constant."""

    def test_the_tag_names_the_v8_build(self) -> None:
        from pydeno import _pydeno
        from pydeno import _snapshot_auth as auth

        identity = _pydeno._build_identity()  # noqa: SLF001
        assert auth._engine_version() == identity.encode()  # noqa: SLF001
        assert "+v8-" in identity and identity.startswith("pydeno-")

    def test_a_blob_differing_only_in_v8_version_is_refused(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from pydeno import _pydeno
        from pydeno import _snapshot_auth as auth

        key = b"k" * 32
        real = _pydeno._build_identity()  # noqa: SLF001
        head, _, v8 = real.rpartition("+v8-")
        monkeypatch.setattr(_pydeno, "_build_identity", lambda: f"{head}+v8-{v8}.other")
        blob = auth.sign_snapshot(b"payload", key)
        monkeypatch.setattr(_pydeno, "_build_identity", lambda: real)
        with pytest.raises(auth.SnapshotAuthenticationError, match="made by pydeno"):
            auth.verify_snapshot(blob, key)

    def test_an_unreadable_identity_fails_closed(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from pydeno import _pydeno
        from pydeno import _snapshot_auth as auth

        key = b"k" * 32
        blob = auth.sign_snapshot(b"payload", key)
        for broken in (lambda: "", lambda: "x" * 300, lambda: 1 / 0):
            monkeypatch.setattr(_pydeno, "_build_identity", broken)
            with pytest.raises(RuntimeError, match="cannot identify"):
                auth.sign_snapshot(b"payload", key)
            with pytest.raises(RuntimeError, match="cannot identify"):
                auth.verify_snapshot(blob, key)

    def test_the_old_unknown_tag_is_refused(self) -> None:
        import hashlib
        import hmac

        from pydeno import _snapshot_auth as auth

        key = b"k" * 32
        head = auth._MAGIC + bytes([len(b"unknown")]) + b"unknown"  # noqa: SLF001
        mac = hmac.new(key, head + b"payload", hashlib.sha256).digest()
        with pytest.raises(auth.SnapshotAuthenticationError, match="made by pydeno"):
            auth.verify_snapshot(head + mac + b"payload", key)
