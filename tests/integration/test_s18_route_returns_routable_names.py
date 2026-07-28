"""S18: POST /route must surface collections created by single-file ingest.

Reproduces the backlog bug ``202607280906-S18-routable_names_non_empty``:
after ingesting one file into a fresh collection via ``POST /ingest`` (which
writes a LanceDB table + meta row but never a config path), ``POST /route``
returned ``routable_names: []``. The route handler fetched the correct
namespace-scoped meta rows from the store but never handed them to the
``MultiCollectionRouter``; the router instead issued a self-call HTTP fetch to
the bare REST root (not ``/mcp``), which returns nothing. Same class of bug as
S09/S12/S22 — meta-only collections were invisible to a config-oriented path.
"""
from __future__ import annotations

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app


@pytest.mark.integration
def test_route_lists_single_file_ingest_collection_as_routable(tmp_path, monkeypatch) -> None:
    doc = tmp_path / "deploy.md"
    doc.write_text(
        "# Deployment\nThis document covers container deployment and orchestration.\n"
    )

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        ingest_file_via_path(client, "container-docs", str(doc), api_key=api_key)

        resp = client.post("/route", json={"query": "container deployment"}, headers=headers)
        assert resp.status_code == 200, resp.text

        data = resp.json()
        assert set(data) == {"pre_context", "pinned_names", "routable_names", "decomposer_invoked"}
        assert isinstance(data["routable_names"], list)
        # The meta-only collection must be routable (empty before the fix).
        assert data["routable_names"], f"expected a routable collection, got: {data}"
        assert "container-docs" in data["routable_names"], data
