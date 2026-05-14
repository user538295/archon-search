"""GET /telemetry/stats and GET /telemetry/entries route handlers — FEAT-039c Tasks 3.2, 3.3."""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from archon_search.config import SearchConfig
from archon_search.server.schemas_telemetry import DisabledResponse, EntriesResponse, StatsResponse
from archon_search.telemetry.reader import TelemetryReader

logger = logging.getLogger("archon.search")

router = APIRouter()


# Enum wrappers for query parameter validation — FastAPI only auto-validates
# Enum types with 422; Literal aliases are treated as plain strings.
class _EndpointKind(str, Enum):
    search = "search"
    search_with_context = "search_with_context"
    route = "route"


class _Status(str, Enum):
    ok = "ok"
    validation_error = "validation_error"
    timeout = "timeout"
    internal_error = "internal_error"


class _ErrorKind(str, Enum):
    empty_query = "empty_query"
    slot_out_of_range = "slot_out_of_range"
    timeout = "timeout"
    internal_error = "internal_error"
    validation_error = "validation_error"
    other = "other"


@router.get("/telemetry/stats")
async def get_telemetry_stats(
    request: Request,
    since: Annotated[date | None, Query()] = None,
    until: Annotated[date | None, Query()] = None,
) -> StatsResponse | DisabledResponse:
    config: SearchConfig = request.app.state.config
    if not config.telemetry.enabled:
        return DisabledResponse()
    log_dir = Path(config.telemetry.log_dir).expanduser()
    reader = TelemetryReader(log_dir, config.telemetry.retention_days)
    try:
        since_d, until_d = reader.resolve_dates(since, until)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    entries, skipped = await asyncio.to_thread(reader.read_entries, since_d, until_d)
    return reader.compute_stats(entries, since_d, until_d, skipped)


@router.get("/telemetry/entries")
async def get_telemetry_entries(
    request: Request,
    since: Annotated[date | None, Query()] = None,
    until: Annotated[date | None, Query()] = None,
    collection: Annotated[str | None, Query()] = None,
    endpoint: Annotated[_EndpointKind | None, Query()] = None,
    status: Annotated[_Status | None, Query()] = None,
    error_kind: Annotated[_ErrorKind | None, Query()] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> EntriesResponse | DisabledResponse:
    config: SearchConfig = request.app.state.config
    if not config.telemetry.enabled:
        return DisabledResponse()
    log_dir = Path(config.telemetry.log_dir).expanduser()
    reader = TelemetryReader(log_dir, config.telemetry.retention_days)
    try:
        since_d, until_d = reader.resolve_dates(since, until)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    entries, skipped = await asyncio.to_thread(reader.read_entries, since_d, until_d)
    filtered = reader.filter_entries(
        entries,
        collection=collection,
        endpoint=endpoint.value if endpoint is not None else None,
        status=status.value if status is not None else None,
        error_kind=error_kind.value if error_kind is not None else None,
    )
    page, total = reader.paginate(filtered, offset, limit)
    return EntriesResponse(
        schema_version=1,
        enabled=True,
        entries=[e.model_dump() for e in page],
        next_offset=offset + len(page),
        total_in_window=total,
        skipped_lines=skipped,
    )
