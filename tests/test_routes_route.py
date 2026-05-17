"""Tests for POST /route endpoint (Task 5.5)."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.sync import path_to_collection_name


def _make_client(
    tmp_path: Path,
    pinned_collections: list[str] | None = None,
    shortlist_size: int = 8,
    confidence_threshold: float = 0.30,
) -> TestClient:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.pinned_collections = pinned_collections or []
    config.routing_shortlist_size = shortlist_size
    config.routing_confidence_threshold = confidence_threshold
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)

    # Wire a mock store so the namespace filter in POST /route can call get_all_collections_meta().
    # The default single key resolves to "default" namespace; all pinned collections resolve
    # to that namespace so existing test assertions are unaffected.
    pinned_names = [path_to_collection_name(p) for p in (pinned_collections or [])]
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(
        return_value=[CollectionMeta(name=n, namespace="default") for n in pinned_names]
    )
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


def _patch_router(
    pre_context: str | None,
    routable_names: list[str],
    decomposer_invoked: bool,
) -> MagicMock:
    """Return a mock MultiCollectionRouter whose get_pre_context returns the given values."""
    router_mock = MagicMock()
    router_mock.get_pre_context = AsyncMock(return_value=pre_context)
    router_mock.last_routable_names = routable_names
    router_mock.decomposer_was_invoked = decomposer_invoked
    return router_mock


# ---------------------------------------------------------------------------
# 1. pre_context is returned when decomposer is needed
# ---------------------------------------------------------------------------
def test_route_returns_pre_context_when_decomposer_needed(tmp_path: Path) -> None:
    expected_block = "<search_collections>\n- col1: desc\n</search_collections>"
    router_mock = _patch_router(
        pre_context=expected_block,
        routable_names=["col1", "col2"],
        decomposer_invoked=True,
    )
    with patch(
        "archon_search.server.routes_route._build_router", return_value=router_mock
    ):
        client = _make_client(tmp_path)
        response = client.post("/route", json={"query": "what is the answer?"})

    assert response.status_code == 200
    data = response.json()
    assert data["pre_context"] == expected_block


# ---------------------------------------------------------------------------
# 2. pinned_names always contains the resolved pinned list
# ---------------------------------------------------------------------------
def test_route_returns_pinned_names(tmp_path: Path) -> None:
    router_mock = _patch_router(
        pre_context=None,
        routable_names=[],
        decomposer_invoked=False,
    )
    with patch(
        "archon_search.server.routes_route._build_router", return_value=router_mock
    ):
        client = _make_client(tmp_path, pinned_collections=["/pinned/docs", "/notes"])
        response = client.post("/route", json={"query": "find something"})

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["pinned_names"], list)
    # path_to_collection_name("/pinned/docs") → "docs", path_to_collection_name("/notes") → "notes"
    assert data["pinned_names"] == ["docs", "notes"]


# ---------------------------------------------------------------------------
# 3. routable_names mirrors the router's last_routable_names
# ---------------------------------------------------------------------------
def test_route_returns_routable_names_list(tmp_path: Path) -> None:
    router_mock = _patch_router(
        pre_context=None,
        routable_names=["alpha", "beta", "gamma"],
        decomposer_invoked=False,
    )
    with patch(
        "archon_search.server.routes_route._build_router", return_value=router_mock
    ):
        client = _make_client(tmp_path)
        response = client.post("/route", json={"query": "some query"})

    assert response.status_code == 200
    data = response.json()
    assert data["routable_names"] == ["alpha", "beta", "gamma"]


# ---------------------------------------------------------------------------
# 4. decomposer_invoked flag is surfaced correctly
# ---------------------------------------------------------------------------
def test_route_returns_decomposer_invoked_flag(tmp_path: Path) -> None:
    router_mock = _patch_router(
        pre_context="<search_collections>...</search_collections>",
        routable_names=["x"],
        decomposer_invoked=True,
    )
    with patch(
        "archon_search.server.routes_route._build_router", return_value=router_mock
    ):
        client = _make_client(tmp_path)
        response = client.post("/route", json={"query": "need decomposer"})

    assert response.status_code == 200
    assert response.json()["decomposer_invoked"] is True


# ---------------------------------------------------------------------------
# 5. Empty query returns 400
# ---------------------------------------------------------------------------
def test_route_empty_query_returns_400(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    response = client.post("/route", json={"query": ""})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 5b. Whitespace-only query returns 400
# ---------------------------------------------------------------------------
def test_route_whitespace_only_query_returns_400(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    response = client.post("/route", json={"query": "   "})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 6. slots overrides routing_shortlist_size
# ---------------------------------------------------------------------------
def test_slots_overrides_shortlist_size(tmp_path: Path) -> None:
    captured: dict = {}

    def fake_build_router(config: SearchConfig, shortlist_size: int, embedder=None) -> MagicMock:
        captured["shortlist_size"] = shortlist_size
        return _patch_router(pre_context=None, routable_names=[], decomposer_invoked=False)

    with patch(
        "archon_search.server.routes_route._build_router", side_effect=fake_build_router
    ):
        client = _make_client(tmp_path, shortlist_size=8)
        client.post("/route", json={"query": "hello", "slots": 3})

    assert captured["shortlist_size"] == 3


# ---------------------------------------------------------------------------
# 6b. Default shortlist_size comes from config when slots is not provided
# ---------------------------------------------------------------------------
def test_slots_default_uses_config_shortlist(tmp_path: Path) -> None:
    captured: dict = {}

    def fake_build_router(config: SearchConfig, shortlist_size: int, embedder=None) -> MagicMock:
        captured["shortlist_size"] = shortlist_size
        return _patch_router(pre_context=None, routable_names=[], decomposer_invoked=False)

    with patch(
        "archon_search.server.routes_route._build_router", side_effect=fake_build_router
    ):
        client = _make_client(tmp_path, shortlist_size=5)
        client.post("/route", json={"query": "hello"})

    assert captured["shortlist_size"] == 5


# ---------------------------------------------------------------------------
# 7. Pinned collections are always included in pinned_names even without any
#    routable collections
# ---------------------------------------------------------------------------
def test_pinned_always_included(tmp_path: Path) -> None:
    router_mock = _patch_router(
        pre_context=None,
        routable_names=[],
        decomposer_invoked=False,
    )
    with patch(
        "archon_search.server.routes_route._build_router", return_value=router_mock
    ):
        client = _make_client(tmp_path, pinned_collections=["/alpha", "/beta", "/gamma"])
        response = client.post("/route", json={"query": "anything"})

    assert response.status_code == 200
    data = response.json()
    assert len(data["pinned_names"]) == 3


# ---------------------------------------------------------------------------
# 8. Confidence gate: low-similarity → config threshold is threaded through
# ---------------------------------------------------------------------------
def test_confidence_gate_filters_low_similarity(tmp_path: Path) -> None:
    captured: dict = {}

    def fake_build_router(config: SearchConfig, shortlist_size: int, embedder=None) -> MagicMock:
        captured["confidence_threshold"] = config.routing_confidence_threshold
        captured["shortlist_size"] = shortlist_size
        # Simulate confidence gate filtered all out
        return _patch_router(pre_context=None, routable_names=[], decomposer_invoked=False)

    with patch(
        "archon_search.server.routes_route._build_router", side_effect=fake_build_router
    ):
        client = _make_client(tmp_path, confidence_threshold=0.99, shortlist_size=8)
        response = client.post("/route", json={"query": "unrelated query"})

    assert response.status_code == 200
    data = response.json()
    assert data["pre_context"] is None
    assert data["routable_names"] == []
    assert data["decomposer_invoked"] is False
    # Verify config's confidence_threshold was passed through to _build_router
    assert captured["confidence_threshold"] == 0.99
    assert captured["shortlist_size"] == 8


# ---------------------------------------------------------------------------
# 9. When all centroids are None the confidence gate is bypassed
# ---------------------------------------------------------------------------
def test_centroid_none_bypasses_confidence_gate(tmp_path: Path) -> None:
    captured: dict = {}

    def fake_build_router(config: SearchConfig, shortlist_size: int, embedder=None) -> MagicMock:
        captured["confidence_threshold"] = config.routing_confidence_threshold
        # All-None centroid case: router still returns collections and decomposer may be invoked
        return _patch_router(
            pre_context="<search_collections>\n- no-centroid: desc\n</search_collections>",
            routable_names=["no-centroid"],
            decomposer_invoked=True,
        )

    with patch(
        "archon_search.server.routes_route._build_router", side_effect=fake_build_router
    ):
        client = _make_client(tmp_path, confidence_threshold=0.99)
        response = client.post("/route", json={"query": "anything"})

    assert response.status_code == 200
    data = response.json()
    assert data["routable_names"] == ["no-centroid"]
    assert data["decomposer_invoked"] is True
    # Confidence threshold of 0.99 was passed through to _build_router
    assert captured["confidence_threshold"] == 0.99


# ---------------------------------------------------------------------------
# 10. Router output order is preserved in the response
# ---------------------------------------------------------------------------
def test_router_order_is_preserved(tmp_path: Path) -> None:
    ordered = ["first", "second", "third", "fourth"]
    router_mock = _patch_router(
        pre_context="<search_collections>...</search_collections>",
        routable_names=ordered,
        decomposer_invoked=True,
    )
    with patch(
        "archon_search.server.routes_route._build_router", return_value=router_mock
    ):
        client = _make_client(tmp_path)
        response = client.post("/route", json={"query": "order matters"})

    assert response.status_code == 200
    assert response.json()["routable_names"] == ordered


# ---------------------------------------------------------------------------
# 11. slots=0 returns 400
# ---------------------------------------------------------------------------
def test_slots_zero_returns_400(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    response = client.post("/route", json={"query": "hello", "slots": 0})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 12. slots=-1 returns 400
# ---------------------------------------------------------------------------
def test_slots_negative_returns_400(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    response = client.post("/route", json={"query": "hello", "slots": -1})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 13. Namespace filter — only pinned collections belonging to caller's namespace
#     are included in routing candidates (Task 5.4 — FEAT-043)
# ---------------------------------------------------------------------------


def _make_app_with_namespaces(
    tmp_path: Path,
    pinned_paths: list[str],
    namespaces: dict[str, str],
    all_meta: list[CollectionMeta],
) -> tuple:
    """Return (app, mock_store) with namespace-aware config wired up."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.pinned_collections = pinned_paths
    config.routing_shortlist_size = 8
    config.routing_confidence_threshold = 0.30
    config.namespaces = namespaces
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=all_meta)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store
    return app, mock_store


def test_route_filters_pinned_by_namespace(tmp_path: Path) -> None:
    """Pinned collections in a different namespace are excluded from routing candidates."""
    key_a = "a" * 64
    key_b = "b" * 64

    # Two pinned paths — one will be meta-tagged to tenantA, one to tenantB
    path_a = "/docs/tenantA"
    path_b = "/docs/tenantB"
    name_a = path_to_collection_name(path_a)
    name_b = path_to_collection_name(path_b)

    meta_a = CollectionMeta(name=name_a, namespace="tenantA")
    meta_b = CollectionMeta(name=name_b, namespace="tenantB")

    app, mock_store = _make_app_with_namespaces(
        tmp_path,
        pinned_paths=[path_a, path_b],
        namespaces={key_a: "tenantA", key_b: "tenantB"},
        all_meta=[meta_a, meta_b],
    )

    router_mock = _patch_router(pre_context=None, routable_names=[], decomposer_invoked=False)

    with patch("archon_search.server.routes_route._build_router", return_value=router_mock):
        client = TestClient(app, headers={"Authorization": f"Bearer {key_a}"})
        response = client.post("/route", json={"query": "anything"})

    assert response.status_code == 200
    data = response.json()
    assert name_a in data["pinned_names"]
    assert name_b not in data["pinned_names"]


def test_route_includes_pinned_for_correct_namespace(tmp_path: Path) -> None:
    """A pinned collection belonging to the caller's namespace is included in routing candidates."""
    key_a = "a" * 64

    path_a = "/data/myproject"
    name_a = path_to_collection_name(path_a)
    meta_a = CollectionMeta(name=name_a, namespace="tenantA")

    app, mock_store = _make_app_with_namespaces(
        tmp_path,
        pinned_paths=[path_a],
        namespaces={key_a: "tenantA"},
        all_meta=[meta_a],
    )

    router_mock = _patch_router(pre_context=None, routable_names=[], decomposer_invoked=False)

    with patch("archon_search.server.routes_route._build_router", return_value=router_mock):
        client = TestClient(app, headers={"Authorization": f"Bearer {key_a}"})
        response = client.post("/route", json={"query": "anything"})

    assert response.status_code == 200
    data = response.json()
    assert name_a in data["pinned_names"]


def test_route_no_pinned_for_namespace(tmp_path: Path) -> None:
    """When no pinned collection belongs to the caller's namespace, pinned_names is empty."""
    key_a = "a" * 64

    # Pinned path exists in config but its meta row belongs to a DIFFERENT namespace
    path_x = "/data/other"
    name_x = path_to_collection_name(path_x)
    meta_x = CollectionMeta(name=name_x, namespace="tenantB")

    app, mock_store = _make_app_with_namespaces(
        tmp_path,
        pinned_paths=[path_x],
        namespaces={key_a: "tenantA"},
        all_meta=[meta_x],
    )

    router_mock = _patch_router(pre_context=None, routable_names=[], decomposer_invoked=False)

    with patch("archon_search.server.routes_route._build_router", return_value=router_mock):
        client = TestClient(app, headers={"Authorization": f"Bearer {key_a}"})
        response = client.post("/route", json={"query": "anything"})

    assert response.status_code == 200
    data = response.json()
    assert data["pinned_names"] == []
