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


def test_collections_list_schema_in_spec(app) -> None:  # type: ignore[no-untyped-def]
    """GET /collections/ 200 response schema is an array of CollectionSummary-shaped objects."""
    schema = app.openapi()
    get_op = schema["paths"]["/collections/"]["get"]
    resp_200 = get_op["responses"]["200"]
    content = resp_200.get("content", {})
    json_schema = content.get("application/json", {}).get("schema", {})
    # Top-level schema must be an array
    assert json_schema.get("type") == "array", "GET /collections/ must return an array"
    # Items should reference CollectionSummary
    items = json_schema.get("items", {})
    ref = items.get("$ref", "")
    assert ref, "Array items must reference a named schema ($ref)"
    schema_name = ref.split("/")[-1]
    model_schema = schema["components"]["schemas"][schema_name]
    props = model_schema.get("properties", {})
    for field in ("name", "path", "description", "doc_count", "chunk_count", "namespace", "status"):
        assert field in props, f"CollectionSummary schema must have '{field}' property"


def test_list_collections_returns_typed_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /collections/ returns JSON array where each item has all CollectionSummary fields."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.collection_meta import CollectionMeta
    from archon_search.config import SearchConfig
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    valid_key = "c" * 64
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", valid_key)
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(tmp_path / "docs")]
    job_store = JobStore(path=tmp_path / "jobs.json")
    full_app = create_app(cfg, job_store)

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(
        return_value=[CollectionMeta(name="docs", namespace="default")]
    )
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    full_app.state.search_store = mock_store

    mock_state_store = MagicMock()
    mock_state_store.read.return_value = None
    full_app.state.state_store = mock_state_store

    client = TestClient(full_app, raise_server_exceptions=False)
    response = client.get("/collections/", headers={"Authorization": f"Bearer {valid_key}"})
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 1
    item = body[0]
    for field in ("name", "path", "description", "doc_count", "chunk_count", "namespace", "status"):
        assert field in item, f"Response item must have '{field}' field"
    assert item["name"] == "docs"
    assert item["namespace"] == "default"


def test_collection_detail_schema_in_spec(app) -> None:  # type: ignore[no-untyped-def]
    """GET /collections/{name} 200 response schema includes acl_protected_count and embedding_model."""
    schema = app.openapi()
    get_op = schema["paths"]["/collections/{name}"]["get"]
    resp_200 = get_op["responses"]["200"]
    content = resp_200.get("content", {})
    json_schema = content.get("application/json", {}).get("schema", {})
    ref = json_schema.get("$ref", "")
    assert ref, "GET /collections/{name} 200 must reference a named schema ($ref)"
    schema_name = ref.split("/")[-1]
    model_schema = schema["components"]["schemas"][schema_name]
    props = model_schema.get("properties", {})
    assert "acl_protected_count" in props, "CollectionDetail schema must have 'acl_protected_count'"
    assert "embedding_model" in props, "CollectionDetail schema must have 'embedding_model'"
    # Also verify it inherits CollectionSummary fields
    for field in ("name", "path", "description", "doc_count", "chunk_count", "namespace", "status"):
        assert field in props, f"CollectionDetail schema must have '{field}' from CollectionSummary"


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


def test_add_collection_202_schema_in_spec(app) -> None:  # type: ignore[no-untyped-def]
    """POST /collections/ 202 response schema matches JobResponse shape."""
    schema = app.openapi()
    post_op = schema["paths"]["/collections/"]["post"]
    assert "202" in post_op["responses"], "POST /collections/ must declare a 202 response"
    resp_202 = post_op["responses"]["202"]
    content = resp_202.get("content", {})
    json_schema = content.get("application/json", {}).get("schema", {})
    ref = json_schema.get("$ref", "")
    assert ref, "POST /collections/ 202 response must reference a named schema ($ref)"
    schema_name = ref.split("/")[-1]
    props = schema["components"]["schemas"][schema_name].get("properties", {})
    for field in ("job_id", "status", "created_at", "updated_at", "namespace"):
        assert field in props, f"JobResponse schema must have '{field}' property"


def test_ingest_202_schema_in_spec(app) -> None:  # type: ignore[no-untyped-def]
    """POST /ingest 202 response schema matches JobResponse shape."""
    schema = app.openapi()
    post_op = schema["paths"]["/ingest"]["post"]
    assert "202" in post_op["responses"], "POST /ingest must declare a 202 response"
    resp_202 = post_op["responses"]["202"]
    content = resp_202.get("content", {})
    json_schema = content.get("application/json", {}).get("schema", {})
    ref = json_schema.get("$ref", "")
    assert ref, "POST /ingest 202 response must reference a named schema ($ref)"
    schema_name = ref.split("/")[-1]
    props = schema["components"]["schemas"][schema_name].get("properties", {})
    for field in ("job_id", "status", "created_at", "updated_at", "namespace"):
        assert field in props, f"JobResponse schema must have '{field}' property"


def test_reindex_202_schema_in_spec(app) -> None:  # type: ignore[no-untyped-def]
    """POST /collections/{name}/reindex 202 response schema matches JobResponse shape."""
    schema = app.openapi()
    post_op = schema["paths"]["/collections/{name}/reindex"]["post"]
    assert "202" in post_op["responses"], "POST /collections/{name}/reindex must declare a 202 response"
    resp_202 = post_op["responses"]["202"]
    content = resp_202.get("content", {})
    json_schema = content.get("application/json", {}).get("schema", {})
    ref = json_schema.get("$ref", "")
    assert ref, "POST /collections/{name}/reindex 202 response must reference a named schema ($ref)"
    schema_name = ref.split("/")[-1]
    props = schema["components"]["schemas"][schema_name].get("properties", {})
    for field in ("job_id", "status", "created_at", "updated_at", "namespace"):
        assert field in props, f"JobResponse schema must have '{field}' property"


# ---------------------------------------------------------------------------
# Task 2.6 tests
# ---------------------------------------------------------------------------


def _resolve_schema(schema: dict, json_schema: dict) -> dict:
    """Resolve $ref or return inline schema."""
    ref = json_schema.get("$ref", "")
    if ref:
        schema_name = ref.split("/")[-1]
        return schema["components"]["schemas"][schema_name]
    return json_schema


def test_delete_collection_schema_in_spec(app) -> None:  # type: ignore[no-untyped-def]
    """DELETE /collections/{name} 200 response schema has 'name' and 'deleted' fields."""
    schema = app.openapi()
    delete_op = schema["paths"]["/collections/{name}"]["delete"]
    assert "200" in delete_op["responses"], "DELETE /collections/{name} must declare a 200 response"
    resp_200 = delete_op["responses"]["200"]
    content = resp_200.get("content", {})
    json_schema = content.get("application/json", {}).get("schema", {})
    model_schema = _resolve_schema(schema, json_schema)
    props = model_schema.get("properties", {})
    assert "name" in props, "DeleteResponse must have 'name' property"
    assert "deleted" in props, "DeleteResponse must have 'deleted' property"


def test_get_job_schema_in_spec(app) -> None:  # type: ignore[no-untyped-def]
    """GET /jobs/{job_id} 200 response schema matches JobResponse."""
    schema = app.openapi()
    get_op = schema["paths"]["/jobs/{job_id}"]["get"]
    assert "200" in get_op["responses"], "GET /jobs/{job_id} must declare a 200 response"
    resp_200 = get_op["responses"]["200"]
    content = resp_200.get("content", {})
    json_schema = content.get("application/json", {}).get("schema", {})
    model_schema = _resolve_schema(schema, json_schema)
    props = model_schema.get("properties", {})
    for field in ("job_id", "status", "created_at", "updated_at", "namespace"):
        assert field in props, f"JobResponse schema must have '{field}' property"


def test_no_empty_schemas_remain(app) -> None:  # type: ignore[no-untyped-def]
    """No 200 response schema across all paths/operations should be empty ({}) or missing."""
    schema = app.openapi()
    for path, path_item in schema["paths"].items():
        for method, op in path_item.items():
            if not isinstance(op, dict):
                continue
            resp_200 = op.get("responses", {}).get("200")
            if resp_200 is None:
                continue
            content = resp_200.get("content", {})
            json_schema = content.get("application/json", {}).get("schema", {})
            assert json_schema, (
                f"{method.upper()} {path} 200 response has empty or missing schema"
            )


def test_error_schemas_documented(app) -> None:  # type: ignore[no-untyped-def]
    """Endpoints that can 404 must have a 404 response with ErrorDetail-shaped schema in the spec."""
    schema = app.openapi()
    for path, path_item in schema["paths"].items():
        for method, op in path_item.items():
            if not isinstance(op, dict):
                continue
            if "404" not in op.get("responses", {}):
                continue
            resp_404 = op["responses"]["404"]
            content = resp_404.get("content", {})
            json_schema = content.get("application/json", {}).get("schema", {})
            model_schema = _resolve_schema(schema, json_schema)
            props = model_schema.get("properties", {})
            assert "detail" in props, (
                f"{method.upper()} {path} 404 schema must have a 'detail' property"
            )
            assert props["detail"].get("type") == "string", (
                f"{method.upper()} {path} 404 'detail' must be type string"
            )


def test_404_runtime_response_matches_error_detail(
    tmp_path: "Path", monkeypatch: "pytest.MonkeyPatch"
) -> None:
    """GET /collections/nonexistent returns HTTP 404 with body matching ErrorDetail shape."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.config import SearchConfig
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    valid_key = "d" * 64
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

    mock_state_store = MagicMock()
    mock_state_store.read.return_value = None
    full_app.state.state_store = mock_state_store

    client = TestClient(full_app, raise_server_exceptions=False)
    response = client.get(
        "/collections/nonexistent", headers={"Authorization": f"Bearer {valid_key}"}
    )
    assert response.status_code == 404
    body = response.json()
    assert "detail" in body, "404 response body must have 'detail' key"
    assert isinstance(body["detail"], str), "'detail' must be a string"


def test_delete_active_job_returns_202(
    tmp_path: "Path", monkeypatch: "pytest.MonkeyPatch"
) -> None:
    """DELETE /jobs/{job_id} for an in-progress job returns 202 with JobResponse body."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.config import SearchConfig
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    valid_key = "e" * 64
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
    # Create a job and immediately try to delete it while still PENDING
    post_resp = client.post(
        "/ingest",
        json={"collection": "testcol"},
        headers={"Authorization": f"Bearer {valid_key}"},
    )
    assert post_resp.status_code == 202
    job_id = post_resp.json()["job_id"]

    del_resp = client.delete(
        f"/jobs/{job_id}", headers={"Authorization": f"Bearer {valid_key}"}
    )
    # Must be 202 (cancelling active) or 200 (already terminal from fast stub)
    assert del_resp.status_code in (200, 202)
    body = del_resp.json()
    for field in ("job_id", "status", "created_at", "updated_at", "namespace"):
        assert field in body, f"DELETE job response must have '{field}' field"
