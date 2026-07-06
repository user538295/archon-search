"""Unit and integration tests for AliasLoader — E2f BE-6.

Tests:
- test_alias_loader_parses_toml_pairs: TOML with "K8s" = "Kubernetes" + mock
  find_nodes_by_name → one GraphEdge(extraction_method="manual") + skip-set
- test_alias_loader_unresolvable_name_logs_warning_and_skips: one name resolves
  to zero nodes → pair skipped with WARNING
- test_alias_loader_missing_file_returns_empty_with_warning: non-existent path
  logs WARNING, returns ([], set())
- test_alias_loader_invalid_toml_logs_warning_and_returns_empty: malformed TOML
  logs WARNING, returns ([], set())
- test_alias_loader_self_alias_produces_no_edge: TOML "K8s" = "k8s" resolves to
  the same node → self-loop guard fires → edges == []
- test_alias_loader_non_utf8_file_returns_empty_with_warning: non-UTF-8 bytes →
  UnicodeDecodeError branch → WARNING, returns ([], set())
- test_alias_file_creates_manual_synonym_edge (integration): configure alias_file
  pointing to a temp TOML file; assert exactly one synonym_of edge exists with
  extraction_method='manual'
- test_synonym_enrichment_alias_load_failure_falls_back_to_ann: AliasLoader.load
  raises RuntimeError → SynonymDetector.detect still called with skip_pairs=set(),
  write_graph still called, exception does not propagate
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.config import GraphConfig
from archon_search.graph_types import (
    EntityType,
    GraphEdge,
    GraphNode,
    RelationshipType,
    make_stable_edge_id,
    make_stable_entity_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_node(
    name: str,
    entity_type: EntityType = EntityType.concept,
    collection: str = "test-col",
) -> GraphNode:
    return GraphNode(
        id=make_stable_entity_id(entity_type.value, name),
        entity_name=name,
        entity_type=entity_type,
        source_doc_id="doc-test",
        collection_name=collection,
    )


def _make_graph_store_mock(
    find_nodes_result: list[GraphNode] | None = None,
) -> MagicMock:
    store = MagicMock()
    store.write_graph = AsyncMock(return_value=None)
    store.get_all_nodes = AsyncMock(return_value=[])
    store.find_nodes_by_name = AsyncMock(
        return_value=find_nodes_result if find_nodes_result is not None else []
    )
    return store


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_alias_loader_parses_toml_pairs(tmp_path: Path) -> None:
    """TOML file with "K8s" = "Kubernetes" + mock find_nodes_by_name returning one node
    each → produces one GraphEdge with extraction_method="manual" and a skip-set
    containing the resolved entity-ID pair (canonically ordered).
    """
    from archon_search.alias_loader import AliasLoader

    alias_file = tmp_path / "aliases.toml"
    alias_file.write_text('"K8s" = "Kubernetes"\n', encoding="utf-8")

    node_k8s = _make_node("K8s")
    node_kube = _make_node("Kubernetes")

    store = _make_graph_store_mock()
    store.find_nodes_by_name = AsyncMock(return_value=[node_k8s, node_kube])

    cfg = GraphConfig(alias_file=str(alias_file))
    loader = AliasLoader(config=cfg, graph_store=store)
    edges, skip_pairs = asyncio.run(loader.load("test-col", "default"))

    assert len(edges) == 1
    edge = edges[0]
    assert edge.relationship_type == RelationshipType.synonym_of
    assert edge.extraction_method == "manual"

    src_id = min(node_k8s.id, node_kube.id)
    tgt_id = max(node_k8s.id, node_kube.id)
    assert edge.source_node_id == src_id
    assert edge.target_node_id == tgt_id

    expected_skip = {(src_id, tgt_id)}
    assert skip_pairs == expected_skip


def test_alias_loader_unresolvable_name_logs_warning_and_skips(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Alias pair where one name resolves to zero nodes is skipped with WARNING."""
    from archon_search.alias_loader import AliasLoader

    alias_file = tmp_path / "aliases.toml"
    alias_file.write_text('"Ghost" = "Kubernetes"\n', encoding="utf-8")

    # find_nodes_by_name returns only the Kubernetes node; Ghost is absent
    node_kube = _make_node("Kubernetes")
    store = _make_graph_store_mock()
    store.find_nodes_by_name = AsyncMock(return_value=[node_kube])

    cfg = GraphConfig(alias_file=str(alias_file))
    loader = AliasLoader(config=cfg, graph_store=store)

    with caplog.at_level(logging.WARNING, logger="archon_search.alias_loader"):
        edges, skip_pairs = asyncio.run(loader.load("test-col", "default"))

    assert edges == []
    assert skip_pairs == set()
    assert any("Ghost" in msg or "ghost" in msg.lower() for msg in caplog.messages), (
        f"Expected WARNING mentioning 'Ghost'; got: {caplog.messages}"
    )


def test_alias_loader_missing_file_returns_empty_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Non-existent alias_file path logs WARNING and returns ([], set())."""
    from archon_search.alias_loader import AliasLoader

    store = _make_graph_store_mock()
    cfg = GraphConfig(alias_file=str(tmp_path / "does-not-exist.toml"))
    loader = AliasLoader(config=cfg, graph_store=store)

    with caplog.at_level(logging.WARNING, logger="archon_search.alias_loader"):
        edges, skip_pairs = asyncio.run(loader.load("test-col", "default"))

    assert edges == []
    assert skip_pairs == set()
    assert caplog.messages, "Expected at least one WARNING to be logged"


def test_alias_loader_invalid_toml_logs_warning_and_returns_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Malformed TOML logs WARNING and returns ([], set())."""
    from archon_search.alias_loader import AliasLoader

    alias_file = tmp_path / "bad.toml"
    alias_file.write_text("this is not = [valid toml\n", encoding="utf-8")

    store = _make_graph_store_mock()
    cfg = GraphConfig(alias_file=str(alias_file))
    loader = AliasLoader(config=cfg, graph_store=store)

    with caplog.at_level(logging.WARNING, logger="archon_search.alias_loader"):
        edges, skip_pairs = asyncio.run(loader.load("test-col", "default"))

    assert edges == []
    assert skip_pairs == set()
    assert caplog.messages, "Expected at least one WARNING to be logged"


def test_alias_loader_multiple_nodes_same_type_creates_all_pairs(
    tmp_path: Path,
) -> None:
    """When a name resolves to multiple nodes, edges are created for same-type pairs only."""
    from archon_search.alias_loader import AliasLoader

    alias_file = tmp_path / "aliases.toml"
    alias_file.write_text('"K8s" = "Kubernetes"\n', encoding="utf-8")

    # Two K8s concept nodes and one Kubernetes system node (different type → no pair)
    node_k8s_concept = GraphNode(
        id=make_stable_entity_id("concept", "K8s"),
        entity_name="K8s",
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name="test-col",
    )
    node_k8s_system = GraphNode(
        id=make_stable_entity_id("system", "K8s"),
        entity_name="K8s",
        entity_type=EntityType.system,
        source_doc_id="doc-2",
        collection_name="test-col",
    )
    node_kube_concept = GraphNode(
        id=make_stable_entity_id("concept", "Kubernetes"),
        entity_name="Kubernetes",
        entity_type=EntityType.concept,
        source_doc_id="doc-3",
        collection_name="test-col",
    )

    store = _make_graph_store_mock()
    store.find_nodes_by_name = AsyncMock(
        return_value=[node_k8s_concept, node_k8s_system, node_kube_concept]
    )

    cfg = GraphConfig(alias_file=str(alias_file))
    loader = AliasLoader(config=cfg, graph_store=store)
    edges, skip_pairs = asyncio.run(loader.load("test-col", "default"))

    # Only the concept-concept pair qualifies (same entity_type)
    assert len(edges) == 1
    edge = edges[0]
    src_id = min(node_k8s_concept.id, node_kube_concept.id)
    tgt_id = max(node_k8s_concept.id, node_kube_concept.id)
    assert edge.source_node_id == src_id
    assert edge.target_node_id == tgt_id
    assert skip_pairs == {(src_id, tgt_id)}


def test_alias_loader_self_alias_produces_no_edge(tmp_path: Path) -> None:
    """TOML "K8s" = "k8s" where both names resolve to the same node → self-loop guard
    fires → edges == [], skip_pairs == set().
    """
    from archon_search.alias_loader import AliasLoader

    alias_file = tmp_path / "aliases.toml"
    alias_file.write_text('"K8s" = "k8s"\n', encoding="utf-8")

    # One node whose name matches both name_a.lower() == "k8s" and name_b.lower() == "k8s"
    shared_node = GraphNode(
        id=make_stable_entity_id("concept", "k8s"),
        entity_name="k8s",
        entity_type=EntityType.concept,
        source_doc_id="doc-test",
        collection_name="test-col",
    )

    store = _make_graph_store_mock()
    store.find_nodes_by_name = AsyncMock(return_value=[shared_node])

    cfg = GraphConfig(alias_file=str(alias_file))
    loader = AliasLoader(config=cfg, graph_store=store)
    edges, skip_pairs = asyncio.run(loader.load("test-col", "default"))

    assert edges == [], f"Expected no edges (self-loop guard), got: {edges}"
    assert skip_pairs == set()


def test_alias_loader_non_utf8_file_returns_empty_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """File with non-UTF-8 bytes triggers UnicodeDecodeError branch → WARNING, returns ([], set())."""
    from archon_search.alias_loader import AliasLoader

    alias_file = tmp_path / "bad-encoding.toml"
    alias_file.write_bytes(b"\x80\x81\x82")

    store = _make_graph_store_mock()
    cfg = GraphConfig(alias_file=str(alias_file))
    loader = AliasLoader(config=cfg, graph_store=store)

    with caplog.at_level(logging.WARNING, logger="archon_search.alias_loader"):
        edges, skip_pairs = asyncio.run(loader.load("test-col", "default"))

    assert edges == []
    assert skip_pairs == set()
    assert caplog.messages, "Expected at least one WARNING to be logged"


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_alias_file_creates_manual_synonym_edge(tmp_path: Path) -> None:
    """End-to-end: configure alias_file pointing to a temp TOML file; run AliasLoader.load()
    against a real GraphStore; assert exactly one synonym_of edge with
    extraction_method='manual'.
    """
    from archon_search.alias_loader import AliasLoader
    from archon_search.graph_store import GraphStore

    alias_file = tmp_path / "aliases.toml"
    alias_file.write_text('"K8s" = "Kubernetes"\n', encoding="utf-8")

    db_path = str(tmp_path / "test-graph-db")
    collection = "alias-test"
    ns = "default"

    node_k8s = GraphNode(
        id=make_stable_entity_id("system", "K8s"),
        entity_name="K8s",
        entity_type=EntityType.system,
        source_doc_id="doc-k8s",
        collection_name=collection,
    )
    node_kube = GraphNode(
        id=make_stable_entity_id("system", "Kubernetes"),
        entity_name="Kubernetes",
        entity_type=EntityType.system,
        source_doc_id="doc-kube",
        collection_name=collection,
    )

    async def _run() -> tuple[list[GraphEdge], set[tuple[str, str]]]:
        gs = GraphStore(db_path=db_path)
        await gs.connect()
        try:
            # Ensure tables exist before writing nodes
            await gs.ensure_graph_tables(collection, ns)
            # Write both nodes into the graph store
            await gs.write_graph(collection, [node_k8s, node_kube], [], ns=ns)

            cfg = GraphConfig(alias_file=str(alias_file))
            loader = AliasLoader(config=cfg, graph_store=gs)
            return await loader.load(collection, ns)
        finally:
            await gs.disconnect()

    edges, skip_pairs = asyncio.run(_run())

    # Exactly one synonym_of edge for the pair
    synonym_edges = [e for e in edges if e.relationship_type == RelationshipType.synonym_of]
    assert len(synonym_edges) == 1, (
        f"Expected exactly 1 synonym_of edge, got {len(synonym_edges)}: {synonym_edges}"
    )

    edge = synonym_edges[0]
    assert edge.extraction_method == "manual", (
        f"Expected extraction_method='manual', got {edge.extraction_method!r}"
    )
    # Confirm skip-set has the canonical pair
    src_id = min(node_k8s.id, node_kube.id)
    tgt_id = max(node_k8s.id, node_kube.id)
    assert (src_id, tgt_id) in skip_pairs


# ---------------------------------------------------------------------------
# Orchestrator wiring test
# ---------------------------------------------------------------------------


def test_synonym_enrichment_uses_alias_loader_skip_pairs(tmp_path: Path) -> None:
    """_run_synonym_enrichment calls AliasLoader.load() and passes skip_pairs to
    SynonymDetector.detect(); both alias and ANN edges are written together via
    write_graph().
    """
    from archon_search.config import GraphConfig, MaintenanceConfig
    from archon_search.jobs.maintenance_loop import MaintenanceLoop

    # Build two distinct edges to tell alias vs ANN results apart.
    node_a = _make_node("K8s")
    node_b = _make_node("Kubernetes")
    src_id = min(node_a.id, node_b.id)
    tgt_id = max(node_a.id, node_b.id)
    alias_edge = GraphEdge(
        id=make_stable_edge_id(src_id, tgt_id, RelationshipType.synonym_of.value),
        source_node_id=src_id,
        target_node_id=tgt_id,
        relationship_type=RelationshipType.synonym_of,
        source_doc_id="alias-loader",
        extraction_method="manual",
    )
    alias_skip = {(src_id, tgt_id)}

    node_c = _make_node("docker")
    node_d = _make_node("Docker")
    ann_src = min(node_c.id, node_d.id)
    ann_tgt = max(node_c.id, node_d.id)
    ann_edge = GraphEdge(
        id=make_stable_edge_id(ann_src, ann_tgt, RelationshipType.synonym_of.value),
        source_node_id=ann_src,
        target_node_id=ann_tgt,
        relationship_type=RelationshipType.synonym_of,
        source_doc_id="synonym-detector",
        extraction_method="embedding",
    )

    mock_graph_store = _make_graph_store_mock()

    graph_cfg = GraphConfig()
    loop = MaintenanceLoop(
        job_store=MagicMock(),
        search_store=MagicMock(),
        config=MaintenanceConfig(),
        data_dir=tmp_path,
        graph_store=mock_graph_store,
        graph_config=graph_cfg,
    )

    with (
        patch(
            "archon_search.alias_loader.AliasLoader.load",
            new_callable=AsyncMock,
            return_value=([alias_edge], alias_skip),
        ) as mock_load,
        patch(
            "archon_search.synonym_detector.SynonymDetector.detect",
            new_callable=AsyncMock,
            return_value=[ann_edge],
        ) as mock_detect,
    ):
        asyncio.run(loop._run_synonym_enrichment("test-col", "default"))

    # AliasLoader.load was called with the correct positional args.
    mock_load.assert_called_once_with("test-col", "default")

    # SynonymDetector.detect received the skip_pairs from AliasLoader.
    detect_call_kwargs = mock_detect.call_args
    assert detect_call_kwargs is not None
    passed_skip_pairs = detect_call_kwargs.kwargs.get("skip_pairs")
    assert passed_skip_pairs == alias_skip, (
        f"Expected skip_pairs={alias_skip!r}, got {passed_skip_pairs!r}"
    )

    # write_graph was called once with both alias_edge and ann_edge.
    mock_graph_store.write_graph.assert_called_once()
    write_call_args = mock_graph_store.write_graph.call_args
    written_edges = write_call_args.args[2] if len(write_call_args.args) >= 3 else write_call_args.kwargs.get("edges", [])
    assert alias_edge in written_edges, "alias_edge must be written"
    assert ann_edge in written_edges, "ann_edge must be written"


def test_synonym_enrichment_alias_load_failure_falls_back_to_ann(tmp_path: Path) -> None:
    """When AliasLoader.load() raises, _run_synonym_enrichment falls back to ANN-only:
    SynonymDetector.detect() is still called with skip_pairs=set(), write_graph is
    still called with the ANN edge(s), and the exception does not propagate.
    """
    from archon_search.config import GraphConfig, MaintenanceConfig
    from archon_search.jobs.maintenance_loop import MaintenanceLoop

    node_c = _make_node("docker")
    node_d = _make_node("Docker")
    ann_src = min(node_c.id, node_d.id)
    ann_tgt = max(node_c.id, node_d.id)
    ann_edge = GraphEdge(
        id=make_stable_edge_id(ann_src, ann_tgt, RelationshipType.synonym_of.value),
        source_node_id=ann_src,
        target_node_id=ann_tgt,
        relationship_type=RelationshipType.synonym_of,
        source_doc_id="synonym-detector",
        extraction_method="embedding",
    )

    mock_graph_store = _make_graph_store_mock()

    graph_cfg = GraphConfig()
    loop = MaintenanceLoop(
        job_store=MagicMock(),
        search_store=MagicMock(),
        config=MaintenanceConfig(),
        data_dir=tmp_path,
        graph_store=mock_graph_store,
        graph_config=graph_cfg,
    )

    with (
        patch(
            "archon_search.alias_loader.AliasLoader.load",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db error"),
        ),
        patch(
            "archon_search.synonym_detector.SynonymDetector.detect",
            new_callable=AsyncMock,
            return_value=[ann_edge],
        ) as mock_detect,
    ):
        # Must not raise
        asyncio.run(loop._run_synonym_enrichment("test-col", "default"))

    # detect() was still called — with skip_pairs=set() because alias load failed
    mock_detect.assert_called_once()
    passed_skip_pairs = mock_detect.call_args.kwargs.get("skip_pairs")
    assert passed_skip_pairs == set(), (
        f"Expected skip_pairs=set() after alias load failure, got {passed_skip_pairs!r}"
    )

    # write_graph was called with the ANN edge
    mock_graph_store.write_graph.assert_called_once()
    write_call_args = mock_graph_store.write_graph.call_args
    written_edges = write_call_args.args[2] if len(write_call_args.args) >= 3 else write_call_args.kwargs.get("edges", [])
    assert ann_edge in written_edges, "ANN edge must be written even when alias load fails"
