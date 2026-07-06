"""Tests for BE-5: CommunityBuilder.build(seed) deterministic Leiden seeding."""
import inspect

import pytest

from archon_search.community_builder import (
    CommunityBuilder,
    _cluster_with_size_limit,
    _run_leiden_partition_sync,
)


class TestCommunityBuilderSeedSignatures:
    """Unit tests for seed parameter signatures."""

    def test_community_builder_build_accepts_seed_kwarg(self) -> None:
        """CommunityBuilder.build() signature accepts seed: int | None = None."""
        sig = inspect.signature(CommunityBuilder.build)
        assert "seed" in sig.parameters, "build() missing seed parameter"
        seed_param = sig.parameters["seed"]
        assert seed_param.default is None, "seed default must be None"
        # Verify it's keyword-only
        assert seed_param.kind == inspect.Parameter.KEYWORD_ONLY, "seed must be keyword-only"

    def test_run_leiden_partition_sync_accepts_seed(self) -> None:
        """_run_leiden_partition_sync() signature accepts seed: int | None = None."""
        sig = inspect.signature(_run_leiden_partition_sync)
        assert "seed" in sig.parameters, "_run_leiden_partition_sync() missing seed parameter"
        seed_param = sig.parameters["seed"]
        assert seed_param.default is None, "seed default must be None"

    def test_cluster_with_size_limit_accepts_seed(self) -> None:
        """_cluster_with_size_limit() signature accepts seed: int | None = None."""
        sig = inspect.signature(_cluster_with_size_limit)
        assert "seed" in sig.parameters, "_cluster_with_size_limit() missing seed parameter"
        seed_param = sig.parameters["seed"]
        assert seed_param.default is None, "seed default must be None"


@pytest.mark.integration
class TestCommunityBuilderDeterminism:
    """Integration tests for deterministic community building with seed=42."""

    def test_community_builder_deterministic_with_seed(self, tmp_path) -> None:
        """Two build(seed=42) calls on identical graph produce identical representative_chunk_ids.

        This test:
        1. Creates a simple test graph with 3 nodes
        2. Calls CommunityBuilder.build(collection, ns, seed=42) twice
        3. Asserts representative_chunk_ids are byte-identical (S9)

        Note: community_id uses uuid4() and built_at is wall-clock, so those won't
        be identical. Only representative_chunk_ids (sorted list) must be identical.
        Requires leidenalg/igraph (graph extras).
        """
        pytest.importorskip("leidenalg")

        import asyncio

        from archon_search.config import GraphConfig
        from archon_search.graph_store import GraphStore
        from archon_search.graph_types import GraphEdge, GraphNode
        from archon_search.store import SearchStore
        from archon_search.community_builder import CommunityBuilder

        # Config with graph enabled
        graph_config = GraphConfig(
            enabled=True,
            leiden_resolution=1.0,
            max_community_size=10,
            community_summary_chunks=3,
            max_global_candidates=100,
        )

        from archon_search.graph_types import EntityType, RelationshipType
        from archon_search.graph_types import (
            make_stable_entity_id,
            make_stable_edge_id,
        )

        # Create a simple test graph (3 nodes in a chain)
        node_a = GraphNode(
            id=make_stable_entity_id(EntityType.concept.value, "entity_a"),
            entity_name="entity_a",
            entity_type=EntityType.concept,
            source_doc_id="doc_1",
            collection_name="test_collection",
        )
        node_b = GraphNode(
            id=make_stable_entity_id(EntityType.concept.value, "entity_b"),
            entity_name="entity_b",
            entity_type=EntityType.concept,
            source_doc_id="doc_1",
            collection_name="test_collection",
        )
        node_c = GraphNode(
            id=make_stable_entity_id(EntityType.concept.value, "entity_c"),
            entity_name="entity_c",
            entity_type=EntityType.concept,
            source_doc_id="doc_1",
            collection_name="test_collection",
        )
        nodes = [node_a, node_b, node_c]

        edges = [
            GraphEdge(
                id=make_stable_edge_id(node_a.id, node_b.id, RelationshipType.uses.value),
                source_node_id=node_a.id,
                target_node_id=node_b.id,
                relationship_type=RelationshipType.uses,
                source_doc_id="doc_1",
            ),
            GraphEdge(
                id=make_stable_edge_id(node_b.id, node_c.id, RelationshipType.uses.value),
                source_node_id=node_b.id,
                target_node_id=node_c.id,
                relationship_type=RelationshipType.uses,
                source_doc_id="doc_1",
            ),
        ]

        # First build
        db_path_1 = str(tmp_path / "store_1.db")
        graph_store_1 = GraphStore(db_path_1)

        async def setup_and_build_1():
            await graph_store_1.connect()
            try:
                await graph_store_1.ensure_graph_tables("test_collection", ns="default")
                await graph_store_1.write_graph("test_collection", nodes, edges, ns="default")
                builder = CommunityBuilder(graph_store_1, graph_config)
                return await builder.build("test_collection", "default", seed=42)
            finally:
                await graph_store_1.disconnect()

        communities_1 = asyncio.run(setup_and_build_1())

        # Extract representative_chunk_ids and sort for comparison
        rep_ids_1_all = sorted(
            [rid for c in communities_1 for rid in c.representative_chunk_ids]
        )

        # Second build (identical setup)
        db_path_2 = str(tmp_path / "store_2.db")
        graph_store_2 = GraphStore(db_path_2)

        async def setup_and_build_2():
            await graph_store_2.connect()
            try:
                await graph_store_2.ensure_graph_tables("test_collection", ns="default")
                await graph_store_2.write_graph("test_collection", nodes, edges, ns="default")
                builder = CommunityBuilder(graph_store_2, graph_config)
                return await builder.build("test_collection", "default", seed=42)
            finally:
                await graph_store_2.disconnect()

        communities_2 = asyncio.run(setup_and_build_2())
        rep_ids_2_all = sorted(
            [rid for c in communities_2 for rid in c.representative_chunk_ids]
        )

        # Assert byte-identical lists
        assert rep_ids_1_all == rep_ids_2_all, (
            f"Representative chunk IDs differ between two seed=42 builds.\n"
            f"Build 1: {rep_ids_1_all}\n"
            f"Build 2: {rep_ids_2_all}"
        )

    def test_community_builder_deterministic_with_oversized_communities(
        self, tmp_path
    ) -> None:
        """Oversized community splitting is deterministic with seed=42.

        Creates a graph configured to trigger the max_community_size split path,
        runs build(seed=42) twice, and asserts representative_chunk_ids match.
        Requires leidenalg/igraph (graph extras).
        """
        pytest.importorskip("leidenalg")

        import asyncio

        from archon_search.config import GraphConfig
        from archon_search.graph_store import GraphStore
        from archon_search.graph_types import GraphEdge, GraphNode
        from archon_search.store import SearchStore
        from archon_search.community_builder import CommunityBuilder

        # Config with small max_community_size to trigger splitting
        graph_config = GraphConfig(
            enabled=True,
            leiden_resolution=1.0,
            max_community_size=2,  # Triggers split: >2 entities per community
            community_summary_chunks=3,
            max_global_candidates=100,
        )

        from archon_search.graph_types import EntityType, RelationshipType
        from archon_search.graph_types import (
            make_stable_entity_id,
            make_stable_edge_id,
        )

        # Create a larger graph (6+ nodes) that Leiden will initially group together
        nodes = [
            GraphNode(
                id=make_stable_entity_id(EntityType.concept.value, f"entity_{i}"),
                entity_name=f"entity_{i}",
                entity_type=EntityType.concept,
                source_doc_id="doc_1",
                collection_name="test_collection",
            )
            for i in range(6)
        ]

        # Create a path-like graph (linear connections)
        edges = [
            GraphEdge(
                id=make_stable_edge_id(nodes[i].id, nodes[i + 1].id, RelationshipType.uses.value),
                source_node_id=nodes[i].id,
                target_node_id=nodes[i + 1].id,
                relationship_type=RelationshipType.uses,
                source_doc_id="doc_1",
            )
            for i in range(5)
        ]

        # First build
        db_path_1 = str(tmp_path / "store_oversized_1.db")
        graph_store_1 = GraphStore(db_path_1)

        async def setup_and_build_1():
            await graph_store_1.connect()
            try:
                await graph_store_1.ensure_graph_tables("test_collection", ns="default")
                await graph_store_1.write_graph("test_collection", nodes, edges, ns="default")
                builder = CommunityBuilder(graph_store_1, graph_config)
                return await builder.build("test_collection", "default", seed=42)
            finally:
                await graph_store_1.disconnect()

        communities_1 = asyncio.run(setup_and_build_1())
        rep_ids_1_all = sorted(
            [rid for c in communities_1 for rid in c.representative_chunk_ids]
        )

        # Second build
        db_path_2 = str(tmp_path / "store_oversized_2.db")
        graph_store_2 = GraphStore(db_path_2)

        async def setup_and_build_2():
            await graph_store_2.connect()
            try:
                await graph_store_2.ensure_graph_tables("test_collection", ns="default")
                await graph_store_2.write_graph("test_collection", nodes, edges, ns="default")
                builder = CommunityBuilder(graph_store_2, graph_config)
                return await builder.build("test_collection", "default", seed=42)
            finally:
                await graph_store_2.disconnect()

        communities_2 = asyncio.run(setup_and_build_2())
        rep_ids_2_all = sorted(
            [rid for c in communities_2 for rid in c.representative_chunk_ids]
        )

        # Assert determinism
        assert rep_ids_1_all == rep_ids_2_all, (
            f"Oversized community splitting is non-deterministic.\n"
            f"Build 1: {rep_ids_1_all}\n"
            f"Build 2: {rep_ids_2_all}"
        )

    def test_community_builder_seed_none_still_builds_communities(self, tmp_path) -> None:
        """build() with seed=None (default) still completes and returns communities.

        Verifies the non-deterministic path works — seed=None preserves existing behavior.
        Requires leidenalg/igraph (graph extras).
        """
        pytest.importorskip("leidenalg")

        import asyncio

        from archon_search.config import GraphConfig
        from archon_search.graph_store import GraphStore
        from archon_search.graph_types import EntityType, GraphEdge, GraphNode, RelationshipType
        from archon_search.graph_types import make_stable_edge_id, make_stable_entity_id
        from archon_search.community_builder import CommunityBuilder

        graph_config = GraphConfig(
            enabled=True,
            leiden_resolution=1.0,
            max_community_size=10,
            community_summary_chunks=3,
            max_global_candidates=100,
        )

        node_a = GraphNode(
            id=make_stable_entity_id(EntityType.concept.value, "alpha"),
            entity_name="alpha",
            entity_type=EntityType.concept,
            source_doc_id="doc_1",
            collection_name="test_collection",
        )
        node_b = GraphNode(
            id=make_stable_entity_id(EntityType.concept.value, "beta"),
            entity_name="beta",
            entity_type=EntityType.concept,
            source_doc_id="doc_1",
            collection_name="test_collection",
        )
        edges = [
            GraphEdge(
                id=make_stable_edge_id(node_a.id, node_b.id, RelationshipType.uses.value),
                source_node_id=node_a.id,
                target_node_id=node_b.id,
                relationship_type=RelationshipType.uses,
                source_doc_id="doc_1",
            ),
        ]

        db_path = str(tmp_path / "store_seed_none.db")
        graph_store = GraphStore(db_path)

        async def setup_and_build():
            await graph_store.connect()
            try:
                await graph_store.ensure_graph_tables("test_collection", ns="default")
                await graph_store.write_graph("test_collection", [node_a, node_b], edges, ns="default")
                builder = CommunityBuilder(graph_store, graph_config)
                # seed=None is the default — verifies the non-deterministic path still works
                return await builder.build("test_collection", "default")
            finally:
                await graph_store.disconnect()

        communities = asyncio.run(setup_and_build())

        assert len(communities) >= 1, "seed=None build must return at least one community"
        all_entity_ids = {eid for c in communities for eid in c.entity_ids}
        assert node_a.id in all_entity_ids, "node_a must appear in some community"
        assert node_b.id in all_entity_ids, "node_b must appear in some community"
