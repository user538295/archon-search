"""Unit tests for archon_search/telemetry/hasher.py (D8 BE-2)."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# hash_doc_id — pure function tests
# ---------------------------------------------------------------------------


def test_hash_doc_id_returns_64_char_hex() -> None:
    """Output is exactly 64 lowercase hex characters."""
    from archon_search.telemetry.hasher import hash_doc_id

    salt = b"\x01" * 32
    result = hash_doc_id(salt, "some-doc-id")
    assert isinstance(result, str)
    assert len(result) == 64
    assert result == result.lower()
    assert all(c in "0123456789abcdef" for c in result)


def test_hash_doc_id_differs_from_plain_sha256() -> None:
    """Security: output must be genuinely HMAC'd, not a plain SHA-256 (S2)."""
    from archon_search.telemetry.hasher import hash_doc_id

    salt = b"\xab" * 32
    doc_id = "some-doc-id-for-hmac-check"
    hmac_result = hash_doc_id(salt, doc_id)
    plain_sha256 = hashlib.sha256(doc_id.encode()).hexdigest()
    assert hmac_result != plain_sha256, (
        "hash_doc_id should produce HMAC-SHA256, not plain SHA-256"
    )


def test_hash_doc_id_deterministic() -> None:
    """Same salt + input → same output (S13)."""
    from archon_search.telemetry.hasher import hash_doc_id

    salt = b"\xff" * 32
    doc_id = "deterministic-test-doc"
    result1 = hash_doc_id(salt, doc_id)
    result2 = hash_doc_id(salt, doc_id)
    assert result1 == result2


def test_hash_doc_id_distinct_inputs() -> None:
    """Different doc_ids → different outputs (S14)."""
    from archon_search.telemetry.hasher import hash_doc_id

    salt = b"\x12" * 32
    result_a = hash_doc_id(salt, "doc-id-alpha")
    result_b = hash_doc_id(salt, "doc-id-beta")
    assert result_a != result_b


def test_hash_doc_id_empty_string_input() -> None:
    """hash_doc_id handles empty-string doc_id without raising (returns 64-char hex)."""
    from archon_search.telemetry.hasher import hash_doc_id

    salt = b"\x99" * 32
    result = hash_doc_id(salt, "")
    assert isinstance(result, str) and len(result) == 64


# ---------------------------------------------------------------------------
# load_or_create_salt — I/O tests
# ---------------------------------------------------------------------------


def test_load_or_create_salt_returns_none_when_disabled(tmp_path: Path) -> None:
    """flag=False → None returned, no file created."""
    from archon_search.telemetry.hasher import load_or_create_salt

    salt_path = tmp_path / ".telemetry-salt"
    result = load_or_create_salt(hash_doc_ids_enabled=False, salt_path=salt_path)
    assert result is None
    assert not salt_path.exists()


def test_load_or_create_salt_generates_file_atomically_mode_600(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Creates file via atomic_write_bytes with mode 0o600, returns 32 bytes, WARNING logged (S3)."""
    from archon_search.telemetry.hasher import load_or_create_salt

    salt_path = tmp_path / ".telemetry-salt"
    with caplog.at_level(logging.WARNING, logger="archon_search.telemetry.hasher"):
        result = load_or_create_salt(hash_doc_ids_enabled=True, salt_path=salt_path)

    assert result is not None
    assert isinstance(result, bytes)
    assert len(result) == 32
    assert salt_path.exists()

    # Verify mode 600 (ignoring directory bit)
    mode = salt_path.stat().st_mode & 0o777
    assert mode == 0o600, f"Expected mode 600, got {oct(mode)}"

    # Verify file contents match returned salt bytes (FIX-4)
    assert salt_path.read_bytes() == result, "File contents must match returned salt bytes"

    # Verify WARNING was logged for new salt generation (FIX-5: filter by WARNING level)
    assert any(
        record.levelno == logging.WARNING and "salt" in record.message.lower()
        for record in caplog.records
    ), (
        "Expected a WARNING log when generating a new salt"
    )


def test_load_or_create_salt_reuses_existing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Existing file read without regenerating; returned bytes equal file contents (S4).

    Verifies both that the return value matches the known salt AND that the file
    on disk was not overwritten (no regeneration) AND that no WARNING was logged
    (WARNING is only emitted on first-time generation).
    """
    from archon_search.telemetry.hasher import load_or_create_salt

    salt_path = tmp_path / ".telemetry-salt"
    # Write a known salt
    known_salt = b"\xde\xad\xbe\xef" * 8  # 32 bytes
    salt_path.write_bytes(known_salt)

    with caplog.at_level(logging.WARNING, logger="archon_search.telemetry.hasher"):
        result = load_or_create_salt(hash_doc_ids_enabled=True, salt_path=salt_path)

    assert result == known_salt
    # File on disk must be unchanged (no regeneration)
    assert salt_path.read_bytes() == known_salt, "File on disk must not be overwritten"
    # No WARNING should be logged (WARNING only fires on first-time generation)
    warning_records = [
        r for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert not warning_records, f"No WARNING expected for reuse path, got: {warning_records}"


def test_load_or_create_salt_unreadable_logs_error_and_returns_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Unreadable file → None + ERROR logged (S5).

    Uses monkeypatch of the read so the fallback branch is exercised
    deterministically regardless of root/non-root CI environment.
    """
    from archon_search.telemetry.hasher import load_or_create_salt

    salt_path = tmp_path / ".telemetry-salt"
    # Write a valid salt, then patch the module-level read to raise PermissionError.
    # Patching at module level (archon_search.telemetry.hasher.Path) is more surgical
    # than patch.object(Path, "read_bytes") which would affect ALL Path instances.
    salt_path.write_bytes(b"\x00" * 32)

    with patch("archon_search.telemetry.hasher.Path") as mock_path_cls:
        # salt_path.exists() must return True (file is present) so we reach read_bytes
        mock_path_instance = mock_path_cls.return_value
        mock_path_instance.exists.return_value = True
        mock_path_instance.read_bytes.side_effect = PermissionError("Permission denied")
        with caplog.at_level(logging.ERROR, logger="archon_search.telemetry.hasher"):
            result = load_or_create_salt(hash_doc_ids_enabled=True, salt_path=mock_path_instance)

    assert result is None
    assert any(record.levelno == logging.ERROR for record in caplog.records), (
        "Expected an ERROR log when salt file is unreadable"
    )


def test_load_or_create_salt_wrong_size_treated_as_corrupt(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """File with != 32 bytes → ERROR logged, returns None (no weak HMAC from short key)."""
    from archon_search.telemetry.hasher import load_or_create_salt

    for bad_size in [0, 16, 1000]:
        salt_path = tmp_path / f".telemetry-salt-{bad_size}"
        salt_path.write_bytes(b"\xaa" * bad_size)

        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="archon_search.telemetry.hasher"):
            result = load_or_create_salt(hash_doc_ids_enabled=True, salt_path=salt_path)

        assert result is None, f"Expected None for wrong-size salt ({bad_size} bytes)"
        assert any(record.levelno == logging.ERROR for record in caplog.records), (
            f"Expected ERROR log for wrong-size salt ({bad_size} bytes)"
        )


def test_load_or_create_salt_write_failure_returns_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When atomic_write_bytes raises OSError during generation, returns None + ERROR logged.

    Covers the resilience path: server must not crash if the salt directory is unwritable.
    """
    from archon_search.telemetry.hasher import load_or_create_salt

    salt_path = tmp_path / ".telemetry-salt"
    # Salt does not yet exist; patch atomic_write_bytes to simulate a write failure
    with patch(
        "archon_search.telemetry.hasher.atomic_write_bytes",
        side_effect=OSError("disk full"),
    ):
        with caplog.at_level(logging.ERROR, logger="archon_search.telemetry.hasher"):
            result = load_or_create_salt(hash_doc_ids_enabled=True, salt_path=salt_path)

    assert result is None, "Expected None when write fails"
    assert any(record.levelno == logging.ERROR for record in caplog.records), (
        "Expected ERROR log when salt write fails"
    )


# ---------------------------------------------------------------------------
# Integration test: app lifespan sets state correctly
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_app_state_set_on_startup_with_hashing_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan sets app.state.salt_bytes (bytes) and app.state.doc_id_hasher (Callable) when hash_doc_ids=True."""
    import secrets
    from fastapi.testclient import TestClient

    from archon_search.config import SearchConfig
    from archon_search.jobs.store import JobStore
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.server.app import create_app

    api_key = secrets.token_hex(32)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", api_key)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.mcp.enabled = False
    cfg.telemetry.log_dir = str(tmp_path / "search-logs")
    cfg.telemetry.hash_doc_ids = True

    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )

    app = create_app(cfg, job_store, scheduler=scheduler)
    with TestClient(app):
        # salt_bytes: non-null bytes of length 32
        assert hasattr(app.state, "salt_bytes"), "app.state.salt_bytes not set"
        assert isinstance(app.state.salt_bytes, bytes), "salt_bytes must be bytes"
        assert len(app.state.salt_bytes) == 32, "salt_bytes must be 32 bytes"

        # doc_id_hasher: a callable
        assert hasattr(app.state, "doc_id_hasher"), "app.state.doc_id_hasher not set"
        assert callable(app.state.doc_id_hasher), "doc_id_hasher must be callable"

        # The hasher closure must produce 64-char hex
        result = app.state.doc_id_hasher("test-doc-id")
        assert isinstance(result, str) and len(result) == 64


@pytest.mark.integration
def test_app_state_set_on_startup_with_hashing_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan sets app.state.salt_bytes=None and app.state.doc_id_hasher=None when hash_doc_ids=False."""
    import secrets
    from fastapi.testclient import TestClient

    from archon_search.config import SearchConfig
    from archon_search.jobs.store import JobStore
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.server.app import create_app

    api_key = secrets.token_hex(32)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", api_key)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.mcp.enabled = False
    cfg.telemetry.log_dir = str(tmp_path / "search-logs")
    cfg.telemetry.hash_doc_ids = False

    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )

    app = create_app(cfg, job_store, scheduler=scheduler)
    with TestClient(app):
        assert app.state.salt_bytes is None, "salt_bytes must be None when hash_doc_ids=False"
        assert app.state.doc_id_hasher is None, "doc_id_hasher must be None when hash_doc_ids=False"
