"""S309: acl_context toggles the acl_gate envelope on POST /search results.

Regression test for S309-acl_context_true_attaches_gate.

Setup: ingest two docs mentioning "programming language" into an isolated collection.
Assert:
- acl_context=true  → every result carries a populated acl_gate dict with a `source`.
- acl_context=false → no result carries a populated acl_gate (omitted or null).
- acl_context omitted → HTTP 200 with a results list, no error.
"""
from __future__ import annotations

import pytest

from tests.integration.conftest import ingest_doc, make_real_app

pytestmark = pytest.mark.integration

_COLLECTION = "s309-acl-context-gate"
_QUERY = "programming language"


def test_s309_acl_context_toggles_acl_gate(tmp_path, monkeypatch) -> None:
    """acl_context=true attaches acl_gate; false/omitted does not error and does not attach it."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        ingest_doc(
            client,
            _COLLECTION,
            "Python is a high level programming language used for scripting.",
            "/s309/alpha.md",
            api_key=api_key,
        )
        ingest_doc(
            client,
            _COLLECTION,
            "Rust is a systems programming language focused on memory safety.",
            "/s309/beta.md",
            api_key=api_key,
        )

        # Step 2 (control): acl_context=true → gate attached.
        resp = client.post(
            "/search",
            json={"collection": _COLLECTION, "query": _QUERY, "acl_context": True},
            headers=headers,
        )
        assert resp.status_code == 200, (
            f"POST /search (acl_context=true) returned {resp.status_code}: {resp.text}"
        )
        results_true = resp.json()["results"]
        assert results_true, "expected at least one result for the control query"
        for r in results_true:
            gate = r.get("acl_gate")
            assert isinstance(gate, dict), (
                f"acl_gate must be a populated dict when acl_context=true; got {gate!r}"
            )
            assert gate.get("source"), (
                f"acl_gate must carry a populated source when acl_context=true; got {gate!r}"
            )

        # Step 3: acl_context=false → no populated gate.
        resp = client.post(
            "/search",
            json={"collection": _COLLECTION, "query": _QUERY, "acl_context": False},
            headers=headers,
        )
        assert resp.status_code == 200, (
            f"POST /search (acl_context=false) returned {resp.status_code}: {resp.text}"
        )
        for r in resp.json()["results"]:
            assert r.get("acl_gate") is None, (
                f"acl_gate must be omitted or null when acl_context=false; got {r.get('acl_gate')!r}"
            )

        # Step 4: acl_context omitted entirely → 200 with a results list.
        resp = client.post(
            "/search",
            json={"collection": _COLLECTION, "query": _QUERY},
            headers=headers,
        )
        assert resp.status_code == 200, (
            f"POST /search (acl_context omitted) returned {resp.status_code}: {resp.text}"
        )
        assert isinstance(resp.json()["results"], list)
