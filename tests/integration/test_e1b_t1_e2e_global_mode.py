"""E1b / T-1 — e2e tests for build-communities CLI and graph_mode=global search.

Covers:
- (b) POST /search graph_mode=global returns 200 + non-empty results +
      graph_expansion_applied=true when communities are built  (S2)
- (c) POST /search graph_mode=global returns 422 graph_communities_not_built
      when communities have NOT been built  (S11)

Tests (b) and (c) use make_real_app + direct community seeding; no leidenalg needed.

Note (GBC110 BE-8): the former in-process ``build-communities`` CLI e2e test (a)
was removed here — ``cli/graph_cmd.py`` was converted from an in-process command
to an HTTP proxy against ``POST /graph/{collection}/rebuild-communities``. The
real-subprocess CLI-vs-server e2e replacement is GBC110 T-1
(``test_e2e_graph_build_communities_wait_against_server``), using the BE-9
graph-enabled smoke-server fixture.
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# spaCy stub — needed for make_real_app(graph_enabled=True)
# ---------------------------------------------------------------------------


def _install_spacy_stub_no_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake spaCy that returns NO named entities.

    Must be called BEFORE make_real_app because create_app calls _check_graph_deps
    which imports spacy synchronously.
    """

    class _FakeDoc:
        def __init__(self) -> None:
            self.ents: list = []

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            return _FakeDoc()

    nlp_instance = _FakeNLP()

    fake_util = types.ModuleType("spacy.util")
    fake_util.get_installed_models = lambda: ["en_core_web_sm"]  # type: ignore[attr-defined]
    fake_cli = types.ModuleType("spacy.cli")
    fake_cli.download = lambda model: None  # type: ignore[attr-defined]
    fake_spacy = types.ModuleType("spacy")
    fake_spacy.load = lambda model: nlp_instance  # type: ignore[attr-defined]
    fake_spacy.util = fake_util  # type: ignore[attr-defined]
    fake_spacy.cli = fake_cli  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setitem(sys.modules, "spacy.util", fake_util)
    monkeypatch.setitem(sys.modules, "spacy.cli", fake_cli)


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# Helper: write communities directly to the GraphStore from sync test
# ---------------------------------------------------------------------------


async def _write_communities_to_store(
    db_path: str,
    col: str,
    chunk_ids: list[str],
    *,
    community_id: str = "test-comm-1",
    entity_ids: list[str] | None = None,
) -> None:
    """Write a single community to the GraphStore at db_path."""
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import Community

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        await gs.ensure_communities_table(col, ns="default")
        community = Community(
            community_id=community_id,
            entity_ids=entity_ids or ["entity-1"],
            representative_chunk_ids=chunk_ids,
            built_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            summary_text=None,
        )
        await gs.write_communities(col, [community], ns="default")
    finally:
        await gs.disconnect()


# ---------------------------------------------------------------------------
# (b) test_e2e_global_mode_returns_results
# ---------------------------------------------------------------------------


def test_e2e_global_mode_returns_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After build-communities; POST /search graph_mode=global → 200, non-empty results,
    graph_expansion_applied=true  (S2).

    Communities are seeded directly into the store (no leidenalg needed).
    """
    _install_spacy_stub_no_entities(monkeypatch)

    col = "t1-global-mode-results"
    doc = tmp_path / "test_doc.txt"
    doc.write_text(
        "SearchPipeline provides vector and FTS retrieval. "
        "Community detection clusters related entities.\n" * 5,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        # Ingest the document so the collection and chunks exist.
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Retrieve a chunk_id from the store.
        async def _get_chunk_ids() -> list[str]:
            from archon_search.constants import DEFAULT_NAMESPACE
            from archon_search.store import SearchStore

            s = SearchStore(cfg.db_path)
            await s.connect()
            try:
                rows = [r async for r in s.list_chunks_raw(col, DEFAULT_NAMESPACE)]
                return [r["chunk_id"] for r in rows]
            finally:
                await s.disconnect()

        chunk_ids = asyncio.run(_get_chunk_ids())
        assert chunk_ids, "Ingest must have created at least one chunk"

        # Seed a community pointing to the first chunk.
        asyncio.run(
            _write_communities_to_store(cfg.db_path, col, chunk_ids[:1])
        )

        # POST /search with graph_mode=global should return 200 with results.
        resp = client.post(
            "/search",
            json={"collection": col, "query": "community retrieval", "graph_mode": "global"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"Expected 200 for graph_mode=global with communities built; "
            f"got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "results" in body, f"Expected 'results' key in response: {list(body.keys())}"
        assert body.get("graph_expansion_applied") is True, (
            f"Expected graph_expansion_applied=True; "
            f"got {body.get('graph_expansion_applied')!r}. Response: {body}"
        )
        assert len(body["results"]) > 0, (
            f"Expected non-empty results from global mode search; got empty list. "
            f"Response: {body}"
        )
        # Verify results originate from the community representative: the seeded community
        # points to chunk_ids[:1]. The source_path of results must include the ingested doc.
        result_sources = [r.get("source_path", "") for r in body["results"]]
        assert any(str(doc) in src for src in result_sources), (
            f"Expected at least one result from the ingested document {str(doc)!r}; "
            f"got source_paths: {result_sources!r}"
        )


# ---------------------------------------------------------------------------
# (c) test_e2e_global_mode_no_communities_422
# ---------------------------------------------------------------------------


def test_e2e_global_mode_no_communities_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No build-communities; POST /search graph_mode=global → 422 graph_communities_not_built  (S11).

    Verifies that the route catches GraphCommunitiesNotBuiltError and returns a 422
    with {"detail": {"code": "graph_communities_not_built"}} body.
    """
    _install_spacy_stub_no_entities(monkeypatch)

    col = "t1-global-no-communities"
    doc = tmp_path / "test_doc.txt"
    doc.write_text("Some content for testing graph_mode=global.\n" * 5, encoding="utf-8")

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, _cfg, api_key):
        # Ingest a document so the collection exists — but do NOT build communities.
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={"collection": col, "query": "test query", "graph_mode": "global"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 422, (
            f"Expected 422 when no communities built; "
            f"got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "detail" in body, f"Expected 'detail' key in 422 body: {body!r}"
        detail = body["detail"]
        assert isinstance(detail, dict), (
            f"Expected detail to be a dict; got {type(detail).__name__!r}: {detail!r}"
        )
        assert detail.get("code") == "graph_communities_not_built", (
            f"Expected code='graph_communities_not_built'; got: {detail!r}"
        )
        # The 422 body must contain a message (per TypeSpec contract)
        assert "message" in detail, (
            f"Expected 'message' key in error detail; got: {detail!r}"
        )
        assert detail["message"], (
            f"'message' must be a non-empty string; got: {detail!r}"
        )
