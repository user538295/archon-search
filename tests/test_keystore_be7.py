"""Tests for BE-7: KeyStore.rotate_default_key() — rotation logic.

Covers:
- S6  (rotate returns new token; old key revoked; .search.env NOT written by KeyStore)
- S15 (grace period: old key gets expires_at = now + grace_seconds)
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from archon_search.key_manager import KeyRecord, KeyStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path) -> KeyStore:
    return KeyStore(tmp_path / "keys.json")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Unit tests for KeyStore.rotate_default_key()
# ---------------------------------------------------------------------------


class TestRotateDefaultKey:
    def test_rotate_returns_new_token_and_old_record(self, tmp_path: Path) -> None:
        """rotate_default_key() returns {new_key_id, new_token, old_record?}

        The old_record should be the previous managed key (if it existed in
        keys.json).  The method does NOT write .search.env — that is the
        caller's (route handler's) responsibility.  (S6)
        """
        store = _make_store(tmp_path)

        # Seed an existing managed key that matches the "current default"
        seed = asyncio.run(store.create(ns="default", label="old-default", expires_at=None))
        old_token = seed["token"]
        old_key_id = seed["id"]

        result = asyncio.run(store.rotate_default_key(current_token=old_token, grace_seconds=0))

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

        asyncio.run(store.rotate_default_key(current_token=old_token, grace_seconds=0))

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
        asyncio.run(store.rotate_default_key(current_token=old_token, grace_seconds=grace))
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
        result = asyncio.run(
            store.rotate_default_key(current_token="deadbeef" * 8, grace_seconds=0)
        )

        assert result["new_token"] is not None
        assert result["new_key_id"] is not None
        assert result["old_record"] is None

        # New record should be in keys.json
        records = asyncio.run(store.load())
        assert len(records) == 1
        assert records[0].status == "active"

    def test_rotate_new_key_in_keys_json(self, tmp_path: Path) -> None:
        """After rotate_default_key(), the new key is present and active in keys.json."""
        store = _make_store(tmp_path)
        seed = asyncio.run(store.create(ns="default", label=None, expires_at=None))
        old_token = seed["token"]

        result = asyncio.run(store.rotate_default_key(current_token=old_token, grace_seconds=0))
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

    def test_rotate_empty_token_raises(self, tmp_path: Path) -> None:
        """Empty current_token raises ValueError — not a silent no-match."""
        store = _make_store(tmp_path)
        with pytest.raises(ValueError, match="current_token must not be empty"):
            asyncio.run(store.rotate_default_key(current_token="", grace_seconds=0))

    def test_rotate_does_not_write_search_env(self, tmp_path: Path) -> None:
        """rotate_default_key() does NOT write .search.env — that is the route handler's job.

        The method must only modify keys.json. If the route handler forgets to
        write .search.env, the server would continue accepting the old key from
        the env file — so we verify the Use Cases layer does not reach into
        the Frameworks layer for env-file I/O.
        """
        store = _make_store(tmp_path)
        seed = asyncio.run(store.create(ns="default", label=None, expires_at=None))
        old_token = seed["token"]

        search_env = tmp_path / ".search.env"
        search_env.write_text("ARCHON_SEARCH_API_KEY=oldkey\n")
        original_mtime = search_env.stat().st_mtime

        asyncio.run(store.rotate_default_key(current_token=old_token, grace_seconds=0))

        # .search.env must NOT have been modified
        assert search_env.stat().st_mtime == original_mtime
        assert search_env.read_text() == "ARCHON_SEARCH_API_KEY=oldkey\n"

    def test_rotate_unmatched_token_no_revocation(self, tmp_path: Path) -> None:
        """If current_token does not match any keys.json record, no revocation occurs.

        Only the new key is created. This handles the case where the default
        key was loaded from .search.env and never registered in keys.json.
        """
        store = _make_store(tmp_path)
        # Create one unrelated managed key
        existing = asyncio.run(store.create(ns="other", label=None, expires_at=None))

        unrelated_token = "aabbccdd" * 8  # does not match any record
        result = asyncio.run(
            store.rotate_default_key(current_token=unrelated_token, grace_seconds=0)
        )

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
        """The new key created by rotate_default_key() has namespace='default'."""
        store = _make_store(tmp_path)

        result = asyncio.run(
            store.rotate_default_key(current_token="ff" * 32, grace_seconds=0)
        )

        records = asyncio.run(store.load())
        new_records = [r for r in records if r.id == result["new_key_id"]]
        assert len(new_records) == 1
        assert new_records[0].namespace == "default"

    def test_rotate_negative_grace_raises(self, tmp_path: Path) -> None:
        """Negative grace_seconds raises ValueError."""
        store = _make_store(tmp_path)
        with pytest.raises(ValueError, match="grace_seconds must be >= 0"):
            asyncio.run(store.rotate_default_key(current_token="aa" * 32, grace_seconds=-1))

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

        result1 = asyncio.run(store.rotate_default_key(current_token=old_token, grace_seconds=0))
        result2 = asyncio.run(store.rotate_default_key(current_token=old_token, grace_seconds=0))

        assert result2["old_record"] is None

        records = asyncio.run(store.load())
        old_recs = [r for r in records if r.id == old_key_id]
        assert len(old_recs) == 1
        assert old_recs[0].status == "revoked"  # unchanged from first rotate

        new_ids = {result1["new_key_id"], result2["new_key_id"]}
        assert new_ids.issubset({r.id for r in records if r.status == "active"})

    def test_rotate_skips_synthetic_record_with_matching_hash(self, tmp_path: Path) -> None:
        """Synthetic records (id=None) must never be revoked by rotate_default_key().

        The id is not None guard (line 286) prevents revoking synthetic TOML records
        even if their token_hash happens to match current_token.
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
        result = asyncio.run(
            store.rotate_default_key(current_token=synthetic_token, grace_seconds=0)
        )

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
        result1 = asyncio.run(
            store.rotate_default_key(current_token="aa" * 32, grace_seconds=0)
        )
        assert result1["old_record"] is None

        # Second rotation: uses token from first rotation as current_token
        result2 = asyncio.run(
            store.rotate_default_key(current_token=result1["new_token"], grace_seconds=0)
        )
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

        result = asyncio.run(
            store.rotate_default_key(current_token=unmanaged_token, grace_seconds=3600)
        )

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
        asyncio.run(store.rotate_default_key(current_token=old_token, grace_seconds=0))
        # Retry with the same (now-revoked) token, this time with grace>0.
        result2 = asyncio.run(
            store.rotate_default_key(current_token=old_token, grace_seconds=3600)
        )

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
        asyncio.run(store.rotate_default_key(current_token=old_token, grace_seconds=grace_seconds))

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
