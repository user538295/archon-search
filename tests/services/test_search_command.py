"""``SearchCommand`` and its one normaliser: REST's check order, REST's exact strings, stripping, the ORed metadata flag."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from archon_search.filters import SearchFilters
from archon_search.services.search_command import (
    SearchCommand,
    build_search_filters,
    normalize_request_shape,
    normalize_search_command,
)
from archon_search.services.service_error import (
    INVALID_SEARCH_FILTERS_DETAIL,
    ServiceError,
    ServiceErrorKind,
    fanout_exceeded_detail,
    top_k_exceeded_detail,
)

_MAX_FANOUT = 3
_TOP_K_MAX = 20


def _command(**overrides: Any) -> SearchCommand:
    base = SearchCommand(query="q", collection="docs")
    return replace(base, **overrides)


def _normalize(command: SearchCommand, *, graph_enabled: bool = True) -> SearchCommand:
    return normalize_search_command(
        command, max_fanout=_MAX_FANOUT, top_k_max=_TOP_K_MAX, graph_enabled=graph_enabled
    )


def _failure(command: SearchCommand, *, graph_enabled: bool = True) -> ServiceError:
    with pytest.raises(ServiceError) as caught:
        _normalize(command, graph_enabled=graph_enabled)
    return caught.value


def test_valid_single_collection_command_passes_unchanged() -> None:
    command = _command(top_k=5, hyde=True, rag_fusion=True, acl_context=True)
    assert _normalize(command) == command


def test_validation_order_matches_the_rest_handler() -> None:
    # Breaks every rule at once; each step removes the rule that just fired.
    command = _command(
        collection=None,
        collections=("a", "b", "c", "d"),
        top_k=_TOP_K_MAX + 1,
        scope_filter="*bad",
        graph_mode="ppr",
    )
    failure = _failure(command, graph_enabled=False)
    assert failure.kind is ServiceErrorKind.VALIDATION
    assert failure.detail == fanout_exceeded_detail(_MAX_FANOUT)

    # REST's field checks (top_k below one, graph_mode outside the set) run before the handler's fan-out check.
    below_minimum = _failure(replace(command, top_k=0), graph_enabled=False)
    assert below_minimum.detail == "top_k must be at least 1"
    bad_mode = _failure(replace(command, graph_mode="bogus"), graph_enabled=False)
    assert bad_mode.kind is ServiceErrorKind.INVALID_GRAPH_MODE

    command = replace(command, collections=("a", "b"))
    failure = _failure(command, graph_enabled=False)
    assert failure.kind is ServiceErrorKind.VALIDATION
    assert failure.detail == top_k_exceeded_detail(_TOP_K_MAX + 1, _TOP_K_MAX)

    command = replace(command, top_k=None)
    failure = _failure(command, graph_enabled=False)
    assert failure.kind is ServiceErrorKind.INVALID_SCOPE_FILTER
    assert failure.detail == "scope_filter wildcard '*' must appear only at the end of the string"

    command = replace(command, scope_filter="user:*")
    failure = _failure(command, graph_enabled=False)
    assert failure.kind is ServiceErrorKind.SCOPE_FILTER_GRAPH_MODE_INCOMPATIBLE
    assert failure.detail == "scope_filter is not supported with graph_mode"

    command = replace(command, scope_filter=None)
    failure = _failure(command, graph_enabled=False)
    assert failure.kind is ServiceErrorKind.VALIDATION
    assert failure.detail == "graph_mode is not supported with multi-collection fanout; use a single collection"

    command = replace(command, graph_mode="local")
    failure = _failure(command, graph_enabled=False)
    assert failure.kind is ServiceErrorKind.GRAPH_DISABLED
    assert failure.detail == "graph_mode requires [graph] enabled=true in server config"

    assert _normalize(command, graph_enabled=True) == command


def test_ppr_with_collections_is_rejected_even_for_one_name() -> None:
    failure = _failure(_command(collection=None, collections=("only",), graph_mode="ppr"))
    assert failure.kind is ServiceErrorKind.VALIDATION
    assert failure.detail == "graph_mode is not supported with multi-collection fanout; use a single collection"


def test_ppr_with_a_single_collection_is_accepted() -> None:
    assert _normalize(_command(graph_mode="ppr")).graph_mode == "ppr"


def test_include_metadata_flags_are_ored_once() -> None:
    nested = SearchFilters(include_metadata=True)
    assert _normalize(_command(filters=nested)).wants_metadata is True
    assert _normalize(_command(include_metadata=True)).wants_metadata is True
    assert _normalize(_command(include_metadata=True, filters=nested)).wants_metadata is True
    assert _normalize(_command(filters=SearchFilters(file_type="md"))).wants_metadata is False
    assert _normalize(_command()).wants_metadata is False


def test_empty_or_padded_query_and_collection_are_validated_and_stripped() -> None:
    for query in ("", "   "):
        failure = _failure(_command(query=query))
        assert (failure.kind, failure.detail) == (ServiceErrorKind.VALIDATION, "query must not be empty")
    for collection in ("", "  "):
        failure = _failure(_command(collection=collection))
        assert (failure.kind, failure.detail) == (ServiceErrorKind.VALIDATION, "collection must not be empty")

    normalized = _normalize(_command(query="  hello \n", collection="  docs "))
    assert (normalized.query, normalized.collection) == ("hello", "docs")


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"collection": None}, "supply either collection or collections"),
        ({"collections": ("a",)}, "supply either collection or collections, not both"),
        ({"collection": None, "collections": ()}, "collections must not be empty"),
        ({"collection": None, "collections": ("a", " ")}, "collection names must not be empty or whitespace"),
    ],
)
def test_collection_selection_errors_use_rest_text(overrides: dict[str, Any], detail: str) -> None:
    failure = _failure(_command(**overrides))
    assert (failure.kind, failure.detail) == (ServiceErrorKind.VALIDATION, detail)


def test_collections_are_stripped_and_deduplicated_before_the_fanout_limit() -> None:
    padded = ("a", " a", "b ", "b", "c", "c ")
    normalized = _normalize(_command(collection=None, collections=padded))
    assert normalized.collections == ("a", "b", "c")
    assert normalized.collection is None


def test_top_k_bounds() -> None:
    assert _normalize(_command(top_k=1)).top_k == 1
    assert _normalize(_command(top_k=_TOP_K_MAX)).top_k == _TOP_K_MAX
    assert _normalize(_command(top_k=None)).top_k is None
    below = _failure(_command(top_k=0))
    assert (below.kind, below.detail) == (ServiceErrorKind.VALIDATION, "top_k must be at least 1")
    above = _failure(_command(top_k=_TOP_K_MAX + 1))
    assert above.detail == top_k_exceeded_detail(_TOP_K_MAX + 1, _TOP_K_MAX)


def test_invalid_graph_mode_is_its_own_kind() -> None:
    failure = _failure(_command(graph_mode="bogus"))
    assert failure.kind is ServiceErrorKind.INVALID_GRAPH_MODE
    assert failure.detail == "graph_mode must be one of ['naive', 'local', 'global', 'ppr']"


@pytest.mark.parametrize(
    ("scope_filter", "detail"),
    [
        ("", "scope_filter must not be empty"),
        ("*", "bare '*' is not a valid scope_filter; use a prefix followed by '*' for wildcard matching"),
        ("a*b*", "scope_filter contains multiple '*' characters; only a single trailing '*' is permitted"),
    ],
)
def test_invalid_scope_filter_syntax_uses_rest_text(scope_filter: str, detail: str) -> None:
    failure = _failure(_command(scope_filter=scope_filter))
    assert failure.kind is ServiceErrorKind.INVALID_SCOPE_FILTER
    assert failure.detail == detail


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"query": " ", "top_k": 0, "graph_mode": "bogus"}, "query must not be empty"),
        ({"top_k": 0, "graph_mode": "bogus"}, "top_k must be at least 1"),
        ({"graph_mode": "bogus"}, "graph_mode must be one of ['naive', 'local', 'global', 'ppr']"),
    ],
)
def test_field_checks_run_before_the_selection_check(overrides: dict[str, Any], detail: str) -> None:
    failure = _failure(_command(collection=None, **overrides))
    assert failure.detail == detail


def test_request_shape_needs_no_limits_and_skips_the_limit_rules() -> None:
    over_every_limit = _command(
        collection=None,
        collections=tuple(f"c{i}" for i in range(_MAX_FANOUT + 1)),
        top_k=_TOP_K_MAX + 1,
        scope_filter="*bad",
        graph_mode="ppr",
    )
    assert normalize_request_shape(over_every_limit).collections == over_every_limit.collections
    with pytest.raises(ServiceError):
        normalize_request_shape(_command(query=" "))


def test_no_filter_means_no_filter() -> None:
    assert build_search_filters() is None
    assert _normalize(_command()).filters is None


def test_flat_filters_build_the_same_model_as_nested() -> None:
    built = build_search_filters(file_type=".MD", language="EN")
    assert built == SearchFilters(file_type=".MD", language="EN")
    assert built is not None
    assert built.file_type == "md"


@pytest.mark.parametrize(
    "bad",
    [
        {"indexed_after": "not-a-date"},
        {"source_path_glob": ""},
        {"file_type": ""},
        {"language": "english"},
    ],
)
def test_invalid_filter_value_is_a_validation_error_with_a_fixed_detail(bad: dict[str, Any]) -> None:
    with pytest.raises(ServiceError) as caught:
        build_search_filters(**bad)
    assert caught.value.kind is ServiceErrorKind.VALIDATION
    assert caught.value.detail == INVALID_SEARCH_FILTERS_DETAIL
    assert str(caught.value) == INVALID_SEARCH_FILTERS_DETAIL
