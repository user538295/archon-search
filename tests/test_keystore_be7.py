"""Tests for BE-7: default-key rotation logic recorded in ``keys.json``.

Drives the rotate use case ``rotate_and_persist_default_key`` (which records the rotation
through ``KeyStore.record_rotation``). Input validation and the ``.search.env`` write are
covered in ``tests/test_key_manager.py``.

Covers:
- S6  (rotate returns new token; old key revoked)
- S15 (grace period: old key gets expires_at = now + grace_seconds)
"""
from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from archon_search.constants import MAX_ROTATE_GRACE_SECONDS
from archon_search.key_manager import (
    KeyRecord,
    KeyRotationInputError,
    KeyStore,
    rotate_and_persist_default_key,
    validate_rotation_grace,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path) -> KeyStore:
    return KeyStore(tmp_path / "keys.json")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _rotate(tmp_path: Path, store: KeyStore, current_token: str, grace_seconds: int) -> Coroutine[Any, Any, dict]:
    """Rotate *store*'s default key from *current_token*, writing the key file under *tmp_path*."""
    return rotate_and_persist_default_key(
        store,
        key_file=tmp_path / ".search.env",
        read_current_token=AsyncMock(return_value=current_token),
        grace_seconds=grace_seconds,
    )


# ---------------------------------------------------------------------------
# Unit tests for the default-key rotation
# ---------------------------------------------------------------------------


class TestRotateDefaultKey:
    def test_rotate_returns_new_token_and_old_record(self, tmp_path: Path) -> None:
        """The rotation returns {new_key_id, new_token, old_record?}

        The old_record should be the previous managed key (if it existed in
        keys.json).  (S6)
        """
        store = _make_store(tmp_path)

        # Seed an existing managed key that matches the "current default"
        seed = asyncio.run(store.create(ns="default", label="old-default", expires_at=None))
        old_token = seed["token"]
        old_key_id = seed["id"]

        result = asyncio.run(_rotate(tmp_path, store, old_token, 0))

        assert "new_key_id" in result
        assert "new_token" in result
        assert "old_record" in result

        assert isinstance(result["new_key_id"], str)
        assert isinstance(result["new_token"], str)
        assert result["new_key_id"] != old_key_id
        assert result["new_token"] != old_token

        old_record = result["old_record"]
        assert old_record is not None
        assert isinstance(old_record, KeyRecord)
        assert old_record.id == old_key_id
        assert old_record.status == "revoked"  # grace_seconds=0 → immediately revoked

    def test_rotate_immediate_revoke_grace_0(self, tmp_path: Path) -> None:
        """Old key status=revoked in keys.json when grace_seconds=0 (S6)."""
        store = _make_store(tmp_path)
        seed = asyncio.run(store.create(ns="default", label=None, expires_at=None))
        old_token = seed["token"]
        old_key_id = seed["id"]

        asyncio.run(_rotate(tmp_path, store, old_token, 0))

        records = asyncio.run(store.load())
        old_records = [r for r in records if r.id == old_key_id]
        assert len(old_records) == 1
        assert old_records[0].status == "revoked"
        assert old_records[0].expires_at is None

    def test_rotate_grace_sets_expires_at(self, tmp_path: Path) -> None:
        """Old key gets expires_at = now + grace when grace_seconds > 0 (S15)."""
        store = _make_store(tmp_path)
        seed = asyncio.run(store.create(ns="default", label=None, expires_at=None))
        old_token = seed["token"]
        old_key_id = seed["id"]

        grace = 60
        before = datetime.now(UTC)
        asyncio.run(_rotate(tmp_path, store, old_token, grace))
        after = datetime.now(UTC)

        records = asyncio.run(store.load())
        old_records = [r for r in records if r.id == old_key_id]
        assert len(old_records) == 1
        old = old_records[0]

        # Old key should still be "active" (not revoked) but have an expires_at
        assert old.status == "active"
        assert old.expires_at is not None
        expected_min = before + timedelta(seconds=grace)
        expected_max = after + timedelta(seconds=grace)
        assert expected_min <= old.expires_at <= expected_max

    def test_rotate_no_old_key_ok(self, tmp_path: Path) -> None:
        """rotate when no previous managed key matching current_token exists.

        Returns new token; old_record is None; no crash. (S6)
        """
        store = _make_store(tmp_path)
        # No existing keys.json or no matching record
        result = asyncio.run(_rotate(tmp_path, store, "deadbeef" * 8, 0))

        assert result["new_token"] is not None
        assert result["new_key_id"] is not None
        assert result["old_record"] is None

        # New record should be in keys.json
        records = asyncio.run(store.load())
        assert len(records) == 1
        assert records[0].status == "active"

    def test_rotate_new_key_in_keys_json(self, tmp_path: Path) -> None:
        """After a rotation, the new key is present and active in keys.json."""
        store = _make_store(tmp_path)
        seed = asyncio.run(store.create(ns="default", label=None, expires_at=None))
        old_token = seed["token"]

        result = asyncio.run(_rotate(tmp_path, store, old_token, 0))
        new_key_id = result["new_key_id"]
        new_token = result["new_token"]

        records = asyncio.run(store.load())
        new_records = [r for r in records if r.id == new_key_id]
        assert len(new_records) == 1
        new = new_records[0]
        assert new.status == "active"
        assert new.token_hash == _token_hash(new_token)
        # Security invariant: raw token must never appear in keys.json on disk.
        assert new_token not in (tmp_path / "keys.json").read_text()

    def test_rotate_unmatched_token_no_revocation(self, tmp_path: Path) -> None:
        """If current_token does not match any keys.json record, no revocation occurs.

        Only the new key is created. This handles the case where the default
        key was loaded from .search.env and never registered in keys.json.
        """
        store = _make_store(tmp_path)
        # Create one unrelated managed key
        existing = asyncio.run(store.create(ns="other", label=None, expires_at=None))

        unrelated_token = "aabbccdd" * 8  # does not match any record
        result = asyncio.run(_rotate(tmp_path, store, unrelated_token, 0))

        assert result["old_record"] is None

        records = asyncio.run(store.load())
        # Two records: the original unrelated key (still active) + the new rotated key
        assert len(records) == 2
        statuses = {r.status for r in records}
        assert statuses == {"active"}

        ids = {r.id for r in records}
        assert existing["id"] in ids
        assert result["new_key_id"] in ids

    def test_rotate_new_key_namespace_is_default(self, tmp_path: Path) -> None:
        """The new key created by a rotation has namespace='default'."""
        store = _make_store(tmp_path)

        result = asyncio.run(_rotate(tmp_path, store, "ff" * 32, 0))

        records = asyncio.run(store.load())
        new_records = [r for r in records if r.id == result["new_key_id"]]
        assert len(new_records) == 1
        assert new_records[0].namespace == "default"

    def test_rotate_grace_at_max_sets_expiry(self, tmp_path: Path) -> None:
        """The largest accepted grace_seconds still yields a representable expires_at."""
        store = _make_store(tmp_path)
        before = datetime.now(UTC)
        result = asyncio.run(_rotate(tmp_path, store, "aa" * 32, MAX_ROTATE_GRACE_SECONDS))
        expires_at = result["old_record"].expires_at
        assert expires_at >= before + timedelta(seconds=MAX_ROTATE_GRACE_SECONDS)

    def test_validate_rotation_grace_rejects_a_grace_that_would_overflow_datetime(self) -> None:
        """The bound exists because ``now + timedelta(seconds=grace)`` overflows past year 9999."""
        overflowing = 10**12
        with pytest.raises(OverflowError):
            datetime.now(UTC) + timedelta(seconds=overflowing)
        with pytest.raises(KeyRotationInputError, match="grace_seconds must be <="):
            validate_rotation_grace(overflowing)

    def test_rotate_double_call_same_token_safe(self, tmp_path: Path) -> None:
        """Second rotate with the same current_token is safe (retry idempotency).

        The first call revokes the old key; the second call finds no active
        record matching that token, so old_record=None and a second new key is
        created without re-revoking or corrupting the already-revoked record.
        """
        store = _make_store(tmp_path)
        seed = asyncio.run(store.create(ns="default", label=None, expires_at=None))
        old_token = seed["token"]
        old_key_id = seed["id"]

        result1 = asyncio.run(_rotate(tmp_path, store, old_token, 0))
        result2 = asyncio.run(_rotate(tmp_path, store, old_token, 0))

        assert result2["old_record"] is None

        records = asyncio.run(store.load())
        old_recs = [r for r in records if r.id == old_key_id]
        assert len(old_recs) == 1
        assert old_recs[0].status == "revoked"  # unchanged from first rotate

        new_ids = {result1["new_key_id"], result2["new_key_id"]}
        assert new_ids.issubset({r.id for r in records if r.status == "active"})

    def test_rotate_skips_synthetic_record_with_matching_hash(self, tmp_path: Path) -> None:
        """Synthetic records (id=None) must never be revoked by a rotation.

        The ``record.id is not None`` guard in ``record_rotation`` prevents revoking
        synthetic TOML records even if their token_hash happens to match current_token.
        """
        import json
        store = _make_store(tmp_path)

        # Manually write a synthetic record (id=None) with a known token
        synthetic_token = "cc" * 32
        synthetic_hash = hashlib.sha256(synthetic_token.encode()).hexdigest()
        synthetic_record = {
            "id": None,
            "token_hash": synthetic_hash,
            "namespace": "toml-ns",
            "label": None,
            "created_at": datetime.now(UTC).isoformat(),
            "expires_at": None,
            "status": "active",
        }
        (tmp_path / "keys.json").write_bytes(json.dumps([synthetic_record]).encode())

        # Rotate using the same token that matches the synthetic record
        result = asyncio.run(_rotate(tmp_path, store, synthetic_token, 0))

        # old_record must be None — synthetic records are never matched by rotate
        assert result["old_record"] is None

        # Synthetic record must remain active (not revoked)
        records = asyncio.run(store.load())
        synthetic_records = [r for r in records if r.id is None]
        assert len(synthetic_records) == 1
        assert synthetic_records[0].status == "active"

        # New managed record must also be present
        assert len(records) == 2

    def test_rotate_sequential_creates_correct_record_count(self, tmp_path: Path) -> None:
        """Rotating twice produces 2 records: one revoked + one active."""
        store = _make_store(tmp_path)

        # First rotation: no prior key in keys.json
        result1 = asyncio.run(_rotate(tmp_path, store, "aa" * 32, 0))
        assert result1["old_record"] is None

        # Second rotation: uses token from first rotation as current_token
        result2 = asyncio.run(_rotate(tmp_path, store, result1["new_token"], 0))
        assert result2["old_record"] is not None
        assert result2["old_record"].id == result1["new_key_id"]

        records = asyncio.run(store.load())
        # 2 records: first rotation's key (revoked) + second rotation's new key (active)
        assert len(records) == 2
        revoked = [r for r in records if r.status == "revoked"]
        active = [r for r in records if r.status == "active"]
        assert len(revoked) == 1
        assert len(active) == 1
        assert revoked[0].id == result1["new_key_id"]
        assert active[0].id == result2["new_key_id"]

    def test_rotate_unmanaged_key_with_grace_synthesizes_active_record(
        self, tmp_path: Path
    ) -> None:
        """S133: unmanaged current_token + grace>0 → synthesize an active grace record.

        The initial default key lives only in ``.search.env`` (never in
        ``keys.json``). Rotating it with grace must still keep it accepted during
        the window, so a grace record is synthesized from its hash. Only the
        SHA-256 hash is persisted — never the raw token.
        """
        store = _make_store(tmp_path)
        unmanaged_token = "ab" * 32  # not in keys.json

        result = asyncio.run(_rotate(tmp_path, store, unmanaged_token, 3600))

        old_record = result["old_record"]
        assert old_record is not None, "grace record must be synthesized (S133)"
        assert old_record.status == "active"
        assert old_record.expires_at is not None
        assert old_record.token_hash == _token_hash(unmanaged_token)

        records = asyncio.run(store.load())
        # Two records: synthesized grace record (active, expiring) + new key.
        assert len(records) == 2
        grace_recs = [r for r in records if r.token_hash == _token_hash(unmanaged_token)]
        assert len(grace_recs) == 1
        assert grace_recs[0].status == "active"
        assert grace_recs[0].expires_at is not None
        # Security invariant: raw old token must never be persisted.
        assert unmanaged_token not in (tmp_path / "keys.json").read_text()

    def test_rotate_double_call_with_grace_does_not_resurrect_revoked(
        self, tmp_path: Path
    ) -> None:
        """Retry with an already-revoked token + grace>0 must NOT resurrect it.

        Preserves double-rotation idempotency: the ``not current_hash_seen``
        guard means a token that matches a revoked record is not re-synthesized
        into a fresh active grace record.
        """
        store = _make_store(tmp_path)
        seed = asyncio.run(store.create(ns="default", label=None, expires_at=None))
        old_token = seed["token"]
        old_key_id = seed["id"]

        # First rotation revokes the managed key (grace=0).
        asyncio.run(_rotate(tmp_path, store, old_token, 0))
        # Retry with the same (now-revoked) token, this time with grace>0.
        result2 = asyncio.run(_rotate(tmp_path, store, old_token, 3600))

        assert result2["old_record"] is None, "revoked key must not be resurrected"
        records = asyncio.run(store.load())
        old_recs = [r for r in records if r.id == old_key_id]
        assert len(old_recs) == 1
        assert old_recs[0].status == "revoked"  # still revoked, not re-activated


# ---------------------------------------------------------------------------
# Integration test: old key rejected after grace window (S15)
# ---------------------------------------------------------------------------


class TestRotateGraceIntegration:
    @pytest.mark.integration
    def test_rotate_old_key_rejected_after_grace(self, tmp_path: Path) -> None:
        """Freeze time past grace window; old token → 401 (S15).

        Uses KeyStore directly (not the HTTP route — BE-8 handles the route).
        The test creates a managed key, rotates it with a 30-second grace, then
        verifies that the old key is excluded from active_keys() once we mock
        the clock past the grace window.
        """
        store = _make_store(tmp_path)

        # Create a key and treat its token as the "current default"
        seed = asyncio.run(store.create(ns="default", label=None, expires_at=None))
        old_token = seed["token"]
        old_key_id = seed["id"]

        grace_seconds = 30
        asyncio.run(_rotate(tmp_path, store, old_token, grace_seconds))

        # Verify old key has expires_at set (grace window active)
        records = asyncio.run(store.load())
        old_rec = next(r for r in records if r.id == old_key_id)
        assert old_rec.expires_at is not None

        # Before grace expires: old key should still be in active_keys()
        active_before = asyncio.run(store.active_keys())
        old_ids_before = [r.id for r in active_before]
        assert old_key_id in old_ids_before

        # After grace expires: freeze time past expires_at
        past_expiry = old_rec.expires_at + timedelta(seconds=1)
        with patch("archon_search.key_manager.datetime") as mock_dt:
            mock_dt.now.return_value = past_expiry
            active_after = asyncio.run(store.active_keys())

        old_ids_after = [r.id for r in active_after]
        assert old_key_id not in old_ids_after
