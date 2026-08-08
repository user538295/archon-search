"""S310: POST /sync must not corrupt the namespace of a REST-ingested collection.

Regression guard: SearchCollectionSync.sync() reads get_all_collections_meta()
and passes the stored namespace forward. A collection ingested via a namespace
key must still be accessible under that namespace after a sync run.
"""
from __future__ import annotations

import secrets
import time

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_NAMESPACE = "sync-team"
_COLLECTION = "s310-sync"


def test_s310_sync_preserves_namespace(tmp_path, monkeypatch) -> None:
    """A collection's namespace must survive a POST /sync run.

    Steps:
    1. Ingest a file via a namespace key (collection stored under 'sync-team').
    2. Trigger POST /sync and wait for DONE.
    3. Assert: the collection is still accessible under the namespace key.
    4. Assert: POST /search via the namespace key still returns results.
    """
    doc = tmp_path / "fox.md"
    doc.write_text("# Fox\nThe quick brown fox jumps over the lazy dog.\n")

    ns_key = secrets.token_hex(32)

    with make_real_app(tmp_path, monkeypatch, namespaces={ns_key: _NAMESPACE}) as (
        client, _cfg, default_key
    ):
        ns_headers = {"Authorization": f"Bearer {ns_key}"}

        ingest_file_via_path(client, _COLLECTION, str(doc), api_key=ns_key)

        # Verify initial state: collection is under the namespace.
        resp = client.get(f"/collections/{_COLLECTION}", headers=ns_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["namespace"] == _NAMESPACE, (
            f"pre-sync: expected namespace {_NAMESPACE!r}, got {resp.json()['namespace']!r}"
        )

        # Trigger a sync run.
        resp = client.post("/sync", headers=ns_headers)
        assert resp.status_code == 202, f"POST /sync failed: {resp.status_code} {resp.text}"
        job_id = resp.json()["job_id"]

        # Poll until the sync job completes.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            r = client.get(f"/jobs/{job_id}", headers=ns_headers)
            assert r.status_code == 200
            status = r.json()["status"]
            if status in {"DONE", "FAILED"}:
                break
            time.sleep(0.1)
        assert r.json()["status"] == "DONE", f"Sync job failed: {r.json()}"

        # After sync: collection must still be under the namespace.
        resp = client.get(f"/collections/{_COLLECTION}", headers=ns_headers)
        assert resp.status_code == 200, (
            f"namespace key cannot see collection after sync: {resp.status_code} {resp.text}"
        )
        assert resp.json()["namespace"] == _NAMESPACE, (
            f"sync corrupted the namespace: got {resp.json()['namespace']!r}"
        )

        # Search must still work via the namespace key.
        resp = client.post(
            "/search",
            json={"collection": _COLLECTION, "query": "fox"},
            headers=ns_headers,
        )
        assert resp.status_code == 200, (
            f"search failed after sync: {resp.status_code} {resp.text}"
        )
        assert resp.json()["results"], (
            f"expected non-empty results after sync: {resp.json()}"
        )

        # Default key must not see the collection (namespace isolation).
        resp = client.get(
            "/collections/",
            headers={"Authorization": f"Bearer {default_key}"},
        )
        assert resp.status_code == 200, resp.text
        default_names = [c["name"] for c in resp.json()]
        assert _COLLECTION not in default_names, (
            f"collection leaked into default namespace after sync: {default_names}"
        )
