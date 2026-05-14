"""GET /telemetry/stats route handler — FEAT-039c Task 3.2."""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from archon_search.config import SearchConfig
from archon_search.server.schemas_telemetry import DisabledResponse, StatsResponse
from archon_search.telemetry.reader import TelemetryReader

logger = logging.getLogger("archon.search")

router = APIRouter()


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
