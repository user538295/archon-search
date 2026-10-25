"""GET/POST /graph endpoints for graph inspection and community rebuilds — E2b, BE-2."""
from __future__ import annotations

import asyncio
import importlib.resources
import json
import logging
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

if TYPE_CHECKING:
    from archon_search.config import GraphConfig
    from archon_search.graph_enrichment_protocol import LLMEnrichmentClientProtocol
    from archon_search.graph_store import GraphStore
    from archon_search.jobs.store import JobStore
    from archon_search.store import SearchStore
    from archon_search.types import CommunityRebuildJob

from archon_search.community_builder import (
    COMMUNITY_DEPENDENCY_DETAIL,
    CommunityBuilder,
    CommunityDependencyError,
    NoGraphNodesError,
)
from archon_search.constants import GRAPH_VIEW_TOKEN_TTL_SECONDS, VIEW_TOKEN_QUERY_PARAM
from archon_search.graph_inspector import (
    GRAPHML_DEPENDENCY_DETAIL,
    CollectionGraphView,
    CrossCollectionGraphView,
    GraphMLDependencyError,
    inspect_collection,
    inspect_cross_collection,
    to_graphml,
)
from archon_search.graph_types import (
    DEFAULT_IMPACT_DEPTH,
    ImpactDirection,
    ImpactResult,
)
from archon_search.jobs.model import job_to_dict
from archon_search.server._collection_job import (
    SERVICE_NOT_READY_DETAIL,
    create_collection_job,
    refuse_active_job_or_fail_legacy,
)
from archon_search.server._ingest_lock import STORE_BUSY_503_DESCRIPTION
from archon_search.server.graph_view_token import mint_view_token
from archon_search.server.routes_jobs import INTERNAL_ERROR_DETAIL, _persist_job_status
from archon_search.server.schemas import (
    CrossCollectionGraphInspectionResponse,
    ErrorDetail,
    GraphEdgeResponse,
    GraphImpactResponse,
    GraphInspectionResponse,
    GraphNodeResponse,
    GraphViewTokenResponse,
    ImpactEdgeResponse,
    ImpactGroupResponse,
    JobResponse,
)
from archon_search.types import JobStatus

logger = logging.getLogger(__name__)

router = APIRouter()

# Job ``error`` for a community rebuild that failed with an exception whose message
# may embed internals (GraphStore / LanceDB I/O). Only the builder's curated
# NoGraphNodesError message reaches the job record; CommunityDependencyError records
# COMMUNITY_DEPENDENCY_DETAIL (its message embeds the install command).
COMMUNITY_REBUILD_FAILED_DETAIL = "community rebuild failed; see the server log for details"

# Error map for rebuild_communities: 401 (auth), 404 (collection not found),
# 409 (rebuild already in progress), 422 (graph disabled or ?namespace= mismatch), 500 (job could not be saved),
# 503 (SERVICE_NOT_READY_DETAIL: no search store; or store_busy: collection lock not acquired in time).
_REBUILD_ERROR_RESPONSES = {
    401: {"model": ErrorDetail},
    404: {"model": ErrorDetail},
    409: {"model": ErrorDetail},
    422: {"model": ErrorDetail},
    500: {
        "model": ErrorDetail,
        "description": f'Creating the community rebuild job failed (body {{"detail": "{INTERNAL_ERROR_DETAIL}"}})',
    },
    503: {
        "model": ErrorDetail,
        "description": (
            f'Service not ready — the search store is not initialised (body {{"detail": "{SERVICE_NOT_READY_DETAIL}"}}). '
            f"{STORE_BUSY_503_DESCRIPTION}"
        ),
    },
}

# Pinned "in progress" status set for the community_rebuild_job_id guard
# (BE-5, CM-3). This route creates the job RUNNING and runs it itself; DELETE
# /jobs/{id} moves it to CANCELLING without stopping the task, so that still
# counts. QUEUED/PENDING are deliberately absent: nothing schedules a
# CommunityRebuildJob, so such a row (written by an older server, which created
# it QUEUED first) never runs — it is stale: lazily cleared and marked FAILED, not a 409.
_REBUILD_ACTIVE_STATUSES = {JobStatus.RUNNING, JobStatus.CANCELLING}

#: Log-line prefix for ``_community_rebuild_task``'s best-effort job status writes (``_persist_job_status``).
_REBUILD_TASK_LOG_CONTEXT = "_community_rebuild_task"


async def _community_rebuild_task(
    job: CommunityRebuildJob,
    job_store: JobStore,
    graph_store: GraphStore,
    graph_config: GraphConfig,
    search_store: SearchStore | None = None,
    enrichment_client: LLMEnrichmentClientProtocol | None = None,
) -> None:
    """Coroutine that drives a single CommunityRebuildJob to completion.

    Mirrors ``_migration_task`` (``routes_collections.py``). The caller is
    responsible for ensuring the job is already in RUNNING state before
    invoking this coroutine (the route creates the job RUNNING, then passes
    it to ``asyncio.create_task``).

    Lifecycle (caller sets RUNNING -> this task drives to terminal):
      1. Construct a fresh ``CommunityBuilder``.
      2. ``await build(collection, ns)``.
      3. Transition to DONE with ``result={"communities_built": N}`` on success.
      4. Transition to FAILED on any exception: the builder's curated message for
         ``NoGraphNodesError``, ``COMMUNITY_DEPENDENCY_DETAIL`` for
         ``CommunityDependencyError``, else ``COMMUNITY_REBUILD_FAILED_DETAIL``.

    BE-5: on every terminal exit (DONE and FAILED), actively clears
    ``CollectionMeta.community_rebuild_job_id`` (mirrors ``_reindex_task``'s
    pattern, ``routes_jobs.py``). The clear is wrapped in its own
    ``try/except Exception`` so a clear failure logs and is swallowed, never
    crashing the task -- the lazy clear in the route's guard is the fallback
    that unwedges the collection if this active clear never runs (S16).
    """
    job_id = job.job_id
    builder = CommunityBuilder(
        graph_store, graph_config, search_store=search_store, enrichment_client=enrichment_client
    )

    failure_error: str | None = None
    communities = None
    try:
        communities = await builder.build(job.collection, job.namespace)
    except NoGraphNodesError as exc:
        # Curated, expected outcome: no traceback.
        logger.warning("_community_rebuild_task: job %s failed: %s", job_id, exc)
        failure_error = str(exc)  # the builder's curated message
    except CommunityDependencyError as exc:
        logger.warning("_community_rebuild_task: job %s failed: %s", job_id, exc)  # the install command: operator-only
        failure_error = COMMUNITY_DEPENDENCY_DETAIL
    except Exception:
        # Catch everything: an exception escaping here would leave the job RUNNING forever.
        logger.exception("_community_rebuild_task: job %s failed", job_id)
        failure_error = COMMUNITY_REBUILD_FAILED_DETAIL

    # Clear community_rebuild_job_id BEFORE transitioning to a terminal state
    # so any observer that sees DONE/FAILED is guaranteed to also see it cleared.
    # ponytail: eager clear prevents the poll→read race that only manifests on slow CI disks.
    if search_store is not None:
        try:
            meta = await search_store.get_collection_meta(job.collection, namespace=job.namespace)
            if meta is not None and meta.community_rebuild_job_id == job_id:
                meta.community_rebuild_job_id = None
                await search_store.update_collection_meta(meta)
        except Exception:  # noqa: BLE001 — best-effort pointer cleanup in a background task; failure is logged, never raised
            logger.warning(
                "_community_rebuild_task: failed to clear community_rebuild_job_id for job %s",
                job_id,
            )

    # Both terminal writes are best-effort: a persist failure is logged and suppressed, never escaping this
    # fire-and-forget task.
    if failure_error is not None:
        _persist_job_status(job_store, job_id, JobStatus.FAILED, context=_REBUILD_TASK_LOG_CONTEXT, error=failure_error)
    else:
        _persist_job_status(
            job_store,
            job_id,
            JobStatus.DONE,
            context=_REBUILD_TASK_LOG_CONTEXT,
            result={"communities_built": len(communities)},
        )


@router.post(
    "/graph/{collection}/rebuild-communities",
    name="rebuild_communities",
    status_code=202,
    response_model=JobResponse,
    responses=_REBUILD_ERROR_RESPONSES,
)
async def rebuild_communities(
    collection: str,
    request: Request,
    namespace: str = Query(default="default"),
) -> JSONResponse:
    """Enqueue an async Leiden community rebuild for a collection — BE-2.

    Follows the migrate route's pattern (``routes_collections.py``,
    ``migrate_collection``): validate -> create the job RUNNING in one write
    -> spawn task -> return 202 with the full JobResponse body
    (``job_to_dict(running_job)``), so the response reports RUNNING, not
    PENDING (S1).

    Auth is handled solely by ``APIKeyMiddleware``. A token resolving to
    ``INVALID_NAMESPACE_SENTINEL`` never reaches this handler; the middleware
    returns a bare 500 first (S14).

    The optional ``?namespace=`` query param lets the CLI explicitly confirm
    which namespace it intends to target. If provided, it must match the
    namespace resolved from the Bearer token; a mismatch returns 422 so the
    caller knows to use the correct API key for the intended namespace.

    Returns:
    - 202: JobResponse-shaped body, status RUNNING.
    - 404: Collection not found in the caller's namespace (also when a DELETE dropped it meanwhile).
    - 409: A rebuild is already in progress for this collection (BE-5).
    - 422: graph.enabled=false, or ?namespace= does not match token namespace.
    - 500: The job could not be saved (sanitized ``{"detail": "internal error"}``).
    - 503: ``service not ready`` — no search store (checked after the 422 guards); or ``store_busy`` — the
      collection lock was not acquired within ``INGEST_LOCK_TIMEOUT_S``.
    """
    config = request.app.state.config
    graph_store = request.app.state.graph_store
    job_store: JobStore = request.app.state.job_store
    search_store: SearchStore | None = request.app.state.search_store
    ns: str = request.state.namespace

    # Guard 0: if the caller explicitly declared a ?namespace=, it must match
    # the namespace their Bearer token authorises. This prevents a token for
    # namespace A from accidentally triggering a rebuild in namespace B.
    if namespace != ns:
        raise HTTPException(
            status_code=422,
            detail=f"namespace mismatch: token authorises '{ns}', but ?namespace='{namespace}' was requested",
        )

    # Guard 1: graph enabled (verbatim detail string, matches the other four guards in this file).
    if not config.graph.enabled:
        raise HTTPException(
            status_code=422,
            detail="graph inspection requires [graph] enabled=true in server config",
        )

    # Guard 1b: graph_store may be None if graph.enabled=true but connect()
    # failed at startup. Without this guard the task below would spawn with
    # graph_store=None and every rebuild would fail inside build() with an
    # AttributeError (caught by the task's ``except Exception``: the job goes
    # FAILED with the generic COMMUNITY_REBUILD_FAILED_DETAIL) — this answers
    # the request with a clear 422 instead of creating a job that cannot succeed.
    if graph_store is None:
        raise HTTPException(
            status_code=422,
            detail="graph inspection requires [graph] enabled=true in server config",
        )

    # Readiness, before the first store access: without a search store no meta row, lock or job can be used. After
    # the 422 guards, which a retry once the server is ready would answer all the same.
    if search_store is None:
        return JSONResponse({"detail": SERVICE_NOT_READY_DETAIL}, status_code=503)

    # Guard 2 (404: collection not in the caller's namespace) and Guard 3 (BE-5, C2/CM-1: 409 while
    # community_rebuild_job_id names an active rebuild, mirroring the reindex/migrate reindex_job_id checks) run on the
    # meta row read before the collection lock, then again under it; the job and its meta pointer (set on enqueue, so
    # Guard 3 rejects a duplicate rebuild while this one is active) are created under the same lock
    # (create_collection_job). A pointer naming a missing, terminal or legacy QUEUED/PENDING job is stale and is
    # overwritten — the lazy clear that unwedges a collection after a server crash mid-rebuild (S16).
    running_job = await create_collection_job(
        search_store,
        collection,
        ns,
        job_store=job_store,
        log_context="rebuild_communities",
        not_found_detail="collection not found",
        refuse_duplicate=lambda meta: refuse_active_job_or_fail_legacy(
            job_store,
            meta.community_rebuild_job_id,
            active_statuses=_REBUILD_ACTIVE_STATUSES,
            conflict_detail="community rebuild already in progress for this collection",
            log_context="rebuild_communities",
        ),
        # Created RUNNING in one write: the route runs the task itself, so the response
        # never reports QUEUED (mirrors migrate_collection, S1).
        create_job=lambda _meta: job_store.create_community_rebuild(collection=collection, namespace=ns),
        pointer_field="community_rebuild_job_id",
    )
    if isinstance(running_job, JSONResponse):
        return running_job

    task = asyncio.create_task(
        _community_rebuild_task(
            job=running_job,
            job_store=job_store,
            graph_store=graph_store,
            graph_config=config.graph,
            search_store=search_store,
            enrichment_client=request.app.state.enrichment_client,
        )
    )
    request.app.state._background_tasks.add(task)
    task.add_done_callback(request.app.state._background_tasks.discard)

    return JSONResponse(job_to_dict(running_job), status_code=202)


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


async def _require_viewable_collection(request: Request, collection: str, namespace: str) -> None:
    """Raise 422 when graph inspection is disabled, 404 when *collection* is not visible to *namespace*."""
    if not request.app.state.config.graph.enabled:
        raise HTTPException(
            status_code=422,
            detail="graph inspection requires [graph] enabled=true in server config",
        )
    collection_meta = await request.app.state.pipeline.get_collection_meta(collection, namespace=namespace)
    if collection_meta is None:
        raise HTTPException(status_code=404, detail="collection not found")


@router.post(
    "/graph/{collection}/view-token",
    name="create_graph_view_token",
    response_model=GraphViewTokenResponse,
    responses={401: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
async def create_graph_view_token(collection: str, request: Request) -> GraphViewTokenResponse:
    """Mint a short-lived token that opens the graph viewer for *collection* in a browser.

    Open ``view_url`` in a browser: a page navigation cannot carry an ``Authorization`` header, and the real
    API key must never be put in a URL (access logs, history, proxies). The token is valid for
    ``expires_in`` seconds, grants read access to this collection's graph in the caller's namespace and
    nothing else, and is accepted only by ``GET /graph/{collection}/view`` and ``GET /graph/{collection}``.

    Returns:
    - 200: the view token and the viewer URL that carries it.
    - 401: missing or invalid auth (a view token cannot mint another one).
    - 404: collection not found.
    - 422: graph.enabled=false.
    """
    namespace: str = request.state.namespace
    await _require_viewable_collection(request, collection, namespace)
    view_token = mint_view_token(request.app.state.graph_view_secret, namespace, collection)
    return GraphViewTokenResponse(
        view_token=view_token,
        view_url=f"/graph/{collection}/view?{VIEW_TOKEN_QUERY_PARAM}={view_token}",
        expires_in=GRAPH_VIEW_TOKEN_TTL_SECONDS,
    )


@router.get("/graph/{collection}/view", name="get_graph_view", response_class=HTMLResponse)
async def get_graph_view(collection: str, *, request: Request) -> Response:
    """Serve a self-contained graph viewer HTML page for a collection — E2j BE-2.

    Authentication (handled by ``APIKeyMiddleware``): an ``Authorization: Bearer`` header with any valid
    key, or — for a browser navigation — the ``view_token`` query parameter minted by
    ``POST /graph/{collection}/view-token``. The page never contains the caller's API key: it embeds a
    freshly minted view token for its own data fetch, valid for ``GRAPH_VIEW_TOKEN_TTL_SECONDS`` and scoped
    to this collection's graph.

    Returns:
    - 200: Self-contained HTML page with C4 placeholders substituted.
    - 401: Missing or invalid auth (WWW-Authenticate: Bearer header always present).
    - 404: Collection not found.
    - 422: graph.enabled=false.
    """
    namespace: str = request.state.namespace
    await _require_viewable_collection(request, collection, namespace)
    config = request.app.state.config

    # --- Build HTML with C4 placeholder substitution ---
    html_bytes = _load_viewer_html()
    html = html_bytes.decode("utf-8")

    # String values: json.dumps adds surrounding quotes and escapes special chars.
    # Integer values: str(int(value)) — bare number in JS.
    view_token = mint_view_token(request.app.state.graph_view_secret, namespace, collection)
    html = html.replace("__ARCHON_COLLECTION__", _js_safe_json(collection))
    html = html.replace("__ARCHON_TOKEN__", _js_safe_json(view_token))
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

    # Guard 3b: bound the fan-out one call may ask for (the names are not echoed back)
    max_collections = config.graph.max_cross_collection_collections
    if len(collection_list) > max_collections:
        raise HTTPException(
            status_code=422,
            detail=f"collections parameter may name at most {max_collections} collections ([graph].max_cross_collection_collections)",
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
        visibility_source=request.app.state.search_store,
    )

    # Branch on format
    if format == "graphml":
        try:
            graphml_bytes = to_graphml(view)
        except GraphMLDependencyError as e:
            logger.warning("get_graph_cross_collection: GraphML export unavailable: %s", e)
            raise HTTPException(
                status_code=500,
                detail=GRAPHML_DEPENDENCY_DETAIL,
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
        visibility_source=request.app.state.search_store,
    )

    # Branch on format
    if format == "graphml":
        try:
            graphml_bytes = to_graphml(view)
        except GraphMLDependencyError as e:
            logger.warning("get_graph: GraphML export unavailable: %s", e)
            raise HTTPException(
                status_code=500,
                detail=GRAPHML_DEPENDENCY_DETAIL,
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
        visibility=await request.app.state.search_store.get_chunk_visibility(collection, ns),
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

