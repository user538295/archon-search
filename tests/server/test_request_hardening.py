"""Network and resource defaults on the real ASGI app: CORS and the request-body cap (SecReview #5, #7a)."""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from tests._collection_access_stub import allow_collection_access

_DEFAULT_KEY = "0" * 64  # tests/conftest.py ARCHON_SEARCH_API_KEY
_AUTH = {"Authorization": f"Bearer {_DEFAULT_KEY}"}
_APP_ORIGIN = "https://app.example.com"
_EVIL_ORIGIN = "https://evil.example"
_BODY_LIMIT_MB = 1
_MIB = 1024 * 1024
_REQUEST_TOO_LARGE = 413
_ALLOW_ORIGIN = "access-control-allow-origin"
_ALLOW_CREDENTIALS = "access-control-allow-credentials"


def _client(tmp_path: Path, **config_overrides: object) -> TestClient:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    for name, value in config_overrides.items():
        setattr(config, name, value)
    return TestClient(create_app(config, JobStore(path=tmp_path / "jobs.json")), raise_server_exceptions=True)


def _preflight(client: TestClient, origin: str):
    return client.options(
        "/search",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )


# ---------------------------------------------------------------------------
# #5 — CORS: no cross-origin access unless [server].cors_origins names the origin
# ---------------------------------------------------------------------------


def test_default_sends_no_cors_headers_to_any_origin(tmp_path: Path) -> None:
    client = _client(tmp_path)

    preflight = _preflight(client, _EVIL_ORIGIN)
    simple = client.get("/health", headers={"Origin": _EVIL_ORIGIN})

    assert _ALLOW_ORIGIN not in preflight.headers
    assert _ALLOW_ORIGIN not in simple.headers


def test_configured_origin_is_allowed_without_credentials(tmp_path: Path) -> None:
    client = _client(tmp_path, cors_origins=[_APP_ORIGIN])

    preflight = _preflight(client, _APP_ORIGIN)

    assert preflight.status_code == 200
    assert preflight.headers[_ALLOW_ORIGIN] == _APP_ORIGIN
    assert _ALLOW_CREDENTIALS not in preflight.headers
    assert "authorization" in preflight.headers["access-control-allow-headers"].lower()


def test_unlisted_origin_stays_refused_when_others_are_configured(tmp_path: Path) -> None:
    client = _client(tmp_path, cors_origins=[_APP_ORIGIN])

    preflight = _preflight(client, _EVIL_ORIGIN)
    simple = client.get("/health", headers={"Origin": _EVIL_ORIGIN})

    assert _ALLOW_ORIGIN not in preflight.headers
    assert _ALLOW_ORIGIN not in simple.headers


def test_wildcard_is_never_sent_even_when_an_origin_is_configured(tmp_path: Path) -> None:
    client = _client(tmp_path, cors_origins=[_APP_ORIGIN])

    simple = client.get("/health", headers={"Origin": _APP_ORIGIN})

    assert simple.headers[_ALLOW_ORIGIN] == _APP_ORIGIN
    assert simple.headers[_ALLOW_ORIGIN] != "*"


# ---------------------------------------------------------------------------
# #7a — request bodies are capped before they are buffered
# ---------------------------------------------------------------------------


def _oversized_ingest_body() -> bytes:
    document = {"text": "x" * (_BODY_LIMIT_MB * _MIB), "source_path": "/docs/big.md"}
    return json.dumps({"collection": "docs", "documents": [document]}).encode()


def _chunks(payload: bytes, size: int = 64 * 1024) -> Iterator[bytes]:
    for start in range(0, len(payload), size):
        yield payload[start : start + size]


def test_declared_oversized_body_is_413_and_sanitized(tmp_path: Path) -> None:
    client = _client(tmp_path, max_request_body_mb=_BODY_LIMIT_MB)

    response = client.post("/ingest", content=_oversized_ingest_body(), headers={**_AUTH, "Content-Type": "application/json"})

    assert response.status_code == _REQUEST_TOO_LARGE
    assert response.json() == {"detail": "request body too large", "code": "request_body_too_large"}


def test_chunked_oversized_body_without_content_length_is_413(tmp_path: Path) -> None:
    client = _client(tmp_path, max_request_body_mb=_BODY_LIMIT_MB)

    response = client.post(
        "/ingest",
        content=_chunks(_oversized_ingest_body()),
        headers={**_AUTH, "Content-Type": "application/json"},
    )

    assert response.status_code == _REQUEST_TOO_LARGE
    assert response.json()["code"] == "request_body_too_large"


def test_oversized_body_is_refused_before_authentication(tmp_path: Path) -> None:
    """An unauthenticated client cannot make the server buffer a large body."""
    client = _client(tmp_path, max_request_body_mb=_BODY_LIMIT_MB)

    response = client.post("/ingest", content=_oversized_ingest_body(), headers={"Content-Type": "application/json"})

    assert response.status_code == _REQUEST_TOO_LARGE


def test_the_413_carries_cors_headers_so_a_configured_origin_can_read_it(tmp_path: Path) -> None:
    """The cap sits inside the CORS middleware: a browser client of a listed origin sees the 413, not an opaque failure."""
    client = _client(tmp_path, max_request_body_mb=_BODY_LIMIT_MB, cors_origins=[_APP_ORIGIN])

    response = client.post(
        "/ingest",
        content=_oversized_ingest_body(),
        headers={**_AUTH, "Content-Type": "application/json", "Origin": _APP_ORIGIN},
    )

    assert response.status_code == _REQUEST_TOO_LARGE
    assert response.headers[_ALLOW_ORIGIN] == _APP_ORIGIN
    assert response.json()["code"] == "request_body_too_large"


def test_the_413_gives_an_unlisted_origin_no_cors_header(tmp_path: Path) -> None:
    client = _client(tmp_path, max_request_body_mb=_BODY_LIMIT_MB, cors_origins=[_APP_ORIGIN])

    response = client.post(
        "/ingest",
        content=_oversized_ingest_body(),
        headers={**_AUTH, "Content-Type": "application/json", "Origin": _EVIL_ORIGIN},
    )

    assert response.status_code == _REQUEST_TOO_LARGE
    assert _ALLOW_ORIGIN not in response.headers


def test_body_within_the_limit_reaches_the_handler(tmp_path: Path) -> None:
    client = _client(tmp_path, max_request_body_mb=_BODY_LIMIT_MB)

    response = client.post("/search", json={"query": "q"}, headers=_AUTH)

    assert response.status_code != _REQUEST_TOO_LARGE


def test_default_limit_admits_a_batch_of_inline_documents(tmp_path: Path) -> None:
    """The 32 MiB default fits an inline ``documents`` ingest well above one document's worth."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    # The app runs without its lifespan, so its store is not connected; ownership is not what is under test here.
    client = TestClient(allow_collection_access(create_app(config, JobStore(path=tmp_path / "jobs.json"))))
    document = {"text": "y" * (4 * _MIB), "source_path": "/docs/four-mib.md"}
    body = json.dumps({"collection": "docs", "documents": [document]})

    response = client.post("/ingest", content=body, headers={**_AUTH, "Content-Type": "application/json"})

    assert response.status_code == 202


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_bodyless_requests_are_unaffected(tmp_path: Path, method: str) -> None:
    client = _client(tmp_path, max_request_body_mb=_BODY_LIMIT_MB)

    response = client.request(method, "/health")

    assert response.status_code != _REQUEST_TOO_LARGE
