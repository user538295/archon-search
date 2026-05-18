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
