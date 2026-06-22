"""Tests for BE-4: POST /keys endpoint + KeyCreateRequest/KeyCreateResponse schemas.

Covers:
- S1: POST /keys creates key with id, token, namespace, created_at, status=active
- S13: invalid namespace returns 422
- S26: POST /keys with past expires_at returns 201; auth with that token returns 401
- KeyCreateRequest schema validation
- KeyCreateResponse schema (includes token and status='active')
- Multiple keys for same namespace allowed
- POST /keys requires auth (401 without Bearer)
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_app(tmp_path, monkeypatch, *, api_key: str | None = None):
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


def test_key_create_request_schema():
    """KeyCreateRequest validates: namespace required, label optional, expires_at optional."""
    from archon_search.server.schemas import KeyCreateRequest

    # Minimal required field
    req = KeyCreateRequest(namespace="my-ns")
    assert req.namespace == "my-ns"
    assert req.label is None
    assert req.expires_at is None

    # All fields
    dt = datetime(2030, 1, 1, tzinfo=UTC)
    req2 = KeyCreateRequest(namespace="my-ns", label="my-label", expires_at=dt)
    assert req2.label == "my-label"
    assert req2.expires_at == dt

    # namespace is required
    with pytest.raises(ValidationError):
        KeyCreateRequest()


def test_key_create_response_has_token():
    """KeyCreateResponse includes token field and status='active'."""
    from archon_search.server.schemas import KeyCreateResponse

    dt = datetime(2026, 1, 1, tzinfo=UTC)
    resp = KeyCreateResponse(
        id="some-uuid",
        token="deadbeef" * 8,
        namespace="my-ns",
        created_at=dt,
        status="active",
    )
    assert resp.token == "deadbeef" * 8
    assert resp.status == "active"
    assert resp.id == "some-uuid"


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


def test_post_keys_creates_key(tmp_path, monkeypatch):
    """POST /keys returns 201 with id, token, namespace, created_at, status=active (S1)."""
    from fastapi.testclient import TestClient

    app, api_key = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        resp = client.post(
            "/keys",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"namespace": "my-ns"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "id" in body
        assert "token" in body
        assert body["namespace"] == "my-ns"
        assert "created_at" in body
        assert body["status"] == "active"
        assert body["expires_at"] is None


def test_post_keys_with_expires_at_echoed(tmp_path, monkeypatch):
    """POST /keys with expires_at returns 201; expires_at echoed and persisted (C2-TEST-2)."""
    import json as _json
    from fastapi.testclient import TestClient

    app, api_key = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        resp = client.post(
            "/keys",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"namespace": "my-ns", "expires_at": "2030-01-01T00:00:00Z"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        # expires_at should be echoed in the response
        assert body["expires_at"] is not None
        resp_dt = datetime.fromisoformat(body["expires_at"])
        assert resp_dt.year == 2030

        # Persistence check: the stored record should carry the same expires_at.
        raw_records = _json.loads((tmp_path / "keys.json").read_text())
        stored = next(r for r in raw_records if r.get("id") == body["id"])
        stored_dt = datetime.fromisoformat(stored["expires_at"])
        assert stored_dt.year == 2030


def test_post_keys_with_label_echoed(tmp_path, monkeypatch):
    """POST /keys with label returns 201; label echoed and persisted (C1-TEST-1, C2-TEST-1)."""
    import json as _json
    from fastapi.testclient import TestClient

    app, api_key = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        resp = client.post(
            "/keys",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"namespace": "my-ns", "label": "my-label"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        # Response echoes the label
        assert body["label"] == "my-label"
        # Persistence check: the stored record should carry the label
        raw_records = _json.loads((tmp_path / "keys.json").read_text())
        stored = next(r for r in raw_records if r.get("id") == body["id"])
        assert stored.get("label") == "my-label"


def test_post_keys_same_namespace_multiple_allowed(tmp_path, monkeypatch):
    """Creating two keys with the same namespace is allowed; both should appear in keys.json."""
    from fastapi.testclient import TestClient

    app, api_key = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        resp1 = client.post(
            "/keys",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"namespace": "shared-ns"},
        )
        resp2 = client.post(
            "/keys",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"namespace": "shared-ns"},
        )
        assert resp1.status_code == 201, resp1.text
        assert resp2.status_code == 201, resp2.text

        # Both IDs must be distinct
        id1 = resp1.json()["id"]
        id2 = resp2.json()["id"]
        assert id1 != id2

        # Both records must be in the key store (read file directly to avoid
        # asyncio.run() dual-event-loop fragility inside TestClient context).
        import json as _json
        keys_file = tmp_path / "keys.json"
        raw_records = _json.loads(keys_file.read_text())
        managed = [r for r in raw_records if r.get("id") is not None and r.get("namespace") == "shared-ns"]
        assert len(managed) == 2


def test_post_keys_invalid_namespace_422(tmp_path, monkeypatch):
    """POST /keys with an invalid namespace returns 422 (S13)."""
    from fastapi.testclient import TestClient

    app, api_key = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        # deny-all is reserved; also test empty and invalid-char namespaces
        for bad_ns in ["deny-all", "", "!invalid", "a" * 65]:
            resp = client.post(
                "/keys",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"namespace": bad_ns},
            )
            assert resp.status_code == 422, (
                f"Expected 422 for namespace {bad_ns!r}, got {resp.status_code}: {resp.text}"
            )
            body = resp.json()
            assert "detail" in body and body["detail"], (
                f"Expected non-empty detail for namespace {bad_ns!r}, got {body}"
            )


def test_post_keys_requires_auth(tmp_path, monkeypatch):
    """Unauthenticated POST /keys returns 401."""
    from fastapi.testclient import TestClient

    app, api_key = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        resp = client.post(
            "/keys",
            json={"namespace": "my-ns"},
        )
        assert resp.status_code == 401, resp.text


def test_post_keys_with_past_expires_at_creates_expired_key(tmp_path, monkeypatch):
    """POST /keys with past expires_at returns 201; auth with that token returns 401 (S26)."""
    from fastapi.testclient import TestClient

    app, api_key = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        past = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        resp = client.post(
            "/keys",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"namespace": "my-ns", "expires_at": past},
        )
        assert resp.status_code == 201, resp.text
        expired_token = resp.json()["token"]

        # Auth with the expired token must fail
        auth_resp = client.get(
            "/collections",
            headers={"Authorization": f"Bearer {expired_token}"},
        )
        assert auth_resp.status_code == 401, (
            f"Expected 401 for immediately-expired key, got {auth_resp.status_code}"
        )
