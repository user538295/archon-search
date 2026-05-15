"""Telemetry entry model — privacy-safe Pydantic schema for FEAT-039b.

The model enforces the structural privacy guarantee: it has no field that can
carry raw query text. Factories (Task 1.4) further constrain construction to
keyword-only safe arguments.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class EndpointKind(StrEnum):
    search = "search"
    search_with_context = "search_with_context"
    route = "route"


class Status(StrEnum):
    ok = "ok"
    validation_error = "validation_error"
    timeout = "timeout"
    internal_error = "internal_error"


class ErrorKind(StrEnum):
    empty_query = "empty_query"
    slot_out_of_range = "slot_out_of_range"
    timeout = "timeout"
    internal_error = "internal_error"
    validation_error = "validation_error"
    other = "other"

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

    @staticmethod
    def _new_query_id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    @classmethod
    def from_search_tool_result(
        cls,
        *,
        endpoint: Literal["search", "search_with_context"],
        collection: str,
        result_doc_ids: list[str],
        latency_ms: float,
    ) -> TelemetryEntry:
        if endpoint not in ("search", "search_with_context"):
            raise ValueError(
                f"from_search_tool_result endpoint must be 'search' or "
                f"'search_with_context', got {endpoint!r}"
            )
        return cls(
            query_id=cls._new_query_id(),
            timestamp=cls._now_iso(),
            endpoint=endpoint,
            latency_ms=latency_ms,
            status="ok",
            collection=collection,
            result_count=len(result_doc_ids),
            result_doc_ids=result_doc_ids,
        )

    @classmethod
    def from_route_response(
        cls,
        *,
        collections: list[str],
        decomposer_invoked: bool,
        latency_ms: float,
    ) -> TelemetryEntry:
        return cls(
            query_id=cls._new_query_id(),
            timestamp=cls._now_iso(),
            endpoint="route",
            latency_ms=latency_ms,
            status="ok",
            collections=collections,
            decomposer_invoked=decomposer_invoked,
        )

    @classmethod
    def from_error(
        cls,
        *,
        endpoint: EndpointKind,
        status: Status,
        error_kind: ErrorKind,
        latency_ms: float,
    ) -> TelemetryEntry:
        if status == "ok":
            raise ValueError("from_error requires a non-'ok' status")
        return cls(
            query_id=cls._new_query_id(),
            timestamp=cls._now_iso(),
            endpoint=endpoint,
            latency_ms=latency_ms,
            status=status,
            error_kind=error_kind,
        )
