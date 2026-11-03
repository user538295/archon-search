"""``ServiceError``: the closed kind set, fixed sanitized details, telemetry mapping and the shared wire constants."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from archon_search.embedder_cache import (
    EMBEDDER_NOT_READY_CODE,
    EMBEDDER_NOT_READY_DETAIL,
)
from archon_search.pipeline import (
    METADATA_STORE_UNAVAILABLE_CODE,
    METADATA_STORE_UNAVAILABLE_DETAIL,
)
from archon_search.services.service_error import (
    COLLECTION_NOT_FOUND_DETAIL,
    GRAPH_COMMUNITIES_NOT_BUILT_CODE,
    INTERNAL_ERROR_CODE,
    INVALID_SCOPE_FILTER_CODE,
    RETRY_AFTER_SECONDS,
    SEARCH_FAILED_DETAIL,
    ServiceError,
    ServiceErrorKind,
    TelemetryOutcome,
    fanout_exceeded_detail,
    top_k_exceeded_detail,
)

_SUPPLIED_DETAIL_KINDS = frozenset(
    {
        ServiceErrorKind.VALIDATION,
        ServiceErrorKind.INVALID_GRAPH_MODE,
        ServiceErrorKind.GRAPH_DISABLED,
        ServiceErrorKind.SCOPE_FILTER_GRAPH_MODE_INCOMPATIBLE,
        ServiceErrorKind.INVALID_SCOPE_FILTER,
        ServiceErrorKind.GRAPH_COMMUNITIES_NOT_BUILT,
    }
)
_FIXED_DETAIL_KINDS = frozenset(ServiceErrorKind) - _SUPPLIED_DETAIL_KINDS
_EXCEPTION_TEXT = "lance error: vector dimension 384 != 768 at /var/data/store"


def test_kind_set_is_the_closed_plan_set() -> None:
    assert {kind.value for kind in ServiceErrorKind} == {
        "validation",
        "invalid_graph_mode",
        "graph_disabled",
        "scope_filter_graph_mode_incompatible",
        "invalid_scope_filter",
        "graph_communities_not_built",
        "not_found",
        "metadata_store_error",
        "embedder_not_ready",
        "timeout",
        "hyde_failed",
        "internal",
    }


@pytest.mark.parametrize("kind", sorted(_FIXED_DETAIL_KINDS))
def test_service_error_kinds_carry_fixed_sanitized_details(kind: ServiceErrorKind) -> None:
    first, second = ServiceError(kind), ServiceError(kind)
    assert (first.code, first.detail) == (second.code, second.detail)
    assert first.code
    assert first.detail
    assert _EXCEPTION_TEXT not in first.detail


@pytest.mark.parametrize("kind", sorted(_FIXED_DETAIL_KINDS))
def test_fixed_detail_kinds_refuse_a_caller_supplied_detail(kind: ServiceErrorKind) -> None:
    with pytest.raises(ValueError, match="fixed detail"):
        ServiceError(kind, _EXCEPTION_TEXT)


@pytest.mark.parametrize("kind", sorted(_SUPPLIED_DETAIL_KINDS))
def test_supplied_detail_kinds_carry_the_callers_built_text_and_need_one(kind: ServiceErrorKind) -> None:
    error = ServiceError(kind, "built from typed data")
    assert error.detail == "built from typed data"
    assert error.kind is kind
    with pytest.raises(ValueError, match="requires a detail"):
        ServiceError(kind)


def test_str_of_the_error_is_its_fixed_detail() -> None:
    assert str(ServiceError(ServiceErrorKind.INTERNAL)) == SEARCH_FAILED_DETAIL


@pytest.mark.parametrize(
    ("kind", "code", "detail"),
    [
        (ServiceErrorKind.NOT_FOUND, "not_found", COLLECTION_NOT_FOUND_DETAIL),
        (ServiceErrorKind.METADATA_STORE_ERROR, METADATA_STORE_UNAVAILABLE_CODE, METADATA_STORE_UNAVAILABLE_DETAIL),
        (ServiceErrorKind.EMBEDDER_NOT_READY, EMBEDDER_NOT_READY_CODE, EMBEDDER_NOT_READY_DETAIL),
        (ServiceErrorKind.TIMEOUT, "timeout", "search timed out"),
        (ServiceErrorKind.HYDE_FAILED, INTERNAL_ERROR_CODE, SEARCH_FAILED_DETAIL),
        (ServiceErrorKind.INTERNAL, INTERNAL_ERROR_CODE, SEARCH_FAILED_DETAIL),
    ],
)
def test_fixed_kinds_keep_the_wire_code_and_text_the_interfaces_use_today(
    kind: ServiceErrorKind, code: str, detail: str
) -> None:
    error = ServiceError(kind)
    assert (error.code, error.detail) == (code, detail)


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        (ServiceErrorKind.VALIDATION, "validation_error"),
        (ServiceErrorKind.INVALID_GRAPH_MODE, "invalid_graph_mode"),
        (ServiceErrorKind.GRAPH_DISABLED, "graph_disabled"),
        (ServiceErrorKind.SCOPE_FILTER_GRAPH_MODE_INCOMPATIBLE, "scope_filter_graph_mode_incompatible"),
        (ServiceErrorKind.INVALID_SCOPE_FILTER, INVALID_SCOPE_FILTER_CODE),
        (ServiceErrorKind.GRAPH_COMMUNITIES_NOT_BUILT, GRAPH_COMMUNITIES_NOT_BUILT_CODE),
    ],
)
def test_supplied_detail_kinds_keep_the_wire_code_the_interfaces_use_today(
    kind: ServiceErrorKind, code: str
) -> None:
    assert ServiceError(kind, "x").code == code


def test_only_embedder_not_ready_carries_the_retry_after_seconds() -> None:
    for kind in ServiceErrorKind:
        error = ServiceError(kind) if kind in _FIXED_DETAIL_KINDS else ServiceError(kind, "x")
        expected = RETRY_AFTER_SECONDS if kind is ServiceErrorKind.EMBEDDER_NOT_READY else None
        assert error.retry_after_seconds == expected


def test_the_wire_constant_values_are_unchanged() -> None:
    assert RETRY_AFTER_SECONDS == 30
    assert SEARCH_FAILED_DETAIL == "search failed"
    assert INTERNAL_ERROR_CODE == "internal_error"
    assert COLLECTION_NOT_FOUND_DETAIL == "collection not found"
    assert INVALID_SCOPE_FILTER_CODE == "invalid_scope_filter"
    assert GRAPH_COMMUNITIES_NOT_BUILT_CODE == "graph_communities_not_built"


def test_fanout_and_top_k_messages_are_byte_identical_to_the_adapters_today() -> None:
    assert fanout_exceeded_detail(5) == "collections length exceeds maximum of 5"
    assert top_k_exceeded_detail(60, 50) == "top_k 60 exceeds operator-configured maximum of 50"


@pytest.mark.parametrize(
    ("kind", "outcome"),
    [
        (ServiceErrorKind.TIMEOUT, TelemetryOutcome(status="timeout", error_kind="timeout")),
        (ServiceErrorKind.INTERNAL, TelemetryOutcome(status="internal_error", error_kind="other")),
        (ServiceErrorKind.HYDE_FAILED, TelemetryOutcome(status="internal_error", error_kind="other")),
        (ServiceErrorKind.EMBEDDER_NOT_READY, TelemetryOutcome(status="internal_error", error_kind="other")),
        (ServiceErrorKind.METADATA_STORE_ERROR, TelemetryOutcome(status="internal_error", error_kind="other")),
    ],
)
def test_service_error_telemetry_mapping_matches_the_plan_table(kind: ServiceErrorKind, outcome: TelemetryOutcome) -> None:
    assert ServiceError(kind).telemetry == outcome


@pytest.mark.parametrize(
    "kind",
    [
        ServiceErrorKind.VALIDATION,
        ServiceErrorKind.INVALID_GRAPH_MODE,
        ServiceErrorKind.GRAPH_DISABLED,
        ServiceErrorKind.SCOPE_FILTER_GRAPH_MODE_INCOMPATIBLE,
        ServiceErrorKind.INVALID_SCOPE_FILTER,
        ServiceErrorKind.GRAPH_COMMUNITIES_NOT_BUILT,
        ServiceErrorKind.NOT_FOUND,
    ],
)
def test_validation_class_kinds_and_not_found_record_nothing(kind: ServiceErrorKind) -> None:
    error = ServiceError(kind) if kind in _FIXED_DETAIL_KINDS else ServiceError(kind, "x")
    assert error.telemetry is None


def test_telemetry_outcomes_are_accepted_by_the_telemetry_entry_factory() -> None:
    from archon_search.telemetry.entry import TelemetryEntry

    for kind in ServiceErrorKind:
        error = ServiceError(kind) if kind in _FIXED_DETAIL_KINDS else ServiceError(kind, "x")
        if error.telemetry is None:
            continue
        entry = TelemetryEntry.from_error(
            endpoint="search", status=error.telemetry.status, error_kind=error.telemetry.error_kind, latency_ms=1.0
        )
        assert entry.status == error.telemetry.status


_ADAPTER_SOURCES = ("mcp.py", "routes_search.py", "routes_explain.py", "app.py", "schemas.py")
_DUPLICATED_STRING_CONSTANTS = frozenset(
    {
        "collection not found",
        "invalid_scope_filter",
        "graph_communities_not_built",
    }
)
# ``internal_error`` is also a telemetry status, so only its use as a wire ``code=`` counts.
_DUPLICATED_CODE_VALUES = frozenset({"internal_error"})
# A bare "search failed" is also a log message (``logger.exception("search failed")``); every other use is wire text.
_SEARCH_FAILED_WIRE_USE = re.compile(r'(?<!exception\()"search failed"')
_DUPLICATED_FRAGMENTS = (
    '"Retry-After": "30"',
    '"Retry-After"] = "30"',
    "collections length exceeds maximum of",
    "exceeds operator-configured maximum of",
)


@pytest.mark.parametrize("name", _ADAPTER_SOURCES)
def test_adapters_keep_no_copy_of_the_wire_text_the_service_owns(name: str) -> None:
    import archon_search.server as server_pkg

    source = (Path(server_pkg.__file__).parent / name).read_text(encoding="utf-8")
    constants = {
        node.value for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    code_values = {
        node.value.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.keyword)
        and node.arg == "code"
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    assert sorted(constants & _DUPLICATED_STRING_CONSTANTS) == []
    assert sorted(code_values & _DUPLICATED_CODE_VALUES) == []
    assert _SEARCH_FAILED_WIRE_USE.findall(source) == []
    assert [fragment for fragment in _DUPLICATED_FRAGMENTS if fragment in source] == []
