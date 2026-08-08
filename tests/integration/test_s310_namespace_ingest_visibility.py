"""S310: POST /ingest must create the collection in the caller's namespace.

Reproduces the backlog bug ``S310-namespace_key_collection_not_found``: a bearer
token mapped to a ``[namespaces]`` entry ingests a file, but ``ingest_chunks``
dropped the ``namespace`` argument on its way to ``_do_update_meta_on_add``, so
the ``CollectionMeta`` row was written under ``'default'``.  Consequences:
``GET /collections`` with the namespace key returns ``[]``, ``POST /search``
returns 404 for a collection that key just ingested, and the collection leaks
into the default namespace.
"""
from __future__ import annotations

import secrets

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_NAMESPACE = "s310-team"
_COLLECTION = "s310-ns"


def test_s310_namespace_key_collection_not_found(tmp_path, monkeypatch) -> None:
    doc = tmp_path / "fox.md"
    doc.write_text("# Fox\nThe quick brown fox jumps over the lazy dog.\n")

    ns_key = secrets.token_hex(32)
    with make_real_app(
        tmp_path, monkeypatch, namespaces={ns_key: _NAMESPACE}
    ) as (client, _cfg, default_key):
        ns_headers = {"Authorization": f"Bearer {ns_key}"}
        ingest_file_via_path(client, _COLLECTION, str(doc), api_key=ns_key)

        resp = client.get("/collections/", headers=ns_headers)
        assert resp.status_code == 200, resp.text
        ns_collections = {c["name"]: c for c in resp.json()}
        assert _COLLECTION in ns_collections, (
            f"namespace key cannot see the collection it ingested: {resp.json()}"
        )
        assert ns_collections[_COLLECTION]["namespace"] == _NAMESPACE

        resp = client.get("/collections/", headers={"Authorization": f"Bearer {default_key}"})
        assert resp.status_code == 200, resp.text
        default_names = [c["name"] for c in resp.json()]
        assert _COLLECTION not in default_names, (
            f"collection leaked into the default namespace: {resp.json()}"
        )

        resp = client.post(
            "/search",
            json={"collection": _COLLECTION, "query": "fox"},
            headers=ns_headers,
        )
        assert resp.status_code == 200, f"search failed: {resp.status_code} {resp.text}"
