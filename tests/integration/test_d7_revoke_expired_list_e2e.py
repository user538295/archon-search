"""T-2: E2e — revoke a key and confirm 401; expired key rejected; key list shows hint.

Covers:
- S9: revoked key returns 401 immediately on the next request
- S10: key with ``expires_at`` in the past returns 401 on first auth attempt
- S3: ``GET /keys`` returns only active keys by default; ``hidden_revoked_count`` > 0
      shown as hint when revoked keys exist

The default API key (from ``ARCHON_SEARCH_API_KEY`` env var) is used for key
management operations (POST /keys, DELETE /keys/{id}, GET /keys).  The managed
key token under test is used for an authenticated ``GET /collections`` request —
sufficient to prove auth accepts/rejects correctly without requiring ingested data.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

UTC = timezone.utc


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# T-2 test 1: create → use → revoke → 401
# ---------------------------------------------------------------------------


def test_e2e_revoke_and_reject(tmp_path, monkeypatch):
    """Full create → use → revoke → 401 cycle (S9).

    Steps:
    1. Create a managed key.
    2. Use the token to authenticate a real request — assert 200.
    3. Revoke the key via DELETE /keys/{id}.
    4. Re-use the same token — assert 401 (without server restart).
    """
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, default_api_key):
        # Step 1 — create managed key.
        create_resp = client.post(
            "/keys",
            headers=_auth(default_api_key),
            json={"namespace": "revoke-test-ns", "label": "t2-revoke"},
        )
        assert create_resp.status_code == 201, (
            f"POST /keys failed: {create_resp.status_code} {create_resp.text}"
        )
        body = create_resp.json()
        key_id = body["id"]
        managed_token = body["token"]

        # Step 2 — verify the managed key authenticates successfully.
        auth_resp = client.get("/collections", headers=_auth(managed_token))
        assert auth_resp.status_code == 200, (
            f"Managed key should authenticate GET /collections; "
            f"got {auth_resp.status_code}: {auth_resp.text}"
        )

        # Step 3 — revoke the managed key.
        revoke_resp = client.delete(
            f"/keys/{key_id}",
            headers=_auth(default_api_key),
        )
        assert revoke_resp.status_code == 200, (
            f"DELETE /keys/{key_id} failed: {revoke_resp.status_code} {revoke_resp.text}"
        )
        assert revoke_resp.json()["status"] == "revoked"

        # Verify on-disk persistence via API read-back: the revoked key must appear
        # with status='revoked' in the full view (S4).
        all_keys_resp = client.get("/keys?status=all", headers=_auth(default_api_key))
        assert all_keys_resp.status_code == 200, all_keys_resp.text
        all_keys = all_keys_resp.json()["keys"]
        key_in_all = next((k for k in all_keys if k["id"] == key_id), None)
        assert key_in_all is not None, (
            f"Revoked key {key_id} must appear in GET /keys?status=all; "
            f"got ids: {[k['id'] for k in all_keys]}"
        )
        assert key_in_all["status"] == "revoked", (
            f"Key {key_id} must have status='revoked' in GET /keys?status=all; "
            f"got status={key_in_all['status']!r}"
        )

        # Step 4 — the same token must now be rejected with 401 (S9).
        rejected_resp = client.get("/collections", headers=_auth(managed_token))
        assert rejected_resp.status_code == 401, (
            f"Revoked key should be rejected with 401; "
            f"got {rejected_resp.status_code}: {rejected_resp.text}"
        )
        assert rejected_resp.headers.get("WWW-Authenticate") == "Bearer", (
            f"Revoked-key 401 must include WWW-Authenticate: Bearer header; "
            f"got: {rejected_resp.headers.get('WWW-Authenticate')!r}"
        )


# ---------------------------------------------------------------------------
# T-2 test 2: born-expired key rejected immediately (S10)
# ---------------------------------------------------------------------------


def test_e2e_expired_key_rejected(tmp_path, monkeypatch):
    """Create a key with ``expires_at`` in the past; confirm 401 on first auth (S10).

    Per the plan (S26 / S10): the server accepts creation of a key with a past
    ``expires_at`` (201); the key is immediately expired and rejected on auth.
    """
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, default_api_key):
        # Create a key that is already expired (1 day in the past).
        past_expires = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        create_resp = client.post(
            "/keys",
            headers=_auth(default_api_key),
            json={"namespace": "expire-test-ns", "expires_at": past_expires},
        )
        assert create_resp.status_code == 201, (
            f"POST /keys with past expires_at should return 201; "
            f"got {create_resp.status_code}: {create_resp.text}"
        )
        expired_token = create_resp.json()["token"]

        # Auth with an immediately-expired key must return 401 (S10).
        auth_resp = client.get("/collections", headers=_auth(expired_token))
        assert auth_resp.status_code == 401, (
            f"Immediately-expired key should be rejected with 401; "
            f"got {auth_resp.status_code}: {auth_resp.text}"
        )
        assert auth_resp.headers.get("WWW-Authenticate") == "Bearer", (
            f"Expired-key 401 must include WWW-Authenticate: Bearer header; "
            f"got: {auth_resp.headers.get('WWW-Authenticate')!r}"
        )


# ---------------------------------------------------------------------------
# T-2 test 3: list shows active-only + hint count (S3)
# ---------------------------------------------------------------------------


def test_e2e_list_shows_hint(tmp_path, monkeypatch):
    """Create two keys, revoke one; GET /keys returns 1 active + hint count (S3).

    Verifies:
    - Default GET /keys view shows only active keys.
    - ``hidden_revoked_count`` equals the number of revoked keys not shown.
    - The revoked key does NOT appear in the default view.
    - The revoked key DOES appear in GET /keys?status=all.
    """
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, default_api_key):
        # Create key A.
        resp_a = client.post(
            "/keys",
            headers=_auth(default_api_key),
            json={"namespace": "list-test-ns", "label": "key-a"},
        )
        assert resp_a.status_code == 201, resp_a.text
        key_a_id = resp_a.json()["id"]

        # Create key B.
        resp_b = client.post(
            "/keys",
            headers=_auth(default_api_key),
            json={"namespace": "list-test-ns", "label": "key-b"},
        )
        assert resp_b.status_code == 201, resp_b.text
        key_b_id = resp_b.json()["id"]

        # Revoke key B.
        revoke_resp = client.delete(
            f"/keys/{key_b_id}",
            headers=_auth(default_api_key),
        )
        assert revoke_resp.status_code == 200, revoke_resp.text

        # GET /keys (default: active only) — must show only key A.
        list_resp = client.get("/keys", headers=_auth(default_api_key))
        assert list_resp.status_code == 200, (
            f"GET /keys failed: {list_resp.status_code} {list_resp.text}"
        )
        list_body = list_resp.json()

        active_keys = list_body["keys"]
        active_ids = {k["id"] for k in active_keys}

        assert key_a_id in active_ids, (
            f"Active key A ({key_a_id}) must appear in the default list; "
            f"got ids: {active_ids}"
        )
        assert key_b_id not in active_ids, (
            f"Revoked key B ({key_b_id}) must NOT appear in the default active list; "
            f"got ids: {active_ids}"
        )

        # hidden_revoked_count must reflect the 1 revoked key that was excluded (S3).
        hidden_count = list_body["hidden_revoked_count"]
        assert hidden_count == 1, (
            f"Expected hidden_revoked_count == 1 (one revoked key hidden); "
            f"got hidden_revoked_count={hidden_count}"
        )

        # GET /keys?status=all — must show both keys.
        all_resp = client.get("/keys?status=all", headers=_auth(default_api_key))
        assert all_resp.status_code == 200, all_resp.text
        all_ids = {k["id"] for k in all_resp.json()["keys"]}
        assert key_a_id in all_ids, f"key A must appear in status=all; got {all_ids}"
        assert key_b_id in all_ids, f"key B must appear in status=all; got {all_ids}"
        all_key_records = all_resp.json()["keys"]
        key_b_record = next((k for k in all_key_records if k["id"] == key_b_id), None)
        assert key_b_record is not None, f"key B must appear in status=all; got ids: {all_ids}"
        assert key_b_record["status"] == "revoked", (
            f"Key B must have status='revoked' in status=all view; "
            f"got status={key_b_record['status']!r}"
        )
        assert all_resp.json()["hidden_revoked_count"] == 0, (
            "hidden_revoked_count must be 0 when status=all"
        )
