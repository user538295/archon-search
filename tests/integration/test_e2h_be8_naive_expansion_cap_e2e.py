"""E2h BE-8 integration e2e — naive expansion cap end-to-end.

S9: POST /search graph_mode="naive" with a high-degree entity seeded in
graph → expansion_used=True, expanded query bounded to ≤ naive_max_expansion_terms.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_CUSTOM_CAP = 5  # non-default; default is 20
_NEIGHBOUR_COUNT = 10  # exceeds the custom cap to prove the cap is enforced


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _install_spacy_stub(monkeypatch: pytest.MonkeyPatch, seed_entity: str) -> None:
    """Install a spaCy stub that recognises seed_entity as an ORG."""

    class _FakeEnt:
        def __init__(self, text: str, label: str) -> None:
            self.text = text
            self.label_ = label

    class _FakeDoc:
        def __init__(self, ents: list[_FakeEnt]) -> None:
            self.ents = ents

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            ents: list[_FakeEnt] = []
            if seed_entity in text:
                ents.append(_FakeEnt(seed_entity, "ORG"))
            return _FakeDoc(ents)

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


def test_naiveCap_endToEnd_expandedQueryBounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Naive mode with a non-default cap (5): expansion_used=True and at most 5 terms added.

    Setup:
    - Configure naive_max_expansion_terms=5 (non-default; default is 20) via TOML so the
      app.py wiring is exercised end-to-end.
    - Seed _NEIGHBOUR_COUNT (10) distinct neighbour entities connected to "HubEntity".
    - Ingest one doc containing "HubEntity" text so the entity appears in the graph.
    - POST /search with graph_mode="naive" for query "HubEntity".

    Expected:
    - graph_expansion_applied = True (from SearchResponse).
    - expansion_used = True (route-layer derived from graph_expansion_applied).
    - The GraphExpander (instantiated with cap=5) adds at most 5 terms to the query,
      verified by calling GraphExpander.expand() directly against the seeded graph store.
    """
    _SEED_ENTITY = "HubEntity"

    _install_spacy_stub(monkeypatch, _SEED_ENTITY)

    col = "e2h-be8-cap-e2e"

    # Create a doc with many occurrences of HubEntity so graph extraction fires.
    seed_doc = tmp_path / "hub_entity_doc.txt"
    seed_doc.write_text(
        (f"{_SEED_ENTITY} is a central component of the system. " * 5 + "\n") * 6,
        encoding="utf-8",
    )

    # Pass a non-default cap via TOML so the app.py wiring (FIX-1) is exercised.
    toml_content = f"[graph]\nnaive_max_expansion_terms = {_CUSTOM_CAP}\n"

    with make_real_app(
        tmp_path, monkeypatch, graph_enabled=True, toml_content=toml_content
    ) as (client, cfg, api_key):
        auth = _auth(api_key)

        # Confirm the config reflects the custom cap.
        assert cfg.graph.naive_max_expansion_terms == _CUSTOM_CAP, (
            f"Expected naive_max_expansion_terms={_CUSTOM_CAP}, "
            f"got {cfg.graph.naive_max_expansion_terms}"
        )

        # Ingest the seed document.
        ingest_file_via_path(client, col, str(seed_doc), api_key=api_key)

        # Seed the graph with HubEntity + _NEIGHBOUR_COUNT distinct neighbours via
        # GraphStore directly so we have more neighbours than the cap.
        import asyncio

        from archon_search.graph_store import GraphStore
        from archon_search.graph_types import (
            EntityType,
            GraphEdge,
            GraphNode,
            RelationshipType,
            make_stable_edge_id,
            make_stable_entity_id,
        )

        db_path = str(tmp_path / "db")

        async def _seed_graph() -> None:
            gs = GraphStore(db_path)
            await gs.connect()

            hub_node = GraphNode(
                id=make_stable_entity_id("system", _SEED_ENTITY),
                entity_name=_SEED_ENTITY,
                entity_type=EntityType.system,
                source_doc_id="hub-doc",
                collection_name=col,
            )
            neighbour_nodes = [
                GraphNode(
                    id=make_stable_entity_id("concept", f"Neighbour{i:03d}"),
                    entity_name=f"Neighbour{i:03d}",
                    entity_type=EntityType.concept,
                    source_doc_id="hub-doc",
                    collection_name=col,
                )
                for i in range(_NEIGHBOUR_COUNT)
            ]
            edges = [
                GraphEdge(
                    id=make_stable_edge_id(
                        hub_node.id,
                        n.id,
                        RelationshipType.related_to.value,
                    ),
                    source_node_id=hub_node.id,
                    target_node_id=n.id,
                    relationship_type=RelationshipType.related_to,
                    source_doc_id="hub-doc",
                )
                for n in neighbour_nodes
            ]
            all_nodes = [hub_node] + neighbour_nodes
            await gs.ensure_graph_tables(col, ns="default")
            await gs.write_graph(col, all_nodes, edges, ns="default")
            await gs.disconnect()

        asyncio.run(_seed_graph())

        # POST /search with graph_mode="naive" — confirms the HTTP path works end-to-end.
        resp = client.post(
            "/search",
            json={"collection": col, "query": _SEED_ENTITY, "graph_mode": "naive"},
            headers=auth,
        )
        assert resp.status_code == 200, f"/search failed: {resp.status_code} {resp.text}"
        body = resp.json()

        assert body["graph_expansion_applied"] is True, (
            "Expected graph_expansion_applied=True; "
            f"graph_expansion_applied={body.get('graph_expansion_applied')}"
        )
        assert body["expansion_used"] is True, (
            f"Expected expansion_used=True; got {body.get('expansion_used')}"
        )

        # Verify the wiring: the app's pipeline expander must carry the config cap,
        # not just the class default. This directly catches a wiring regression
        # (e.g. reverting app.py:521 to GraphExpander(_graph_store)).
        pipeline = client.app.state.pipeline  # type: ignore[attr-defined]
        expander = pipeline._graph_expander
        assert expander is not None, "GraphExpander not wired into pipeline"
        assert expander._naive_max_expansion_terms == _CUSTOM_CAP, (
            f"Expected pipeline expander cap={_CUSTOM_CAP} "
            f"(from TOML naive_max_expansion_terms), "
            f"got {expander._naive_max_expansion_terms}"
        )

        # Also verify the expander actually caps: call expand() with the real store
        # and assert exactly _CUSTOM_CAP terms added (not merely ≤, since we seeded
        # _NEIGHBOUR_COUNT > _CUSTOM_CAP distinct non-query neighbours).
        async def _check_cap() -> int:
            gs = GraphStore(db_path)
            await gs.connect()
            result = await expander.expand(_SEED_ENTITY, col, ns="default")
            await gs.disconnect()
            return len(result.neighbour_names_added)

        added_count = asyncio.run(_check_cap())
        assert added_count == _CUSTOM_CAP, (
            f"Expected exactly {_CUSTOM_CAP} added terms (cap={_CUSTOM_CAP}, "
            f"neighbours={_NEIGHBOUR_COUNT}), but got {added_count}"
        )
