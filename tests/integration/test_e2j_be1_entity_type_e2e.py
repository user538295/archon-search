"""Integration tests for BE-1: entity_type on graph inspection responses.

Tests:
  - test_graph_response_includes_entity_type
    GET /graph/{col} JSON response includes entity_type on every node.
  - test_cross_collection_graph_response_includes_entity_type
    GET /graph/cross-collection returns 200 with entity_type on every merged node.

Run with:
    uv run pytest tests/integration/test_e2j_be1_entity_type_e2e.py -n0 -v --no-cov
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _install_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a stub spaCy module that returns NO named entities."""

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


_STUB_EMBEDDING_DIM = 384

# Valid EntityType values from archon_search.graph_types.EntityType
_VALID_ENTITY_TYPES = {"person", "concept", "system", "event", "code_symbol"}


async def _seed_graph_with_typed_nodes(
    db_path: str,
    collection: str,
    ns: str = "default",
) -> None:
    """Seed two nodes with different entity_types into the graph store.

    - Kubernetes: EntityType.system
    - Alice: EntityType.person
    Also creates a minimal chunk record so collection is visible.
    """
    import hashlib
    from datetime import datetime, timezone

    from archon_search._types import ChunkRecord, normalize_iso_utc
    from archon_search.collection_meta import CollectionMeta
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import (
        EntityType,
        GraphMention,
        GraphNode,
        make_stable_entity_id,
    )
    from archon_search.store import SearchStore

    store = SearchStore(db_path)
    await store.connect()
    try:
        await store.ensure_collection(collection, _STUB_EMBEDDING_DIM)
        doc_id = hashlib.sha256(collection.encode()).hexdigest()
        chunks = [
            ChunkRecord(
                doc_id=doc_id,
                chunk_id=f"{doc_id}-000000",
                text="Kubernetes is managed by Alice",
                vector=[0.0] * _STUB_EMBEDDING_DIM,
                source_path=f"/fake/{collection}.txt",
                indexed_at=normalize_iso_utc(datetime.now(timezone.utc)),
            )
        ]
        await store.ingest_chunks(collection, chunks)
        meta = CollectionMeta(
            name=collection,
            active_embedding_model="stub-model",
            doc_count=1,
            chunk_count=1,
            namespace=ns,
        )
        await store.update_collection_meta(meta)
    finally:
        await store.disconnect()

    node_system_id = make_stable_entity_id("system", "Kubernetes")
    node_person_id = make_stable_entity_id("person", "Alice")

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        await gs.ensure_graph_tables(collection, ns=ns)
        doc_id = hashlib.sha256(collection.encode()).hexdigest()
        nodes = [
            GraphNode(
                id=node_system_id,
                entity_name="Kubernetes",
                entity_type=EntityType.system,
                source_doc_id=doc_id,
                collection_name=collection,
            ),
            GraphNode(
                id=node_person_id,
                entity_name="Alice",
                entity_type=EntityType.person,
                source_doc_id=doc_id,
                collection_name=collection,
            ),
        ]
        await gs.write_graph(collection, nodes, [], ns=ns)
        mentions = [
            GraphMention(entity_id=node_system_id, chunk_id=f"{doc_id}-000000", doc_id=doc_id),
            GraphMention(entity_id=node_person_id, chunk_id=f"{doc_id}-000000", doc_id=doc_id),
        ]
        await gs.write_mentions(collection, mentions, ns=ns)
    finally:
        await gs.disconnect()


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


def test_graph_response_includes_entity_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /graph/{col} JSON response includes entity_type on every node with a valid EntityType value.

    Seeds two nodes (system='Kubernetes', person='Alice') and asserts:
    - Each node response has an entity_type field
    - entity_type values are in the known valid set
    - The specific expected types are present
    """
    _install_spacy_stub(monkeypatch)
    col = "e2j-be1-entity-type-single"

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        db_path = cfg.db_path
        asyncio.run(_seed_graph_with_typed_nodes(db_path, col))

        resp = client.get(f"/graph/{col}", headers=_auth(api_key))
        assert resp.status_code == 200, f"GET /graph/{col} failed: {resp.status_code} {resp.text}"
        data = resp.json()

        assert "nodes" in data
        nodes = data["nodes"]
        assert len(nodes) > 0, "Expected at least one node in graph response"

        # Every node must have entity_type present and valid
        for node in nodes:
            assert "entity_type" in node, (
                f"Node {node.get('entity_id')} is missing entity_type field. "
                f"Node keys: {list(node.keys())}"
            )
            assert node["entity_type"] in _VALID_ENTITY_TYPES, (
                f"Node {node.get('entity_id')} has invalid entity_type={node['entity_type']!r}. "
                f"Valid: {_VALID_ENTITY_TYPES}"
            )

        # Check specific expected entity types are present
        entity_types_seen = {n["entity_type"] for n in nodes}
        assert "system" in entity_types_seen, (
            f"Expected 'system' entity_type for Kubernetes node, got: {entity_types_seen}"
        )
        assert "person" in entity_types_seen, (
            f"Expected 'person' entity_type for Alice node, got: {entity_types_seen}"
        )


def test_cross_collection_graph_response_includes_entity_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /graph/cross-collection returns 200 with entity_type on every merged node.

    Seeds two collections each with two typed nodes and asserts:
    - HTTP 200 (not a 500 Pydantic validation error)
    - Every node in the cross-collection response has entity_type
    - entity_type values are valid EntityType values
    """
    _install_spacy_stub(monkeypatch)
    col_a = "e2j-be1-cross-col-a"
    col_b = "e2j-be1-cross-col-b"

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        db_path = cfg.db_path
        asyncio.run(_seed_graph_with_typed_nodes(db_path, col_a))
        asyncio.run(_seed_graph_with_typed_nodes(db_path, col_b))

        resp = client.get(
            f"/graph/cross-collection?collections={col_a},{col_b}",
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"GET /graph/cross-collection failed with {resp.status_code} "
            f"(expected 200, not 500 Pydantic error): {resp.text}"
        )
        data = resp.json()

        assert "nodes" in data
        nodes = data["nodes"]
        assert len(nodes) > 0, "Expected at least one merged node in cross-collection response"

        # Every node must have entity_type present and valid
        for node in nodes:
            assert "entity_type" in node, (
                f"Merged node {node.get('entity_id')} is missing entity_type field. "
                f"Node keys: {list(node.keys())}"
            )
            assert node["entity_type"] in _VALID_ENTITY_TYPES, (
                f"Merged node {node.get('entity_id')} has invalid entity_type={node['entity_type']!r}. "
                f"Valid: {_VALID_ENTITY_TYPES}"
            )
