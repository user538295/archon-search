"""D8 / T-3 — e2e: salt lifecycle edge cases and data-dir override.

Scenarios covered:
- S3: hash_doc_ids=true and no salt file → server creates .telemetry-salt with mode 600
- S4: hash_doc_ids=true and salt already exists → same hashed value on second start
- S5: hash_doc_ids=true and salt file is unreadable (mode 000) → hashing falls back,
      raw doc_ids in JSONL, server does not crash.
      Skipped when running as root (chmod 000 is ineffective for root).
- S15: ARCHON_SEARCH_DATA_DIR=/custom/path → salt created at custom path
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import make_real_app, ingest_file_via_path

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _read_telemetry_entries(cfg: Any) -> list[dict]:
    """Read all JSONL telemetry entries written to cfg.telemetry.log_dir."""
    log_dir = Path(cfg.telemetry.log_dir)
    entries: list[dict] = []
    if not log_dir.exists():
        return entries
    for jsonl_file in log_dir.glob("*.jsonl"):
        for line in jsonl_file.read_text().splitlines():
            stripped = line.strip()
            if stripped:
                entries.append(json.loads(stripped))
    return entries


# ---------------------------------------------------------------------------
# S3 — salt file created on first start with mode 600
# ---------------------------------------------------------------------------


def test_e2e_salt_file_created_on_first_start_with_mode_600(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    """Real tmp data dir, hashing on, no pre-existing salt → .telemetry-salt created mode 600 (S3).

    Verifies:
    - The salt file does not exist before server start.
    - After startup, the salt file exists at <data_dir>/.telemetry-salt.
    - The file has mode 0o600 (owner read/write only).
    - The file contains exactly 32 bytes (the expected salt size).
    - A WARNING is logged mentioning telemetry and salt (first-create notice).
    - Searches produce telemetry entries with doc_ids_hashed=True and 64-char hex result_doc_ids.
    """
    salt_path = tmp_path / ".telemetry-salt"
    assert not salt_path.exists(), "Pre-condition: salt file must not exist before server start."

    text_file = tmp_path / "col-s3" / "doc.txt"
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text("salt creation test document for S3 e2e")

    with caplog.at_level(logging.WARNING, logger="archon_search.telemetry.hasher"):
        with make_real_app(
            tmp_path, monkeypatch, telemetry_enabled=True, hash_doc_ids_enabled=True
        ) as (client, cfg, api_key):
            # Server started — assert the salt file was created.
            assert salt_path.exists(), (
                f"Expected salt file at {salt_path} after server start with hash_doc_ids=True (S3)."
            )
            # Verify size: must be exactly 32 bytes.
            raw_salt = salt_path.read_bytes()
            assert len(raw_salt) == 32, (
                f"Expected 32-byte salt; got {len(raw_salt)} bytes at {salt_path} (S3)."
            )
            # Verify mode: must be 0o600 (owner read/write, no other permissions).
            file_mode = stat.S_IMODE(salt_path.stat().st_mode)
            assert file_mode == 0o600, (
                f"Expected salt file mode 0o600; got 0o{file_mode:o} (S3). "
                "The salt file must be unreadable by other users."
            )
            # Confirm searches still work (server did not crash).
            ingest_file_via_path(client, "col-s3", str(text_file), api_key=api_key)
            headers = {"Authorization": f"Bearer {api_key}"}
            resp = client.post(
                "/search",
                json={"collection": "col-s3", "query": "salt creation"},
                headers=headers,
            )
            assert resp.status_code == 200, (
                f"Server must not crash after creating salt file (S3). "
                f"Status: {resp.status_code} {resp.text[:200]}"
            )

    # Assert WARNING was logged about telemetry salt (first-create event).
    warning_msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        "telemetry" in m.lower() and "salt" in m.lower() for m in warning_msgs
    ), (
        f"Expected a WARNING log containing 'telemetry' and 'salt' from "
        f"archon_search.telemetry.hasher (S3). Captured warnings: {warning_msgs!r}"
    )

    # Read JSONL after the `with` block exits (writer has flushed on shutdown).
    entries = _read_telemetry_entries(cfg)
    search_ok = [
        e for e in entries
        if e.get("endpoint") == "search" and e.get("status") == "ok"
    ]
    assert search_ok, f"Expected search/ok telemetry entries after S3 run. All: {entries!r}"

    first_entry = search_ok[0]
    assert first_entry.get("doc_ids_hashed") is True, (
        f"S3: doc_ids_hashed must be True when hashing is active. Entry: {first_entry!r}"
    )
    result_doc_ids = first_entry.get("result_doc_ids") or []
    assert result_doc_ids, (
        f"S3: result_doc_ids must be non-empty in telemetry entry. Entry: {first_entry!r}"
    )
    for doc_id in result_doc_ids:
        assert len(doc_id) == 64 and all(c in "0123456789abcdef" for c in doc_id), (
            f"S3: each result_doc_id must be a 64-char hex HMAC. Got: {doc_id!r}"
        )


# ---------------------------------------------------------------------------
# S4 — salt reused across server restarts
# ---------------------------------------------------------------------------


def test_e2e_salt_reused_across_server_restarts(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Explicit restart: same salt → byte-identical hashed doc_id in JSONL (S4).

    Steps:
    1. Start server with hashing on (no prior salt file).
    2. Ingest a document and search → record the hashed doc_id from JSONL.
    3. Stop server (exit the context manager).
    4. Restart with the SAME data dir (salt file persists on disk).
    5. Search the same document.
    6. Assert the JSONL hashed doc_id is byte-identical to step 2.

    This proves salt reuse, not merely "salt file still exists".
    """
    text_file = tmp_path / "col-s4" / "doc.txt"
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text("salt reuse document for S4 determinism across restarts")

    # --- Session 1: start fresh, ingest, search, record hashed doc_id ---
    with make_real_app(
        tmp_path, monkeypatch, telemetry_enabled=True, hash_doc_ids_enabled=True
    ) as (client1, cfg1, api_key1):
        ingest_file_via_path(client1, "col-s4", str(text_file), api_key=api_key1)
        headers1 = {"Authorization": f"Bearer {api_key1}"}
        resp1 = client1.post(
            "/search",
            json={"collection": "col-s4", "query": "salt reuse"},
            headers=headers1,
        )
        assert resp1.status_code == 200, f"Session 1 search failed: {resp1.status_code}"
        assert resp1.json()["results"], "Session 1: search must return results for S4."

    # Read telemetry AFTER session 1 closes (writer flushes on shutdown).
    entries_s1 = _read_telemetry_entries(cfg1)
    search_ok_s1 = [
        e for e in entries_s1
        if e.get("endpoint") == "search" and e.get("status") == "ok"
    ]
    assert search_ok_s1, (
        f"Expected search/ok telemetry entries after session 1 (S4). All: {entries_s1!r}"
    )
    hashed_ids_s1 = search_ok_s1[0].get("result_doc_ids") or []
    assert hashed_ids_s1, (
        "Session 1: telemetry entry must have non-empty result_doc_ids (S4). "
        f"Entry: {search_ok_s1[0]!r}"
    )
    # Confirm the hash field is set — proves hashing was active in session 1.
    assert search_ok_s1[0].get("doc_ids_hashed") is True, (
        f"Session 1 must have doc_ids_hashed=True (S4). Entry: {search_ok_s1[0]!r}"
    )

    # Verify the salt file exists at the expected path before restarting.
    salt_path = tmp_path / ".telemetry-salt"
    assert salt_path.exists(), (
        f"Salt file must persist after session 1 shutdown (S4). Path: {salt_path}"
    )

    # --- Session 2: restart with same data dir (same DB, same salt file) ---
    with make_real_app(
        tmp_path, monkeypatch, telemetry_enabled=True, hash_doc_ids_enabled=True
    ) as (client2, cfg2, api_key2):
        # Clear logs from session 1 by noting the existing entries count.
        entries_before_s2 = _read_telemetry_entries(cfg2)
        before_count = len([
            e for e in entries_before_s2
            if e.get("endpoint") == "search" and e.get("status") == "ok"
        ])

        headers2 = {"Authorization": f"Bearer {api_key2}"}
        resp2 = client2.post(
            "/search",
            # Same collection (persisted in DB), same query → same doc returned.
            json={"collection": "col-s4", "query": "salt reuse"},
            headers=headers2,
        )
        assert resp2.status_code == 200, f"Session 2 search failed: {resp2.status_code}"
        assert resp2.json()["results"], "Session 2: search must return results for S4."

    # Read telemetry AFTER session 2 closes.
    entries_s2 = _read_telemetry_entries(cfg2)
    search_ok_all = [
        e for e in entries_s2
        if e.get("endpoint") == "search" and e.get("status") == "ok"
    ]
    # The new entries from session 2 are those added after before_count.
    assert len(search_ok_all) > before_count, (
        f"Session 2 must add a new search/ok telemetry entry (S4). "
        f"Before: {before_count}, after: {len(search_ok_all)}. All: {entries_s2!r}"
    )
    # search_ok_all[before_count] is the first entry written by session 2
    # (entries 0..before_count-1 are from session 1, still present in the JSONL files).
    hashed_ids_s2 = search_ok_all[before_count].get("result_doc_ids") or []
    assert hashed_ids_s2, (
        "Session 2: telemetry entry must have non-empty result_doc_ids (S4). "
        f"Entry: {search_ok_all[before_count]!r}"
    )
    assert search_ok_all[before_count].get("doc_ids_hashed") is True, (
        f"Session 2 must have doc_ids_hashed=True (S4). Entry: {search_ok_all[before_count]!r}"
    )

    # Core S4 assertion: the hashed doc_id from session 2 is byte-identical to session 1.
    # Same salt + same doc_id → same HMAC output (determinism via salt reuse).
    assert sorted(hashed_ids_s1) == sorted(hashed_ids_s2), (
        "HMAC-SHA256 must produce the same hashed doc_ids when the same salt is reused (S4). "
        f"Session 1 hashes: {sorted(hashed_ids_s1)!r}\n"
        f"Session 2 hashes: {sorted(hashed_ids_s2)!r}\n"
        "If these differ, the salt was regenerated rather than reused across restarts."
    )


# ---------------------------------------------------------------------------
# S5 — unreadable salt file → server falls back and does not crash
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    getattr(os, "getuid", lambda: -1)() == 0,
    reason="chmod 000 is ineffective for root — cannot test unreadable-salt fallback as root",
)
def test_e2e_unreadable_salt_server_falls_back_and_does_not_crash(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    """Unreadable salt file → hashing disabled, raw doc_ids in JSONL, server does not crash (S5).

    Setup: create a valid-looking (but locked) salt file before server start.
    chmod 000 prevents load_or_create_salt from reading it → ERROR logged,
    hashing falls back to disabled for the session.

    Skip when running as root because root bypasses POSIX permission bits.
    """
    text_file = tmp_path / "col-s5" / "doc.txt"
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text("unreadable salt fallback document for S5 e2e")

    # Pre-create the salt file with random bytes, then make it unreadable.
    salt_path = tmp_path / ".telemetry-salt"
    salt_path.write_bytes(secrets.token_bytes(32))
    salt_path.chmod(0o000)

    try:
        with caplog.at_level(logging.ERROR, logger="archon_search.telemetry.hasher"):
            with make_real_app(
                tmp_path, monkeypatch, telemetry_enabled=True, hash_doc_ids_enabled=True
            ) as (client, cfg, api_key):
                # Server must start successfully even with an unreadable salt file.
                ingest_file_via_path(client, "col-s5", str(text_file), api_key=api_key)
                headers = {"Authorization": f"Bearer {api_key}"}
                resp = client.post(
                    "/search",
                    json={"collection": "col-s5", "query": "unreadable salt"},
                    headers=headers,
                )
                assert resp.status_code == 200, (
                    f"Server must not crash when salt file is unreadable (S5). "
                    f"Status: {resp.status_code} {resp.text[:200]}"
                )
                results = resp.json()["results"]
                assert results, "S5: search must return results (server is functional)."

                # Assert GET /status reflects hashing is disabled due to unreadable salt.
                status_resp = client.get("/status", headers={"Authorization": f"Bearer {api_key}"})
                assert status_resp.status_code == 200
                telemetry = status_resp.json().get("telemetry")
                assert telemetry is not None, "S5: telemetry sub-object must be present"
                assert telemetry.get("hash_doc_ids_enabled") is False, (
                    f"Expected hash_doc_ids_enabled=False in status when salt is unreadable "
                    f"(S5 fallback). Got: {telemetry!r}"
                )
    finally:
        # Restore permissions so tmp_path cleanup can proceed.
        salt_path.chmod(0o600)

    # Assert ERROR was logged about unreadable salt.
    error_msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(
        "telemetry" in m.lower() and ("cannot read salt" in m.lower() or "unreadable" in m.lower())
        for m in error_msgs
    ), (
        f"Expected an ERROR log containing 'telemetry' and 'cannot read salt'/'unreadable' "
        f"from archon_search.telemetry.hasher (S5). Captured errors: {error_msgs!r}"
    )

    entries = _read_telemetry_entries(cfg)
    search_ok = [
        e for e in entries
        if e.get("endpoint") == "search" and e.get("status") == "ok"
    ]
    assert search_ok, f"Expected search/ok telemetry entries for S5. All: {entries!r}"

    # Core S5 assertion: hashing must be DISABLED (fallback mode).
    # Entries must have raw (not HMAC) doc_ids and doc_ids_hashed=False.
    for entry in search_ok:
        assert entry.get("doc_ids_hashed") is False, (
            f"Expected doc_ids_hashed=False when salt is unreadable (S5 fallback). "
            f"Entry: {entry!r}"
        )

    # Verify result_doc_ids are raw (non-HMAC) SHA-256 values, not 64-char HMAC hashes.
    # The raw doc_id is derived from the file path: SHA-256 of the resolved path string.
    raw_doc_id = hashlib.sha256(str(text_file.resolve()).encode()).hexdigest()
    all_result_ids = [
        doc_id
        for entry in search_ok
        for doc_id in (entry.get("result_doc_ids") or [])
    ]
    assert all_result_ids, (
        f"S5: result_doc_ids must be non-empty in fallback mode. Entries: {search_ok!r}"
    )
    assert raw_doc_id in all_result_ids, (
        f"S5: expected raw doc_id {raw_doc_id!r} in result_doc_ids when hashing is disabled. "
        f"Got: {all_result_ids!r}"
    )


# ---------------------------------------------------------------------------
# S15 — ARCHON_SEARCH_DATA_DIR override → salt in correct location
# ---------------------------------------------------------------------------


def test_e2e_custom_data_dir_salt_in_correct_location(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """ARCHON_SEARCH_DATA_DIR=/custom/path, hashing on → salt at /custom/path/.telemetry-salt (S15).

    The salt file must follow the data-dir override, not default to ~/.archon-search/.
    This test uses a subdirectory of tmp_path as the custom data dir to stay isolated.
    """
    custom_data_dir = tmp_path / "custom-data-dir"
    custom_data_dir.mkdir()

    # Override ARCHON_SEARCH_DATA_DIR to the custom path BEFORE make_real_app,
    # so get_data_dir() returns custom_data_dir during lifespan startup.
    # make_real_app itself also calls monkeypatch.setenv for ARCHON_SEARCH_DATA_DIR,
    # but we override it here first; make_real_app will re-set it to tmp_path.
    # Instead, we use a fresh monkeypatch layer by passing the custom path as tmp_path.
    # The simplest approach: use the custom_data_dir as tmp_path for make_real_app
    # (which sets ARCHON_SEARCH_DATA_DIR=str(tmp_path)), but keep a separate
    # directory for LanceDB (cfg.db_path) to avoid conflicts with the salt file.
    #
    # make_real_app sets:
    #   - ARCHON_SEARCH_DATA_DIR = str(tmp_path_arg)    → get_data_dir() → salt_path
    #   - cfg.db_path = str(tmp_path_arg / "db")        → database
    #   - cfg.telemetry.log_dir = str(tmp_path_arg / "search-logs")
    #
    # By passing custom_data_dir as tmp_path_arg, the salt lands at
    # custom_data_dir / ".telemetry-salt" — proving ARCHON_SEARCH_DATA_DIR is honored.

    text_file = custom_data_dir / "col-s15" / "doc.txt"
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text("custom data dir document for S15 e2e salt location test")

    expected_salt_path = custom_data_dir / ".telemetry-salt"

    assert not expected_salt_path.exists(), (
        f"Pre-condition: salt must not exist at {expected_salt_path} before test."
    )

    with make_real_app(
        custom_data_dir, monkeypatch, telemetry_enabled=True, hash_doc_ids_enabled=True
    ) as (client, cfg, api_key):
        # Server started — salt must be at the custom data dir, not the default.
        assert expected_salt_path.exists(), (
            f"Salt file must be created at {expected_salt_path} when "
            f"ARCHON_SEARCH_DATA_DIR={custom_data_dir} (S15)."
        )
        # The home-dir salt must NOT have been created or touched by this run.
        # (We cannot assert it doesn't exist globally because other runs may have
        # created it. We assert the custom-path salt is correct and non-empty.)
        raw_salt = expected_salt_path.read_bytes()
        assert len(raw_salt) == 32, (
            f"Expected 32-byte salt at custom path; got {len(raw_salt)} bytes (S15)."
        )
        file_mode = stat.S_IMODE(expected_salt_path.stat().st_mode)
        assert file_mode == 0o600, (
            f"Expected mode 0o600 for salt at custom path; got 0o{file_mode:o} (S15)."
        )
        # Confirm searches work with hashing enabled at the custom data dir.
        ingest_file_via_path(client, "col-s15", str(text_file), api_key=api_key)
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = client.post(
            "/search",
            json={"collection": "col-s15", "query": "custom data dir"},
            headers=headers,
        )
        assert resp.status_code == 200, (
            f"Search must work with custom ARCHON_SEARCH_DATA_DIR (S15). "
            f"Status: {resp.status_code} {resp.text[:200]}"
        )

    # Read JSONL after the `with` block exits (writer has flushed on shutdown).
    entries = _read_telemetry_entries(cfg)
    search_ok = [
        e for e in entries
        if e.get("endpoint") == "search" and e.get("status") == "ok"
    ]
    assert search_ok, f"Expected search/ok telemetry entries after S15 run. All: {entries!r}"

    first_entry = search_ok[0]
    assert first_entry.get("doc_ids_hashed") is True, (
        f"S15: doc_ids_hashed must be True when hashing is active at custom data dir. "
        f"Entry: {first_entry!r}"
    )
    result_doc_ids = first_entry.get("result_doc_ids") or []
    assert result_doc_ids, (
        f"S15: result_doc_ids must be non-empty in telemetry entry. Entry: {first_entry!r}"
    )
    for doc_id in result_doc_ids:
        assert len(doc_id) == 64 and all(c in "0123456789abcdef" for c in doc_id), (
            f"S15: each result_doc_id must be a 64-char hex HMAC. Got: {doc_id!r}"
        )
