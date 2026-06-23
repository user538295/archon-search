"""Tests for BE-8: POST /keys/rotate endpoint + KeyRotateRequest/KeyRotateResponse schemas.

Covers:
- S6: POST /keys/rotate returns 200 with new token; old key revoked; .search.env updated
- S15: POST /keys/rotate with grace_seconds → old key gets expires_at set
- S17: Corrupted keys.json → graceful degradation (server starts, default key still works)
- S23: POST /keys/rotate returns 409 when ARCHON_SEARCH_API_KEY env var is set
- S24: MCP api_key not hot-reloaded on rotation (documented limitation)
- KeyRotateRequest/KeyRotateResponse schema tests
"""
from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

ENV_VAR = "ARCHON_SEARCH_API_KEY"

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_app(tmp_path, monkeypatch, *, api_key: str | None = None, grace_seconds: int | None = None):
    """Build a minimal FastAPI TestClient using the real create_app."""
    from archon_search.config import SearchConfig, AuthConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    if api_key is None:
        api_key = secrets.token_hex(32)

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(ENV_VAR, api_key)

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
    return app, api_key


# ---------------------------------------------------------------------------
# Schema unit tests
# ---------------------------------------------------------------------------


def test_rotate_response_has_token():
    """KeyRotateResponse includes token field for new key."""
    from archon_search.server.schemas import KeyRotateResponse

    resp = KeyRotateResponse(
        new_key_id="some-uuid",
        token="new-raw-token",
        status="active",
    )
    assert resp.new_key_id == "some-uuid"
    assert resp.token == "new-raw-token"
    assert resp.status == "active"
    assert resp.old_key_id is None
    assert resp.old_key_expires_at is None
    assert resp.old_key_status is None


def test_rotate_response_with_old_key_fields():
    """KeyRotateResponse optional old_key fields are present when set."""
    from archon_search.server.schemas import KeyRotateResponse

    expires = datetime(2030, 6, 1, tzinfo=UTC)
    resp = KeyRotateResponse(
        new_key_id="new-uuid",
        token="new-raw-token",
        status="active",
        old_key_id="old-uuid",
        old_key_expires_at=expires,
        old_key_status="active",
    )
    assert resp.old_key_id == "old-uuid"
    assert resp.old_key_expires_at == expires
    assert resp.old_key_status == "active"


def test_rotate_request_grace_seconds_optional():
    """KeyRotateRequest grace_seconds is optional; defaults to None."""
    from archon_search.server.schemas import KeyRotateRequest

    req = KeyRotateRequest()
    assert req.grace_seconds is None


def test_rotate_request_grace_seconds_set():
    """KeyRotateRequest grace_seconds can be set to an integer."""
    from archon_search.server.schemas import KeyRotateRequest

    req = KeyRotateRequest(grace_seconds=60)
    assert req.grace_seconds == 60


def test_rotate_request_grace_seconds_zero():
    """KeyRotateRequest grace_seconds=0 is valid (immediate revocation)."""
    from archon_search.server.schemas import KeyRotateRequest

    req = KeyRotateRequest(grace_seconds=0)
    assert req.grace_seconds == 0


def test_rotate_request_grace_seconds_negative_rejected():
    """KeyRotateRequest grace_seconds < 0 is invalid → ValidationError (ge=0 constraint)."""
    from archon_search.server.schemas import KeyRotateRequest

    with pytest.raises(ValidationError):
        KeyRotateRequest(grace_seconds=-1)


def test_post_keys_rotate_negative_grace_returns_422(tmp_path, monkeypatch):
    """POST /keys/rotate with negative grace_seconds → 422, not 500."""
    from fastapi.testclient import TestClient

    api_key = secrets.token_hex(32)
    app, _ = _make_app(tmp_path, monkeypatch, api_key=api_key)

    with TestClient(app) as client:
        resp = client.post(
            "/keys/rotate",
            json={"grace_seconds": -1},
            headers={"Authorization": f"Bearer {api_key}"},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Unit test: S23 — 409 when ARCHON_SEARCH_API_KEY env var is set
# ---------------------------------------------------------------------------


def test_post_keys_rotate_env_var_set_409(tmp_path, monkeypatch):
    """POST /keys/rotate when ARCHON_SEARCH_API_KEY env var is set returns 409 (S23)."""
    from fastapi.testclient import TestClient

    api_key = secrets.token_hex(32)
    # _make_app already sets ARCHON_SEARCH_API_KEY — this is the trigger for S23
    app, api_key = _make_app(tmp_path, monkeypatch, api_key=api_key)

    with TestClient(app) as client:
        resp = client.post(
            "/keys/rotate",
            json={},
            headers={"Authorization": f"Bearer {api_key}"},
        )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail == (
        "Cannot rotate: ARCHON_SEARCH_API_KEY env var is set; "
        "unset it first and restart the server to use managed key rotation."
    )


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_post_keys_rotate(tmp_path, monkeypatch):
    """POST /keys/rotate returns 200 with new token; old key revoked; .search.env updated (S6).

    The rotation test uses a managed key (issued via POST /keys) as the initial
    default, so the revocation guard in APIKeyMiddleware can block it via keys.json.
    Auto-generated keys (never in keys.json) cannot be blocked by keys.json revocation —
    that is a documented limitation (S24 class: MCP api_key not hot-reloaded on rotation).
    """
    from fastapi.testclient import TestClient
    from archon_search.key_manager import get_key_file, ENV_VAR as _ENV_VAR

    # Start without ARCHON_SEARCH_API_KEY so app auto-generates a key.
    # The app.state.api_key then holds the auto-generated token.
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(_ENV_VAR, raising=False)

    from archon_search.config import SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )
    app = create_app(cfg, job_store, scheduler=scheduler)
    initial_api_key = app.state.api_key

    with TestClient(app) as client:
        # First rotation: auto-generated key is NOT in keys.json.
        # Rotation still works and .search.env is updated.
        resp = client.post(
            "/keys/rotate",
            json={},
            headers={"Authorization": f"Bearer {initial_api_key}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "token" in body
        assert "new_key_id" in body
        assert body["status"] == "active"
        first_new_token = body["token"]
        assert first_new_token != initial_api_key

        # app.state.api_key must reflect the new token immediately (no restart needed).
        assert app.state.api_key == first_new_token

        # .search.env must have been updated with the new token
        key_file = get_key_file()
        content = key_file.read_text()
        assert f"ARCHON_SEARCH_API_KEY={first_new_token}" in content
        # .search.env must be written with mode 0600 (sensitive file).
        assert (key_file.stat().st_mode & 0o777) == 0o600

        # New token should authenticate successfully (it's a managed key in keys.json)
        search_resp = client.get(
            "/keys",
            headers={"Authorization": f"Bearer {first_new_token}"},
        )
        assert search_resp.status_code == 200

        # Second rotation: this time the current key IS a managed record in keys.json
        # (it was created by the first rotation). So revocation IS tracked.
        resp2 = client.post(
            "/keys/rotate",
            json={},
            headers={"Authorization": f"Bearer {first_new_token}"},
        )
        assert resp2.status_code == 200, resp2.text
        body2 = resp2.json()
        second_new_token = body2["token"]
        assert second_new_token != first_new_token

        # first_new_token is now revoked in keys.json — must return 401
        old_resp = client.get(
            "/keys",
            headers={"Authorization": f"Bearer {first_new_token}"},
        )
        assert old_resp.status_code == 401


@pytest.mark.integration
def test_post_keys_rotate_grace(tmp_path, monkeypatch):
    """POST /keys/rotate with grace_seconds=30 → old managed key expires_at set (S15).

    Uses two rotations: first rotation promotes the auto-generated key to a managed
    key record in keys.json; second rotation with grace_seconds=30 exercises the
    grace window logic on that managed key so the assertions always fire.
    """
    from fastapi.testclient import TestClient
    from archon_search.key_manager import ENV_VAR as _ENV_VAR

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(_ENV_VAR, raising=False)

    from archon_search.config import SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )
    app = create_app(cfg, job_store, scheduler=scheduler)
    initial_api_key = app.state.api_key

    grace = 30

    with TestClient(app) as client:
        # First rotation: auto-generated key is NOT in keys.json — creates a managed key.
        resp1 = client.post(
            "/keys/rotate",
            json={},
            headers={"Authorization": f"Bearer {initial_api_key}"},
        )
        assert resp1.status_code == 200, resp1.text
        managed_token = resp1.json()["token"]

        # Second rotation: managed_token IS in keys.json now — grace window applies.
        before = datetime.now(UTC)
        resp2 = client.post(
            "/keys/rotate",
            json={"grace_seconds": grace},
            headers={"Authorization": f"Bearer {managed_token}"},
        )
        assert resp2.status_code == 200, resp2.text
        body2 = resp2.json()

        # .search.env must be updated with the second rotation's new raw token
        from archon_search.key_manager import get_key_file
        key_file = get_key_file()
        content = key_file.read_text()
        assert f"ARCHON_SEARCH_API_KEY={body2['token']}" in content

        # managed_token was in keys.json → old_key_id must be present
        assert body2.get("old_key_id") is not None, "Expected old_key_id for managed token"
        assert body2["old_key_expires_at"] is not None, "Grace should set expires_at"
        assert body2["old_key_status"] == "active"  # grace → old key still active

        after = datetime.now(UTC)

        # old_key_expires_at should be within range [before+grace, after+grace]
        expires_str = body2["old_key_expires_at"]
        expires_dt = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
        assert before + timedelta(seconds=grace) <= expires_dt <= after + timedelta(seconds=grace)

        # managed_token still works during grace window (expires_at in the future)
        old_resp = client.get(
            "/keys",
            headers={"Authorization": f"Bearer {managed_token}"},
        )
        assert old_resp.status_code == 200


@pytest.mark.integration
def test_post_keys_rotate_body_grace_overrides_config(tmp_path, monkeypatch):
    """Body grace_seconds=0 overrides server config rotate_grace_seconds=300 (body wins).

    Uses two rotations: first rotation generates a managed key from an unmanaged default;
    second rotation uses that managed key as current_token so body=grace_seconds=0 wins
    over config=300, immediately revoking it.
    """
    from fastapi.testclient import TestClient
    from archon_search.key_manager import ENV_VAR as _ENV_VAR

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(_ENV_VAR, raising=False)

    from archon_search.config import SearchConfig, AuthConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.auth = AuthConfig(rotate_grace_seconds=300)  # config default: 5 minutes
    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )
    app = create_app(cfg, job_store, scheduler=scheduler)
    initial_api_key = app.state.api_key

    with TestClient(app) as client:
        # First rotation: get a managed key from an unmanaged default.
        resp1 = client.post(
            "/keys/rotate",
            json={},
            headers={"Authorization": f"Bearer {initial_api_key}"},
        )
        assert resp1.status_code == 200, resp1.text
        managed_token = resp1.json()["token"]

        # Second rotation: managed_token IS in keys.json now.
        # body grace_seconds=0 must override config rotate_grace_seconds=300.
        resp2 = client.post(
            "/keys/rotate",
            json={"grace_seconds": 0},  # body overrides config
            headers={"Authorization": f"Bearer {managed_token}"},
        )
        assert resp2.status_code == 200, resp2.text
        body2 = resp2.json()
        # managed_token was in keys.json → old_key_id is present
        assert body2.get("old_key_id") is not None
        # grace_seconds=0 (body) → immediately revoked, NOT grace-expiring (config says 300)
        assert body2["old_key_status"] == "revoked"
        assert body2["old_key_expires_at"] is None

        # managed_token should now be rejected immediately (revoked, not grace-expiring)
        old_resp = client.get(
            "/keys",
            headers={"Authorization": f"Bearer {managed_token}"},
        )
        assert old_resp.status_code == 401


@pytest.mark.integration
def test_corruption_graceful_degradation(tmp_path, monkeypatch):
    """Write garbage to keys.json; create_app() starts; default key still works (S17)."""
    from fastapi.testclient import TestClient

    api_key = secrets.token_hex(32)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(ENV_VAR, api_key)

    # Write corrupted keys.json before app starts
    keys_json = tmp_path / "keys.json"
    keys_json.write_bytes(b"this is not valid json !!!")

    from archon_search.config import SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )

    # App must start without raising — corrupted keys.json should not crash startup
    app = create_app(cfg, job_store, scheduler=scheduler)

    with TestClient(app) as client:
        # Default api_key must still work (env var path)
        resp = client.get(
            "/keys",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200
        # Managed key store is empty (corrupted) but server is operational
        body = resp.json()
        # TOML synthetic keys may be present; default key has no managed entry
        assert isinstance(body["keys"], list)


# ---------------------------------------------------------------------------
# Auth guard test
# ---------------------------------------------------------------------------


def test_post_keys_rotate_requires_auth(tmp_path, monkeypatch):
    """POST /keys/rotate without Bearer token → 401 with WWW-Authenticate header."""
    from fastapi.testclient import TestClient

    api_key = secrets.token_hex(32)
    app, _ = _make_app(tmp_path, monkeypatch, api_key=api_key)

    with TestClient(app) as client:
        resp = client.post("/keys/rotate", json={})
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers


# ---------------------------------------------------------------------------
# Config grace fallback test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_post_keys_rotate_config_grace_used_when_body_absent(tmp_path, monkeypatch):
    """When grace_seconds absent from body, config.auth.rotate_grace_seconds is used.

    Uses two rotations: first rotation creates a managed key from an unmanaged default;
    second rotation omits grace_seconds so the config value (300 s) governs the outcome —
    old key should be grace-expiring (still active), not immediately revoked.
    """
    from fastapi.testclient import TestClient
    from archon_search.key_manager import ENV_VAR as _ENV_VAR
    from archon_search.config import SearchConfig, AuthConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(_ENV_VAR, raising=False)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.auth = AuthConfig(rotate_grace_seconds=300)  # non-zero config grace
    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )
    app = create_app(cfg, job_store, scheduler=scheduler)
    initial_api_key = app.state.api_key

    with TestClient(app) as client:
        # First rotation: produces a managed key from an unmanaged default.
        resp1 = client.post(
            "/keys/rotate",
            json={},
            headers={"Authorization": f"Bearer {initial_api_key}"},
        )
        assert resp1.status_code == 200, resp1.text
        managed_token = resp1.json()["token"]

        # Second rotation: no grace_seconds in body → config 300 s governs.
        resp2 = client.post(
            "/keys/rotate",
            json={},  # grace_seconds absent
            headers={"Authorization": f"Bearer {managed_token}"},
        )
        assert resp2.status_code == 200, resp2.text
        body2 = resp2.json()

        # managed_token was in keys.json → old_key_id must be present
        assert body2.get("old_key_id") is not None, "Expected old_key_id to be set for a managed key"
        # Config grace (300 s) → old key is grace-expiring, NOT immediately revoked
        assert body2["old_key_status"] == "active", "Config grace should keep old key active"
        assert body2["old_key_expires_at"] is not None, "Config grace should set expires_at"


# ---------------------------------------------------------------------------
# Security regression: initial auto-generated key rejected after first rotation
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_initial_key_rejected_after_rotation(tmp_path, monkeypatch):
    """After the first rotation, the initial auto-generated key must return 401.

    The initial key is never in keys.json (it was auto-generated before any
    managed key store existed).  After rotation the middleware's dynamic
    api_key lookup (request.app.state.api_key) reads the NEW key, so the old
    initial key no longer matches the legacy fallback — even without a revocation
    record in keys.json.

    This is the critical security regression: a leaked initial key must not
    remain permanently valid after rotation.
    """
    from fastapi.testclient import TestClient
    from archon_search.key_manager import ENV_VAR as _ENV_VAR
    from archon_search.config import SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(_ENV_VAR, raising=False)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )
    app = create_app(cfg, job_store, scheduler=scheduler)
    initial_api_key = app.state.api_key

    with TestClient(app) as client:
        # Confirm initial key works before rotation.
        before_resp = client.get(
            "/keys",
            headers={"Authorization": f"Bearer {initial_api_key}"},
        )
        assert before_resp.status_code == 200

        # Rotate the default key.
        rot_resp = client.post(
            "/keys/rotate",
            json={},
            headers={"Authorization": f"Bearer {initial_api_key}"},
        )
        assert rot_resp.status_code == 200, rot_resp.text
        new_token = rot_resp.json()["token"]
        assert new_token != initial_api_key

        # Initial key must now be rejected — even though it was never in keys.json.
        # The dynamic api_key lookup in the middleware sees the updated app.state.api_key
        # which no longer equals the initial key.
        after_resp = client.get(
            "/keys",
            headers={"Authorization": f"Bearer {initial_api_key}"},
        )
        assert after_resp.status_code == 401, (
            "Initial auto-generated key must be rejected after rotation "
            "(middleware must read api_key dynamically from app.state)"
        )

        # New key must still work.
        new_resp = client.get(
            "/keys",
            headers={"Authorization": f"Bearer {new_token}"},
        )
        assert new_resp.status_code == 200


# ---------------------------------------------------------------------------
# Double-rotation idempotency
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_double_rotation_idempotency(tmp_path, monkeypatch):
    """Two sequential POST /keys/rotate calls both succeed and produce distinct keys.

    After the first rotation the route updates app.state.api_key to the new token.
    The second rotation is authenticated with that new token and reads
    app.state.api_key as current_token — so it rotates the new key, not a ghost
    of the old one.  No 500 must be raised; the two rotations produce distinct
    new_key_id values.
    """
    from fastapi.testclient import TestClient
    from archon_search.key_manager import ENV_VAR as _ENV_VAR
    from archon_search.config import SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(_ENV_VAR, raising=False)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )
    app = create_app(cfg, job_store, scheduler=scheduler)
    initial_api_key = app.state.api_key

    with TestClient(app) as client:
        # First rotation.
        resp1 = client.post(
            "/keys/rotate",
            json={},
            headers={"Authorization": f"Bearer {initial_api_key}"},
        )
        assert resp1.status_code == 200, resp1.text
        token1 = resp1.json()["token"]
        key_id1 = resp1.json()["new_key_id"]

        # Second rotation — old initial key is now 401; use new token.
        resp2 = client.post(
            "/keys/rotate",
            json={},
            headers={"Authorization": f"Bearer {token1}"},
        )
        assert resp2.status_code == 200, resp2.text
        key_id2 = resp2.json()["new_key_id"]
        # Each rotation produces a distinct key ID.
        assert key_id2 != key_id1


# ---------------------------------------------------------------------------
# Grace-window expiry enforcement
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_grace_window_expiry_rejects_old_key(tmp_path, monkeypatch):
    """After grace window expires, old key returns 401.

    Uses two rotations to get a managed key in keys.json with a short grace window,
    then patches datetime in key_manager so active_keys() sees the old key as expired.
    """
    from unittest.mock import patch, MagicMock
    from fastapi.testclient import TestClient
    from archon_search.key_manager import ENV_VAR as _ENV_VAR
    from archon_search.config import SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app
    import archon_search.key_manager as km_mod

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(_ENV_VAR, raising=False)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )
    app = create_app(cfg, job_store, scheduler=scheduler)
    initial_api_key = app.state.api_key
    grace = 60  # 60 seconds

    with TestClient(app) as client:
        # First rotation: auto-generated → managed key.
        resp1 = client.post(
            "/keys/rotate",
            json={},
            headers={"Authorization": f"Bearer {initial_api_key}"},
        )
        assert resp1.status_code == 200, resp1.text
        managed_token = resp1.json()["token"]

        # Second rotation with grace_seconds=60.
        resp2 = client.post(
            "/keys/rotate",
            json={"grace_seconds": grace},
            headers={"Authorization": f"Bearer {managed_token}"},
        )
        assert resp2.status_code == 200, resp2.text
        body2 = resp2.json()
        assert body2["old_key_expires_at"] is not None
        assert body2["old_key_status"] == "active"
        new_token = body2["token"]

        # managed_token is still valid during the grace window.
        grace_resp = client.get(
            "/keys",
            headers={"Authorization": f"Bearer {managed_token}"},
        )
        assert grace_resp.status_code == 200

        # Fast-forward time past the grace window.
        # We patch datetime in key_manager so active_keys() sees `managed_token`'s
        # expires_at as expired and excludes it from the active set.  The 401 is
        # produced because:
        #   1. Managed key path: active_keys() (mocked time) excludes the key → no match.
        #   2. TOML path: no TOML tokens → no match.
        #   3. Legacy api_key path: app.state.api_key == new_token ≠ managed_token → no match.
        # The middleware's own revocation guard (middleware_auth.py datetime) uses real
        # time and is NOT patched — but that path is not reached because the legacy
        # fallback already fails at step 3.
        future_now = datetime.now(UTC) + timedelta(seconds=grace + 10)
        mock_dt = MagicMock()
        mock_dt.now.return_value = future_now

        with patch.object(km_mod, "datetime", mock_dt):
            expired_resp = client.get(
                "/keys",
                headers={"Authorization": f"Bearer {managed_token}"},
            )
        assert expired_resp.status_code == 401, (
            "Grace-expired key must return 401 after grace window passes"
        )

        # New key must still work regardless of time mock.
        new_resp = client.get(
            "/keys",
            headers={"Authorization": f"Bearer {new_token}"},
        )
        assert new_resp.status_code == 200


# ---------------------------------------------------------------------------
# OSError on atomic_write_bytes → 500, keys.json unchanged
# ---------------------------------------------------------------------------


def test_post_keys_rotate_search_env_write_fails_500(tmp_path, monkeypatch):
    """If .search.env write raises OSError, route returns 500 and keys.json is NOT mutated.

    Verifies the safe write order: .search.env is written first, and if it fails,
    keys.json is never touched.
    """
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from archon_search.key_manager import ENV_VAR as _ENV_VAR
    from archon_search.config import SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app
    import archon_search.server.routes_keys as rk_mod

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(_ENV_VAR, raising=False)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )
    app = create_app(cfg, job_store, scheduler=scheduler)
    initial_api_key = app.state.api_key

    # Capture keys.json content before the failed rotation attempt.
    keys_json_path = tmp_path / "keys.json"

    with TestClient(app) as client:
        # Confirm no managed keys exist yet.
        before_resp = client.get(
            "/keys",
            headers={"Authorization": f"Bearer {initial_api_key}"},
        )
        assert before_resp.status_code == 200
        keys_before = before_resp.json()["keys"]

        # Make atomic_write_bytes raise OSError to simulate .search.env write failure.
        with patch.object(rk_mod, "atomic_write_bytes", side_effect=OSError("disk full")):
            resp = client.post(
                "/keys/rotate",
                json={},
                headers={"Authorization": f"Bearer {initial_api_key}"},
            )
        assert resp.status_code == 500
        assert "rotation aborted" in resp.json()["detail"]

        # keys.json must NOT have been mutated (no new managed key added).
        # app.state.api_key must still be the initial key (not updated).
        assert app.state.api_key == initial_api_key

        # GET /keys with initial key must still work (rotation didn't corrupt state).
        after_resp = client.get(
            "/keys",
            headers={"Authorization": f"Bearer {initial_api_key}"},
        )
        assert after_resp.status_code == 200
        keys_after = after_resp.json()["keys"]
        # No new managed keys were added by the failed rotation.
        assert len(keys_after) == len(keys_before)
