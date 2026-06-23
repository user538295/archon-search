"""T-3: E2e — rotate default key + grace window; TOML + managed coexist; corruption degrades gracefully.

Covers:
- S15: rotate with grace; old token works during grace window, fails after expiry
- S7:  TOML [namespaces] token accepted against full app (backward-compat)
- S8:  TOML token and managed token both accepted simultaneously (coexistence)
- S17: write corrupt keys.json; start app via TestClient; default key still returns 200
- S20: create key; read keys.json; assert raw token absent (regression)

For rotate tests (S6, S15): ``ARCHON_SEARCH_API_KEY`` env var must NOT be set —
the route returns 409 when it is (S23).  We use ``monkeypatch.delenv`` so the app
auto-generates a key from ``.search.env`` and the env-var guard is inactive.

Two-rotation pattern: the first rotation converts the auto-generated key into a
managed ``KeyRecord`` in ``keys.json``; the second rotation uses that managed record
as ``current_token`` so grace/revocation assertions are provably triggered.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

# Embedding dimension used by the stub fastembed backend.
_EMBEDDING_DIM = 384
_COL_COEXIST = "managed-coexist-col"
_DOC_PATH_COEXIST = "/data/t3/coexist-doc.md"
_DOC_TEXT_COEXIST = "t3 coexist namespace isolation document"


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _doc_id(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()


async def _inject_chunks_ns(store, col: str, embedding_model: str, namespace: str) -> None:
    """Create a collection + insert one chunk under *namespace* so /search can find it."""
    from archon_search._types import ChunkRecord, normalize_iso_utc
    from archon_search.collection_meta import CollectionMeta

    doc_id = _doc_id(_DOC_PATH_COEXIST)
    chunk = ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text=_DOC_TEXT_COEXIST,
        vector=[0.0] * _EMBEDDING_DIM,
        source_path=_DOC_PATH_COEXIST,
        indexed_at=normalize_iso_utc(datetime.now(UTC)),
        acl=None,
    )

    await store.ensure_collection(col, _EMBEDDING_DIM)
    await store.ingest_chunks(col, [chunk], namespace=namespace)
    await store.rebuild_fts_index(col)
    meta = CollectionMeta(
        name=col,
        active_embedding_model=embedding_model,
        doc_count=1,
        chunk_count=1,
        namespace=namespace,
    )
    await store.update_collection_meta(meta)


def _make_rotate_app(tmp_path, monkeypatch, *, grace_seconds: int | None = None):
    """Build an app WITHOUT ``ARCHON_SEARCH_API_KEY`` set so POST /keys/rotate is allowed.

    Returns ``(app, initial_api_key)`` with an auto-generated initial key loaded
    from ``.search.env``.
    """
    from archon_search.config import AuthConfig, SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.key_manager import ENV_VAR
    from archon_search.server.app import create_app

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(ENV_VAR, raising=False)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    if grace_seconds is not None:
        cfg.auth = AuthConfig(rotate_grace_seconds=grace_seconds)

    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )
    app = create_app(cfg, job_store, scheduler=scheduler)
    return app, app.state.api_key


# ---------------------------------------------------------------------------
# T-3 test 1: rotate with grace window — old token works during window, fails after (S15)
# ---------------------------------------------------------------------------


def test_e2e_rotate_grace_window(tmp_path, monkeypatch):
    """Rotate with grace; old token works during the grace window; fails after expiry (S15).

    Pattern:
    - First rotation: auto-generated key (not in keys.json) → creates a new managed key.
    - Second rotation with grace_seconds=30: the first rotation's managed token IS in
      keys.json, so the grace window path is exercised.  The old token gets
      ``expires_at = now + 30s`` (still active) and the new token is created.
    - Assert old token still works immediately (within grace window).
    - Assert old token is rejected AFTER the grace window expires (by patching datetime.now
      in key_manager so active_keys() filters the expired token out).
    """
    import archon_search.key_manager as km_mod

    from fastapi.testclient import TestClient
    from archon_search.key_manager import get_key_file

    grace = 30
    app, initial_api_key = _make_rotate_app(tmp_path, monkeypatch, grace_seconds=grace)

    with TestClient(app) as client:
        # First rotation: promotes the auto-generated key to a managed record in keys.json.
        resp1 = client.post(
            "/keys/rotate",
            json={},
            headers=_auth(initial_api_key),
        )
        assert resp1.status_code == 200, f"First rotate failed: {resp1.status_code} {resp1.text}"
        managed_token = resp1.json()["token"]

        # Second rotation: managed_token IS now in keys.json → grace_seconds=30 applies.
        before_rotate = datetime.now(UTC)
        resp2 = client.post(
            "/keys/rotate",
            json={"grace_seconds": grace},
            headers=_auth(managed_token),
        )
        assert resp2.status_code == 200, f"Second rotate failed: {resp2.status_code} {resp2.text}"
        body2 = resp2.json()
        after_rotate = datetime.now(UTC)

        new_token = body2["token"]
        assert new_token != managed_token, "New token must differ from old managed token"
        assert body2["status"] == "active"

        # Grace window assertions: old_key_expires_at must be in [before+grace, after+grace].
        assert body2.get("old_key_id") is not None, (
            "old_key_id must be present — managed_token was in keys.json"
        )
        assert body2["old_key_expires_at"] is not None, (
            "old_key_expires_at must be set when grace_seconds > 0 (S15)"
        )
        assert body2["old_key_status"] == "active", (
            "old key status must be 'active' during grace window (not 'revoked')"
        )

        expires_raw = body2["old_key_expires_at"]
        expires_dt = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
        assert before_rotate + timedelta(seconds=grace) <= expires_dt <= after_rotate + timedelta(seconds=grace), (
            f"old_key_expires_at={expires_dt} must be within [{before_rotate + timedelta(seconds=grace)}, "
            f"{after_rotate + timedelta(seconds=grace)}]"
        )

        # Old token must still be accepted during grace window (expires_at is in the future).
        grace_resp = client.get("/keys", headers=_auth(managed_token))
        assert grace_resp.status_code == 200, (
            f"Old token should be accepted during grace window; "
            f"got {grace_resp.status_code}: {grace_resp.text}"
        )

        # New token must authenticate successfully.
        new_resp = client.get("/keys", headers=_auth(new_token))
        assert new_resp.status_code == 200, (
            f"New rotation token should authenticate; got {new_resp.status_code}: {new_resp.text}"
        )

        # .search.env must contain the new token.
        key_file = get_key_file()
        assert f"ARCHON_SEARCH_API_KEY={new_token}" in key_file.read_text(), (
            ".search.env must have been updated with the new token after rotation"
        )

        # Verify expiry rejection: advance time past expires_at by patching datetime.now in
        # key_manager.  This causes active_keys() to filter out managed_token (expires_at <= now),
        # so it gets no match in the managed-key path, no match in TOML, and no match in the
        # legacy fallback (app.state.api_key was already updated to new_token by the rotation).
        # The middleware's legacy-fallback guard (middleware_auth.py:85-96) is not entered here
        # because managed_token != current_api_key — the km_mod patch is the load-bearing one.
        post_grace_time = expires_dt + timedelta(seconds=1)

        mock_km_dt = MagicMock(spec=km_mod.datetime)
        mock_km_dt.now.return_value = post_grace_time
        mock_km_dt.fromisoformat = datetime.fromisoformat
        mock_km_dt.UTC = UTC

        with patch.object(km_mod, "datetime", mock_km_dt):
            post_grace_resp = client.get("/keys", headers=_auth(managed_token))

        assert post_grace_resp.status_code == 401, (
            f"Old token must return 401 after grace window expires (S15); "
            f"got {post_grace_resp.status_code}: {post_grace_resp.text}"
        )
        assert post_grace_resp.headers.get("WWW-Authenticate") == "Bearer", (
            "401 response must include WWW-Authenticate: Bearer header"
        )


# ---------------------------------------------------------------------------
# T-3 test 2: TOML token and managed token both accepted simultaneously (S7, S8)
# ---------------------------------------------------------------------------


def test_e2e_toml_and_managed_coexist(tmp_path, monkeypatch):
    """TOML token and managed token both accepted against the full app (S7, S8).

    Verifies:
    - A token from the TOML [namespaces] map authenticates successfully (S7).
    - A managed key issued via POST /keys authenticates successfully (S8).
    - Both tokens are accepted simultaneously — coexistence is proven.
    - Namespace isolation: managed token's namespace is correctly resolved —
      a collection under 'managed-coexist-ns' is visible to the managed token
      but NOT to the TOML token (which has namespace 'toml-ns').
    """
    toml_raw_token = secrets.token_hex(32)
    toml_namespace = "toml-ns"

    with make_real_app(
        tmp_path,
        monkeypatch,
        namespaces={toml_raw_token: toml_namespace},
    ) as (client, cfg, default_api_key):
        # Step 1 — TOML token authenticates (S7).
        toml_resp = client.get("/collections", headers=_auth(toml_raw_token))
        assert toml_resp.status_code == 200, (
            f"TOML namespace token should authenticate GET /collections (S7); "
            f"got {toml_resp.status_code}: {toml_resp.text}"
        )

        # Step 2 — Create a managed key via POST /keys (S8).
        create_resp = client.post(
            "/keys",
            headers=_auth(default_api_key),
            json={"namespace": "managed-coexist-ns", "label": "t3-coexist"},
        )
        assert create_resp.status_code == 201, (
            f"POST /keys should return 201; got {create_resp.status_code}: {create_resp.text}"
        )
        managed_token = create_resp.json()["token"]

        # Step 3 — Managed token authenticates (S8).
        managed_resp = client.get("/collections", headers=_auth(managed_token))
        assert managed_resp.status_code == 200, (
            f"Managed key should authenticate GET /collections (S8); "
            f"got {managed_resp.status_code}: {managed_resp.text}"
        )

        # Step 4 — Both still authenticate simultaneously (coexistence).
        # Re-check TOML token after a managed key was created — must still work.
        toml_resp2 = client.get("/collections", headers=_auth(toml_raw_token))
        assert toml_resp2.status_code == 200, (
            f"TOML token must still authenticate after a managed key is created (coexistence); "
            f"got {toml_resp2.status_code}: {toml_resp2.text}"
        )

        # GET /keys should include the TOML synthetic entry (id=null) and the managed key.
        list_resp = client.get("/keys?status=all", headers=_auth(default_api_key))
        assert list_resp.status_code == 200, list_resp.text
        all_keys = list_resp.json()["keys"]

        # At least one TOML synthetic entry with id=null must be present.
        toml_entries = [k for k in all_keys if k["id"] is None]
        assert len(toml_entries) >= 1, (
            f"GET /keys must include at least one TOML synthetic entry with id=null; "
            f"got keys: {all_keys}"
        )
        toml_entry_namespaces = {k["namespace"] for k in toml_entries}
        assert toml_namespace in toml_entry_namespaces, (
            f"TOML namespace '{toml_namespace}' must appear in synthetic entries; "
            f"got namespaces: {toml_entry_namespaces}"
        )

        # At least one managed entry with a real UUID id must be present.
        managed_entries = [k for k in all_keys if k["id"] is not None]
        assert len(managed_entries) >= 1, (
            f"GET /keys must include at least one managed entry with a real id; "
            f"got keys: {all_keys}"
        )

        # Step 5 — Namespace isolation: register a collection under 'managed-coexist-ns'
        # so we can prove the managed token's namespace is resolved correctly.
        store = client.app.state.search_store
        asyncio.run(
            _inject_chunks_ns(store, _COL_COEXIST, cfg.embedding_model, namespace="managed-coexist-ns")
        )

        # Managed token (namespace='managed-coexist-ns') must find the collection.
        managed_search = client.post(
            "/search",
            headers=_auth(managed_token),
            json={"collection": _COL_COEXIST, "query": "t3 coexist", "top_k": 5},
        )
        assert managed_search.status_code == 200, (
            f"Managed token should see 'managed-coexist-ns' collection; "
            f"got {managed_search.status_code}: {managed_search.text}"
        )

        # TOML token (namespace='toml-ns') must NOT see the 'managed-coexist-ns' collection.
        toml_isolation = client.post(
            "/search",
            headers=_auth(toml_raw_token),
            json={"collection": _COL_COEXIST, "query": "t3 coexist", "top_k": 5},
        )
        assert toml_isolation.status_code == 404, (
            f"TOML token (namespace='toml-ns') should not see 'managed-coexist-ns' collection; "
            f"got {toml_isolation.status_code}: {toml_isolation.text}"
        )


# ---------------------------------------------------------------------------
# T-3 test 3: corrupted keys.json — app starts; default key still returns 200 (S17)
# ---------------------------------------------------------------------------


def test_e2e_corruption_degradation(tmp_path, monkeypatch, caplog):
    """Write corrupt keys.json; start app via TestClient; default key still returns 200 (S17).

    Verifies:
    - Server starts without crashing when keys.json is unparseable.
    - The default env/file key still authenticates successfully.
    - The managed key store is empty (graceful fallback to empty).
    - An ERROR log is emitted describing the JSON parse failure (S17).
    """
    from archon_search.config import SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.key_manager import ENV_VAR
    from archon_search.server.app import create_app
    from fastapi.testclient import TestClient

    api_key = secrets.token_hex(32)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(ENV_VAR, api_key)

    # Write corrupted keys.json BEFORE starting the app.
    keys_json_path: Path = tmp_path / "keys.json"
    keys_json_path.write_bytes(b"{ this is certainly not valid json !!! }")

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )

    # App must start without raising despite corrupted keys.json.
    app = create_app(cfg, job_store, scheduler=scheduler)

    with caplog.at_level(logging.ERROR, logger="archon_search.key_manager"):
        with TestClient(app) as client:
            # Default env-var key must still work (S17 requirement).
            auth_resp = client.get("/collections", headers=_auth(api_key))
            assert auth_resp.status_code == 200, (
                f"Default key must work after corrupted keys.json startup (S17); "
                f"got {auth_resp.status_code}: {auth_resp.text}"
            )

            # Managed key store falls back to empty — GET /keys returns an empty list.
            list_resp = client.get("/keys", headers=_auth(api_key))
            assert list_resp.status_code == 200, list_resp.text
            body = list_resp.json()
            # The managed keys list may be empty or contain only TOML synthetics;
            # no managed keys from the corrupted file should appear.
            assert isinstance(body["keys"], list), "GET /keys must return a list even after corruption"

            managed_from_file = [k for k in body["keys"] if k["id"] is not None]
            assert managed_from_file == [], (
                f"No managed keys from the corrupted file should be loaded; "
                f"got managed entries: {managed_from_file}"
            )

    # S17: ERROR log must be emitted describing the JSON parse failure.
    error_messages = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("not valid JSON" in msg or "corrupted key store" in msg for msg in error_messages), (
        f"Expected ERROR log about corrupted keys.json (S17); "
        f"got error messages: {error_messages}"
    )


# ---------------------------------------------------------------------------
# T-3 test 4: raw token absent from keys.json (regression — S20)
# ---------------------------------------------------------------------------


def test_e2e_token_not_stored_in_keys_json(tmp_path, monkeypatch):
    """Create a managed key; read keys.json directly; assert raw token absent (S20).

    Proves that only the SHA-256 hash is stored on disk — the raw bearer token
    is never persisted in keys.json (the plaintext is only returned once in the
    POST /keys response).
    """
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, default_api_key):
        create_resp = client.post(
            "/keys",
            headers=_auth(default_api_key),
            json={"namespace": "s20-test-ns", "label": "t3-s20-regression"},
        )
        assert create_resp.status_code == 201, (
            f"POST /keys failed: {create_resp.status_code} {create_resp.text}"
        )
        raw_token = create_resp.json()["token"]

        # Read keys.json from disk directly (bypassing the API).
        keys_json_path: Path = tmp_path / "keys.json"
        assert keys_json_path.exists(), (
            "keys.json must exist after POST /keys"
        )
        keys_json_bytes = keys_json_path.read_bytes()
        keys_json_text = keys_json_bytes.decode("utf-8")

        # Regression: raw token must NOT appear anywhere in keys.json.
        assert raw_token not in keys_json_text, (
            f"Raw token found in keys.json — only the SHA-256 hash should be stored (S20). "
            f"Found token in file content (first 200 chars): {keys_json_text[:200]}"
        )

        # Sanity check: the file must be valid JSON with at least one record.
        records = json.loads(keys_json_text)
        assert isinstance(records, list), "keys.json must contain a JSON array"
        managed_records = [r for r in records if r.get("id") is not None]
        assert len(managed_records) >= 1, (
            "At least one managed record must be present in keys.json after POST /keys"
        )

        # Each record must have a token_hash field (SHA-256 hex, 64 chars).
        for record in managed_records:
            assert "token_hash" in record, f"Record missing token_hash: {record}"
            th = record["token_hash"]
            assert len(th) == 64 and all(c in "0123456789abcdef" for c in th), (
                f"token_hash must be a 64-char lowercase hex string; got: {th!r}"
            )
            assert "token" not in record, (
                f"Record must not have a 'token' field (raw token must never be stored); "
                f"got record keys: {list(record.keys())}"
            )
