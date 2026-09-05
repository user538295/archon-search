"""Integration tests for enrichment client injection — LLCP BE-7 (S20a, S20b).

Verifies the enrichment client built once at composition root
(``create_app()``'s ``_enrichment_client`` local, stored as
``app.state.enrichment_client``) reaches both community-summarisation
construction sites with the correct concrete type when ``[graph].provider`` is
configured. Since BE-17 narrowed enrichment to summarisation, ``GraphExtractor``
no longer consumes the enrichment client — only ``CommunityBuilder`` does:

1. ``routes_graph.py`` — the ``CommunityBuilder`` built per rebuild-communities request.
2. ``maintenance_loop.py`` — the ``CommunityBuilder`` built for a GC-triggered rebuild.

Covers:
- #integration_test test_app_state_has_enrichment_client_for_summarisation_sites (S20a, S20b)
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tests.integration.conftest import make_real_app
from tests._graph_engine_stub import install_graph_engine_stub

pytestmark = pytest.mark.integration

_STUB_EMBEDDING_DIM = 384


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


async def _seed_collection(db_path: str, collection: str, ns: str = "default") -> None:
    """Seed a minimal collection record so get_collection_meta returns non-None."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.store import SearchStore

    store = SearchStore(db_path)
    await store.connect()
    try:
        await store.ensure_collection(collection, _STUB_EMBEDDING_DIM)
        meta = CollectionMeta(
            name=collection,
            active_embedding_model="stub-model",
            doc_count=0,
            chunk_count=0,
            namespace=ns,
        )
        await store.update_collection_meta(meta)
    finally:
        await store.disconnect()


def _poll_job(client, job_id: str, api_key: str, timeout_s: float = 10.0) -> dict:
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", headers=_auth(api_key))
        assert r.status_code == 200
        body = r.json()
        if body["status"] in {"DONE", "FAILED", "FAILED_EXPIRED", "CANCELLED"}:
            return body
        time.sleep(0.1)
    pytest.fail(f"job {job_id} did not reach a terminal state in {timeout_s}s")


def test_app_state_has_enrichment_client_for_summarisation_sites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[graph].provider="llama_cpp" -> CommunityBuilder (rebuild route) and
    CommunityBuilder (MaintenanceLoop GC path) each receive a non-None
    LlamaCppEnrichmentClient built once at composition root (S20a, S20b).
    GraphExtractor no longer consumes it (BE-17)."""
    from archon_search.enrichment.llama_cpp import LlamaCppEnrichmentClient

    install_graph_engine_stub(monkeypatch)
    # BE-23's startup probe would otherwise fire a real inference POST toward
    # `llama_cpp_base_url` (default http://localhost:8080) if a llama-server happens to be
    # running on the machine running this test — this test is only about client injection.
    monkeypatch.setattr(
        "archon_search.model_validation._probe_extraction_model",
        AsyncMock(return_value=[]),
    )
    toml_content = (
        "[graph]\n"
        "enabled = true\n"
        'provider = "llama_cpp"\n'
        'extraction_model = "model-x"\n'
    )
    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (
        client,
        cfg,
        api_key,
    ):
        # Site 0: app.state.enrichment_client itself (the composition-root local).
        assert isinstance(client.app.state.enrichment_client, LlamaCppEnrichmentClient)

        # Site 1: CommunityBuilder built per rebuild-communities request (routes_graph.py).
        asyncio.run(_seed_collection(cfg.db_path, "testcol"))
        with patch("archon_search.server.routes_graph.CommunityBuilder") as mock_cb:
            mock_cb.return_value.build = AsyncMock(return_value=[])
            resp = client.post(
                "/graph/testcol/rebuild-communities", headers=_auth(api_key)
            )
            assert resp.status_code == 202
            _poll_job(client, resp.json()["job_id"], api_key)

        mock_cb.assert_called_once()
        assert isinstance(
            mock_cb.call_args.kwargs["enrichment_client"], LlamaCppEnrichmentClient
        )

        # Site 2: CommunityBuilder built for a GC-triggered rebuild (maintenance_loop.py).
        with patch("archon_search.community_builder.CommunityBuilder") as mock_cb2:
            mock_cb2.return_value.build = AsyncMock(return_value=[])
            asyncio.run(
                client.app.state.maintenance_loop._rebuild_communities_async(
                    "default", "testcol"
                )
            )

        mock_cb2.assert_called_once()
        assert isinstance(
            mock_cb2.call_args.kwargs["enrichment_client"], LlamaCppEnrichmentClient
        )
