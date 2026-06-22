"""Tests for BE-5: KeyStore.revoke(), list_keys(), active_keys() with expiry enforcement.

Covers:
- C2 (KeyStore ↔ APIKeyMiddleware contract)
- S4 (revoke key)
- S9 (revoked key → 401)
- S10 (expired key → 401 with INFO log once)
- S18 (revoking only active key leaves server operational)
"""
from __future__ import annotations

import asyncio
import json
import logging
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


def _write_records(tmp_path: Path, records: list[dict]) -> None:
    """Write raw record dicts to keys.json."""
    (tmp_path / "keys.json").write_bytes(json.dumps(records).encode())


def _make_record(
    *,
    key_id: str,
    token_hash: str = "a" * 64,
    namespace: str = "ns",
    status: str = "active",
    expires_at: datetime | None = None,
) -> dict:
    now = datetime.now(UTC)
    return {
        "id": key_id,
        "token_hash": token_hash,
        "namespace": namespace,
        "label": None,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat() if expires_at is not None else None,
        "status": status,
    }


# ---------------------------------------------------------------------------
# KeyStore.revoke() tests
# ---------------------------------------------------------------------------


class TestKeyStoreRevoke:
    def test_keystore_revoke_marks_status(self, tmp_path: Path) -> None:
        """revoke(id) sets status=revoked in keys.json (S4)."""
        store = _make_store(tmp_path)
        result = asyncio.run(store.create(ns="ns", label=None, expires_at=None))
        key_id = result["id"]

        asyncio.run(store.revoke(key_id))

        records = asyncio.run(store.load())
        assert len(records) == 1
        assert records[0].status == "revoked"

    def test_keystore_active_keys_excludes_revoked(self, tmp_path: Path) -> None:
        """Revoked key is not in active_keys() (S9)."""
        store = _make_store(tmp_path)
        result = asyncio.run(store.create(ns="ns", label=None, expires_at=None))
        key_id = result["id"]

        asyncio.run(store.revoke(key_id))

        active = asyncio.run(store.active_keys())
        assert active == []

    def test_keystore_revoke_nonexistent_raises_key_error(self, tmp_path: Path) -> None:
        """revoke('no-such-id') raises KeyError for unknown IDs."""
        store = _make_store(tmp_path)
        # Empty store
        _write_records(tmp_path, [])

        with pytest.raises(KeyError):
            asyncio.run(store.revoke("no-such-id"))

    def test_keystore_revoke_already_revoked_noop(self, tmp_path: Path) -> None:
        """revoke() on an already-revoked key is a no-op — no exception and no disk write."""
        store = _make_store(tmp_path)
        result = asyncio.run(store.create(ns="ns", label=None, expires_at=None))
        key_id = result["id"]

        asyncio.run(store.revoke(key_id))  # first call — performs the write

        # Spy on _write to verify the second revoke does NOT write to disk.
        original_write = store._write
        write_calls: list[int] = []

        def spy_write(records):  # type: ignore[no-untyped-def]
            write_calls.append(1)
            return original_write(records)

        store._write = spy_write  # type: ignore[method-assign]
        asyncio.run(store.revoke(key_id))  # second call — must be a true no-op

        assert len(write_calls) == 0, "second revoke must not write to disk"
        records = asyncio.run(store.load())
        assert records[0].status == "revoked"

    def test_keystore_revoke_one_of_two_leaves_other_active(self, tmp_path: Path) -> None:
        """Revoking key A leaves key B active — revoke is scoped to the target ID."""
        store = _make_store(tmp_path)
        r1 = asyncio.run(store.create(ns="ns", label=None, expires_at=None))
        r2 = asyncio.run(store.create(ns="ns", label=None, expires_at=None))

        asyncio.run(store.revoke(r1["id"]))

        active = asyncio.run(store.active_keys())
        assert len(active) == 1
        assert active[0].id == r2["id"]

    def test_keystore_revoke_only_managed_key(self, tmp_path: Path) -> None:
        """Revoking the last/only key is allowed — no error raised (S18)."""
        store = _make_store(tmp_path)
        result = asyncio.run(store.create(ns="ns", label=None, expires_at=None))
        key_id = result["id"]

        # Must not raise
        asyncio.run(store.revoke(key_id))

        records = asyncio.run(store.load())
        assert records[0].status == "revoked"

    def test_keystore_revoke_none_raises_key_error(self, tmp_path: Path) -> None:
        """revoke(None) raises KeyError — guards against accidental synthetic record match.

        Synthetic TOML records have id=None.  Passing None as key_id would match
        them if not explicitly rejected.  The implementation guards with
        ``isinstance(key_id, str)`` before the disk-read loop.
        """
        store = _make_store(tmp_path)
        # Write a synthetic record with id=None
        synthetic_record = {
            "id": None,
            "token_hash": "c" * 64,
            "namespace": "toml-ns",
            "label": "toml-ns",
            "created_at": datetime.now(UTC).isoformat(),
            "expires_at": None,
            "status": "active",
        }
        _write_records(tmp_path, [synthetic_record])

        with pytest.raises(KeyError):
            asyncio.run(store.revoke(None))  # type: ignore[arg-type]

        # Synthetic record must be untouched
        records = asyncio.run(store.load())
        assert len(records) == 1
        assert records[0].status == "active"

    def test_keystore_revoke_ignores_synthetic_record(self, tmp_path: Path) -> None:
        """revoke() raises KeyError for a string ID that does not match any managed key.

        Synthetic TOML records have id=None. Because key_id is typed as str, no
        string value can equal None — synthetic records can never be targeted.
        This test verifies that passing a nonexistent string ID (when only a
        synthetic record exists) raises KeyError and leaves the synthetic record
        untouched.
        """
        store = _make_store(tmp_path)
        # Write a synthetic-style record (id=None) directly to disk
        synthetic_record = {
            "id": None,
            "token_hash": "b" * 64,
            "namespace": "toml-ns",
            "label": "toml-ns",
            "created_at": datetime.now(UTC).isoformat(),
            "expires_at": None,
            "status": "active",
        }
        _write_records(tmp_path, [synthetic_record])

        # Attempting to revoke a nonexistent string ID raises KeyError
        with pytest.raises(KeyError):
            asyncio.run(store.revoke("some-string-id"))

        # Synthetic record is untouched
        records = asyncio.run(store.load())
        assert len(records) == 1
        assert records[0].id is None
        assert records[0].status == "active"


# ---------------------------------------------------------------------------
# KeyStore.list_keys() tests
# ---------------------------------------------------------------------------


class TestKeyStoreListKeys:
    def test_keystore_list_includes_revoked(self, tmp_path: Path) -> None:
        """list_keys() returns all records including revoked."""
        store = _make_store(tmp_path)
        r1 = asyncio.run(store.create(ns="ns", label=None, expires_at=None))
        r2 = asyncio.run(store.create(ns="ns2", label=None, expires_at=None))

        asyncio.run(store.revoke(r1["id"]))

        records = asyncio.run(store.list_keys())
        assert len(records) == 2
        statuses = {r.status for r in records}
        assert "revoked" in statuses
        assert "active" in statuses

    def test_keystore_list_empty_store(self, tmp_path: Path) -> None:
        """list_keys() returns empty list for an empty key store."""
        store = _make_store(tmp_path)
        _write_records(tmp_path, [])

        records = asyncio.run(store.list_keys())
        assert records == []

    def test_keystore_list_returns_no_filtering(self, tmp_path: Path) -> None:
        """list_keys() returns all records (no expiry filter — for operator display)."""
        store = _make_store(tmp_path)
        past = datetime(2000, 1, 1, tzinfo=UTC)
        # Write one expired-by-time but status=active record
        record = _make_record(key_id="expired-active", expires_at=past)
        _write_records(tmp_path, [record])

        records = asyncio.run(store.list_keys())
        assert len(records) == 1
        assert records[0].id == "expired-active"


# ---------------------------------------------------------------------------
# KeyStore.active_keys() expiry enforcement tests
# ---------------------------------------------------------------------------


class TestKeyStoreActiveKeysExpiry:
    def test_keystore_active_keys_excludes_expired(self, tmp_path: Path) -> None:
        """Key with past expires_at is excluded from active_keys() (S10)."""
        store = _make_store(tmp_path)
        past = datetime(2000, 1, 1, tzinfo=UTC)
        record = _make_record(key_id="exp-id", expires_at=past)
        _write_records(tmp_path, [record])

        active = asyncio.run(store.active_keys())
        assert active == []

    def test_keystore_active_keys_info_log_first_rejection(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """INFO log for expired key appears exactly once across two active_keys() calls (S10)."""
        store = _make_store(tmp_path)
        past = datetime(2000, 1, 1, tzinfo=UTC)
        record = _make_record(key_id="log-once-id", expires_at=past)
        _write_records(tmp_path, [record])

        with caplog.at_level(logging.INFO, logger="archon_search.key_manager"):
            asyncio.run(store.active_keys())
            asyncio.run(store.active_keys())

        info_msgs = [
            r
            for r in caplog.records
            if r.levelname == "INFO" and "log-once-id" in r.message
        ]
        assert len(info_msgs) == 1, f"Expected exactly 1 INFO log, got {len(info_msgs)}"

    def test_keystore_active_keys_valid_one_second_before_expiry(
        self, tmp_path: Path
    ) -> None:
        """Key with expires_at = now + 1s is still in active_keys()."""
        store = _make_store(tmp_path)
        future = datetime.now(UTC) + timedelta(seconds=1)
        record = _make_record(key_id="almost-exp", expires_at=future)
        _write_records(tmp_path, [record])

        active = asyncio.run(store.active_keys())
        assert len(active) == 1
        assert active[0].id == "almost-exp"

    def test_keystore_active_keys_invalid_at_exact_expiry(self, tmp_path: Path) -> None:
        """Key exactly at expires_at is NOT in active_keys() (strict > comparison).

        Uses a mocked clock so that ``datetime.now(UTC)`` returns exactly
        ``expires_at``, proving the guard is ``<=`` (not just ``<``).  A key
        whose expiry instant equals "now" must be rejected.
        """
        store = _make_store(tmp_path)
        fixed_now = datetime(2026, 6, 22, 12, 0, 0, tzinfo=UTC)
        # expires_at == fixed_now  ← exactly at expiry
        record = _make_record(key_id="exact-exp", expires_at=fixed_now)
        _write_records(tmp_path, [record])

        with patch("archon_search.key_manager.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            active = asyncio.run(store.active_keys())

        assert active == []

    def test_keystore_active_keys_includes_null_expiry(self, tmp_path: Path) -> None:
        """Key with expires_at=None (no expiry) is included in active_keys()."""
        store = _make_store(tmp_path)
        record = _make_record(key_id="no-exp", expires_at=None)
        _write_records(tmp_path, [record])

        active = asyncio.run(store.active_keys())
        assert len(active) == 1
        assert active[0].id == "no-exp"


# ---------------------------------------------------------------------------
# Integration test: revoked key returns 401 via TestClient (S9)
# ---------------------------------------------------------------------------


class TestKeyStoreRevokeIntegration:
    @pytest.mark.integration
    def test_revoked_key_returns_401(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Revoke key via KeyStore.revoke(); subsequent auth request → 401 (S9).

        Uses the HTTP app's key_store directly to revoke the key.  Because
        active_keys() re-reads keys.json on every request, the next request
        with the managed token must be rejected with 401 immediately.
        """
        from tests.integration.conftest import make_real_app

        with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
            # Create a managed key via POST /keys
            resp = client.post(
                "/keys",
                json={"namespace": "default"},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            assert resp.status_code == 201, resp.text
            managed_token = resp.json()["token"]
            key_id = resp.json()["id"]

            # Confirm managed key works before revocation
            r1 = client.get(
                "/status",
                headers={"Authorization": f"Bearer {managed_token}"},
            )
            assert r1.status_code == 200

            # Revoke the managed key directly via KeyStore.revoke() — bypasses the
            # HTTP route (DELETE /keys/{id} is BE-6, not yet implemented). Because
            # active_keys() reads keys.json from disk on every call, the next request
            # with the managed token will see the revoked status immediately.
            key_store_path = tmp_path / "keys.json"
            from archon_search.key_manager import KeyStore as _KeyStore
            revoke_store = _KeyStore(key_store_path)
            asyncio.run(revoke_store.revoke(key_id))

            # Now the managed token must be rejected
            r2 = client.get(
                "/status",
                headers={"Authorization": f"Bearer {managed_token}"},
            )
            assert r2.status_code == 401
