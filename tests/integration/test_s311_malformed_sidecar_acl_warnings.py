"""S311: acl_gate.warnings non-empty when the .acl sidecar contains a malformed entry.

Reproduces the backlog bug ``S311-malformed_sidecar_acl_gate_warnings_non_empty``:
when a sidecar contains ``s311-team\ndeny-all\n``, the ``deny-all`` entry is an
invalid namespace name (rejected by ``is_acl_namespace_valid``) and must surface
as a warning in ``acl_gate.warnings`` on the search result.

The 404 root cause (namespace key ingest creating the collection under 'default'
instead of the caller's namespace) is exercised here too — if that bug were present,
the search would return 404 before we could assert on ``acl_gate.warnings``.
"""
from __future__ import annotations

import secrets

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_NAMESPACE = "s311-team"
_COLLECTION = "s311-ns"


@pytest.mark.integration
def test_s311_malformed_sidecar_acl_gate_warnings_non_empty(tmp_path, monkeypatch) -> None:
    """Sidecar with 'deny-all' produces non-empty acl_gate.warnings on search result.

    Flow:
    1. Namespace key for 's311-team'.
    2. Ingest doc.md with doc.md.acl containing 's311-team\\ndeny-all\\n'.
    3. POST /search with acl_context=true as the namespace key.
    4. Assert HTTP 200, results present, results[0].acl_gate.warnings is non-empty.
    """
    doc = tmp_path / "doc.md"
    doc.write_text("# S311 Test\nThe quick brown fox jumps over the lazy dog.\n")

    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text(f"{_NAMESPACE}\ndeny-all\n")

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
        assert results, "expected at least one search result"

        gate = results[0].get("acl_gate")
        assert gate is not None, "acl_gate must be present when acl_context=true"

        warnings = gate.get("warnings", [])
        assert isinstance(warnings, list), f"acl_gate.warnings must be a list, got {type(warnings)}"
        assert len(warnings) > 0, (
            f"acl_gate.warnings must be non-empty for a sidecar containing 'deny-all'; "
            f"got: {warnings!r}. gate={gate!r}"
        )
