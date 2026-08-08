"""S310: acl_gate.sidecar_path is non-empty when a .acl sidecar is present.

Regression test for S310-namespace_key_acl_gate_sidecar_path_populated:
- A namespace key ingests doc.md + doc.md.acl (content: 's310-team\n').
- POST /search with acl_context=true as the namespace key must return HTTP 200 with
  results[0].acl_gate.sidecar_path non-empty.

Also validates that the search route is namespace-blind (POST /search does not namespace-gate
the collection meta lookup; ACL is the per-chunk access control, not namespace).
"""
from __future__ import annotations

import secrets

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_NAMESPACE = "s310-team"
_COLLECTION = "s310-sidecar-path"


def test_s310_namespace_key_acl_gate_sidecar_path_populated(tmp_path, monkeypatch) -> None:
    """acl_gate.sidecar_path is non-empty for a sidecar-backed ACL when namespace key searches."""
    doc = tmp_path / "doc.md"
    doc.write_text("# S310\nThe quick brown fox jumps over the lazy dog.\n")

    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text(f"{_NAMESPACE}\n")

    ns_key = secrets.token_hex(32)
    with make_real_app(
        tmp_path, monkeypatch, namespaces={ns_key: _NAMESPACE}
    ) as (client, _cfg, _default_key):
        ns_headers = {"Authorization": f"Bearer {ns_key}"}

        ingest_file_via_path(client, _COLLECTION, str(doc), api_key=ns_key)

        resp = client.post(
            "/search",
            json={"collection": _COLLECTION, "query": "fox", "acl_context": True},
            headers=ns_headers,
        )
        assert resp.status_code == 200, (
            f"POST /search (namespace key, acl_context=true) returned {resp.status_code}: {resp.json()}"
        )

        data = resp.json()
        results = data.get("results", [])
        assert results, "expected at least one search result for namespace key"

        gate = results[0].get("acl_gate")
        assert gate is not None, "acl_gate must be present when acl_context=true"

        assert gate.get("source") == "sidecar", (
            f"expected acl_gate.source='sidecar'; got: {gate.get('source')!r}"
        )
        assert gate.get("sidecar_path"), (
            f"expected acl_gate.sidecar_path to be non-empty; got: {gate.get('sidecar_path')!r}. gate={gate!r}"
        )
