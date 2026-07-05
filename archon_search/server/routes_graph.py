"""GET /graph endpoints for graph inspection — E2b."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

if TYPE_CHECKING:
    from archon_search.graph_store import GraphStore
    from archon_search.pipeline import SearchPipeline
    from archon_search.config import SearchConfig

from archon_search.graph_inspector import (
    CollectionGraphView,
    CrossCollectionGraphView,
    inspect_collection,
    inspect_cross_collection,
    to_graphml,
)
from archon_search.server.schemas import (
    CrossCollectionGraphInspectionResponse,
    GraphEdgeResponse,
    GraphInspectionResponse,
    GraphNodeResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/graph/cross-collection", name="get_graph_cross_collection", response_model=CrossCollectionGraphInspectionResponse)
async def get_graph_cross_collection(
    collections: str = Query(...),
    format: Literal["json", "graphml"] = Query(default="json"),
    salience: Literal["frequency", "tfidf"] = Query(default="frequency"),
    request: Request = None,
):
    """Inspect and merge graph data across multiple collections.

    Query parameters:
    - `collections`: Comma-separated list of collection names (at least 2 required, deduped).
    - `format`: "json" (default) or "graphml" to export as GraphML XML.
    - `salience`: "frequency" (default, chunk ratio clamped to [0,1]) or
      "tfidf" (TF×IDF across all namespace collections).

    Returns:
    - 200: Merged graph inspection response (JSON or GraphML)
    - 404: Collection not found
    - 422: graph.enabled=false or <2 collections after dedup or invalid format or invalid salience value

    Scenarios:
    - S3: Merged graph data present → 200 JSON with merged nodes, edges, truncated flag
    - S4: GraphML format → 200 XML response
    - S5: graph.enabled=false → 422
    - S7: Less than 2 collections → 422
    - S17: One collection has no data → 200 with data from other collections
    """
    pipeline = request.app.state.pipeline
    graph_store = request.app.state.graph_store
    config = request.app.state.config
    ns = request.state.namespace

    # Guard 1: Check if graph is enabled
    if not config.graph.enabled:
        raise HTTPException(
            status_code=422,
            detail="graph inspection requires [graph] enabled=true in server config",
        )

    # Guard 2: Validate collections parameter is present and non-empty
    if not collections or not collections.strip():
        raise HTTPException(
            status_code=422,
            detail="collections parameter is required and must not be empty",
        )

    # Guard 3: Parse collections, deduplicate, and check count
    collection_list = [c.strip() for c in collections.split(",") if c.strip()]
    collection_list = list(dict.fromkeys(collection_list))  # Deduplicate while preserving order
    if len(collection_list) < 2:
        raise HTTPException(
            status_code=422,
            detail="collections parameter must specify at least 2 distinct collections",
        )

    # Guard 4: Check if each collection exists
    total_chunk_counts: dict[str, int] = {}
    for col in collection_list:
        collection_meta = await pipeline.get_collection_meta(col, namespace=ns)
        if collection_meta is None:
            raise HTTPException(status_code=404, detail="collection not found")
        total_chunk_counts[col] = collection_meta.chunk_count

    # Resolve entity presence for IDF denominator — Presentation→Frameworks&Drivers direct call
    # (E2c architectural exception: GraphStore is already available on app.state alongside the
    # pipeline the route holds; a Use Case wrapper for this single read-only fanout is unnecessary
    # abstraction per the Key Decisions note in the E2c plan.)
    if salience == "tfidf":
        all_meta = await pipeline.get_all_collections_meta(ns)
        all_ns_collection_names = [m.name for m in all_meta]
        entity_presence = await graph_store.get_entity_presence_across_collections(
            all_ns_collection_names, ns=ns
        )
        num_collections = len(all_ns_collection_names)
    else:
        entity_presence = None
        num_collections = 1

    # Inspect the cross-collection graph
    view = await inspect_cross_collection(
        graph_store=graph_store,
        collections=collection_list,
        total_chunk_counts=total_chunk_counts,
        max_nodes=config.graph.max_inspection_nodes,
        max_edges=config.graph.max_inspection_edges,
        salience_mode=salience,
        entity_presence=entity_presence,
        num_collections=num_collections,
        ns=ns,
    )

    # Branch on format
    if format == "graphml":
        try:
            graphml_bytes = to_graphml(view)
        except ImportError as e:
            raise HTTPException(
                status_code=500,
                detail=str(e),
            )
        return Response(content=graphml_bytes, media_type="application/xml")
    else:
        # JSON format (default)
        return _cross_collection_view_to_response(view)


@router.get("/graph/{collection}", name="get_graph", response_model=GraphInspectionResponse)
async def get_graph(
    collection: str,
    format: Literal["json", "graphml"] = Query(default="json"),
    salience: Literal["frequency", "tfidf"] = Query(default="frequency"),
    request: Request = None,
):
    """Inspect a single collection's graph with derived metrics.

    Query parameters:
    - `format`: "json" (default) or "graphml" to export as GraphML XML.
    - `salience`: "frequency" (default, chunk ratio clamped to [0,1]) or
      "tfidf" (TF×IDF across all namespace collections).

    Returns:
    - 200: Graph inspection response (JSON or GraphML)
    - 404: Collection not found
    - 422: graph.enabled=false or invalid format or invalid salience value

    Scenarios:
    - S1: Graph data present → 200 JSON with nodes, edges, truncated flag
    - S2: GraphML format → 200 XML response
    - S5: graph.enabled=false → 422
    - S6: Unknown collection → 404
    - S8: No graph data (tables absent) → 200 with empty nodes/edges
    """
    pipeline = request.app.state.pipeline
    graph_store = request.app.state.graph_store
    config = request.app.state.config
    ns = request.state.namespace

    # Guard 1: Check if graph is enabled
    if not config.graph.enabled:
        raise HTTPException(
            status_code=422,
            detail="graph inspection requires [graph] enabled=true in server config",
        )

    # Guard 2: Check if collection exists in the caller's namespace
    collection_meta = await pipeline.get_collection_meta(collection, namespace=ns)
    if collection_meta is None:
        raise HTTPException(status_code=404, detail="collection not found")

    # Resolve entity presence for IDF denominator — Presentation→Frameworks&Drivers direct call
    # (E2c architectural exception: GraphStore is already available on app.state alongside the
    # pipeline the route holds; a Use Case wrapper for this single read-only fanout is unnecessary
    # abstraction per the Key Decisions note in the E2c plan.)
    if salience == "tfidf":
        all_meta = await pipeline.get_all_collections_meta(ns)
        all_ns_collection_names = [m.name for m in all_meta]
        entity_presence = await graph_store.get_entity_presence_across_collections(
            all_ns_collection_names, ns=ns
        )
        num_collections = len(all_ns_collection_names)
    else:
        entity_presence = None
        num_collections = 1

    # Inspect the collection's graph
    view = await inspect_collection(
        graph_store=graph_store,
        collection=collection,
        total_chunk_count=collection_meta.chunk_count,
        max_nodes=config.graph.max_inspection_nodes,
        max_edges=config.graph.max_inspection_edges,
        salience_mode=salience,
        entity_presence=entity_presence,
        num_collections=num_collections,
        ns=ns,
    )

    # Branch on format
    if format == "graphml":
        try:
            graphml_bytes = to_graphml(view)
        except ImportError as e:
            raise HTTPException(
                status_code=500,
                detail=str(e),
            )
        return Response(content=graphml_bytes, media_type="application/xml")
    else:
        # JSON format (default)
        return _view_to_response(view)


def _cross_collection_view_to_response(
    view: CrossCollectionGraphView,
) -> CrossCollectionGraphInspectionResponse:
    """Convert CrossCollectionGraphView to CrossCollectionGraphInspectionResponse."""
    nodes = [
        GraphNodeResponse(
            entity_id=node.entity_id,
            entity_name=node.entity_name,
            chunk_count=node.chunk_count,
            salience=node.salience,
        )
        for node in view.nodes
    ]
    edges = [
        GraphEdgeResponse(
            edge_id=edge.edge_id,
            source_entity_id=edge.source_entity_id,
            target_entity_id=edge.target_entity_id,
            weight=edge.weight,
            source_chunk_ids=edge.source_chunk_ids,
        )
        for edge in view.edges
    ]
    return CrossCollectionGraphInspectionResponse(
        collections=view.collections,
        nodes=nodes,
        edges=edges,
        truncated=view.truncated,
        node_count=view.node_count,
        edge_count=view.edge_count,
        salience_mode=view.salience_mode,
    )


def _view_to_response(view: CollectionGraphView) -> GraphInspectionResponse:
    """Convert CollectionGraphView to GraphInspectionResponse."""
    nodes = [
        GraphNodeResponse(
            entity_id=node.entity_id,
            entity_name=node.entity_name,
            chunk_count=node.chunk_count,
            salience=node.salience,
        )
        for node in view.nodes
    ]
    edges = [
        GraphEdgeResponse(
            edge_id=edge.edge_id,
            source_entity_id=edge.source_entity_id,
            target_entity_id=edge.target_entity_id,
            weight=edge.weight,
            source_chunk_ids=edge.source_chunk_ids,
        )
        for edge in view.edges
    ]
    return GraphInspectionResponse(
        nodes=nodes,
        edges=edges,
        truncated=view.truncated,
        node_count=view.node_count,
        edge_count=view.edge_count,
        salience_mode=view.salience_mode,
    )


