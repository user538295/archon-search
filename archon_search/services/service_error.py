"""The one error type of the application services, and the wire text that goes with it.

A :class:`ServiceError` is raised by a service for every failure and carries no exception: each kind has a stable wire
code, a fixed sanitized detail (never ``str(exc)``; the full exception is logged where it is caught) and a telemetry
mapping.  REST, MCP and the OpenAI shim each render it through their own error table.

The wire text shared by several adapters (codes, the ``search failed`` detail, ``collection not found``, the fan-out and
``top_k`` messages, the ``Retry-After`` hint) is defined here once; limits and valid sets live in ``constants.py``.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal, NamedTuple

from archon_search.embedder_cache import (
    EMBEDDER_NOT_READY_CODE,
    EMBEDDER_NOT_READY_DETAIL,
)
from archon_search.pipeline import (
    METADATA_STORE_UNAVAILABLE_CODE,
    METADATA_STORE_UNAVAILABLE_DETAIL,
)

# ``CodedErrorDetail`` codes POST /search and POST /explain (and their MCP tools) answer with.
INVALID_SCOPE_FILTER_CODE: Final[str] = "invalid_scope_filter"
GRAPH_COMMUNITIES_NOT_BUILT_CODE: Final[str] = "graph_communities_not_built"

# Wire-safe error pair for an unexpected search failure. Never ``str(exc)``: store and model errors embed internals
# (e.g. LanceDB's vector-dimension message); the full exception is logged server-side.
SEARCH_FAILED_DETAIL: Final[str] = "search failed"
INTERNAL_ERROR_CODE: Final[str] = "internal_error"

NOT_FOUND_CODE: Final[str] = "not_found"
COLLECTION_NOT_FOUND_DETAIL: Final[str] = "collection not found"

# Wire-safe text for a rejected filter value: a Pydantic ``ValidationError`` embeds the rejected input, so it is never echoed.
INVALID_SEARCH_FILTERS_DETAIL: Final[str] = "invalid search filters"

VALIDATION_ERROR_CODE: Final[str] = "validation_error"
INVALID_GRAPH_MODE_CODE: Final[str] = "invalid_graph_mode"
GRAPH_DISABLED_CODE: Final[str] = "graph_disabled"
SCOPE_FILTER_GRAPH_MODE_INCOMPATIBLE_CODE: Final[str] = "scope_filter_graph_mode_incompatible"

TIMEOUT_CODE: Final[str] = "timeout"
SEARCH_TIMEOUT_DETAIL: Final[str] = "search timed out"

# The one hint behind every ``Retry-After`` header of a 503 for an embedder that is not ready.
RETRY_AFTER_SECONDS: Final[int] = 30


def fanout_exceeded_detail(max_fanout: int) -> str:
    """The 422 text for a ``collections`` list longer than the operator's ``max_fanout``."""
    return f"collections length exceeds maximum of {max_fanout}"


def top_k_exceeded_detail(top_k: int, top_k_max: int) -> str:
    """The 422 text for a ``top_k`` above the operator's ``top_k_max``."""
    return f"top_k {top_k} exceeds operator-configured maximum of {top_k_max}"


class ServiceErrorKind(StrEnum):
    """The closed set of failures a search service reports."""

    VALIDATION = "validation"
    INVALID_GRAPH_MODE = "invalid_graph_mode"
    GRAPH_DISABLED = "graph_disabled"
    SCOPE_FILTER_GRAPH_MODE_INCOMPATIBLE = "scope_filter_graph_mode_incompatible"
    INVALID_SCOPE_FILTER = "invalid_scope_filter"
    GRAPH_COMMUNITIES_NOT_BUILT = "graph_communities_not_built"
    NOT_FOUND = "not_found"
    METADATA_STORE_ERROR = "metadata_store_error"
    EMBEDDER_NOT_READY = "embedder_not_ready"
    TIMEOUT = "timeout"
    HYDE_FAILED = "hyde_failed"
    INTERNAL = "internal"


class TelemetryOutcome(NamedTuple):
    """The ``TelemetryEntry.from_error`` arguments a recorded kind maps to."""

    status: Literal["timeout", "internal_error"]
    error_kind: Literal["timeout", "other"]


_TIMEOUT_OUTCOME = TelemetryOutcome(status="timeout", error_kind="timeout")
_INTERNAL_OUTCOME = TelemetryOutcome(status="internal_error", error_kind="other")

# kind -> wire code
_CODES: Final[dict[ServiceErrorKind, str]] = {
    ServiceErrorKind.VALIDATION: VALIDATION_ERROR_CODE,
    ServiceErrorKind.INVALID_GRAPH_MODE: INVALID_GRAPH_MODE_CODE,
    ServiceErrorKind.GRAPH_DISABLED: GRAPH_DISABLED_CODE,
    ServiceErrorKind.SCOPE_FILTER_GRAPH_MODE_INCOMPATIBLE: SCOPE_FILTER_GRAPH_MODE_INCOMPATIBLE_CODE,
    ServiceErrorKind.INVALID_SCOPE_FILTER: INVALID_SCOPE_FILTER_CODE,
    ServiceErrorKind.GRAPH_COMMUNITIES_NOT_BUILT: GRAPH_COMMUNITIES_NOT_BUILT_CODE,
    ServiceErrorKind.NOT_FOUND: NOT_FOUND_CODE,
    ServiceErrorKind.METADATA_STORE_ERROR: METADATA_STORE_UNAVAILABLE_CODE,
    ServiceErrorKind.EMBEDDER_NOT_READY: EMBEDDER_NOT_READY_CODE,
    ServiceErrorKind.TIMEOUT: TIMEOUT_CODE,
    ServiceErrorKind.HYDE_FAILED: INTERNAL_ERROR_CODE,
    ServiceErrorKind.INTERNAL: INTERNAL_ERROR_CODE,
}

# kind -> fixed detail. A kind absent here takes its text from the caller (built from typed data, never an exception).
_FIXED_DETAILS: Final[dict[ServiceErrorKind, str]] = {
    ServiceErrorKind.NOT_FOUND: COLLECTION_NOT_FOUND_DETAIL,
    ServiceErrorKind.METADATA_STORE_ERROR: METADATA_STORE_UNAVAILABLE_DETAIL,
    ServiceErrorKind.EMBEDDER_NOT_READY: EMBEDDER_NOT_READY_DETAIL,
    ServiceErrorKind.TIMEOUT: SEARCH_TIMEOUT_DETAIL,
    ServiceErrorKind.HYDE_FAILED: SEARCH_FAILED_DETAIL,
    ServiceErrorKind.INTERNAL: SEARCH_FAILED_DETAIL,
}

# kind -> telemetry outcome. A kind absent here is not recorded.
_TELEMETRY: Final[dict[ServiceErrorKind, TelemetryOutcome]] = {
    ServiceErrorKind.TIMEOUT: _TIMEOUT_OUTCOME,
    ServiceErrorKind.INTERNAL: _INTERNAL_OUTCOME,
    ServiceErrorKind.HYDE_FAILED: _INTERNAL_OUTCOME,
    ServiceErrorKind.EMBEDDER_NOT_READY: _INTERNAL_OUTCOME,
    ServiceErrorKind.METADATA_STORE_ERROR: _INTERNAL_OUTCOME,
}


class ServiceError(Exception):
    """A search failure: ``kind``, a stable wire ``code`` and a sanitized ``detail``.

    Kinds with a fixed detail refuse a caller-supplied one; the other kinds require the caller's text, which the service
    builds from typed data and never from an exception's message.
    """

    def __init__(self, kind: ServiceErrorKind, detail: str | None = None) -> None:
        fixed = _FIXED_DETAILS.get(kind)
        if fixed is None:
            if detail is None:
                raise ValueError(f"{kind.value} requires a detail")
            resolved = detail
        else:
            if detail is not None:
                raise ValueError(f"{kind.value} has a fixed detail; none may be supplied")
            resolved = fixed
        super().__init__(resolved)
        self.kind = kind
        self.code = _CODES[kind]
        self.detail = resolved

    @property
    def retry_after_seconds(self) -> int | None:
        """The ``Retry-After`` hint; set only for an embedder that is not ready."""
        return RETRY_AFTER_SECONDS if self.kind is ServiceErrorKind.EMBEDDER_NOT_READY else None

    @property
    def telemetry(self) -> TelemetryOutcome | None:
        """The ``TelemetryEntry.from_error`` arguments for a recorded kind, ``None`` for one that is not recorded."""
        return _TELEMETRY.get(self.kind)
