"""POST /search and POST /explain: every error body the routes really send matches the response
the authoritative contract (``GET /openapi.json``) declares for its status code.

Each case drives a real request through the app, reads the declared response for the status it
got, and validates the body against the declared model it must match. A ``422`` is declared as a
union: FastAPI's request-validation body, a string ``detail`` and a ``{code, message}`` ``detail``.
Covered: at least one real body for every (status, declared model) pair the two routes declare —
400 / 404 / 422 / 500 / 503 / 504 — not every individual error site. A
pipeline failure ``/search`` does not catch is a plain-text ``500`` (as its declared description
says), so it has no JSON body to validate.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import BaseModel, ValidationError

from archon_search.config import SearchConfig
from archon_search.hyde import HyDEDependencyError
from archon_search.jobs.store import JobStore
from archon_search.pipeline import (
    CollectionNotFoundError,
    ExplainStageError,
    FanoutTimeoutError,
    GraphCommunitiesNotBuiltError,
    MetadataLookupError,
)
from archon_search.server.app import create_app
from archon_search.server.schemas import (
    GRAPH_COMMUNITIES_NOT_BUILT_CODE,
    INVALID_SCOPE_FILTER_CODE,
    CodedErrorDetail,
    ErrorDetail,
    route_422_response,
)

_HTTP_VALIDATION_ERROR = "HTTPValidationError"
_ROUTES = ("/search", "/explain")
# The pipeline methods the routes' single- and multi-collection paths await.
_PIPELINE_METHODS = ("search", "search_many", "explain")


def _validates(model: type[BaseModel]) -> Callable[[Any], bool]:
    def check(body: Any) -> bool:
        try:
            model.model_validate(body)
        except ValidationError:
            return False
        return True

    return check


def _is_http_validation_error(body: Any) -> bool:
    detail = body.get("detail") if isinstance(body, dict) else None
    return bool(detail) and isinstance(detail, list) and all(
        isinstance(item, dict) and {"loc", "msg", "type"} <= item.keys() for item in detail
    )


_VALIDATORS: dict[str, Callable[[Any], bool]] = {
    ErrorDetail.__name__: _validates(ErrorDetail),
    CodedErrorDetail.__name__: _validates(CodedErrorDetail),
    _HTTP_VALIDATION_ERROR: _is_http_validation_error,
}


def _client(tmp_path: Path, pipeline: MagicMock) -> TestClient:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    with patch("archon_search.chunker.DocumentChunker.__init__", return_value=None):
        app = create_app(config, JobStore(path=tmp_path / "jobs.json"))
    app.state.pipeline = pipeline
    # Single-collection paths then embed with the pipeline's global embedder (no model load).
    app.state.embedder_cache = None
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    return TestClient(app, raise_server_exceptions=False, headers={"Authorization": f"Bearer {key}"})


def _pipeline(raises: Exception | None = None) -> MagicMock:
    pipeline = MagicMock()
    pipeline.warmup_models = AsyncMock(return_value=True)
    meta = SimpleNamespace(name="c", active_embedding_model="")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.get_all_collections_meta = AsyncMock(return_value=[meta])
    for method in _PIPELINE_METHODS:
        setattr(pipeline, method, AsyncMock(side_effect=raises))
    return pipeline


def _declared_models(client: TestClient, route: str, status: int) -> list[str]:
    spec = client.get("/openapi.json").json()
    responses = spec["paths"][route]["post"]["responses"]
    assert str(status) in responses, f"POST {route} sends {status} but does not declare it"
    schema = responses[str(status)]["content"]["application/json"]["schema"]
    names = [member["$ref"].rsplit("/", 1)[1] for member in schema.get("anyOf", [schema])]
    missing = [name for name in names if name not in spec["components"]["schemas"]]
    assert not missing, f"POST {route} {status} references undefined schemas {missing}"
    return names


def _post(client: TestClient, route: str, body: dict[str, Any], hyde_raises: Exception | None) -> Response:
    if hyde_raises is None:
        return client.post(route, json=body)
    module = "routes_search" if route == "/search" else "routes_explain"
    with patch(f"archon_search.server.{module}.resolve_hyde_vector", side_effect=hyde_raises):
        return client.post(route, json=body)


_SINGLE = {"query": "q", "collection": "c"}
_FANOUT = {"query": "q", "collections": ["a", "b"]}
_HYDE_MISSING = HyDEDependencyError("pip install 'archon-search[hyde]'")
_INTERNAL = RuntimeError("/srv/store/c.lance: internal")

# (case id, request body, exception the pipeline call raises, exception HyDE raises, status, model)
_CASES = [
    ("invalid-scope-filter", {**_SINGLE, "scope_filter": "*"}, None, None, 400, CodedErrorDetail.__name__),
    ("request-validation", {"query": "   ", "collection": "c"}, None, None, 422, _HTTP_VALIDATION_ERROR),
    ("graph-mode-disabled", {**_SINGLE, "graph_mode": "local"}, None, None, 422, ErrorDetail.__name__),
    ("hyde-dependency", {**_SINGLE, "hyde": True}, None, _HYDE_MISSING, 422, ErrorDetail.__name__),
    ("communities-not-built", _FANOUT, GraphCommunitiesNotBuiltError("a"), None, 422, CodedErrorDetail.__name__),
    (
        "communities-not-built-single",
        _SINGLE,
        GraphCommunitiesNotBuiltError("c"),
        None,
        422,
        CodedErrorDetail.__name__,
    ),
    ("collection-not-found", _FANOUT, CollectionNotFoundError(["a", "b"]), None, 404, ErrorDetail.__name__),
    ("hyde-failed", {**_SINGLE, "hyde": True}, None, _INTERNAL, 500, ErrorDetail.__name__),
    ("metadata-unavailable", _FANOUT, MetadataLookupError(_INTERNAL), None, 503, ErrorDetail.__name__),
    ("fanout-timeout", _FANOUT, FanoutTimeoutError(), None, 504, ErrorDetail.__name__),
]
# /explain only: /search answers a pipeline failure it does not catch as a plain-text 500.
_EXPLAIN_CASES = [
    ("explain-stage", _FANOUT, ExplainStageError("rerank", _INTERNAL), None, 500, ErrorDetail.__name__),
    ("explain-failed", _FANOUT, _INTERNAL, None, 500, ErrorDetail.__name__),
]
_CASE_PARAMS = [pytest.param(route, *case[1:], id=f"{route}-{case[0]}") for route in _ROUTES for case in _CASES]
_CASE_PARAMS += [pytest.param("/explain", *case[1:], id=f"/explain-{case[0]}") for case in _EXPLAIN_CASES]


@pytest.mark.parametrize(("route", "body", "raises", "hyde_raises", "status", "model"), _CASE_PARAMS)
def test_error_body_matches_the_declared_response(
    tmp_path: Path,
    route: str,
    body: dict[str, Any],
    raises: Exception | None,
    hyde_raises: Exception | None,
    status: int,
    model: str,
) -> None:
    client = _client(tmp_path, _pipeline(raises))

    response = _post(client, route, body, hyde_raises)

    assert response.status_code == status, response.text
    declared = _declared_models(client, route, status)
    assert model in declared, f"POST {route} {status} declares {declared}, not {model}"
    assert _VALIDATORS[model](response.json()), f"{response.json()!r} is not a {model}"


def test_explain_routing_failure_matches_the_declared_503(tmp_path: Path) -> None:
    """A collectionless ``/explain`` whose routing call fails answers the declared ``503`` body."""
    pipeline = _pipeline()
    pipeline._global_embedder.embed_one = AsyncMock(side_effect=_INTERNAL)
    client = _client(tmp_path, pipeline)

    response = client.post("/explain", json={"query": "q"})

    assert response.status_code == 503, response.text
    assert ErrorDetail.__name__ in _declared_models(client, "/explain", 503)
    assert _VALIDATORS[ErrorDetail.__name__](response.json()), response.text


@pytest.mark.parametrize("route", _ROUTES)
@pytest.mark.parametrize(
    ("body", "raises", "code"),
    [
        pytest.param({**_SINGLE, "scope_filter": "*"}, None, INVALID_SCOPE_FILTER_CODE, id="invalid-scope-filter"),
        pytest.param(_FANOUT, GraphCommunitiesNotBuiltError("a"), GRAPH_COMMUNITIES_NOT_BUILT_CODE, id="communities"),
        pytest.param(
            _SINGLE, GraphCommunitiesNotBuiltError("c"), GRAPH_COMMUNITIES_NOT_BUILT_CODE, id="communities-single"
        ),
    ],
)
def test_coded_error_bodies_carry_their_code(
    tmp_path: Path, route: str, body: dict[str, Any], raises: Exception | None, code: str
) -> None:
    response = _client(tmp_path, _pipeline(raises)).post(route, json=body)

    assert CodedErrorDetail.model_validate(response.json()).detail.code == code


@pytest.mark.parametrize("route", _ROUTES)
def test_422_declares_all_three_bodies(tmp_path: Path, route: str) -> None:
    """Declaring ``422`` replaces FastAPI's default entry, so the request-validation body must be
    kept in the union alongside the route's own string and ``{code, message}`` bodies."""
    assert sorted(_declared_models(_client(tmp_path, _pipeline()), route, 422)) == sorted(
        [_HTTP_VALIDATION_ERROR, CodedErrorDetail.__name__, ErrorDetail.__name__]
    )


def test_route_422_response_is_built_fresh_for_each_route() -> None:
    """A FastAPI release that copies a ``responses`` entry only shallowly concatenates into its
    ``anyOf`` list in place, so one shared dict would grow with every route and schema build."""
    first, second = route_422_response(), route_422_response()

    first["content"]["application/json"]["schema"]["anyOf"].append({"$ref": "#/components/schemas/X"})

    assert second["content"]["application/json"]["schema"]["anyOf"] == [
        {"$ref": "#/components/schemas/HTTPValidationError"}
    ]
