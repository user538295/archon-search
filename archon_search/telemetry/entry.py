"""Telemetry entry model — privacy-safe Pydantic schema for FEAT-039b.

The model enforces the structural privacy guarantee: it has no field that can
carry raw query text. Factories (Task 1.4) further constrain construction to
keyword-only safe arguments.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

EndpointKind = Literal["search", "search_with_context", "route"]
Status = Literal["ok", "validation_error", "timeout", "internal_error"]
ErrorKind = Literal[
    "empty_query",
    "slot_out_of_range",
    "timeout",
    "internal_error",
    "validation_error",
    "other",
]

DOCUMENTED_SCHEMA_FIELDS: frozenset[str] = frozenset(
    {
        "query_id",
        "timestamp",
        "endpoint",
        "latency_ms",
        "status",
        "collection",
        "result_count",
        "result_doc_ids",
        "truncated",
        "collections",
        "decomposer_invoked",
        "error_kind",
    }
)


class TelemetryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: str
    timestamp: str
    endpoint: EndpointKind
    latency_ms: float
    status: Status

    collection: str | None = None
    result_count: int | None = None
    result_doc_ids: list[str] | None = None
    truncated: bool | None = None

    collections: list[str] | None = None
    decomposer_invoked: bool | None = None

    error_kind: ErrorKind | None = None
