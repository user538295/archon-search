"""S310: acl_gate.source == 'sidecar' and acl_gate.sidecar_path non-empty for namespace key search.

Reproduces S310-namespace_key_sees_chunk_source_is_sidecar:
- Ingest doc.md + doc.md.acl (content: 's310-team\\n') via namespace key.
- POST /search with namespace key + acl_context=true: results present,
  acl_gate.source == 'sidecar', acl_gate.sidecar_path is non-empty.

The 'acl_filtered: true' check for the default key is owned by S227 and is not
re-asserted here (the default key cannot see a collection in the 's310-team' namespace).
"""
from __future__ import annotations

import secrets

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_NAMESPACE = "s310-team"
_COLLECTION = "s310-sidecar-src"


def test_s310_namespace_key_sees_chunk_source_is_sidecar(tmp_path, monkeypatch) -> None:
    """acl_gate.source == 'sidecar' and acl_gate.sidecar_path non-empty when namespace key searches."""
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
            f"POST /search (namespace key) returned {resp.status_code}: {resp.json()}"
        )

        data = resp.json()
        results = data.get("results", [])
        assert results, "expected at least one search result for namespace key"

        gate = results[0].get("acl_gate")
        assert gate is not None, "acl_gate must be present when acl_context=true"

        assert gate.get("source") == "sidecar", (
            f"expected acl_gate.source='sidecar'; got: {gate.get('source')!r}. gate={gate!r}"
        )
        assert gate.get("sidecar_path"), (
            f"expected acl_gate.sidecar_path to be non-empty; got: {gate.get('sidecar_path')!r}. gate={gate!r}"
        )
