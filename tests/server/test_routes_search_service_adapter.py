"""REST ``POST /search`` single-collection branch: a thin adapter over ``SearchService`` (BE-8).

The route resolves the caller, builds a command, calls the service and renders the outcome or the ``ServiceError``
through one error table.  These tests drive the route with a stub service, so only the adapter is under test.
"""
from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from archon_search._types import SearchResult
from archon_search.embedder_cache import (
    EMBEDDER_NOT_READY_CODE,
    EMBEDDER_NOT_READY_DETAIL,
)
from archon_search.filters import SearchFilters
from archon_search.pipeline import (
    METADATA_STORE_UNAVAILABLE_CODE,
    METADATA_STORE_UNAVAILABLE_DETAIL,
)
from archon_search.server import routes_search
from archon_search.services.authz import AuthIdentity
from archon_search.services.search_command import SearchCommand
from archon_search.services.search_service import AclGate, SearchHit, SearchOutcome
from archon_search.services.service_error import (
    COLLECTION_NOT_FOUND_DETAIL,
    GRAPH_COMMUNITIES_NOT_BUILT_CODE,
    INVALID_SCOPE_FILTER_CODE,
    RETRY_AFTER_SECONDS,
    SEARCH_FAILED_DETAIL,
    ServiceError,
    ServiceErrorKind,
)

_NAMESPACE = "tenant-a"
_FACTORY = "archon_search.server.routes_search.build_search_service_from_state"
_BODY = {"collection": "col", "query": "q"}
_PLAIN_500_BODY = "Internal Server Error"
_TIMEOUT_DETAIL = "Search timed out"


def _app(*, authenticated: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(routes_search.router)
    app.state.pipeline = MagicMock()
    app.state.config = MagicMock()
    if authenticated:

        @app.middleware("http")
        async def _stamp_identity(request: Request, call_next):  # type: ignore[no-untyped-def]
            request.state.namespace = _NAMESPACE
            return await call_next(request)

    return app


def _outcome(*hits: SearchHit, include_metadata: bool = False, **overrides: object) -> SearchOutcome:
    fields: dict[str, object] = {
        "results": tuple(hits),
        "acl_filtered": False,
        "excluded_collections": (),
        "embedding_model": "model-x",
        "hyde_applied": False,
        "rag_fusion_applied": False,
        "rag_fusion_queries_used": 0,
        "rag_fusion_attempted": False,
        "graph_expansion_applied": False,
        "expansion_used": False,
        "expansion_warning": None,
        "applied_filters": None,
        "ppr_entities_matched": None,
        "include_metadata": include_metadata,
    }
    fields.update(overrides)
    return SearchOutcome(**fields)  # type: ignore[arg-type]


def _hit(*, metadata: dict[str, str] | None = None, acl_gate: AclGate | None = None) -> SearchHit:
    result = SearchResult(
        doc_id="d" * 64,
        chunk_id="d" * 64 + "-000001",
        text="chunk text",
        score=0.5,
        source_path="/p.md",
        metadata=metadata or {"k": "v"},
    )
    return SearchHit(result=result, acl_gate=acl_gate)


@pytest.fixture
def stub_service() -> Iterator[MagicMock]:
    service = MagicMock()
    service.search = AsyncMock(return_value=_outcome())
    with patch(_FACTORY, return_value=service):
        yield service


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


_DETAIL_FOR_KIND = {
    ServiceErrorKind.VALIDATION: "validation text",
    ServiceErrorKind.INVALID_GRAPH_MODE: "graph mode text",
    ServiceErrorKind.GRAPH_DISABLED: "graph disabled text",
    ServiceErrorKind.SCOPE_FILTER_GRAPH_MODE_INCOMPATIBLE: "scope filter text",
    ServiceErrorKind.INVALID_SCOPE_FILTER: "scope syntax text",
    ServiceErrorKind.GRAPH_COMMUNITIES_NOT_BUILT: "communities text",
}

_ERROR_TABLE = [
    (ServiceErrorKind.VALIDATION, 422, {"detail": _DETAIL_FOR_KIND[ServiceErrorKind.VALIDATION]}),
    (ServiceErrorKind.INVALID_GRAPH_MODE, 422, {"detail": _DETAIL_FOR_KIND[ServiceErrorKind.INVALID_GRAPH_MODE]}),
    (ServiceErrorKind.GRAPH_DISABLED, 422, {"detail": _DETAIL_FOR_KIND[ServiceErrorKind.GRAPH_DISABLED]}),
    (
        ServiceErrorKind.SCOPE_FILTER_GRAPH_MODE_INCOMPATIBLE,
        422,
        {"detail": _DETAIL_FOR_KIND[ServiceErrorKind.SCOPE_FILTER_GRAPH_MODE_INCOMPATIBLE]},
    ),
    (
        ServiceErrorKind.INVALID_SCOPE_FILTER,
        400,
        {"detail": {"code": INVALID_SCOPE_FILTER_CODE, "message": _DETAIL_FOR_KIND[ServiceErrorKind.INVALID_SCOPE_FILTER]}},
    ),
    (
        ServiceErrorKind.GRAPH_COMMUNITIES_NOT_BUILT,
        422,
        {
            "detail": {
                "code": GRAPH_COMMUNITIES_NOT_BUILT_CODE,
                "message": _DETAIL_FOR_KIND[ServiceErrorKind.GRAPH_COMMUNITIES_NOT_BUILT],
            }
        },
    ),
    (ServiceErrorKind.NOT_FOUND, 404, {"detail": COLLECTION_NOT_FOUND_DETAIL}),
    (
        ServiceErrorKind.METADATA_STORE_ERROR,
        503,
        {"detail": METADATA_STORE_UNAVAILABLE_DETAIL, "code": METADATA_STORE_UNAVAILABLE_CODE},
    ),
    (
        ServiceErrorKind.EMBEDDER_NOT_READY,
        503,
        {"detail": EMBEDDER_NOT_READY_DETAIL, "code": EMBEDDER_NOT_READY_CODE},
    ),
    (ServiceErrorKind.TIMEOUT, 504, {"detail": _TIMEOUT_DETAIL}),
    (ServiceErrorKind.HYDE_FAILED, 500, {"detail": SEARCH_FAILED_DETAIL}),
    (ServiceErrorKind.INTERNAL, 500, None),
]


def test_error_table_covers_every_service_error_kind() -> None:
    assert {row[0] for row in _ERROR_TABLE} == set(ServiceErrorKind)


@pytest.mark.parametrize(("kind", "status", "body"), _ERROR_TABLE, ids=[row[0].value for row in _ERROR_TABLE])
def test_rest_error_table_renders_each_service_error_kind_to_todays_body(
    stub_service: MagicMock, kind: ServiceErrorKind, status: int, body: dict | None
) -> None:
    """Body shape and status per kind: coded 400/422, string 422, 404, 503 with/without Retry-After, 504, JSON and plain 500."""
    stub_service.search = AsyncMock(side_effect=ServiceError(kind, _DETAIL_FOR_KIND.get(kind)))

    response = _client(_app()).post("/search", json=_BODY)

    assert response.status_code == status
    if body is None:
        assert response.text == _PLAIN_500_BODY
        assert response.headers["content-type"].startswith("text/plain")
    else:
        assert response.json() == body
    expected_retry_after = str(RETRY_AFTER_SECONDS) if kind is ServiceErrorKind.EMBEDDER_NOT_READY else None
    assert response.headers.get("retry-after") == expected_retry_after


def test_search_without_an_identity_is_401_authentication_required_before_the_service() -> None:
    """S17: the route mounted without the API-key middleware answers 401 and never builds or calls the service."""
    with patch(_FACTORY) as factory:
        response = _client(_app(authenticated=False)).post("/search", json=_BODY)

    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}
    factory.assert_not_called()


def test_search_hands_the_service_the_command_and_the_resolved_caller(stub_service: MagicMock) -> None:
    filters = {"file_type": "md", "include_metadata": True}
    body = {
        **_BODY,
        "top_k": 7,
        "filters": filters,
        "hyde": True,
        "rag_fusion": True,
        "graph_mode": "naive",
        "scope_filter": "a=b",
        "acl_context": True,
        "include_metadata": True,
    }

    response = _client(_app()).post("/search", json=body)

    assert response.status_code == 200
    command, caller = stub_service.search.await_args.args
    assert caller == AuthIdentity(namespace=_NAMESPACE, admin=False)
    assert command == SearchCommand(
        query="q",
        collection="col",
        collections=None,
        top_k=7,
        filters=SearchFilters(file_type="md", include_metadata=True),
        include_metadata=True,
        hyde=True,
        rag_fusion=True,
        graph_mode="naive",
        scope_filter="a=b",
        acl_context=True,
    )


@pytest.mark.parametrize(
    ("outcome_flag", "expected"), [(False, {}), (True, {"k": "v"})], ids=["blanked", "kept"]
)
def test_search_blanks_metadata_from_the_outcome_flag_for_top_level_and_nested_include_metadata(
    stub_service: MagicMock, outcome_flag: bool, expected: dict[str, str]
) -> None:
    """S14: the route reads the outcome's ORed flag, never the request flags (they differ here on purpose)."""
    stub_service.search = AsyncMock(return_value=_outcome(_hit(), include_metadata=outcome_flag))
    request_flags = {"include_metadata": not outcome_flag, "filters": {"include_metadata": not outcome_flag}}

    response = _client(_app()).post("/search", json={**_BODY, **request_flags})

    assert response.json()["results"][0]["metadata"] == expected


def test_search_renders_acl_gate_and_every_derived_field_from_the_outcome(stub_service: MagicMock) -> None:
    gate = AclGate(allowed_principals=["alice"], source="sidecar", sidecar_path="a.acl", warnings=["w"])
    filters = SearchFilters(file_type="md")
    stub_service.search = AsyncMock(
        return_value=_outcome(
            _hit(acl_gate=gate),
            _hit(),
            acl_filtered=True,
            embedding_model="m-1",
            hyde_applied=True,
            rag_fusion_applied=True,
            rag_fusion_queries_used=3,
            rag_fusion_attempted=True,
            graph_expansion_applied=True,
            expansion_used=True,
            expansion_warning="warned",
            applied_filters=filters,
            ppr_entities_matched=4,
        )
    )

    data = _client(_app()).post("/search", json=_BODY).json()

    assert data["results"][0]["acl_gate"] == {
        "allowed_principals": ["alice"],
        "source": "sidecar",
        "sidecar_path": "a.acl",
        "warnings": ["w"],
    }
    assert data["results"][1]["acl_gate"] is None
    assert {k: v for k, v in data.items() if k != "results"} == {
        "acl_filtered": True,
        "excluded_collections": [],
        "embedding_model": "m-1",
        "hyde_applied": True,
        "rag_fusion_applied": True,
        "rag_fusion_queries_used": 3,
        "rag_fusion_attempted": True,
        "graph_expansion_applied": True,
        "expansion_used": True,
        "expansion_warning": "warned",
        "applied_filters": filters.model_dump(mode="json"),
        "ppr_entities_matched": 4,
    }


def _validation_errors(body: dict) -> list[tuple[tuple[str | int, ...], str]]:
    response = _client(_app()).post("/search", json=body)
    assert response.status_code == 422
    return [(tuple(item["loc"]), item["msg"]) for item in response.json()["detail"]]


@pytest.mark.parametrize(
    ("body", "location", "message"),
    [
        ({"collection": "col", "query": "  "}, ("body", "query"), "Value error, query must not be empty"),
        ({"collection": " ", "query": "q"}, ("body", "collection"), "Value error, collection must not be empty"),
        ({"query": "q"}, ("body",), "Value error, supply either collection or collections"),
        (
            {"query": "q", "collection": "a", "collections": ["b"]},
            ("body",),
            "Value error, supply either collection or collections, not both",
        ),
        ({"query": "q", "collections": []}, ("body",), "Value error, collections must not be empty"),
        (
            {"query": "q", "collections": ["a", " "]},
            ("body",),
            "Value error, collection names must not be empty or whitespace",
        ),
    ],
    ids=["query", "collection", "no-selection", "both-selections", "empty-list", "blank-name"],
)
def test_request_shape_errors_keep_the_framework_422_list_with_their_field_location(
    body: dict, location: tuple[str, ...], message: str
) -> None:
    """The request model stays transport parsing: each rule reports at the location and with the text clients pin."""
    assert _validation_errors(body) == [(location, message)]


def test_every_failing_field_is_reported_not_only_the_first() -> None:
    """Field-level rules accumulate: an empty query and a ``top_k`` of zero both appear in the one 422 list."""
    locations = [location for location, _ in _validation_errors({"collection": "col", "query": "", "top_k": 0})]
    assert sorted(locations) == [("body", "query"), ("body", "top_k")]


def test_multi_collection_branch_hands_the_service_the_collections_command_and_caller(stub_service: MagicMock) -> None:
    """BE-11: ``collections`` reaches the service as the multi-path command; the route holds no fan-out logic of its own."""
    stub_service.search = AsyncMock(return_value=_outcome(_hit(metadata={"k": "v"}), include_metadata=True))

    response = _client(_app()).post("/search", json={"collections": ["a", "b"], "query": "q", "acl_context": True})

    assert response.status_code == 200
    command, caller = stub_service.search.call_args.args
    assert (command.collection, command.collections, command.acl_context) == (None, ("a", "b"), True)
    assert caller == AuthIdentity(namespace=_NAMESPACE, admin=False)
    assert response.json()["results"][0]["metadata"] == {"k": "v"}


def test_route_module_keeps_no_pipeline_helpers_of_the_old_multi_branch() -> None:
    """BE-11: the telemetry/failure helpers, the request's ``wants_metadata`` and the budget alias are gone."""
    for name in ("_enqueue_error", "_report_pipeline_failure", "_hyde_failure_response", "_SEARCH_TIMEOUT_SECONDS"):
        assert not hasattr(routes_search, name), name
    assert not hasattr(routes_search.SearchRequest, "wants_metadata")
