"""Suite 3 — archon-search /route endpoint e2e tests (Task 6.1: H3.1–H3.5, E3.1–E3.5b, H3.6b; Task 6.2: H3.6–H3.11, E3.5–E3.7; Task 6.1-ns: two-namespace isolation)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import os

import tomlkit
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta

from archon_search.config import SearchConfig
from archon_search.jobs.model import JobStatus
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app


def _make_client(
    tmp_path: Path,
    config: SearchConfig | None = None,
) -> TestClient:
    """Create a TestClient with a fresh isolated app instance."""
    from archon_search.constants import DEFAULT_NAMESPACE
    from archon_search.sync import path_to_collection_name

    if config is None:
        config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store, config_path=tmp_path / "config.toml")

    # Build meta rows for all configured collections (pinned + watch-list) so the namespace filter
    # in routes that call get_all_collections_meta() includes them.  They all belong to
    # DEFAULT_NAMESPACE, which is what the single test key resolves to.
    all_configured_names = {
        path_to_collection_name(p)
        for p in list(config.pinned_collections) + list(config.collections)
    }
    pinned_meta = [
        CollectionMeta(name=name, namespace=DEFAULT_NAMESPACE)
        for name in all_configured_names
    ]

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=pinned_meta)
    mock_store.get_collection_meta = AsyncMock(
        side_effect=lambda name, namespace="default": CollectionMeta(name=name, namespace=namespace)
    )
    mock_store.update_collection_meta = AsyncMock()
    mock_store.drop_collection = AsyncMock()
    mock_store.delete_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


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


# ===========================================================================
# Suite 3 — /ingest + /jobs lifecycle (Task 6.2: H3.6–H3.11, E3.5–E3.7)
# ===========================================================================


def _make_ingest_client(
    tmp_path: Path,
    pipeline_fn=None,
) -> tuple[TestClient, "FastAPI"]:  # type: ignore[name-defined]
    """Create a TestClient with optional injected pipeline function."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store, config_path=tmp_path / "config.toml")
    if pipeline_fn is not None:
        app.state.ingest_pipeline = pipeline_fn
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    return TestClient(app, headers={"Authorization": f"Bearer {key}"}), app


# ---------------------------------------------------------------------------
# H3.6 — ingest job transitions PENDING → DONE (real background task)
# ---------------------------------------------------------------------------
def test_H3_6_ingest_job_transitions_pending_to_done(tmp_path: Path) -> None:
    client, app = _make_ingest_client(tmp_path)

    response = client.post("/ingest", json={"collection": "docs", "path": str(tmp_path)})
    assert response.status_code == 202
    data = response.json()
    job_id = data["job_id"]
    assert data["status"] == JobStatus.PENDING.value

    # TestClient runs the event loop synchronously; background task runs to completion
    # before the client context exits. Poll until terminal.
    for _ in range(20):
        get_resp = client.get(f"/jobs/{job_id}")
        assert get_resp.status_code == 200
        status = get_resp.json()["status"]
        if status in (JobStatus.DONE.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value):
            break
        import time
        time.sleep(0.05)

    final = client.get(f"/jobs/{job_id}").json()
    assert final["status"] == JobStatus.DONE.value


# ---------------------------------------------------------------------------
# H3.7 — failing pipeline → FAILED status, error non-empty
# ---------------------------------------------------------------------------
def test_H3_7_ingest_job_failure_sets_failed_status(tmp_path: Path) -> None:
    async def failing_pipeline(job_id: str, store: object, body: object, **kwargs: object) -> None:
        raise RuntimeError("pipeline exploded")

    client, app = _make_ingest_client(tmp_path, pipeline_fn=failing_pipeline)

    response = client.post("/ingest", json={"collection": "docs"})
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    for _ in range(20):
        get_resp = client.get(f"/jobs/{job_id}")
        status = get_resp.json()["status"]
        if status in (JobStatus.DONE.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value):
            break
        import time
        time.sleep(0.05)

    final = client.get(f"/jobs/{job_id}").json()
    assert final["status"] == JobStatus.FAILED.value
    assert final["error"] is not None
    assert len(final["error"]) > 0


# ---------------------------------------------------------------------------
# H3.8 — cancel while RUNNING → CANCELLING (event-based synchronization)
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_H3_8_ingest_cancel_while_running_transitions_to_cancelling(
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def blocking_pipeline(job_id: str, store: object, body: object, **kwargs: object) -> None:
        started.set()
        await blocked.wait()

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store, config_path=tmp_path / "config.toml")
    app.state.ingest_pipeline = blocking_pipeline

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {key}"},
    ) as client:
        async with app.router.lifespan_context(app):
            post_resp = await client.post(
                "/ingest", json={"collection": "docs"}
            )
            assert post_resp.status_code == 202
            job_id = post_resp.json()["job_id"]

            # Wait until the pipeline signals it is running
            await asyncio.wait_for(started.wait(), timeout=5.0)

            del_resp = await client.delete(f"/jobs/{job_id}")
            assert del_resp.status_code == 202

            get_resp = await client.get(f"/jobs/{job_id}")
            assert get_resp.json()["status"] == JobStatus.CANCELLING.value

            # Unblock pipeline so background task can exit cleanly
            blocked.set()


# ---------------------------------------------------------------------------
# H3.9 — cancel DONE job → 200, job unchanged
# ---------------------------------------------------------------------------
def test_H3_9_cancel_done_job_returns_200_unchanged(tmp_path: Path) -> None:
    client, app = _make_ingest_client(tmp_path)
    job = app.state.job_store.create()
    app.state.job_store.update(job.job_id, status=JobStatus.DONE)

    response = client.delete(f"/jobs/{job.job_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == JobStatus.DONE.value
    assert data["job_id"] == job.job_id


# ---------------------------------------------------------------------------
# H3.10 — two concurrent POST /ingest → two distinct job IDs
# ---------------------------------------------------------------------------
def test_H3_10_two_concurrent_ingest_creates_distinct_job_ids(tmp_path: Path) -> None:
    client, _ = _make_ingest_client(tmp_path)

    resp1 = client.post("/ingest", json={"collection": "col-a"})
    resp2 = client.post("/ingest", json={"collection": "col-b"})

    assert resp1.status_code == 202
    assert resp2.status_code == 202

    id1 = resp1.json()["job_id"]
    id2 = resp2.json()["job_id"]
    assert id1 != id2


# ---------------------------------------------------------------------------
# H3.11 — X-Ingested-By header unconditionally replaces body ingested_by
# ---------------------------------------------------------------------------
def test_H3_11_x_ingested_by_header_replaces_body_value(tmp_path: Path) -> None:
    captured: dict = {}

    async def capturing_pipeline(job_id: str, store: object, body: object, **kwargs: object) -> None:
        captured["ingested_by"] = body.ingested_by  # type: ignore[attr-defined]

    client, _ = _make_ingest_client(tmp_path, pipeline_fn=capturing_pipeline)

    response = client.post(
        "/ingest",
        json={"collection": "docs", "ingested_by": "body-value"},
        headers={"X-Ingested-By": "header-value"},
    )
    assert response.status_code == 202

    # Flush background tasks by polling
    job_id = response.json()["job_id"]
    for _ in range(20):
        status = client.get(f"/jobs/{job_id}").json()["status"]
        if status in (JobStatus.DONE.value, JobStatus.FAILED.value):
            break
        import time
        time.sleep(0.05)

    assert captured.get("ingested_by") == "header-value"


# ---------------------------------------------------------------------------
# E3.5 — POST /ingest missing collection field → 422
# ---------------------------------------------------------------------------
def test_E3_5_ingest_missing_collection_returns_422(tmp_path: Path) -> None:
    client, _ = _make_ingest_client(tmp_path)
    response = client.post("/ingest", json={"path": str(tmp_path)})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# E3.6 — GET /jobs/<unknown-uuid> → 404
# ---------------------------------------------------------------------------
def test_E3_6_get_unknown_job_returns_404(tmp_path: Path) -> None:
    client, _ = _make_ingest_client(tmp_path)
    response = client.get("/jobs/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# E3.7 — cancel CANCELLING job twice → both 202
# ---------------------------------------------------------------------------
def test_E3_7_cancel_cancelling_twice_both_return_202(tmp_path: Path) -> None:
    client, app = _make_ingest_client(tmp_path)
    job = app.state.job_store.create()
    app.state.job_store.update(job.job_id, status=JobStatus.CANCELLING)

    resp1 = client.delete(f"/jobs/{job.job_id}")
    resp2 = client.delete(f"/jobs/{job.job_id}")

    assert resp1.status_code == 202
    assert resp2.status_code == 202


# ===========================================================================
# Suite 3 — /collections lifecycle (Task 6.3: H3.12–H3.15, E3.8–E3.10)
# ===========================================================================


# ---------------------------------------------------------------------------
# H3.12 — POST /collections with config_path set → TOML file updated after add
# ---------------------------------------------------------------------------
def test_H3_12_add_collection_updates_toml(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    new_path = str(tmp_path / "my-docs")

    response = client.post("/collections/", json={"path": new_path})
    assert response.status_code == 202

    config_file = tmp_path / "config.toml"
    assert config_file.exists()
    doc = tomlkit.parse(config_file.read_text(encoding="utf-8"))
    resolved = str(Path(new_path).expanduser().resolve())
    assert resolved in doc["collections"]["collections"]


# ---------------------------------------------------------------------------
# H3.13 — DELETE /collections/{name} → TOML file updated after remove
# ---------------------------------------------------------------------------
def test_H3_13_remove_collection_updates_toml(tmp_path: Path) -> None:
    col_path = str(tmp_path / "my-docs")
    config = SearchConfig()
    config.collections = [col_path]
    client = _make_client(tmp_path, config=config)

    name = "my_docs"
    response = client.delete(f"/collections/{name}")
    assert response.status_code == 200

    config_file = tmp_path / "config.toml"
    assert config_file.exists()
    doc = tomlkit.parse(config_file.read_text(encoding="utf-8"))
    resolved = str(Path(col_path).expanduser().resolve())
    assert resolved not in list(doc["collections"]["collections"])


# ---------------------------------------------------------------------------
# H3.14 — path /home/user/my-docs → name my_docs
# ---------------------------------------------------------------------------
def test_H3_14_path_to_name_conversion(tmp_path: Path) -> None:
    col_path = "/home/user/my-docs"
    config = SearchConfig()
    config.collections = [col_path]
    client = _make_client(tmp_path, config=config)

    response = client.get("/collections/")
    assert response.status_code == 200
    names = [entry["name"] for entry in response.json()]
    assert "my_docs" in names


# ---------------------------------------------------------------------------
# H3.15 — one regular + one pinned collection → both appear in GET /collections
# ---------------------------------------------------------------------------
def test_H3_15_regular_and_pinned_both_in_list(tmp_path: Path) -> None:
    regular_path = str(tmp_path / "regular-docs")
    pinned_path = str(tmp_path / "pinned-docs")
    config = SearchConfig()
    config.collections = [regular_path]
    config.pinned_collections = [pinned_path]
    client = _make_client(tmp_path, config=config)

    response = client.get("/collections/")
    assert response.status_code == 200
    names = [entry["name"] for entry in response.json()]
    assert "regular_docs" in names
    assert "pinned_docs" in names


# ---------------------------------------------------------------------------
# E3.8 — path="~/docs" → resolved to absolute (no tilde in stored path)
# ---------------------------------------------------------------------------
def test_E3_8_tilde_path_resolved_to_absolute(tmp_path: Path) -> None:
    client = _make_client(tmp_path)

    response = client.post("/collections/", json={"path": "~/docs"})
    assert response.status_code == 202

    config_file = tmp_path / "config.toml"
    assert config_file.exists()
    doc = tomlkit.parse(config_file.read_text(encoding="utf-8"))
    stored_paths = list(doc["collections"]["collections"])
    assert all(not p.startswith("~") for p in stored_paths)
    expected = str(Path("~/docs").expanduser().resolve())
    assert expected in stored_paths


# ---------------------------------------------------------------------------
# E3.9 — pinned but not in collections → 409
# ---------------------------------------------------------------------------
def test_E3_9_pinned_only_collection_delete_returns_409(tmp_path: Path) -> None:
    pinned_path = str(tmp_path / "pinned-only")
    config = SearchConfig()
    config.pinned_collections = [pinned_path]
    # Not in config.collections
    client = _make_client(tmp_path, config=config)

    name = "pinned_only"
    response = client.delete(f"/collections/{name}")
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# E3.10 — delete then delete again → 404 on second
# ---------------------------------------------------------------------------
def test_E3_10_double_delete_returns_404_on_second(tmp_path: Path) -> None:
    col_path = str(tmp_path / "my-docs")
    config = SearchConfig()
    config.collections = [col_path]
    client = _make_client(tmp_path, config=config)

    name = "my_docs"
    first = client.delete(f"/collections/{name}")
    assert first.status_code == 200

    second = client.delete(f"/collections/{name}")
    assert second.status_code == 404


# ---------------------------------------------------------------------------
# H3.16 — /status returns collections in alphabetical order
# ---------------------------------------------------------------------------
def test_H3_16_status_collections_alphabetical(tmp_path: Path) -> None:
    config = SearchConfig()
    config.collections = ["/data/zebra", "/data/alpha", "/data/mango"]
    client = _make_client(tmp_path, config=config)

    response = client.get("/status")
    assert response.status_code == 200
    names = [c["name"] for c in response.json()["collections"]]
    assert names == sorted(names)


# ---------------------------------------------------------------------------
# H3.17 — fresh collection with no prior ingest → status "not_yet_indexed"
# ---------------------------------------------------------------------------
def test_H3_17_fresh_collection_status_not_yet_indexed(tmp_path: Path) -> None:
    config = SearchConfig()
    config.collections = ["/data/fresh-docs"]
    client = _make_client(tmp_path, config=config)

    response = client.get("/status")
    assert response.status_code == 200
    collections = response.json()["collections"]
    assert len(collections) == 1
    assert collections[0]["name"] == "fresh_docs"
    assert collections[0]["status"] == "not_yet_indexed"


# ---------------------------------------------------------------------------
# H3.18 — /indexing-state fields filtered to expected set
# ---------------------------------------------------------------------------
def test_H3_18_indexing_state_fields_filtered(tmp_path: Path) -> None:
    from archon_search.constants import DEFAULT_NAMESPACE
    from archon_search.progress import CollectionProgress, IndexingState, IndexingStatus
    from archon_search.server.routes_state import _COLLECTION_API_FIELDS

    state = IndexingState(
        collections={
            "docs": CollectionProgress(
                status=IndexingStatus.DONE,
                processed_files=5,
                total_files=5,
                error=None,
                error_count=0,
            )
        }
    )
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store, config_path=tmp_path / "config.toml")
    app.state.state_store.write(state)
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(
        return_value=[CollectionMeta(name="docs", namespace=DEFAULT_NAMESPACE)]
    )
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store
    _key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    test_client = TestClient(app, headers={"Authorization": f"Bearer {_key}"})

    response = test_client.get("/indexing-state")
    assert response.status_code == 200
    data = response.json()
    assert "collections" in data
    for col_data in data["collections"].values():
        assert set(col_data.keys()) <= _COLLECTION_API_FIELDS


# ---------------------------------------------------------------------------
# H3.19 — no prior ingest → /indexing-state returns {}
# ---------------------------------------------------------------------------
def test_H3_19_no_prior_ingest_indexing_state_empty(tmp_path: Path) -> None:
    client = _make_client(tmp_path)

    response = client.get("/indexing-state")
    assert response.status_code == 200
    assert response.json() == {}


# ---------------------------------------------------------------------------
# H3.20 — /status pid matches os.getpid()
# ---------------------------------------------------------------------------
def test_H3_20_status_pid_matches_current_process(tmp_path: Path) -> None:
    import os

    client = _make_client(tmp_path)

    response = client.get("/status")
    assert response.status_code == 200
    assert response.json()["pid"] == os.getpid()


# ===========================================================================
# Suite — two-namespace isolation integration tests (Task 6.1-ns)
# ===========================================================================


def _make_two_namespace_clients(
    tmp_path: Path,
) -> tuple[TestClient, TestClient]:
    """Create two TestClients authenticated as keyA (tenantA) and keyB (tenantB).

    Returns (client_a, client_b).  The app is wired with:
      config.namespaces = {"keyA": "tenantA", "keyB": "tenantB"}
      mock_store: tenantA owns "col_a", tenantB owns "col_b"
    """
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.namespaces = {"keyA": "tenantA", "keyB": "tenantB"}
    # Register both collection paths so route helpers (_all_collection_paths) know their names
    config.collections = [str(tmp_path / "col-a"), str(tmp_path / "col-b")]

    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store, config_path=tmp_path / "config.toml")

    meta_a = CollectionMeta(name="col_a", namespace="tenantA")
    meta_b = CollectionMeta(name="col_b", namespace="tenantB")
    all_meta = [meta_a, meta_b]

    def _get_meta(name: str, namespace: str = "default") -> CollectionMeta | None:
        for m in all_meta:
            if m.name == name and m.namespace == namespace:
                return m
        return None

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=all_meta)
    mock_store.get_collection_meta = AsyncMock(side_effect=_get_meta)
    mock_store.update_collection_meta = AsyncMock()
    mock_store.drop_collection = AsyncMock()
    mock_store.delete_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    client_a = TestClient(app, headers={"Authorization": "Bearer keyA"})
    client_b = TestClient(app, headers={"Authorization": "Bearer keyB"})
    return client_a, client_b


# ---------------------------------------------------------------------------
# test_two_namespace_collection_isolation — key A sees only tenantA collection
# ---------------------------------------------------------------------------
def test_two_namespace_collection_isolation(tmp_path: Path) -> None:
    client_a, client_b = _make_two_namespace_clients(tmp_path)

    resp_a = client_a.get("/collections/")
    assert resp_a.status_code == 200
    names_a = [e["name"] for e in resp_a.json()]
    assert "col_a" in names_a
    assert "col_b" not in names_a

    resp_b = client_b.get("/collections/")
    assert resp_b.status_code == 200
    names_b = [e["name"] for e in resp_b.json()]
    assert "col_b" in names_b
    assert "col_a" not in names_b


# ---------------------------------------------------------------------------
# test_two_namespace_search_isolation — key A cannot search key B's collection (404)
# ---------------------------------------------------------------------------
def test_two_namespace_search_isolation(tmp_path: Path) -> None:
    client_a, _ = _make_two_namespace_clients(tmp_path)

    response = client_a.post("/search", json={"collection": "col_b", "query": "hello"})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# test_two_namespace_status_isolation — key A /status has only tenantA collections
# ---------------------------------------------------------------------------
def test_two_namespace_status_isolation(tmp_path: Path) -> None:
    client_a, client_b = _make_two_namespace_clients(tmp_path)

    resp_a = client_a.get("/status")
    assert resp_a.status_code == 200
    names_a = [c["name"] for c in resp_a.json()["collections"]]
    assert "col_a" in names_a
    assert "col_b" not in names_a

    resp_b = client_b.get("/status")
    assert resp_b.status_code == 200
    names_b = [c["name"] for c in resp_b.json()["collections"]]
    assert "col_b" in names_b
    assert "col_a" not in names_b


# ---------------------------------------------------------------------------
# test_two_namespace_job_isolation — key A cannot see key B's job (404)
# ---------------------------------------------------------------------------
def test_two_namespace_job_isolation(tmp_path: Path) -> None:
    client_a, client_b = _make_two_namespace_clients(tmp_path)

    # key B creates a job
    resp = client_b.post("/ingest", json={"collection": "col_b"})
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    # key A tries to read key B's job
    resp_a = client_a.get(f"/jobs/{job_id}")
    assert resp_a.status_code == 404


# ---------------------------------------------------------------------------
# test_two_namespace_collection_detail_isolation — key A /collections/{name} for B's → 404
# ---------------------------------------------------------------------------
def test_two_namespace_collection_detail_isolation(tmp_path: Path) -> None:
    client_a, _ = _make_two_namespace_clients(tmp_path)

    response = client_a.get("/collections/col_b")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# test_two_namespace_delete_isolation — key A DELETE B's collection → 404; B's still exists
# ---------------------------------------------------------------------------
def test_two_namespace_delete_isolation(tmp_path: Path) -> None:
    client_a, client_b = _make_two_namespace_clients(tmp_path)

    # key A cannot delete key B's collection
    resp = client_a.delete("/collections/col_b")
    assert resp.status_code == 404

    # key B's collection still accessible to key B
    resp_b = client_b.get("/collections/")
    assert resp_b.status_code == 200
    names_b = [e["name"] for e in resp_b.json()]
    assert "col_b" in names_b


# ---------------------------------------------------------------------------
# test_single_key_backward_compat — namespaces={}, existing single key → all collections visible
# ---------------------------------------------------------------------------
def test_single_key_backward_compat(tmp_path: Path) -> None:
    """Single-key mode: no namespaces dict → all collections resolve to DEFAULT_NAMESPACE."""
    from archon_search.constants import DEFAULT_NAMESPACE

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    # Empty namespaces — single-key backward-compat mode
    config.namespaces = {}
    config.collections = [str(tmp_path / "col-x"), str(tmp_path / "col-y")]

    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store, config_path=tmp_path / "config.toml")

    all_meta = [
        CollectionMeta(name="col_x", namespace=DEFAULT_NAMESPACE),
        CollectionMeta(name="col_y", namespace=DEFAULT_NAMESPACE),
    ]
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=all_meta)
    mock_store.get_collection_meta = AsyncMock(
        side_effect=lambda name, namespace="default": CollectionMeta(name=name, namespace=namespace)
    )
    mock_store.update_collection_meta = AsyncMock()
    mock_store.drop_collection = AsyncMock()
    mock_store.delete_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    app.state.search_store = mock_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    resp = client.get("/collections/")
    assert resp.status_code == 200
    names = [e["name"] for e in resp.json()]
    assert "col_x" in names
    assert "col_y" in names
