"""Telemetry entry model — privacy-safe Pydantic schema.

The model enforces the structural privacy guarantee: it has no field that can
carry raw query text. Factories further constrain construction to
keyword-only safe arguments.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.functional_validators import BeforeValidator

if TYPE_CHECKING:
    from archon_search.filters import SearchFilters


def _strict_bool(v: object) -> bool:
    if not isinstance(v, bool):
        raise ValueError(f"Expected bool, got {type(v).__name__!r}")
    return v


StrictBool = Annotated[bool, BeforeValidator(_strict_bool)]


class FilterFlags(BaseModel):
    """Privacy-safe boolean flags indicating which filters were active.

    Only records whether each filter was used — never the raw filter values.
    ``language_filter_used``: True when a language filter was applied.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    file_type: StrictBool = False
    source_path_prefix: StrictBool = False
    source_path_glob: StrictBool = False
    indexed_after: StrictBool = False
    indexed_before: StrictBool = False
    include_metadata: StrictBool = False
    language_filter_used: StrictBool = False

    @classmethod
    def from_search_filters(cls, filters: "SearchFilters") -> "FilterFlags":
        """Build FilterFlags from a SearchFilters — booleans only, no raw values."""
        return cls(
            file_type=filters.file_type is not None,
            source_path_prefix=filters.source_path_prefix is not None,
            source_path_glob=filters.source_path_glob is not None,
            indexed_after=filters.indexed_after is not None,
            indexed_before=filters.indexed_before is not None,
            include_metadata=filters.include_metadata,  # mirrors value directly: already a bool
            language_filter_used=filters.language is not None,
        )


class EndpointKind(StrEnum):
    search = "search"
    search_with_context = "search_with_context"
    search_multi = "search_multi"
    route = "route"
    explain = "explain"


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
        "fanout_count",
        "excluded_count",
        "error_kind",
        "filter_flags",
        "correlation_id",
        "rag_fusion_applied",
        "rag_fusion_queries_used",
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

    fanout_count: int | None = None
    excluded_count: int | None = None

    error_kind: ErrorKind | None = None

    filter_flags: FilterFlags = Field(default_factory=FilterFlags)
    correlation_id: str | None = None

    rag_fusion_applied: bool | None = None
    rag_fusion_queries_used: int | None = None

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
        filter_flags: FilterFlags | None = None,
        correlation_id: str | None = None,
        rag_fusion_applied: bool | None = None,
        rag_fusion_queries_used: int | None = None,
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
            filter_flags=filter_flags if filter_flags is not None else FilterFlags(),
            correlation_id=correlation_id,
            rag_fusion_applied=rag_fusion_applied,
            rag_fusion_queries_used=rag_fusion_queries_used,
        )

    @classmethod
    def from_search_multi_result(
        cls,
        *,
        collections: list[str],
        fanout_count: int,
        result_count: int,
        latency_ms: float,
        excluded_count: int,
        correlation_id: str | None = None,
        rag_fusion_applied: bool | None = None,
        rag_fusion_queries_used: int | None = None,
    ) -> TelemetryEntry:
        """Telemetry for a multi-collection (fan-out) search.

        ``fanout_count`` is the number of collections actually searched (after
        model-mismatch exclusions). No ``query`` parameter — the no-raw-query
        structural invariant is preserved.
        """
        return cls(
            query_id=cls._new_query_id(),
            timestamp=cls._now_iso(),
            endpoint="search_multi",
            latency_ms=latency_ms,
            status="ok",
            collections=collections,
            fanout_count=fanout_count,
            result_count=result_count,
            excluded_count=excluded_count,
            correlation_id=correlation_id,
            rag_fusion_applied=rag_fusion_applied,
            rag_fusion_queries_used=rag_fusion_queries_used,
        )

    @classmethod
    def from_route_response(
        cls,
        *,
        collections: list[str],
        decomposer_invoked: bool,
        latency_ms: float,
        correlation_id: str | None = None,
    ) -> TelemetryEntry:
        return cls(
            query_id=cls._new_query_id(),
            timestamp=cls._now_iso(),
            endpoint="route",
            latency_ms=latency_ms,
            status="ok",
            collections=collections,
            decomposer_invoked=decomposer_invoked,
            correlation_id=correlation_id,
        )

    @classmethod
    def from_error(
        cls,
        *,
        endpoint: EndpointKind,
        status: Status,
        error_kind: ErrorKind,
        latency_ms: float,
        correlation_id: str | None = None,
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
            correlation_id=correlation_id,
        )

    @classmethod
    def from_explain_result(
        cls,
        *,
        collection: str,
        result_count: int,
        latency_ms: float,
        correlation_id: str | None = None,
        rag_fusion_applied: bool | None = None,
        rag_fusion_queries_used: int | None = None,
    ) -> TelemetryEntry:
        return cls(
            query_id=cls._new_query_id(),
            timestamp=cls._now_iso(),
            endpoint="explain",
            latency_ms=latency_ms,
            status="ok",
            collection=collection,
            result_count=result_count,
            correlation_id=correlation_id,
            rag_fusion_applied=rag_fusion_applied,
            rag_fusion_queries_used=rag_fusion_queries_used,
        )
