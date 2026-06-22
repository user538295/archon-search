"""Tests for BE-1: KeyRecord Pydantic model, AuthConfig, KeyStore.create() and load().

Covers:
- C1 (KeyRecord entity)
- S1 (create key)
- S17 (corrupted keys.json graceful degradation)
- S20 (token not stored in file)
- S21 (file mode 0600)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from archon_search.config import AuthConfig, SearchConfig, load_config
from archon_search.key_manager import KeyRecord, KeyStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path) -> KeyStore:
    return KeyStore(tmp_path / "keys.json")


# ---------------------------------------------------------------------------
# KeyRecord model tests
# ---------------------------------------------------------------------------


class TestKeyRecord:
    def test_key_record_model_valid(self) -> None:
        """KeyRecord accepts valid fields."""
        now = datetime.now(UTC)
        record = KeyRecord(
            id="test-id",
            token_hash="a" * 64,
            namespace="ns",
            label="my label",
            created_at=now,
            expires_at=None,
            status="active",
        )
        assert record.id == "test-id"
        assert record.token_hash == "a" * 64
        assert record.namespace == "ns"
        assert record.label == "my label"
        assert record.status == "active"
        assert record.expires_at is None

    def test_key_record_rejects_unknown_fields(self) -> None:
        """KeyRecord ignores unknown fields (extra='ignore')."""
        # With extra='ignore', unknown fields are silently dropped — no error
        record = KeyRecord(
            id="test-id",
            token_hash="a" * 64,
            namespace="ns",
            created_at=datetime.now(UTC),
            status="active",
            unknown_field="should_be_dropped",  # type: ignore[call-arg]
        )
        assert not hasattr(record, "unknown_field")

    def test_key_record_label_optional(self) -> None:
        """label field is optional."""
        record = KeyRecord(
            id="test-id",
            token_hash="b" * 64,
            namespace="ns",
            created_at=datetime.now(UTC),
            status="active",
        )
        assert record.label is None

    def test_key_record_expires_at_optional(self) -> None:
        """expires_at field is optional (None = no expiry)."""
        record = KeyRecord(
            id="test-id",
            token_hash="c" * 64,
            namespace="ns",
            created_at=datetime.now(UTC),
            status="active",
        )
        assert record.expires_at is None

    def test_key_record_status_revoked(self) -> None:
        """status can be 'revoked'."""
        record = KeyRecord(
            id="test-id",
            token_hash="d" * 64,
            namespace="ns",
            created_at=datetime.now(UTC),
            status="revoked",
        )
        assert record.status == "revoked"


# ---------------------------------------------------------------------------
# KeyStore.create() tests
# ---------------------------------------------------------------------------


class TestKeyStoreCreate:
    def test_keystore_create_hashes_token(self, tmp_path: Path) -> None:
        """create() stores SHA-256 hex of token; returns {id, token} with both UUID and raw token."""
        store = _make_store(tmp_path)

        result = asyncio.run(store.create(ns="ns1", label=None, expires_at=None))

        assert "id" in result
        assert "token" in result
        raw_token = result["token"]
        assert len(raw_token) == 64  # 32 bytes hex
        # Load the file and check the hash is stored, not the raw token
        records = asyncio.run(store.load())
        assert len(records) == 1
        expected_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        assert records[0].token_hash == expected_hash
        assert raw_token not in records[0].token_hash  # raw not == hash

    def test_token_not_in_keys_json(self, tmp_path: Path) -> None:
        """Regression: raw token must not appear anywhere in keys.json content (S20)."""
        store = _make_store(tmp_path)

        result = asyncio.run(store.create(ns="ns1", label=None, expires_at=None))
        raw_token = result["token"]

        keys_file = tmp_path / "keys.json"
        file_content = keys_file.read_text()
        assert raw_token not in file_content

    def test_keystore_create_writes_file_mode_600(self, tmp_path: Path) -> None:
        """keys.json is created with mode 0600 on initial creation (S21)."""
        if sys.platform == "win32":
            pytest.skip("chmod not relevant on Windows")
        store = _make_store(tmp_path)

        asyncio.run(store.create(ns="ns1", label=None, expires_at=None))

        keys_file = tmp_path / "keys.json"
        assert keys_file.exists()
        mode = stat.S_IMODE(keys_file.stat().st_mode)
        assert mode == 0o600

    def test_keystore_create_overwrites_existing_file_mode_600(self, tmp_path: Path) -> None:
        """keys.json is re-created with 0600 even if the file already exists with permissive mode."""
        if sys.platform == "win32":
            pytest.skip("chmod not relevant on Windows")
        store = _make_store(tmp_path)
        keys_file = tmp_path / "keys.json"

        # Pre-create with a permissive mode
        keys_file.write_bytes(b"[]")
        keys_file.chmod(0o644)

        asyncio.run(store.create(ns="ns1", label=None, expires_at=None))

        mode = stat.S_IMODE(keys_file.stat().st_mode)
        assert mode == 0o600

    def test_keystore_create_returns_uuid_id(self, tmp_path: Path) -> None:
        """create() returns a UUID4 id string."""
        import uuid

        store = _make_store(tmp_path)
        result = asyncio.run(store.create(ns="ns1", label=None, expires_at=None))
        # Should be a valid UUID
        parsed = uuid.UUID(result["id"])
        assert parsed.version == 4

    def test_keystore_create_stores_namespace(self, tmp_path: Path) -> None:
        """create() stores the namespace on the KeyRecord."""
        store = _make_store(tmp_path)
        asyncio.run(store.create(ns="my-namespace", label=None, expires_at=None))
        records = asyncio.run(store.load())
        assert records[0].namespace == "my-namespace"

    def test_keystore_create_stores_label(self, tmp_path: Path) -> None:
        """create() stores an optional label on the KeyRecord."""
        store = _make_store(tmp_path)
        asyncio.run(store.create(ns="ns1", label="my-label", expires_at=None))
        records = asyncio.run(store.load())
        assert records[0].label == "my-label"

    def test_keystore_create_stores_expires_at(self, tmp_path: Path) -> None:
        """create() stores an optional expires_at on the KeyRecord."""
        store = _make_store(tmp_path)
        future = datetime(2030, 1, 1, tzinfo=UTC)
        asyncio.run(store.create(ns="ns1", label=None, expires_at=future))
        records = asyncio.run(store.load())
        assert records[0].expires_at == future

    def test_keystore_create_naive_expires_at_raises(self, tmp_path: Path) -> None:
        """create() rejects a naive (timezone-unaware) expires_at."""
        store = _make_store(tmp_path)
        naive = datetime(2030, 1, 1)  # no tzinfo
        with pytest.raises(ValueError, match="timezone-aware"):
            asyncio.run(store.create(ns="ns1", label=None, expires_at=naive))

    def test_keystore_create_multiple_keys_accumulate(self, tmp_path: Path) -> None:
        """Multiple create() calls accumulate keys in keys.json."""
        store = _make_store(tmp_path)
        asyncio.run(store.create(ns="ns1", label=None, expires_at=None))
        asyncio.run(store.create(ns="ns2", label=None, expires_at=None))
        records = asyncio.run(store.load())
        assert len(records) == 2

    def test_keystore_concurrent_creates_no_lost_write(self, tmp_path: Path) -> None:
        """Two concurrent asyncio tasks both creating keys — both persist."""
        store = _make_store(tmp_path)

        async def _two_concurrent() -> None:
            t1 = asyncio.create_task(store.create(ns="ns1", label=None, expires_at=None))
            t2 = asyncio.create_task(store.create(ns="ns2", label=None, expires_at=None))
            await asyncio.gather(t1, t2)

        asyncio.run(_two_concurrent())
        records = asyncio.run(store.load())
        assert len(records) == 2


# ---------------------------------------------------------------------------
# KeyStore.load() tests
# ---------------------------------------------------------------------------


class TestKeyStoreLoad:
    def test_keystore_load_tightens_mode(self, tmp_path: Path) -> None:
        """load() calls _chmod_600 after a successful read to tighten permissions."""
        if sys.platform == "win32":
            pytest.skip("chmod not relevant on Windows")
        store = _make_store(tmp_path)
        keys_file = tmp_path / "keys.json"
        # Create with permissive mode
        keys_file.write_bytes(b"[]")
        keys_file.chmod(0o644)

        asyncio.run(store.load())

        mode = stat.S_IMODE(keys_file.stat().st_mode)
        assert mode == 0o600

    def test_keystore_load_empty_array_ok(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """keys.json is [] → empty list with no error logged."""
        store = _make_store(tmp_path)
        keys_file = tmp_path / "keys.json"
        keys_file.write_bytes(b"[]")

        with caplog.at_level(logging.ERROR):
            records = asyncio.run(store.load())

        assert records == []
        assert not any(r.levelname == "ERROR" for r in caplog.records)

    def test_keystore_active_keys_excludes_past_expires_at(self, tmp_path: Path) -> None:
        """Key with expires_at in the past is excluded from active_keys()."""
        store = _make_store(tmp_path)
        keys_file = tmp_path / "keys.json"
        past = datetime(2000, 1, 1, tzinfo=UTC)  # far in the past
        record = {
            "id": "expired-id",
            "token_hash": "b" * 64,
            "namespace": "ns",
            "label": None,
            "created_at": past.isoformat(),
            "expires_at": past.isoformat(),
            "status": "active",
        }
        keys_file.write_bytes(json.dumps([record]).encode())

        active = asyncio.run(store.active_keys())
        assert active == []

    def test_keystore_active_keys_excludes_revoked_status(self, tmp_path: Path) -> None:
        """Key with status=revoked is excluded from active_keys()."""
        store = _make_store(tmp_path)
        keys_file = tmp_path / "keys.json"
        now = datetime.now(UTC)
        record = {
            "id": "revoked-id",
            "token_hash": "c" * 64,
            "namespace": "ns",
            "label": None,
            "created_at": now.isoformat(),
            "expires_at": None,
            "status": "revoked",
        }
        keys_file.write_bytes(json.dumps([record]).encode())

        active = asyncio.run(store.active_keys())
        assert active == []

    def test_keystore_load_missing_file_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When keys.json does not exist, load() returns empty list with no error logged."""
        store = _make_store(tmp_path)
        with caplog.at_level(logging.ERROR):
            records = asyncio.run(store.load())
        assert records == []
        assert not any(r.levelname == "ERROR" for r in caplog.records)

    def test_keystore_active_keys_expired_id_logged_only_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_logged_expired_ids suppresses repeated INFO log for the same expired key (S10)."""
        store = _make_store(tmp_path)
        keys_file = tmp_path / "keys.json"
        past = datetime(2000, 1, 1, tzinfo=UTC)
        record = {
            "id": "once-log-id",
            "token_hash": "e" * 64,
            "namespace": "ns",
            "label": None,
            "created_at": past.isoformat(),
            "expires_at": past.isoformat(),
            "status": "active",
        }
        keys_file.write_bytes(json.dumps([record]).encode())

        with caplog.at_level(logging.INFO, logger="archon_search.key_manager"):
            asyncio.run(store.active_keys())
            asyncio.run(store.active_keys())

        info_msgs = [r for r in caplog.records if r.levelname == "INFO" and "once-log-id" in r.message]
        assert len(info_msgs) == 1, f"Expected exactly 1 INFO log, got {len(info_msgs)}"

    def test_keystore_load_corrupted_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Corrupted keys.json (unparseable JSON) logs ERROR and returns empty list (S17)."""
        store = _make_store(tmp_path)
        keys_file = tmp_path / "keys.json"
        keys_file.write_bytes(b"not-valid-json{{{")

        with caplog.at_level(logging.ERROR):
            records = asyncio.run(store.load())

        assert records == []
        assert any("ERROR" in r.levelname for r in caplog.records)

    def test_keystore_load_wrong_type_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """keys.json contains {} (valid JSON, wrong type) → empty list + ERROR log (S17)."""
        store = _make_store(tmp_path)
        keys_file = tmp_path / "keys.json"
        keys_file.write_bytes(b"{}")

        with caplog.at_level(logging.ERROR):
            records = asyncio.run(store.load())

        assert records == []
        assert any("ERROR" in r.levelname for r in caplog.records)

    def test_keystore_load_invalid_record_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """JSON array with a record missing required fields → empty list + ERROR log (S17)."""
        store = _make_store(tmp_path)
        keys_file = tmp_path / "keys.json"
        # Missing required fields (token_hash, namespace, etc.)
        keys_file.write_bytes(b'[{"id": "test"}]')

        with caplog.at_level(logging.ERROR):
            records = asyncio.run(store.load())

        assert records == []
        assert any("ERROR" in r.levelname for r in caplog.records)

    def test_keystore_load_empty_file_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Empty file (0 bytes) → empty list + ERROR log (S17)."""
        store = _make_store(tmp_path)
        keys_file = tmp_path / "keys.json"
        keys_file.write_bytes(b"")

        with caplog.at_level(logging.ERROR):
            records = asyncio.run(store.load())

        assert records == []
        assert any("ERROR" in r.levelname for r in caplog.records)

    def test_keystore_active_keys_reads_disk_on_each_call(self, tmp_path: Path) -> None:
        """active_keys() re-reads keys.json on every call (disk-read-on-demand)."""
        store = _make_store(tmp_path)
        keys_file = tmp_path / "keys.json"
        keys_file.write_bytes(b"[]")

        # First call: empty
        keys1 = asyncio.run(store.active_keys())
        assert keys1 == []

        # Directly write a key record to disk (bypassing the store's lock)
        now = datetime.now(UTC)
        record = {
            "id": "direct-id",
            "token_hash": "a" * 64,
            "namespace": "ns",
            "label": None,
            "created_at": now.isoformat(),
            "expires_at": None,
            "status": "active",
        }
        keys_file.write_bytes(json.dumps([record]).encode())

        # Second call should see the change
        keys2 = asyncio.run(store.active_keys())
        assert len(keys2) == 1
        assert keys2[0].id == "direct-id"


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


class TestKeyStoreIntegration:
    def test_keystore_create_and_load_roundtrip(self, tmp_path: Path) -> None:
        """Create key, reload from file (simulating restart), key survives."""
        store1 = KeyStore(tmp_path / "keys.json")
        result = asyncio.run(store1.create(ns="ns1", label="test-label", expires_at=None))
        created_id = result["id"]

        # Simulate restart: create a new KeyStore pointing to the same file
        store2 = KeyStore(tmp_path / "keys.json")
        records = asyncio.run(store2.load())

        assert len(records) == 1
        assert records[0].id == created_id
        assert records[0].namespace == "ns1"
        assert records[0].label == "test-label"
        assert records[0].status == "active"


# ---------------------------------------------------------------------------
# AuthConfig tests
# ---------------------------------------------------------------------------


class TestAuthConfig:
    def test_auth_config_defaults(self) -> None:
        """AuthConfig.rotate_grace_seconds defaults to 0."""
        auth = AuthConfig()
        assert auth.rotate_grace_seconds == 0

    def test_search_config_has_auth(self) -> None:
        """SearchConfig has an auth field of type AuthConfig."""
        config = SearchConfig()
        assert isinstance(config.auth, AuthConfig)
        assert config.auth.rotate_grace_seconds == 0

    def test_load_config_auth_section(self, tmp_path: Path) -> None:
        """[auth] TOML section is parsed into config.auth."""
        toml_file = tmp_path / "config.toml"
        toml_file.write_text(
            "[auth]\nrotate_grace_seconds = 30\n",
            encoding="utf-8",
        )
        config = load_config(path=toml_file)
        assert config.auth.rotate_grace_seconds == 30

    def test_load_config_auth_section_negative_raises(self, tmp_path: Path) -> None:
        """[auth].rotate_grace_seconds must be >= 0; negative values raise ConfigError."""
        from archon_search.config import ConfigError

        toml_file = tmp_path / "config.toml"
        toml_file.write_text(
            "[auth]\nrotate_grace_seconds = -1\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError):
            load_config(path=toml_file)
