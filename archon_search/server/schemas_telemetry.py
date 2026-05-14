"""Pydantic response models for telemetry API endpoints (FEAT-039c Task 3.1).

Pure data models — no business logic.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LatencyPercentiles(BaseModel):
    p50: float | None
    p95: float | None


class EndpointStats(BaseModel):
    total: int
    ok: int
    error: int


class CollectionStats(BaseModel):
    # Note: `total` counts can exceed `total_queries` at the response level
    # because routing entries fan out to multiple collections.
    total: int
    ok: int


class ErrorBreakdown(BaseModel):
    empty_query: int = 0
    slot_out_of_range: int = 0
    timeout: int = 0
    internal_error: int = 0
    validation_error: int = 0
    other: int = 0


class StatsResponse(BaseModel):
    schema_version: int = 1
    enabled: bool
    since: str | None = None
    until: str | None = None
    total_queries: int = 0
    success_rate: float | None = None
    skipped_lines: int = 0
    latency_ms: LatencyPercentiles = Field(
        default_factory=lambda: LatencyPercentiles(p50=None, p95=None)
    )
    by_endpoint: dict[str, EndpointStats] = Field(default_factory=dict)
    by_collection: dict[str, CollectionStats] = Field(default_factory=dict)
    error_breakdown: ErrorBreakdown = Field(default_factory=ErrorBreakdown)


class EntriesResponse(BaseModel):
    schema_version: int = 1
    enabled: bool
    entries: list[dict[str, Any]]
    next_offset: int
    total_in_window: int
    skipped_lines: int = 0


class DisabledResponse(BaseModel):
    enabled: bool = False
