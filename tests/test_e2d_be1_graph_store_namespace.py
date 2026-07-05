"""Tests for E2d BE-1 — namespace-scoped GraphStore table names and validation guards.

Covers:
- `_nodes_table_name` includes namespace with double-underscore separator
- `_validate_collection` rejects trailing `_`, leading `_`, internal `__`
- `_validate_namespace` rejects trailing `_`, leading `_`, internal `__`
- Graph tables are isolated across namespaces (real LanceDB in tmp_path)
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from archon_search.constants import _validate_namespace
from archon_search.graph_store import GraphStore
from archon_search.graph_types import EntityType, GraphNode, make_stable_entity_id


# ---------------------------------------------------------------------------
# Unit: table name format
# ---------------------------------------------------------------------------


def test_nodes_table_name_includes_namespace() -> None:
    """`_nodes_table_name('docs', 'ns_a')` returns `_archon_graph_ns_a__docs_nodes`.

    Note: ns is LAST in the call — `_nodes_table_name("docs", "ns_a")`.
    """
    store = GraphStore("/tmp/fake-db-ns-table-name")
    result = store._nodes_table_name("docs", "ns_a")
    assert result == "_archon_graph_ns_a__docs_nodes"


def test_edges_table_name_includes_namespace():
    store = GraphStore.__new__(GraphStore)
    assert store._edges_table_name("docs", "ns_a") == "_archon_graph_ns_a__docs_edges"


def test_communities_table_name_includes_namespace():
    store = GraphStore.__new__(GraphStore)
    assert store._communities_table_name("docs", "ns_a") == "_archon_graph_ns_a__docs_communities"


def test_mentions_table_name_includes_namespace():
    store = GraphStore.__new__(GraphStore)
    assert store._mentions_table_name("docs", "ns_a") == "_archon_graph_ns_a__docs_mentions"


def test_get_all_mentions_ns_is_keyword_only():
    """Verify that ns is keyword-only for get_all_mentions (*, ns sentinel)."""
    import inspect
    sig = inspect.signature(GraphStore.get_all_mentions)
    ns_param = sig.parameters["ns"]
    assert ns_param.kind == inspect.Parameter.KEYWORD_ONLY, (
        "get_all_mentions: ns must be keyword-only (declared after * sentinel)"
    )


# ---------------------------------------------------------------------------
# Unit: _validate_collection guards
# ---------------------------------------------------------------------------


def test_validate_collection_rejects_trailing_underscore() -> None:
    """`docs_` (trailing underscore) must raise ValueError."""
    store = GraphStore("/tmp/fake-db-vc-trailing")
    with pytest.raises(ValueError):
        store._validate_collection("docs_")


def test_validate_collection_rejects_leading_underscore() -> None:
    """`_internal` (leading underscore) must raise ValueError."""
    store = GraphStore("/tmp/fake-db-vc-leading")
    with pytest.raises(ValueError):
        store._validate_collection("_internal")


def test_validate_collection_rejects_internal_double_underscore() -> None:
    """`my__col` (internal double underscore) must raise ValueError."""
    store = GraphStore("/tmp/fake-db-vc-double")
    with pytest.raises(ValueError):
        store._validate_collection("my__col")


def test_validate_collection_accepts_valid_names() -> None:
    """`docs`, `my-col`, `col123` must all pass validation without raising."""
    store = GraphStore("/tmp/fake-db-vc-valid")
    store._validate_collection("docs")
    store._validate_collection("my-col")
    store._validate_collection("col123")


# ---------------------------------------------------------------------------
# Unit: _validate_namespace guards
# ---------------------------------------------------------------------------


def test_validate_namespace_rejects_trailing_underscore() -> None:
    """`tenant_` (trailing underscore) must raise ValueError."""
    with pytest.raises(ValueError):
        _validate_namespace("tenant_")


def test_validate_namespace_rejects_leading_underscore() -> None:
    """`_tenant` (leading underscore) must raise ValueError."""
    with pytest.raises(ValueError):
        _validate_namespace("_tenant")


def test_validate_namespace_rejects_internal_double_underscore() -> None:
    """`ten__ant` (internal double underscore) must raise ValueError."""
    with pytest.raises(ValueError):
        _validate_namespace("ten__ant")


def test_validate_namespace_accepts_valid_names() -> None:
    """`tenant-a`, `tenant_a`, `ns123` must all pass without raising."""
    _validate_namespace("tenant-a")
    _validate_namespace("tenant_a")
    _validate_namespace("ns123")


# ---------------------------------------------------------------------------
# Integration: namespace isolation (real LanceDB in tmp_path)
# ---------------------------------------------------------------------------


def test_graph_tables_isolated_across_namespaces(tmp_path: Path) -> None:
    """ns_a/docs and ns_b/docs hold DIFFERENT data; neither can read the other's.

    Two-sided isolation check:
    - ns_a reads its own data AND does NOT see ns_b data
    - ns_b reads its own data AND does NOT see ns_a data
    """

    def _node(name: str) -> GraphNode:
        return GraphNode(
            id=make_stable_entity_id(EntityType.concept.value, name),
            entity_name=name,
            entity_type=EntityType.concept,
            source_doc_id=f"doc-{name}",
            collection_name="docs",
        )

    node_a = _node("EntityA")
    node_b = _node("EntityB")

    async def _run() -> None:
        store = GraphStore(tmp_path)
        await store.connect()

        # Ensure tables for both namespaces
        await store.ensure_graph_tables("docs", ns="ns_a")
        await store.ensure_graph_tables("docs", ns="ns_b")

        # Write DIFFERENT data to each namespace
        await store.write_graph("docs", [node_a], [], ns="ns_a")
        await store.write_graph("docs", [node_b], [], ns="ns_b")

        # Read back from each namespace
        nodes_a = await store.get_all_nodes("docs", ns="ns_a")
        nodes_b = await store.get_all_nodes("docs", ns="ns_b")

        names_a = {n.entity_name for n in nodes_a}
        names_b = {n.entity_name for n in nodes_b}

        # ns_a reads its own data
        assert "EntityA" in names_a, f"ns_a must contain EntityA; got {names_a}"

        # ns_b reads its own data
        assert "EntityB" in names_b, f"ns_b must contain EntityB; got {names_b}"

        # ns_a writes do NOT appear in ns_b reads
        assert "EntityA" not in names_b, (
            f"EntityA from ns_a must not appear in ns_b reads; got {names_b}"
        )

        # ns_b writes do NOT appear in ns_a reads
        assert "EntityB" not in names_a, (
            f"EntityB from ns_b must not appear in ns_a reads; got {names_a}"
        )

        await store.disconnect()

    asyncio.run(_run())
