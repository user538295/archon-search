"""GET /graph endpoints for graph inspection — E2b."""
from __future__ import annotations

import importlib.resources
import json
import logging
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

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
from archon_search.graph_types import DEFAULT_IMPACT_DEPTH, ImpactDirection, ImpactResult
from archon_search.server.schemas import (
    CrossCollectionGraphInspectionResponse,
    GraphEdgeResponse,
    GraphImpactResponse,
    GraphInspectionResponse,
    GraphNodeResponse,
    ImpactEdgeResponse,
    ImpactGroupResponse,
)
from archon_search.server.middleware_auth import (
    INVALID_NAMESPACE_SENTINEL,
    validate_token_and_get_namespace,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _load_viewer_html() -> bytes:
    """Load graph_viewer.html as bytes from the package resource.

    Extracted as a module-private helper so integration tests can monkeypatch
    it to inject a stable stub without requiring ideal HTML file content.
    """
    pkg = importlib.resources.files("archon_search.server")
    return pkg.joinpath("graph_viewer.html").read_bytes()


def _js_safe_json(value: str) -> str:
    """JSON-encode a string value safe for embedding inside <script> blocks.

    json.dumps adds surrounding quotes and escapes JS string syntax, but leaves
    <, > and & intact, which allows </script> breakout in HTML.
    Unicode escapes are invisible to JS and are the canonical safe-embedding fix.
    """
    encoded = json.dumps(value)
    return encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


@router.get("/graph/{collection}/view", name="get_graph_view", response_class=HTMLResponse)
async def get_graph_view(
    collection: str,
    token: str | None = None,
    *,
    request: Request,
) -> Response:
    """Serve a self-contained graph viewer HTML page for a collection — E2j BE-2.

    Query parameters:
    - ``token``: Optional Bearer token (used when no Authorization header is present).
      The middleware exemption lets this request bypass the header-presence check.

    Returns:
    - 200: Self-contained HTML page with C4 placeholders substituted.
    - 401: Missing or invalid auth (WWW-Authenticate: Bearer header always present).
    - 404: Collection not found.
    - 422: graph.enabled=false.

    Auth resolution order (C3 precedence):
    1. Authorization header (middleware already validated; handler recovers token for embedding).
    2. ?token= query param (handler validates via validate_token_and_get_namespace).
    """
    pipeline = request.app.state.pipeline
    config = request.app.state.config

    # --- Resolve the middleware state --- 
    # Middleware runs before this handler. Two cases:
    #   A. Authorization header present: middleware validated and set request.state.namespace.
    #      The raw token is recovered from the header for embedding in HTML.
    #   B. ?token= path: middleware exemption lets the request through; handler does full auth.
    auth_header = request.headers.get("Authorization", "")
    header_parts = auth_header.split(" ", 1)
    has_bearer_header = len(header_parts) == 2 and header_parts[0] == "Bearer"

    if has_bearer_header:
        # Case A: middleware already validated. Namespace is on request.state.
        raw_token = header_parts[1]
        ns = request.state.namespace
    else:
        # Case B: ?token= path. Run the full auth cascade here.
        if not token:
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        result = await validate_token_and_get_namespace(
            token,
            api_key=request.app.state.api_key,
            namespaces=request.app.state.namespaces,
            key_store=request.app.state.key_store,
        )

        if result is INVALID_NAMESPACE_SENTINEL or result is None:
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        raw_token = token
        ns = result
        request.state.namespace = ns

    # --- Guard 1: graph enabled ---
    if not config.graph.enabled:
        raise HTTPException(
            status_code=422,
            detail="graph inspection requires [graph] enabled=true in server config",
        )

    # --- Guard 2: collection exists ---
    collection_meta = await pipeline.get_collection_meta(collection, namespace=ns)
    if collection_meta is None:
        raise HTTPException(status_code=404, detail="collection not found")

    # --- Build HTML with C4 placeholder substitution ---
    html_bytes = _load_viewer_html()
    html = html_bytes.decode("utf-8")

    # String values: json.dumps adds surrounding quotes and escapes special chars.
    # Integer values: str(int(value)) — bare number in JS.
    html = html.replace("__ARCHON_COLLECTION__", _js_safe_json(collection))
    html = html.replace("__ARCHON_TOKEN__", _js_safe_json(raw_token))
    html = html.replace("__ARCHON_MAX_NODES__", str(int(config.graph.max_inspection_nodes)))
    html = html.replace("__ARCHON_MAX_EDGES__", str(int(config.graph.max_inspection_edges)))

    return Response(
        content=html.encode("utf-8"),
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.get("/graph/cross-collection", name="get_graph_cross_collection", response_model=CrossCollectionGraphInspectionResponse)
async def get_graph_cross_collection(
    collections: str = Query(...),
    format: Literal["json", "graphml"] = Query(default="json"),
    salience: Literal["frequency", "tfidf", "importance"] = Query(default="frequency"),
    request: Request = None,
):
    """Inspect and merge graph data across multiple collections.

    Query parameters:
    - `collections`: Comma-separated list of collection names (at least 2 required, deduped).
    - `format`: "json" (default) or "graphml" to export as GraphML XML.
    - `salience`: "frequency" (default, chunk ratio clamped to [0,1]) or
      "tfidf" (TF×IDF across all namespace collections), or
      "importance" (persisted PageRank score over code-symbol edges, nulls-last).

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
    salience: Literal["frequency", "tfidf", "importance"] = Query(default="frequency"),
    request: Request = None,
):
    """Inspect a single collection's graph with derived metrics.

    Query parameters:
    - `format`: "json" (default) or "graphml" to export as GraphML XML.
    - `salience`: "frequency" (default, chunk ratio clamped to [0,1]) or
      "tfidf" (TF×IDF across all namespace collections), or
      "importance" (persisted PageRank score over code-symbol edges, nulls-last).

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


@router.get("/graph/{collection}/impact/{symbol}", name="get_graph_impact", response_model=GraphImpactResponse)
async def get_graph_impact(
    collection: str,
    symbol: str,
    file_path: str | None = Query(default=None),
    depth: int | None = Query(default=None, ge=1),
    direction: str | None = Query(default=None),
    extraction_method_filter: str | None = Query(default=None),
    request: Request = None,
):
    """Blast-radius (caller/callee) impact analysis for a code symbol — E2g BE-9.

    Query parameters:
    - `file_path`: disambiguates same-named symbols to the one defined in this file.
    - `depth`: traversal depth (default 2, hard-capped at MAX_IMPACT_DEPTH server-side).
    - `direction`: "callers", "callees", or "both" (default "both").
    - `extraction_method_filter`: only traverse edges with this extraction method.

    Returns:
    - 200: GraphImpactResponse mirroring GraphStore.compute_impact's ImpactResult 1:1
    - 404: Collection not found
    - 422: graph.enabled=false or invalid direction value
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

    # BE-9 fills depth/direction defaults at this surface's Presentation->Adapter boundary
    effective_depth = depth if depth is not None else DEFAULT_IMPACT_DEPTH
    try:
        effective_direction = (
            ImpactDirection(direction) if direction is not None else ImpactDirection.both
        )
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"invalid direction {direction!r}; must be one of 'callers', 'callees', 'both'",
        )

    result = await graph_store.compute_impact(
        collection,
        symbol,
        effective_depth,
        effective_direction,
        extraction_method_filter,
        file_path,
        ns,
    )
    return _impact_result_to_response(result)


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
            entity_type=node.entity_type,
            pagerank_score=node.pagerank_score,
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
            relationship_type=edge.relationship_type,
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
            entity_type=node.entity_type,
            pagerank_score=node.pagerank_score,
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
            relationship_type=edge.relationship_type,
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


def _impact_edge_to_response(e) -> ImpactEdgeResponse:
    """Convert one impact-traversal edge entry to an ``ImpactEdgeResponse`` — E2g BE-9."""
    return ImpactEdgeResponse(
        entity_id=e.entity_id,
        entity_name=e.entity_name,
        relationship_type=e.relationship_type,
        extraction_method=e.extraction_method,
        depth=e.depth,
    )


def _impact_group_to_response(group) -> ImpactGroupResponse:
    """Convert an ``ImpactGroup`` to an ``ImpactGroupResponse`` — E2g BE-9."""
    return ImpactGroupResponse(
        direct=[_impact_edge_to_response(e) for e in group.direct],
        indirect=[_impact_edge_to_response(e) for e in group.indirect],
        truncated=group.truncated,
        omitted_count=group.omitted_count,
    )


def _impact_result_to_response(result: ImpactResult) -> GraphImpactResponse:
    """Convert an ``ImpactResult`` to a ``GraphImpactResponse`` 1:1 — E2g BE-9."""
    return GraphImpactResponse(
        symbol=result.symbol,
        callers=_impact_group_to_response(result.callers),
        callees=_impact_group_to_response(result.callees),
        depth_used=result.depth_used,
    )

