"""POST /route endpoint — collection routing pre-context for decomposer (Task 5.5)."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import ExitStack
from time import monotonic
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.embedder import Embedder, ModelEmbedder
from archon_search.observability import bind_stage_recorder, correlation_id as _correlation_id
from archon_search.router import MultiCollectionRouter
from archon_search.sync import path_to_collection_name
from archon_search.telemetry.entry import ErrorKind, TelemetryEntry

logger = logging.getLogger(__name__)

router = APIRouter()


class RouteRequest(BaseModel):
    query: str
    slots: int | None = None


class RouteResponse(BaseModel):
    pre_context: str | None
    pinned_names: list[str]
    routable_names: list[str]
    decomposer_invoked: bool


def _build_router(
    config: SearchConfig,
    shortlist_size: int,
    embedder: Embedder | None = None,
    initial_metadata: list[CollectionMeta] | None = None,
) -> MultiCollectionRouter:
    """Build a MultiCollectionRouter from config. Extracted for test injection.

    ``initial_metadata`` seeds the router's collection cache so it ranks over the
    caller's namespace-scoped meta rows directly instead of issuing a self-call
    HTTP fetch (which posts to the bare REST root, not ``/mcp``, and returns []).
    """
    if embedder is None:
        backend = ModelEmbedder(config.embedding_model, providers=config.providers or None)
        embedder = Embedder(backend)
    search_url = f"http://{config.host}:{config.port}"
    return MultiCollectionRouter(
        search_url=search_url,
        embedder=embedder,
        shortlist_size=shortlist_size,
        confidence_threshold=config.routing_confidence_threshold,
        embedding_model=config.embedding_model,
        initial_metadata=initial_metadata,
        strategy=config.routing_strategy,
        description_weight=config.routing_description_weight,
    )


def _redact_validation(detail: str) -> ErrorKind:
    """Map validation error detail strings to privacy-safe ErrorKind literals."""
    if detail == "query must not be empty":
        return "empty_query"
    if detail == "slots must be >= 1":
        return "slot_out_of_range"
    return "validation_error"


@router.post("/route", response_model=RouteResponse)
async def route(body: RouteRequest, request: Request) -> Any:
    """Route a query to the appropriate search collections.

    Returns the pre_context block to inject before route_task(), along with
    the resolved pinned and routable collection names and a flag indicating
    whether the decomposer will be invoked.
    """
    start = monotonic()
    writer = getattr(request.app.state, "telemetry_writer", None)
    config: SearchConfig = request.app.state.config
    timings_enabled: bool = getattr(getattr(config, "observability", None), "stage_timings_enabled", False)

    with ExitStack() as stack:
        recorder = stack.enter_context(bind_stage_recorder()) if timings_enabled else None
        t0 = time.perf_counter()

        def _emit_timings() -> None:
            if recorder is not None:
                recorder.record("total", (time.perf_counter() - t0) * 1000.0)
                logger.info(
                    "stage timings",
                    extra={
                        "event_type": "stage_timings",
                        "correlation_id": _correlation_id.get(),
                        "endpoint": "route",
                        "collection": None,
                        "stage_timings_ms": recorder.stage_timings_ms,
                    },
                )

        try:
            if not body.query or not body.query.strip():
                raise HTTPException(status_code=400, detail="query must not be empty")

            if body.slots is not None and body.slots < 1:
                raise HTTPException(status_code=400, detail="slots must be >= 1")

            shortlist_size = body.slots if body.slots is not None else config.routing_shortlist_size
            embedder: Embedder | None = getattr(request.app.state, "embedder", None)

            ns: str = request.state.namespace
            store = request.app.state.search_store
            all_meta = await store.get_all_collections_meta()
            ns_meta = [m for m in all_meta if m.namespace == ns]
            ns_names = {m.name for m in ns_meta}

            # Seed the router with this namespace's meta rows so routable collections
            # created by single-file ingest (meta row only, never a config path) are
            # visible — without this the router self-calls HTTP and gets nothing.
            col_router = _build_router(
                config, shortlist_size, embedder=embedder, initial_metadata=ns_meta
            )

            all_pinned = [path_to_collection_name(p) for p in config.pinned_collections]
            pinned_names = [n for n in all_pinned if n in ns_names]

            pre_context = await asyncio.wait_for(
                col_router.get_pre_context(
                    query=body.query,
                    pinned_names=pinned_names,
                    available_slots=shortlist_size,
                ),
                timeout=30.0,
            )

            resp = RouteResponse(
                pre_context=pre_context,
                pinned_names=pinned_names,
                routable_names=col_router.last_routable_names,
                decomposer_invoked=col_router.decomposer_was_invoked,
            )
            if writer is not None:
                try:
                    writer.enqueue(
                        TelemetryEntry.from_route_response(
                            collections=resp.pinned_names + resp.routable_names,
                            decomposer_invoked=resp.decomposer_invoked,
                            latency_ms=(monotonic() - start) * 1000.0,
                            correlation_id=_correlation_id.get(),
                        )
                    )
                except Exception as tel_exc:
                    logger.warning("telemetry enqueue failed: %s", type(tel_exc).__name__)
            _emit_timings()
            return resp

        except asyncio.TimeoutError:
            _emit_timings()
            if writer is not None:
                try:
                    writer.enqueue(
                        TelemetryEntry.from_error(
                            endpoint="route",
                            status="timeout",
                            error_kind="timeout",
                            latency_ms=(monotonic() - start) * 1000.0,
                            correlation_id=_correlation_id.get(),
                        )
                    )
                except Exception as tel_exc:
                    logger.warning("telemetry enqueue failed: %s", type(tel_exc).__name__)
            raise HTTPException(status_code=504, detail="routing timed out")

        except HTTPException as exc:
            if exc.status_code == 400 and writer is not None:
                try:
                    writer.enqueue(
                        TelemetryEntry.from_error(
                            endpoint="route",
                            status="validation_error",
                            error_kind=_redact_validation(exc.detail),
                            latency_ms=(monotonic() - start) * 1000.0,
                            correlation_id=_correlation_id.get(),
                        )
                    )
                except Exception as tel_exc:
                    logger.warning("telemetry enqueue failed: %s", type(tel_exc).__name__)
            raise

        except Exception as exc:
            _emit_timings()
            if writer is not None:
                try:
                    writer.enqueue(
                        TelemetryEntry.from_error(
                            endpoint="route",
                            status="internal_error",
                            error_kind="other",
                            latency_ms=(monotonic() - start) * 1000.0,
                            correlation_id=_correlation_id.get(),
                        )
                    )
                except Exception as tel_exc:
                    logger.warning("telemetry enqueue failed: %s", type(tel_exc).__name__)
            logger.error("route handler failed: %s", type(exc).__name__)
            raise
