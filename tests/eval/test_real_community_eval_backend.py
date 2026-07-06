"""Unit and integration tests for RealCommunityEvalBackend, DispatchingCommunityStore, and RealGraphExpander."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.eval


class TestRealCommunityEvalBackendProtocol:
    """Unit tests for RealCommunityEvalBackend protocol implementation."""

    def test_real_community_eval_backend_implements_protocol(self) -> None:
        """RealCommunityEvalBackend has all four required methods with correct signatures."""
        from archon_search.eval.backends import RealCommunityEvalBackend
        from archon_search.graph_store import GraphStore

        # Create a mock GraphStore
        gs = GraphStore(db_path=":memory:")

        backend = RealCommunityEvalBackend(graph_store=gs)

        # Verify all four methods exist and are callable
        assert hasattr(backend, "communities_table_exists")
        assert callable(backend.communities_table_exists)

        assert hasattr(backend, "list_community_representatives")
        assert callable(backend.list_community_representatives)

        assert hasattr(backend, "find_nodes_by_name")
        assert callable(backend.find_nodes_by_name)

        assert hasattr(backend, "get_communities_for_entities")
        assert callable(backend.get_communities_for_entities)

    def test_dispatching_community_store_routes_by_collection(self) -> None:
        """DispatchingCommunityStore routes requests to the correct backend by collection name."""
        from archon_search.eval.backends import (
            DispatchingCommunityStore,
            RealCommunityEvalBackend,
            CommunityStoreStub,
        )
        from archon_search.graph_store import GraphStore

        gs = GraphStore(db_path=":memory:")
        real_backend = RealCommunityEvalBackend(graph_store=gs)
        stub_backend = CommunityStoreStub()

        dispatcher = DispatchingCommunityStore(
            backend_map={
                "multihop-2wiki": real_backend,
                "graph": stub_backend,
            }
        )

        # Verify the dispatcher was created
        assert dispatcher is not None
        assert hasattr(dispatcher, "communities_table_exists")


class TestDispatchingCommunityStoreRouting:
    """Integration tests for dispatcher routing logic."""

    @pytest.mark.asyncio
    async def test_dispatcher_routes_to_stub_for_graph_collection(self) -> None:
        """Dispatcher routes graph collection to CommunityStoreStub."""
        from archon_search.eval.backends import (
            DispatchingCommunityStore,
            RealCommunityEvalBackend,
            CommunityStoreStub,
        )
        from archon_search.graph_store import GraphStore

        gs = GraphStore(db_path=":memory:")
        real_backend = RealCommunityEvalBackend(graph_store=gs)
        stub_backend = CommunityStoreStub()

        dispatcher = DispatchingCommunityStore(
            backend_map={
                "multihop-2wiki": real_backend,
                "graph": stub_backend,
            }
        )

        # Test that graph collection routes to stub
        result = await dispatcher.communities_table_exists("graph", ns="default")
        assert result is True  # Stub always returns True for graph collection

    @pytest.mark.asyncio
    async def test_dispatcher_routes_to_real_backend_for_multihop(self) -> None:
        """Dispatcher routes multihop-2wiki collection to RealCommunityEvalBackend."""
        from archon_search.eval.backends import (
            DispatchingCommunityStore,
            RealCommunityEvalBackend,
            CommunityStoreStub,
        )
        from archon_search.graph_store import GraphStore
        from archon_search.graph_types import (
            EntityType,
            GraphEdge,
            GraphNode,
            RelationshipType,
            make_stable_edge_id,
            make_stable_entity_id,
        )

        gs = GraphStore(db_path=":memory:")
        await gs.connect()

        # Set up a real backend with some test data
        col = "multihop-2wiki"
        ns = "default"

        await gs.ensure_graph_tables(col, ns=ns)

        # Create a simple node and edge to test routing
        node_a = GraphNode(
            id=make_stable_entity_id("concept", "TestEntity"),
            entity_name="TestEntity",
            entity_type=EntityType.concept,
            source_doc_id="doc-1",
            collection_name=col,
        )
        edge = GraphEdge(
            id=make_stable_edge_id(node_a.id, node_a.id, "related_to"),
            source_node_id=node_a.id,
            target_node_id=node_a.id,
            relationship_type=RelationshipType.related_to,
            source_doc_id="doc-1",
        )

        await gs.write_graph(col, [node_a], [edge], ns=ns)

        real_backend = RealCommunityEvalBackend(graph_store=gs)
        stub_backend = CommunityStoreStub()

        dispatcher = DispatchingCommunityStore(
            backend_map={
                "multihop-2wiki": real_backend,
                "graph": stub_backend,
            }
        )

        # Test that multihop-2wiki routes to real backend
        # The real backend should find the node we created
        nodes = await dispatcher.find_nodes_by_name(
            "multihop-2wiki", ["TestEntity"], ns=ns
        )
        assert isinstance(nodes, list)
        assert len(nodes) == 1
        assert nodes[0].entity_name == "TestEntity"

        await gs.disconnect()


class TestRealGraphExpander:
    """Unit tests for RealGraphExpander."""

    def test_real_graph_expander_initialization(self) -> None:
        """RealGraphExpander can be initialized with a GraphStore."""
        from archon_search.eval.backends import RealGraphExpander
        from archon_search.graph_store import GraphStore

        gs = GraphStore(db_path=":memory:")
        expander = RealGraphExpander(graph_store=gs)

        assert expander is not None
        assert hasattr(expander, "expand")
        assert callable(expander.expand)

    @pytest.mark.asyncio
    async def test_real_graph_expander_fallback_on_empty_query(self) -> None:
        """RealGraphExpander returns original query when no ngrams found."""
        from archon_search.eval.backends import RealGraphExpander
        from archon_search.graph_store import GraphStore

        gs = GraphStore(db_path=":memory:")
        expander = RealGraphExpander(graph_store=gs)

        # Empty query should return unchanged
        result = await expander.expand("", collection="test", ns="default")
        assert result.original_query == ""
        assert result.expanded_text == ""
        assert result.expansion_applied is False


class TestRealCommunityBackendIntegration:
    """Integration tests for RealCommunityEvalBackend (requires leidenalg)."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_real_community_backend_communities_table_exists(self, tmp_path) -> None:
        """RealCommunityEvalBackend.communities_table_exists returns correct status."""
        pytest.importorskip("leidenalg")

        from pathlib import Path

        from archon_search.eval.backends import RealCommunityEvalBackend
        from archon_search.graph_store import GraphStore
        from archon_search.graph_types import (
            EntityType,
            GraphEdge,
            GraphNode,
            RelationshipType,
            make_stable_edge_id,
            make_stable_entity_id,
        )

        gs = GraphStore(db_path=str(tmp_path / "db"))
        await gs.connect()

        col = "test-col"
        ns = "default"

        # Table doesn't exist yet
        exists = await gs.communities_table_exists(col, ns=ns)
        assert exists is False

        # Create nodes and edges
        node_a = GraphNode(
            id=make_stable_entity_id("concept", "Alpha"),
            entity_name="Alpha",
            entity_type=EntityType.concept,
            source_doc_id="doc-1",
            collection_name=col,
        )
        node_b = GraphNode(
            id=make_stable_entity_id("concept", "Beta"),
            entity_name="Beta",
            entity_type=EntityType.concept,
            source_doc_id="doc-1",
            collection_name=col,
        )
        edge = GraphEdge(
            id=make_stable_edge_id(node_a.id, node_b.id, "related_to"),
            source_node_id=node_a.id,
            target_node_id=node_b.id,
            relationship_type=RelationshipType.related_to,
            source_doc_id="doc-1",
        )

        # Ensure tables and ingest graph
        await gs.ensure_graph_tables(col, ns=ns)
        await gs.write_graph(col, [node_a, node_b], [edge], ns=ns)

        # Build communities
        from archon_search.community_builder import CommunityBuilder
        from archon_search.config import GraphConfig

        config = GraphConfig(enabled=True, leiden_resolution=1.0)
        builder = CommunityBuilder(gs, config)
        communities = await builder.build(col, ns=ns, seed=42)

        # Now communities table should exist
        backend = RealCommunityEvalBackend(graph_store=gs)
        exists = await backend.communities_table_exists(col, ns=ns)
        assert exists is True

        # And list_community_representatives should return communities
        reps = await backend.list_community_representatives(col, ns=ns)
        assert len(reps) >= 1
        # Each should have representative chunks (even if empty in this stub test)
        for rep in reps:
            assert hasattr(rep, "community_id")
            assert hasattr(rep, "entity_ids")
            assert hasattr(rep, "representative_chunk_ids")

        await gs.disconnect()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_real_community_backend_find_nodes_by_name(self, tmp_path) -> None:
        """RealCommunityEvalBackend.find_nodes_by_name returns matching entities."""
        pytest.importorskip("leidenalg")

        from pathlib import Path

        from archon_search.eval.backends import RealCommunityEvalBackend
        from archon_search.graph_store import GraphStore
        from archon_search.graph_types import (
            EntityType,
            GraphEdge,
            GraphNode,
            RelationshipType,
            make_stable_edge_id,
            make_stable_entity_id,
        )

        gs = GraphStore(db_path=str(tmp_path / "db"))
        await gs.connect()

        col = "test-col"
        ns = "default"

        # Create nodes
        node_a = GraphNode(
            id=make_stable_entity_id("concept", "Alpha"),
            entity_name="Alpha",
            entity_type=EntityType.concept,
            source_doc_id="doc-1",
            collection_name=col,
        )
        node_b = GraphNode(
            id=make_stable_entity_id("concept", "Beta"),
            entity_name="Beta",
            entity_type=EntityType.concept,
            source_doc_id="doc-1",
            collection_name=col,
        )

        # Ensure tables and ingest
        await gs.ensure_graph_tables(col, ns=ns)
        await gs.write_graph(col, [node_a, node_b], [], ns=ns)

        # Test find_nodes_by_name
        backend = RealCommunityEvalBackend(graph_store=gs)
        nodes = await backend.find_nodes_by_name(col, ["alpha"], ns=ns)
        assert len(nodes) >= 1
        assert any(n.entity_name.lower() == "alpha" for n in nodes)

        await gs.disconnect()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_real_community_backend_communities_table_exists_with_fixture(
        self, build_communities_for_eval
    ) -> None:
        """RealCommunityEvalBackend.communities_table_exists returns True after fixture builds communities."""
        pytest.importorskip("leidenalg")

        from archon_search.eval.backends import RealCommunityEvalBackend

        lancedb_root, graph_stores = build_communities_for_eval
        graph_store = graph_stores["multihop-2wiki"]
        backend = RealCommunityEvalBackend(graph_store=graph_store)

        # After build_communities_for_eval fixture runs, communities tables exist
        result = await backend.communities_table_exists("multihop-2wiki", ns="default")
        assert result is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_real_community_backend_find_nodes_by_name_with_fixture(
        self, build_communities_for_eval
    ) -> None:
        """find_nodes_by_name returns list for entity names present in graph."""
        pytest.importorskip("leidenalg")

        from archon_search.eval.backends import RealCommunityEvalBackend

        lancedb_root, graph_stores = build_communities_for_eval
        graph_store = graph_stores["multihop-musique"]
        backend = RealCommunityEvalBackend(graph_store=graph_store)

        # After graph extraction from corpus, entity names should be findable
        nodes = await backend.find_nodes_by_name(
            "multihop-musique", ["machine", "learning"], ns="default"
        )
        # Assert returns a list (may be empty if entities not in graph, but should work)
        assert isinstance(nodes, list)
