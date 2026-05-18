"""Tests for custom OpenAPI schema — SecurityScheme + per-path security annotations."""
from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app


@pytest.fixture
def app(tmp_path: Path):  # type: ignore[return]
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    return create_app(cfg, JobStore(path=tmp_path / "jobs.json"))


def test_security_scheme_present(app) -> None:  # type: ignore[no-untyped-def]
    schema = app.openapi()
    assert schema["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
    }


def test_health_has_no_security(app) -> None:  # type: ignore[no-untyped-def]
    schema = app.openapi()
    health_ops = schema["paths"].get("/health", {})
    for _method, op in health_ops.items():
        assert "security" not in op, f"/health {_method} should not have security annotation"


def test_search_has_bearer_security(app) -> None:  # type: ignore[no-untyped-def]
    schema = app.openapi()
    post_op = schema["paths"]["/search"]["post"]
    assert post_op["security"] == [{"BearerAuth": []}]


def test_spec_docs_paths_absent_from_schema(app) -> None:  # type: ignore[no-untyped-def]
    """FastAPI registers /docs, /redoc, /openapi.json with include_in_schema=False,
    so they must never appear in spec["paths"]."""
    spec = app.openapi()
    for path in ("/docs", "/openapi.json", "/redoc"):
        assert path not in spec.get("paths", {}), f"{path} must not appear in OpenAPI schema"


def test_openapi_schema_is_cached(app) -> None:  # type: ignore[no-untyped-def]
    """app.openapi() must return the same object on repeated calls (schema caching)."""
    first = app.openapi()
    second = app.openapi()
    assert first is second, "openapi() should return the cached schema object"


from starlette.testclient import TestClient  # noqa: E402


def test_cors_options_preflight_returns_headers(app) -> None:  # type: ignore[no-untyped-def]
    """OPTIONS /search returns Access-Control-Allow-Origin: * without requiring auth."""
    client = TestClient(app, raise_server_exceptions=False)
    response = client.options(
        "/search",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.headers.get("access-control-allow-origin") == "*"


def test_cors_get_health_returns_headers(app) -> None:  # type: ignore[no-untyped-def]
    """GET /health response includes Access-Control-Allow-Origin."""
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert response.headers.get("access-control-allow-origin") == "*"


def test_cors_preflight_to_protected_endpoint_not_blocked(app) -> None:  # type: ignore[no-untyped-def]
    """OPTIONS /search returns CORS headers and NOT 401 (preflight must bypass auth)."""
    client = TestClient(app, raise_server_exceptions=False)
    response = client.options(
        "/search",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code != 401
    assert response.headers.get("access-control-allow-origin") == "*"


def test_health_response_schema_in_spec(app) -> None:  # type: ignore[no-untyped-def]
    """GET /health 200 response schema must have 'status' and 'version' string properties."""
    schema = app.openapi()
    get_op = schema["paths"]["/health"]["get"]
    resp_200 = get_op["responses"]["200"]
    # Resolve $ref if present
    content = resp_200.get("content", {})
    json_schema = content.get("application/json", {}).get("schema", {})
    ref = json_schema.get("$ref", "")
    if ref:
        # e.g. "#/components/schemas/HealthResponse"
        schema_name = ref.split("/")[-1]
        model_schema = schema["components"]["schemas"][schema_name]
    else:
        model_schema = json_schema
    props = model_schema.get("properties", {})
    assert "status" in props, "HealthResponse must have 'status' property"
    assert "version" in props, "HealthResponse must have 'version' property"
    assert props["status"].get("type") == "string"
    assert props["version"].get("type") == "string"


def test_status_response_schema_in_spec(app) -> None:  # type: ignore[no-untyped-def]
    """GET /status 200 response schema must have running, pid, version, collections."""
    schema = app.openapi()
    get_op = schema["paths"]["/status"]["get"]
    resp_200 = get_op["responses"]["200"]
    content = resp_200.get("content", {})
    json_schema = content.get("application/json", {}).get("schema", {})
    ref = json_schema.get("$ref", "")
    if ref:
        schema_name = ref.split("/")[-1]
        model_schema = schema["components"]["schemas"][schema_name]
    else:
        model_schema = json_schema
    props = model_schema.get("properties", {})
    assert "running" in props, "StatusResponse must have 'running' property"
    assert "pid" in props, "StatusResponse must have 'pid' property"
    assert "version" in props, "StatusResponse must have 'version' property"
    assert "collections" in props, "StatusResponse must have 'collections' property"
    assert props["collections"].get("type") == "array"


def test_health_endpoint_returns_typed_response(app) -> None:  # type: ignore[no-untyped-def]
    """GET /health returns JSON with 'status' and 'version' keys."""
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert "status" in body
    assert "version" in body
    assert isinstance(body["status"], str)
    assert isinstance(body["version"], str)


def test_status_endpoint_returns_typed_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /status with a valid auth token returns JSON with running, pid, version, collections."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.config import SearchConfig
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    valid_key = "a" * 64
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", valid_key)
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    full_app = create_app(cfg, job_store)

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    full_app.state.search_store = mock_store

    client = TestClient(full_app, raise_server_exceptions=False)
    response = client.get("/status", headers={"Authorization": f"Bearer {valid_key}"})
    assert response.status_code == 200
    body = response.json()
    assert "running" in body
    assert "pid" in body
    assert "version" in body
    assert "collections" in body
    assert isinstance(body["collections"], list)


def test_status_401_schema_in_spec(app) -> None:  # type: ignore[no-untyped-def]
    """GET /status OpenAPI spec has a 401 response entry with ErrorDetail-shaped schema."""
    schema = app.openapi()
    get_op = schema["paths"]["/status"]["get"]
    assert "401" in get_op["responses"], "GET /status must declare a 401 response in the OpenAPI spec"
    resp_401 = get_op["responses"]["401"]
    content = resp_401.get("content", {})
    json_schema = content.get("application/json", {}).get("schema", {})
    ref = json_schema.get("$ref", "")
    if ref:
        schema_name = ref.split("/")[-1]
        model_schema = schema["components"]["schemas"][schema_name]
    else:
        model_schema = json_schema
    props = model_schema.get("properties", {})
    assert "detail" in props, "401 response schema must have a 'detail' property"
    assert props["detail"].get("type") == "string", "'detail' property must be of type string"


def test_indexing_state_schema_in_spec(app) -> None:  # type: ignore[no-untyped-def]
    """GET /indexing-state 200 response schema must have a 'collections' object property."""
    schema = app.openapi()
    get_op = schema["paths"]["/indexing-state"]["get"]
    resp_200 = get_op["responses"]["200"]
    content = resp_200.get("content", {})
    json_schema = content.get("application/json", {}).get("schema", {})
    ref = json_schema.get("$ref", "")
    if ref:
        schema_name = ref.split("/")[-1]
        model_schema = schema["components"]["schemas"][schema_name]
    else:
        model_schema = json_schema
    props = model_schema.get("properties", {})
    assert "collections" in props, "IndexingStateResponse must have 'collections' property"
    assert props["collections"].get("type") == "object", "'collections' must be of type object"


def test_indexing_state_empty_when_no_state_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /indexing-state returns {"collections": {}} when the state store has no data."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.config import SearchConfig
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    valid_key = "b" * 64
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", valid_key)
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    full_app = create_app(cfg, job_store)

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    full_app.state.search_store = mock_store

    # state_store.read() returns None — no state file
    mock_state_store = MagicMock()
    mock_state_store.read.return_value = None
    full_app.state.state_store = mock_state_store

    client = TestClient(full_app, raise_server_exceptions=False)
    response = client.get("/indexing-state", headers={"Authorization": f"Bearer {valid_key}"})
    assert response.status_code == 200
    body = response.json()
    assert body == {"collections": {}, "last_updated": None, "trigger": None}
