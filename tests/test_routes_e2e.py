"""Suite 3 — archon-search /route endpoint e2e tests (Task 6.1: H3.1–H3.5, E3.1–E3.5b, H3.6b)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app


def _make_client(
    tmp_path: Path,
    config: SearchConfig | None = None,
) -> TestClient:
    """Create a TestClient with a fresh isolated app instance."""
    if config is None:
        config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store, config_path=tmp_path / "config.toml")
    return TestClient(app)


def _patch_router(
    pre_context: str | None = None,
    routable_names: list[str] | None = None,
    decomposer_invoked: bool = False,
) -> MagicMock:
    """Return a mock MultiCollectionRouter with predictable responses."""
    mock = MagicMock()
    mock.get_pre_context = AsyncMock(return_value=pre_context)
    mock.last_routable_names = routable_names or []
    mock.decomposer_was_invoked = decomposer_invoked
    return mock


# ---------------------------------------------------------------------------
# H3.1 — /route returns pre_context with collection metadata
# ---------------------------------------------------------------------------
def test_H3_1_route_returns_pre_context_with_metadata(tmp_path: Path) -> None:
    expected = "<search_collections>\n- col1: description\n</search_collections>"
    router_mock = _patch_router(
        pre_context=expected,
        routable_names=["col1"],
        decomposer_invoked=True,
    )
    with patch("archon_search.server.routes_route._build_router", return_value=router_mock):
        client = _make_client(tmp_path)
        response = client.post("/route", json={"query": "what is archon?"})

    assert response.status_code == 200
    data = response.json()
    assert data["pre_context"] == expected
    assert data["routable_names"] == ["col1"]
    assert data["decomposer_invoked"] is True


# ---------------------------------------------------------------------------
# H3.2 — pinned_collections configured → pinned_names always returned
# ---------------------------------------------------------------------------
def test_H3_2_pinned_collections_always_in_pinned_names(tmp_path: Path) -> None:
    config = SearchConfig()
    config.pinned_collections = ["/data/docs", "/data/notes"]
    router_mock = _patch_router(pre_context=None, routable_names=[], decomposer_invoked=False)
    with patch("archon_search.server.routes_route._build_router", return_value=router_mock):
        client = _make_client(tmp_path, config=config)
        response = client.post("/route", json={"query": "any query"})

    assert response.status_code == 200
    data = response.json()
    # path_to_collection_name("/data/docs") → "docs", "/data/notes" → "notes"
    assert data["pinned_names"] == ["docs", "notes"]


# ---------------------------------------------------------------------------
# H3.3 — slots=2 → shortlist_size=2 passed to router
# ---------------------------------------------------------------------------
def test_H3_3_slots_sets_shortlist_size(tmp_path: Path) -> None:
    captured: dict = {}

    def fake_build(config: SearchConfig, shortlist_size: int, embedder=None) -> MagicMock:
        captured["shortlist_size"] = shortlist_size
        return _patch_router()

    with patch("archon_search.server.routes_route._build_router", side_effect=fake_build):
        client = _make_client(tmp_path)
        client.post("/route", json={"query": "x", "slots": 2})

    assert captured["shortlist_size"] == 2


# ---------------------------------------------------------------------------
# H3.4 — Unicode query → 200, valid response
# ---------------------------------------------------------------------------
def test_H3_4_unicode_query_returns_200(tmp_path: Path) -> None:
    router_mock = _patch_router(pre_context=None, routable_names=[], decomposer_invoked=False)
    with patch("archon_search.server.routes_route._build_router", return_value=router_mock):
        client = _make_client(tmp_path)
        response = client.post("/route", json={"query": "こんにちは世界 — héllo wörld 🌍"})

    assert response.status_code == 200
    data = response.json()
    assert "pre_context" in data
    assert "routable_names" in data


# ---------------------------------------------------------------------------
# H3.5 — 10k character query → 200
# ---------------------------------------------------------------------------
def test_H3_5_long_query_returns_200(tmp_path: Path) -> None:
    long_query = "a" * 10_000
    router_mock = _patch_router(pre_context=None, routable_names=[], decomposer_invoked=False)
    with patch("archon_search.server.routes_route._build_router", return_value=router_mock):
        client = _make_client(tmp_path)
        response = client.post("/route", json={"query": long_query})

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# E3.1 — POST /route {} (missing query field) → 422
# ---------------------------------------------------------------------------
def test_E3_1_missing_query_returns_422(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    response = client.post("/route", json={})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# E3.2 — {"query": null} → 422
# ---------------------------------------------------------------------------
def test_E3_2_null_query_returns_422(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    response = client.post("/route", json={"query": None})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# E3.3 — slots=-1 → 400
# ---------------------------------------------------------------------------
def test_E3_3_negative_slots_returns_400(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    response = client.post("/route", json={"query": "x", "slots": -1})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# E3.4 — slots=0 → 400
# ---------------------------------------------------------------------------
def test_E3_4_zero_slots_returns_400(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    response = client.post("/route", json={"query": "x", "slots": 0})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# E3.5b — asyncio.TimeoutError raised in wait_for → 504 "routing timed out"
# ---------------------------------------------------------------------------
async def _wait_for_that_raises(coro: object, timeout: float) -> None:
    """Consume the coroutine argument then raise TimeoutError (no leaked coroutines)."""
    import inspect
    if inspect.iscoroutine(coro):
        coro.close()  # close without awaiting to suppress ResourceWarning
    raise asyncio.TimeoutError


def test_E3_5b_timeout_returns_504(tmp_path: Path) -> None:
    # Patch both _build_router (returns a mock so get_pre_context is an AsyncMock) and
    # asyncio.wait_for (simulates the 30s routing timeout). The custom wait_for stub
    # closes the coroutine before raising to suppress ResourceWarning about unawaited coros.
    router_mock = _patch_router(pre_context=None, routable_names=[], decomposer_invoked=False)
    with patch(
        "archon_search.server.routes_route._build_router", return_value=router_mock
    ), patch(
        "archon_search.server.routes_route.asyncio.wait_for",
        side_effect=_wait_for_that_raises,
    ):
        client = _make_client(tmp_path)
        response = client.post("/route", json={"query": "slow query"})

    assert response.status_code == 504
    assert "routing timed out" in response.json()["detail"]


# ---------------------------------------------------------------------------
# H3.6b — confidence threshold too high → 200, pre_context=None, routable_names=[]
# ---------------------------------------------------------------------------
def test_H3_6b_all_collections_below_confidence_threshold(tmp_path: Path) -> None:
    # Router returns None/[] when confidence gate eliminates all collections
    router_mock = _patch_router(
        pre_context=None,
        routable_names=[],
        decomposer_invoked=False,
    )
    config = SearchConfig()
    config.routing_confidence_threshold = 1.0  # impossible threshold — nothing passes
    with patch("archon_search.server.routes_route._build_router", return_value=router_mock):
        client = _make_client(tmp_path, config=config)
        response = client.post("/route", json={"query": "unrelated query"})

    assert response.status_code == 200
    data = response.json()
    assert data["pre_context"] is None
    assert data["routable_names"] == []
    assert data["decomposer_invoked"] is False
