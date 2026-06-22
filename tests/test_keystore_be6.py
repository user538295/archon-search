"""Tests for BE-6: GET /keys + DELETE /keys/{id} endpoints + schemas.

Covers:
- S3: GET /keys returns active-only by default; hidden_revoked_count hint when revoked exist
- S4: DELETE /keys/{id} revokes a key
- S5: GET /keys?namespace=ns filters by namespace
- S14: DELETE /keys/{unknown-id} returns 404
- KeyResponse schema (no token field)
- KeyListResponse schema (hidden_revoked_count)
- TOML synthetic keys appear in GET /keys with id=null
- Idempotent DELETE: already-revoked key returns 200
- Literal string 'null' path param returns 404 with TOML hint message
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_app(tmp_path, monkeypatch, *, api_key: str | None = None, namespaces: dict[str, str] | None = None):
    """Build a minimal FastAPI TestClient using the real create_app."""
    from archon_search.config import SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    if api_key is None:
        api_key = secrets.token_hex(32)

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", api_key)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")

    if namespaces is not None:
        cfg.namespaces = namespaces

    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )
    app = create_app(cfg, job_store, scheduler=scheduler)
    return app, api_key


# ---------------------------------------------------------------------------
# Schema unit tests
# ---------------------------------------------------------------------------


def test_key_response_no_token_field():
    """KeyResponse has no token field."""
    from archon_search.server.schemas import KeyResponse

    dt = datetime(2026, 1, 1, tzinfo=UTC)
    # Valid construction
    resp = KeyResponse(
        id="some-uuid",
        namespace="ns",
        created_at=dt,
        status="active",
    )
    assert resp.id == "some-uuid"
    assert resp.status == "active"

    # No token attribute
    assert not hasattr(resp, "token"), "KeyResponse must not have a token field"

    # id can be None (for TOML synthetic keys)
    resp_null_id = KeyResponse(
        id=None,
        namespace="toml-ns",
        created_at=dt,
        status="active",
    )
    assert resp_null_id.id is None


def test_key_list_response_hidden_count():
    """KeyListResponse.hidden_revoked_count is correct."""
    from archon_search.server.schemas import KeyListResponse, KeyResponse

    dt = datetime(2026, 1, 1, tzinfo=UTC)
    active_key = KeyResponse(id="id1", namespace="ns", created_at=dt, status="active")
    resp = KeyListResponse(keys=[active_key], hidden_revoked_count=3)
    assert resp.hidden_revoked_count == 3
    assert len(resp.keys) == 1


# ---------------------------------------------------------------------------
# Integration tests: GET /keys
# ---------------------------------------------------------------------------


def test_get_keys_active_only_default(tmp_path, monkeypatch):
    """GET /keys returns active keys only by default; hidden_revoked_count > 0 when revoked exist (S3)."""
    from fastapi.testclient import TestClient

    app, api_key = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {api_key}"}

        # Create two keys
        r1 = client.post("/keys", headers=headers, json={"namespace": "ns"})
        r2 = client.post("/keys", headers=headers, json={"namespace": "ns"})
        assert r1.status_code == 201
        assert r2.status_code == 201
        id1 = r1.json()["id"]

        # Revoke one key
        del_resp = client.delete(f"/keys/{id1}", headers=headers)
        assert del_resp.status_code == 200

        # GET /keys (default = active only)
        resp = client.get("/keys", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "keys" in body
        assert "hidden_revoked_count" in body

        # Only the non-revoked managed key should appear (plus possibly TOML synthetic if any)
        # Filter by namespace to isolate managed keys
        active_ids = [k["id"] for k in body["keys"] if k.get("id") is not None]
        assert id1 not in active_ids, "Revoked key must not appear in active-only response"
        assert body["hidden_revoked_count"] == 1, (
            f"Expected hidden_revoked_count=1, got {body['hidden_revoked_count']}"
        )


def test_get_keys_status_all(tmp_path, monkeypatch):
    """GET /keys?status=all returns all keys including revoked."""
    from fastapi.testclient import TestClient

    app, api_key = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {api_key}"}

        r1 = client.post("/keys", headers=headers, json={"namespace": "ns"})
        r2 = client.post("/keys", headers=headers, json={"namespace": "ns"})
        assert r1.status_code == 201
        assert r2.status_code == 201
        id1 = r1.json()["id"]

        # Revoke one
        client.delete(f"/keys/{id1}", headers=headers)

        resp = client.get("/keys?status=all", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        managed_ids = [k["id"] for k in body["keys"] if k.get("id") is not None]
        assert id1 in managed_ids, "Revoked key must appear in status=all response"
        assert body["hidden_revoked_count"] == 0, (
            "hidden_revoked_count must be 0 for status=all"
        )


def test_get_keys_status_revoked(tmp_path, monkeypatch):
    """GET /keys?status=revoked returns only revoked keys."""
    from fastapi.testclient import TestClient

    app, api_key = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {api_key}"}

        r1 = client.post("/keys", headers=headers, json={"namespace": "ns"})
        r2 = client.post("/keys", headers=headers, json={"namespace": "ns"})
        assert r1.status_code == 201
        assert r2.status_code == 201
        id1 = r1.json()["id"]
        id2 = r2.json()["id"]

        # Revoke key 1 only
        client.delete(f"/keys/{id1}", headers=headers)

        resp = client.get("/keys?status=revoked", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        managed_ids = [k["id"] for k in body["keys"] if k.get("id") is not None]
        assert id1 in managed_ids, "Revoked key must appear in status=revoked response"
        assert id2 not in managed_ids, "Active key must not appear in status=revoked response"
        assert body["hidden_revoked_count"] == 0, (
            "hidden_revoked_count must be 0 for status=revoked"
        )


def test_get_keys_includes_toml_synthetic_with_null_id(tmp_path, monkeypatch):
    """GET /keys response includes TOML synthetic entry with id=null."""
    from fastapi.testclient import TestClient

    toml_token = secrets.token_hex(32)  # 64 hex chars — valid token
    app, api_key = _make_app(
        tmp_path, monkeypatch,
        namespaces={toml_token: "toml-ns"},
    )
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {api_key}"}

        resp = client.get("/keys", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        null_id_keys = [k for k in body["keys"] if k["id"] is None]
        assert len(null_id_keys) >= 1, (
            f"Expected at least one TOML synthetic key with id=null, got: {body['keys']}"
        )
        toml_namespaces = {k["namespace"] for k in null_id_keys}
        assert "toml-ns" in toml_namespaces


def test_get_keys_filter_namespace(tmp_path, monkeypatch):
    """GET /keys?namespace=ns returns only keys for that namespace (S5)."""
    from fastapi.testclient import TestClient

    app, api_key = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {api_key}"}

        # Create keys in two different namespaces
        r1 = client.post("/keys", headers=headers, json={"namespace": "ns-a"})
        r2 = client.post("/keys", headers=headers, json={"namespace": "ns-b"})
        assert r1.status_code == 201
        assert r2.status_code == 201

        resp = client.get("/keys?namespace=ns-a", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        namespaces_returned = {k["namespace"] for k in body["keys"]}
        assert "ns-a" in namespaces_returned
        assert "ns-b" not in namespaces_returned


# ---------------------------------------------------------------------------
# Integration tests: DELETE /keys/{id}
# ---------------------------------------------------------------------------


def test_delete_keys_id_revokes(tmp_path, monkeypatch):
    """DELETE /keys/{id} returns 200; key is now revoked (S4)."""
    from fastapi.testclient import TestClient

    app, api_key = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {api_key}"}

        r = client.post("/keys", headers=headers, json={"namespace": "ns"})
        assert r.status_code == 201
        key_id = r.json()["id"]
        token = r.json()["token"]

        del_resp = client.delete(f"/keys/{key_id}", headers=headers)
        assert del_resp.status_code == 200, del_resp.text
        body = del_resp.json()
        assert body["id"] == key_id
        assert body["status"] == "revoked"

        # Auth with the revoked token must now return 401
        auth_resp = client.get("/collections", headers={"Authorization": f"Bearer {token}"})
        assert auth_resp.status_code == 401


def test_delete_keys_nonexistent_404(tmp_path, monkeypatch):
    """DELETE /keys/{unknown-id} returns 404 (S14)."""
    from fastapi.testclient import TestClient

    app, api_key = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {api_key}"}

        resp = client.delete("/keys/no-such-id-ever", headers=headers)
        assert resp.status_code == 404, resp.text


def test_delete_keys_already_revoked_200(tmp_path, monkeypatch):
    """DELETE /keys/{id} on an already-revoked key returns 200 (idempotent)."""
    from fastapi.testclient import TestClient

    app, api_key = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {api_key}"}

        r = client.post("/keys", headers=headers, json={"namespace": "ns"})
        assert r.status_code == 201
        key_id = r.json()["id"]

        # First revoke
        resp1 = client.delete(f"/keys/{key_id}", headers=headers)
        assert resp1.status_code == 200

        # Second revoke — must be idempotent (200, not 409 or 404)
        resp2 = client.delete(f"/keys/{key_id}", headers=headers)
        assert resp2.status_code == 200, (
            f"Expected 200 for already-revoked key, got {resp2.status_code}: {resp2.text}"
        )
        body2 = resp2.json()
        assert body2["status"] == "revoked"


def test_delete_keys_null_string_404(tmp_path, monkeypatch):
    """DELETE /keys/null (literal string 'null') returns 404 with TOML hint message.

    TOML synthetic keys have id=None (Python None); the string 'null' is not a
    valid managed key ID. The 404 response body must include a message explaining
    that TOML-managed keys cannot be targeted by this endpoint.
    """
    from fastapi.testclient import TestClient

    toml_token = secrets.token_hex(32)
    app, api_key = _make_app(
        tmp_path, monkeypatch,
        namespaces={toml_token: "toml-ns"},
    )
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {api_key}"}

        resp = client.delete("/keys/null", headers=headers)
        assert resp.status_code == 404, resp.text
        body = resp.json()
        # The error message should hint at TOML management
        assert "archon-search.toml" in body.get("detail", ""), (
            f"Expected TOML hint in 404 detail for /keys/null, got: {body}"
        )


def test_get_keys_requires_auth(tmp_path, monkeypatch):
    """GET /keys without Bearer token returns 401."""
    from fastapi.testclient import TestClient

    app, api_key = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        resp = client.get("/keys")
        assert resp.status_code == 401, resp.text


def test_delete_keys_requires_auth(tmp_path, monkeypatch):
    """DELETE /keys/{id} without Bearer token returns 401."""
    from fastapi.testclient import TestClient

    app, api_key = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        resp = client.delete("/keys/some-id")
        assert resp.status_code == 401, resp.text


def test_get_keys_hidden_revoked_count_scoped_to_namespace(tmp_path, monkeypatch):
    """hidden_revoked_count is scoped to the namespace filter view.

    Revoked keys in ns-b must NOT inflate the count when filtering by ns-a.
    """
    from fastapi.testclient import TestClient

    app, api_key = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {api_key}"}

        # Create one key in ns-a (will be revoked)
        r_a = client.post("/keys", headers=headers, json={"namespace": "ns-a"})
        assert r_a.status_code == 201
        id_a = r_a.json()["id"]

        # Create two keys in ns-b and revoke both
        r_b1 = client.post("/keys", headers=headers, json={"namespace": "ns-b"})
        r_b2 = client.post("/keys", headers=headers, json={"namespace": "ns-b"})
        assert r_b1.status_code == 201
        assert r_b2.status_code == 201
        client.delete(f"/keys/{r_b1.json()['id']}", headers=headers)
        client.delete(f"/keys/{r_b2.json()['id']}", headers=headers)

        # Revoke ns-a key too
        client.delete(f"/keys/{id_a}", headers=headers)

        # GET /keys?namespace=ns-a: hidden_revoked_count should be 1 (ns-a only)
        resp = client.get("/keys?namespace=ns-a", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["hidden_revoked_count"] == 1, (
            f"Expected hidden_revoked_count=1 (ns-a scoped), got {body['hidden_revoked_count']}. "
            "The count must not include revoked keys from ns-b."
        )


def test_get_keys_namespace_and_status_combined(tmp_path, monkeypatch):
    """GET /keys?namespace=ns-a&status=revoked returns only revoked keys in ns-a."""
    from fastapi.testclient import TestClient

    app, api_key = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {api_key}"}

        # Create and revoke a key in ns-a
        r_a = client.post("/keys", headers=headers, json={"namespace": "ns-a"})
        assert r_a.status_code == 201
        id_a = r_a.json()["id"]
        client.delete(f"/keys/{id_a}", headers=headers)

        # Create a key in ns-b (active — should not appear)
        r_b = client.post("/keys", headers=headers, json={"namespace": "ns-b"})
        assert r_b.status_code == 201

        resp = client.get("/keys?namespace=ns-a&status=revoked", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert len(body["keys"]) == 1
        assert body["keys"][0]["id"] == id_a
        assert body["keys"][0]["status"] == "revoked"
        assert body["hidden_revoked_count"] == 0


def test_get_keys_label_and_expires_at_in_response(tmp_path, monkeypatch):
    """GET /keys response includes label and expires_at fields from the key record."""
    from fastapi.testclient import TestClient

    app, api_key = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {api_key}"}

        resp = client.post(
            "/keys",
            headers=headers,
            json={"namespace": "ns", "label": "my-label", "expires_at": "2030-06-01T00:00:00Z"},
        )
        assert resp.status_code == 201
        key_id = resp.json()["id"]

        list_resp = client.get("/keys", headers=headers)
        assert list_resp.status_code == 200, list_resp.text
        body = list_resp.json()

        matching = [k for k in body["keys"] if k.get("id") == key_id]
        assert len(matching) == 1
        k = matching[0]
        assert k["label"] == "my-label", f"Expected label='my-label', got {k['label']}"
        assert k["expires_at"] is not None, "expires_at must appear in GET /keys response"
        from datetime import datetime, UTC
        expires_dt = datetime.fromisoformat(k["expires_at"])
        assert expires_dt.year == 2030


def test_get_keys_status_all_includes_both_active_and_revoked(tmp_path, monkeypatch):
    """GET /keys?status=all returns both active and revoked keys (both IDs present)."""
    from fastapi.testclient import TestClient

    app, api_key = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {api_key}"}

        r1 = client.post("/keys", headers=headers, json={"namespace": "ns"})
        r2 = client.post("/keys", headers=headers, json={"namespace": "ns"})
        assert r1.status_code == 201
        assert r2.status_code == 201
        id1 = r1.json()["id"]
        id2 = r2.json()["id"]

        # Revoke key 1
        client.delete(f"/keys/{id1}", headers=headers)

        resp = client.get("/keys?status=all", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        managed_ids = [k["id"] for k in body["keys"] if k.get("id") is not None]
        assert id1 in managed_ids, "Revoked key id1 must appear in status=all"
        assert id2 in managed_ids, "Active key id2 must appear in status=all"
        assert body["hidden_revoked_count"] == 0
