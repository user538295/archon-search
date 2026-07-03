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

from archon_search.graph_inspector import CollectionGraphView, inspect_collection, to_graphml
from archon_search.server.schemas import (
    GraphEdgeResponse,
    GraphInspectionResponse,
    GraphNodeResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/graph/{collection}", name="get_graph", response_model=None)
async def get_graph(
    collection: str,
    format: Literal["json", "graphml"] = Query(default="json"),
    request: Request = None,
):
    """Inspect a single collection's graph with derived metrics.

    Query parameters:
    - `format`: "json" (default) or "graphml" to export as GraphML XML.

    Returns:
    - 200: Graph inspection response (JSON or GraphML)
    - 404: Collection not found
    - 422: graph.enabled=false

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

    # Guard 1: Check if graph is enabled
    if not config.graph.enabled:
        raise HTTPException(
            status_code=422,
            detail="graph inspection requires [graph] enabled=true in server config",
        )

    # Guard 2: Check if collection exists
    collection_meta = await pipeline.get_collection_meta(collection)
    if collection_meta is None:
        raise HTTPException(status_code=404, detail="collection not found")

    # Inspect the collection's graph
    view = await inspect_collection(
        graph_store=graph_store,
        collection=collection,
        total_chunk_count=collection_meta.chunk_count,
        max_nodes=config.graph.max_inspection_nodes,
        max_edges=config.graph.max_inspection_edges,
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
    )


