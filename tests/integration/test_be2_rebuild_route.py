"""Unit + integration tests for BE-2/BE-5 — POST /graph/{collection}/rebuild-communities.

Follows the migrate route (routes_collections.py, migrate_collection): validate
graph-enabled (422) and collection-exists (404), create the job, transition
QUEUED -> RUNNING, spawn the BE-3 task into _background_tasks, return 202 with
the full JobResponse body. Relies solely on APIKeyMiddleware for auth — no
graph-viewer ?token= branch. BE-5 adds the 409 duplicate-rebuild guard and its
two clear mechanisms (active, in the task; lazy, in the guard's read-path).
Brief 2026-07-15-130 adds the ?namespace= query param and namespace-mismatch
validation (Guard 0).

Covers:
- #unit_test test_rebuild_route_returns_202_running_job
- #integration_test test_rebuild_route_404_unknown_collection
- #integration_test test_rebuild_route_422_graph_disabled
- #integration_test test_rebuild_route_401_missing_token
- #unit_test test_rebuild_route_500_on_invalid_namespace_sentinel
- #integration_test test_rebuild_targets_token_namespace_tables
- #integration_test test_rebuild_route_422_when_graph_store_none
- #unit_test test_second_rebuild_returns_409
- #unit_test test_stale_job_id_cleared_and_proceeds
- #unit_test test_task_clears_job_id_on_every_terminal_exit
- #integration_test test_crash_recovery_unwedges_via_lazy_clear
- #integration_test test_user_request_during_gc_rebuild_returns_202_then_blocks (BE-7, S15)
"""
from __future__ import annotations

import asyncio
import secrets
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

_STUB_EMBEDDING_DIM = 384


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _install_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a minimal spaCy stub so graph-enabled apps can be created."""

    class _FakeDoc:
        ents: list = []

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            return _FakeDoc()

    fake_util = types.ModuleType("spacy.util")
    fake_util.get_installed_models = lambda: ["en_core_web_sm"]  # type: ignore[attr-defined]
    fake_cli = types.ModuleType("spacy.cli")
    fake_cli.download = lambda model: None  # type: ignore[attr-defined]
    fake_spacy = types.ModuleType("spacy")
    fake_spacy.load = lambda model: _FakeNLP()  # type: ignore[attr-defined]
    fake_spacy.util = fake_util  # type: ignore[attr-defined]
    fake_spacy.cli = fake_cli  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setitem(sys.modules, "spacy.util", fake_util)
    monkeypatch.setitem(sys.modules, "spacy.cli", fake_cli)


async def _seed_collection(db_path: str, collection: str, ns: str = "default") -> None:
    """Seed a minimal collection record so get_collection_meta returns non-None."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.store import SearchStore

    store = SearchStore(db_path)
    await store.connect()
    try:
        await store.ensure_collection(collection, _STUB_EMBEDDING_DIM)
        meta = CollectionMeta(
            name=collection,
            active_embedding_model="stub-model",
            doc_count=0,
            chunk_count=0,
            namespace=ns,
        )
        await store.update_collection_meta(meta)
    finally:
        await store.disconnect()


async def _seed_graph_node(db_path: str, collection: str, ns: str, entity_id: str) -> None:
    """Write a single graph node so CommunityBuilder.build has something to cluster."""
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import EntityType, GraphNode

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        await gs.ensure_graph_tables(collection, ns=ns)
        node = GraphNode(
            id=entity_id,
            entity_name="Alpha",
            entity_type=EntityType.concept,
            source_doc_id="doc-1",
            collection_name=collection,
        )
        await gs.write_graph(collection, [node], [], ns=ns)
    finally:
        await gs.disconnect()


async def _communities_table_exists(db_path: str, collection: str, ns: str) -> bool:
    from archon_search.graph_store import GraphStore

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        return await gs.communities_table_exists(collection, ns)
    finally:
        await gs.disconnect()


def _poll_job(client: TestClient, job_id: str, api_key: str, timeout_s: float = 10.0) -> dict:
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", headers=_auth(api_key))
        assert r.status_code == 200
        body = r.json()
        if body["status"] in {"DONE", "FAILED", "FAILED_EXPIRED", "CANCELLED"}:
            return body
        time.sleep(0.1)
    pytest.fail(f"job {job_id} did not reach a terminal state in {timeout_s}s")


# ---------------------------------------------------------------------------
# S1 (unit) — 202 with a JobResponse-shaped body reporting RUNNING.
# ---------------------------------------------------------------------------


def test_rebuild_route_returns_202_running_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """202 + full JobResponse body with status RUNNING (migrate-shaped, not PENDING, S1).

    Also fetches GET /jobs/{job_id} after the POST and asserts the persisted
    status is never QUEUED/PENDING, proving the transition persisted
    server-side and isn't just an artifact of the response serialization
    (C1-I-25). No graph nodes are seeded here, so the background task may
    race to FAILED (no entity graph data to cluster) before this GET runs --
    RUNNING, DONE, and FAILED are all valid post-transition states; only
    QUEUED/PENDING would indicate the transition didn't persist.
    """
    _install_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        asyncio.run(_seed_collection(cfg.db_path, "testcol"))

        resp = client.post("/graph/testcol/rebuild-communities", headers=_auth(api_key))

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "RUNNING"
        assert body["job_id"]
        assert body["collection"] == "testcol"
        assert body["namespace"] == "default"
        assert body["result"] is None
        assert body["error"] is None

        job_resp = client.get(f"/jobs/{body['job_id']}", headers=_auth(api_key))
        assert job_resp.status_code == 200
        assert job_resp.json()["status"] in {"RUNNING", "DONE", "FAILED"}


# ---------------------------------------------------------------------------
# S6 (integration) — unknown collection -> 404.
# ---------------------------------------------------------------------------


def test_rebuild_route_404_unknown_collection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown collection -> 404, error echoed."""
    _install_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        resp = client.post("/graph/no-such-collection/rebuild-communities", headers=_auth(api_key))

    assert resp.status_code == 404
    assert resp.json()["detail"] == "collection not found"


# ---------------------------------------------------------------------------
# S5 (integration) — graph.enabled=false -> 422 with established detail string.
# ---------------------------------------------------------------------------


def test_rebuild_route_422_graph_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """graph.enabled=false -> 422 with the routes_graph.py convention detail string (S5).

    The collection is seeded first (via SearchStore directly -- this doesn't
    require graph.enabled) so the guard-ordering invariant under test is
    isolated: the 422 can only originate from the graph-disabled guard
    (Guard 1), never from the collection-not-found guard (Guard 2), because
    the collection genuinely exists.
    """
    with make_real_app(tmp_path, monkeypatch, graph_enabled=False) as (client, cfg, api_key):
        asyncio.run(_seed_collection(cfg.db_path, "testcol"))

        resp = client.post("/graph/testcol/rebuild-communities", headers=_auth(api_key))

    assert resp.status_code == 422
    assert resp.json()["detail"] == "graph inspection requires [graph] enabled=true in server config"


# ---------------------------------------------------------------------------
# graph_store is None despite graph.enabled=true (startup connect() failure)
# -> 422, no job created. Guards against the AttributeError-inside-task wedge.
# ---------------------------------------------------------------------------


def test_rebuild_route_422_when_graph_store_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """config.graph.enabled=true but app.state.graph_store=None -> 422, no job created.

    Simulates graph_store.connect() failing at startup while graph.enabled
    stays True (a real startup-failure scenario). Without the graph_store-None
    guard, the route would spawn _community_rebuild_task with graph_store=None,
    which raises AttributeError outside the task's caught exception tuple,
    silently wedging the job at RUNNING forever after already returning 202.
    """
    _install_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        asyncio.run(_seed_collection(cfg.db_path, "testcol"))

        job_store = client.app.state.job_store
        assert job_store.list() == []

        client.app.state.graph_store = None

        resp = client.post("/graph/testcol/rebuild-communities", headers=_auth(api_key))

        assert resp.status_code == 422
        assert resp.json()["detail"] == "graph inspection requires [graph] enabled=true in server config"
        assert job_store.list() == []


# ---------------------------------------------------------------------------
# S10 (integration) — missing/invalid Bearer -> 401 via middleware.
# ---------------------------------------------------------------------------


def test_rebuild_route_401_missing_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing or invalid Bearer token -> 401 (middleware, no route-level auth)."""
    _install_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        asyncio.run(_seed_collection(cfg.db_path, "testcol"))

        no_auth_resp = client.post("/graph/testcol/rebuild-communities")
        bad_auth_resp = client.post(
            "/graph/testcol/rebuild-communities",
            headers={"Authorization": "Bearer wrong-token"},
        )

    assert no_auth_resp.status_code == 401
    assert no_auth_resp.headers.get("WWW-Authenticate") == "Bearer"
    assert bad_auth_resp.status_code == 401
    assert bad_auth_resp.headers.get("WWW-Authenticate") == "Bearer"


# ---------------------------------------------------------------------------
# S14 (unit) — INVALID_NAMESPACE_SENTINEL namespace -> 500 (middleware-only).
# ---------------------------------------------------------------------------


def test_rebuild_route_500_on_invalid_namespace_sentinel() -> None:
    """A token resolving to an invalid namespace string -> 500 via APIKeyMiddleware alone.

    Constructs a minimal FastAPI app mounting only the real graph_router with
    APIKeyMiddleware configured with a namespaces map that resolves to an
    invalid namespace string ("has space") -- middleware_auth.validate_token_and_get_namespace
    returns INVALID_NAMESPACE_SENTINEL for this, and the middleware's dispatch
    path returns a bare 500 before the route handler is ever invoked. This
    proves the route is subject to full middleware validation and has no
    handler-level auth bypass for this path.

    Note: this does NOT by itself prove a ``?token=`` branch was not copied
    into this route. The middleware's viewer-exemption regex matches only
    ``/graph/{collection}/view``, so this route is always fully validated by
    middleware regardless of whether such a branch existed in the handler --
    a copied branch would simply never be reached.
    """
    from archon_search.server.middleware_auth import APIKeyMiddleware
    from archon_search.server.routes_graph import router as graph_router

    key = "a" * 64
    app = FastAPI()
    app.add_middleware(APIKeyMiddleware, api_key=key, namespaces={key: "has space"})
    app.include_router(graph_router)

    # No app.state deps are needed: the middleware returns 500 before the route
    # handler is ever invoked, so request.app.state is never touched.
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/graph/testcol/rebuild-communities", headers=_auth(key))

    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# S11 (integration) — two tokens for different namespaces each rebuild their
# own namespace's graph tables.
# ---------------------------------------------------------------------------


def test_rebuild_targets_token_namespace_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two namespaces, same collection name: each rebuild targets its own graph tables (S11).

    Note: collections are seeded under distinct names per namespace
    (col-nsa / col-nsb) because SearchStore.update_collection_meta's
    upsert-by-name-only guard refuses to register the same collection *name*
    under two different namespaces via two separate direct-store seed calls
    within one test process (a pre-existing store limitation unrelated to
    this route) -- the same workaround used in
    tests/integration/test_routes_graph_salience.py's _seed_namespace_collection.
    The namespace scoping under test is the (namespace, collection) lock/table
    key derived from the Bearer token, which this still exercises fully.

    The load-bearing assertion is the NEGATIVE cross-namespace check below:
    without it, the positive per-namespace assertions alone would pass even
    if the route ignored namespace and keyed tables by collection name only
    (since the two collection names are already distinct). Asserting that
    col_a's table does not exist under ns "nsb" (and vice versa) proves each
    rebuild wrote ONLY to its own namespace's tables (C1-I-21/22/2).
    """
    pytest.importorskip("leidenalg", reason="leidenalg not installed; skipping BE-2 S11 integration test")
    _install_spacy_stub(monkeypatch)

    key_a = secrets.token_hex(32)
    key_b = secrets.token_hex(32)
    col_a = "col-nsa"
    col_b = "col-nsb"

    with make_real_app(
        tmp_path,
        monkeypatch,
        graph_enabled=True,
        namespaces={key_a: "nsa", key_b: "nsb"},
    ) as (client, cfg, _default_key):
        asyncio.run(_seed_collection(cfg.db_path, col_a, ns="nsa"))
        asyncio.run(_seed_collection(cfg.db_path, col_b, ns="nsb"))
        asyncio.run(_seed_graph_node(cfg.db_path, col_a, "nsa", "concept:alpha-nsa"))
        asyncio.run(_seed_graph_node(cfg.db_path, col_b, "nsb", "concept:alpha-nsb"))

        resp_a = client.post(f"/graph/{col_a}/rebuild-communities?namespace=nsa", headers=_auth(key_a))
        assert resp_a.status_code == 202
        assert resp_a.json()["namespace"] == "nsa"

        resp_b = client.post(f"/graph/{col_b}/rebuild-communities?namespace=nsb", headers=_auth(key_b))
        assert resp_b.status_code == 202
        assert resp_b.json()["namespace"] == "nsb"

        done_a = _poll_job(client, resp_a.json()["job_id"], key_a)
        done_b = _poll_job(client, resp_b.json()["job_id"], key_b)

    assert done_a["status"] == "DONE"
    assert done_b["status"] == "DONE"
    assert done_a["namespace"] == "nsa"
    assert done_b["namespace"] == "nsb"

    # Each rebuild wrote its own namespace-scoped communities table
    # (_archon_graph_nsa__col-nsa_communities vs _archon_graph_nsb__col-nsb_communities).
    assert asyncio.run(_communities_table_exists(cfg.db_path, col_a, "nsa"))
    assert asyncio.run(_communities_table_exists(cfg.db_path, col_b, "nsb"))

    # Negative cross-namespace check: neither collection's table exists under
    # the OTHER namespace. This is what actually proves namespace-scoped
    # targeting -- without it, the two positive assertions above would pass
    # even if the route keyed tables by collection name alone.
    assert not asyncio.run(_communities_table_exists(cfg.db_path, col_a, "nsb"))
    assert not asyncio.run(_communities_table_exists(cfg.db_path, col_b, "nsa"))


# ---------------------------------------------------------------------------
# S7 (unit) — a second rebuild request while one is active -> 409, no
# duplicate job created.
# ---------------------------------------------------------------------------


def test_second_rebuild_returns_409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An active community_rebuild_job_id -> 409, no duplicate job created (S7)."""
    _install_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        asyncio.run(_seed_collection(cfg.db_path, "testcol"))

        from archon_search.types import JobStatus

        job_store = client.app.state.job_store
        active_job = job_store.create_community_rebuild(collection="testcol", namespace="default")
        job_store.transition(active_job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)

        search_store = client.app.state.search_store
        meta = asyncio.run(search_store.get_collection_meta("testcol", namespace="default"))
        meta.community_rebuild_job_id = active_job.job_id
        asyncio.run(search_store.update_collection_meta(meta))

        jobs_before = len(job_store.list())

        resp = client.post("/graph/testcol/rebuild-communities", headers=_auth(api_key))

        assert resp.status_code == 409
        assert resp.json()["detail"] == "community rebuild already in progress for this collection"
        assert len(job_store.list()) == jobs_before


# ---------------------------------------------------------------------------
# Lazy clear — a stale/missing/terminal referenced job -> id cleared, request
# proceeds to 202.
# ---------------------------------------------------------------------------


def test_stale_job_id_cleared_and_proceeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing/terminal referenced job -> id cleared, request proceeds to 202 (lazy clear)."""
    _install_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        asyncio.run(_seed_collection(cfg.db_path, "testcol"))

        search_store = client.app.state.search_store
        meta = asyncio.run(search_store.get_collection_meta("testcol", namespace="default"))
        # Points at a job_id that was never created (missing) -- the guard
        # must treat "missing" the same as "terminal" and clear it.
        meta.community_rebuild_job_id = "no-such-job-id"
        asyncio.run(search_store.update_collection_meta(meta))

        resp = client.post("/graph/testcol/rebuild-communities", headers=_auth(api_key))

        assert resp.status_code == 202
        assert resp.json()["job_id"] != "no-such-job-id"

        reloaded = asyncio.run(search_store.get_collection_meta("testcol", namespace="default"))
        assert reloaded.community_rebuild_job_id != "no-such-job-id"


# ---------------------------------------------------------------------------
# Active clear — the task clears community_rebuild_job_id on every terminal
# exit (DONE and FAILED), and a clear failure is swallowed.
# ---------------------------------------------------------------------------


def test_task_clears_job_id_on_every_terminal_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The rebuild task clears community_rebuild_job_id on both DONE and FAILED,
    and a clear failure (update_collection_meta raising) is swallowed rather
    than propagating out of the task."""
    import archon_search.server.routes_graph as routes_graph_mod
    from archon_search.jobs.store import JobStore
    from archon_search.types import JobStatus

    async def _run_case(outcome: str, meta_write_raises: bool) -> None:
        from archon_search.collection_meta import CollectionMeta
        from archon_search.graph_store import GraphStore
        from archon_search.store import SearchStore

        db_path = str(tmp_path / f"db-{outcome}-{meta_write_raises}")
        search_store = SearchStore(db_path)
        await search_store.connect()
        graph_store = GraphStore(db_path)
        await graph_store.connect()
        job_store = JobStore(path=tmp_path / f"jobs-{outcome}-{meta_write_raises}.json")

        try:
            await search_store.ensure_collection("testcol", _STUB_EMBEDDING_DIM)
            meta = CollectionMeta(
                name="testcol",
                active_embedding_model="stub-model",
                doc_count=0,
                chunk_count=0,
                namespace="default",
            )
            job = job_store.create_community_rebuild(collection="testcol", namespace="default")
            running_job = job_store.transition(job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)
            meta.community_rebuild_job_id = running_job.job_id
            await search_store.update_collection_meta(meta)

            class _FakeConfig:
                pass

            fake_graph_config = _FakeConfig()

            class _FakeBuilder:
                def __init__(self, *a, **kw):
                    pass

                async def build(self, collection, ns):
                    if outcome == "done":
                        return []
                    raise ValueError("no graph nodes")

            monkeypatch.setattr(routes_graph_mod, "CommunityBuilder", _FakeBuilder)

            if meta_write_raises:
                original_update = search_store.update_collection_meta
                call_count = {"n": 0}

                async def _flaky_update(m):
                    call_count["n"] += 1
                    if call_count["n"] == 1:
                        # first call is the setup write above; let it through
                        # by falling back to original -- but we already awaited
                        # it, so this only affects the clear-time call below.
                        raise OSError("disk full")
                    return await original_update(m)

                monkeypatch.setattr(search_store, "update_collection_meta", _flaky_update)

            # Should never raise, even when the clear-time meta write fails.
            await routes_graph_mod._community_rebuild_task(
                job=running_job,
                job_store=job_store,
                graph_store=graph_store,
                graph_config=fake_graph_config,
                search_store=search_store,
            )

            reloaded_job = job_store.get(running_job.job_id)
            assert reloaded_job.status == (
                JobStatus.DONE if outcome == "done" else JobStatus.FAILED
            )

            if not meta_write_raises:
                reloaded_meta = await search_store.get_collection_meta("testcol", namespace="default")
                assert reloaded_meta.community_rebuild_job_id is None
        finally:
            await search_store.disconnect()
            await graph_store.disconnect()

    asyncio.run(_run_case("done", meta_write_raises=False))
    asyncio.run(_run_case("failed", meta_write_raises=False))
    asyncio.run(_run_case("done", meta_write_raises=True))


# ---------------------------------------------------------------------------
# S16 (integration) — a stale id pointing at a FAILED job (post-restart flip
# stand-in) -> new request returns 202, not 409, and the id is cleared.
# ---------------------------------------------------------------------------


def test_crash_recovery_unwedges_via_lazy_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale community_rebuild_job_id pointing at a FAILED job (standing in
    for JobStore._load's post-restart RUNNING -> FAILED crash flip, which
    never touches CollectionMeta) -> a new request returns 202, not 409, and
    the stale id is cleared (S16)."""
    _install_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        asyncio.run(_seed_collection(cfg.db_path, "testcol"))

        from archon_search.types import JobStatus

        job_store = client.app.state.job_store
        crashed_job = job_store.create_community_rebuild(collection="testcol", namespace="default")
        job_store.transition(crashed_job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)
        # Stand in for the post-restart _load crash-status flip (RUNNING -> FAILED)
        # that never touches CollectionMeta.
        job_store.update(crashed_job.job_id, status=JobStatus.FAILED, error="process_restart")

        search_store = client.app.state.search_store
        meta = asyncio.run(search_store.get_collection_meta("testcol", namespace="default"))
        meta.community_rebuild_job_id = crashed_job.job_id
        asyncio.run(search_store.update_collection_meta(meta))

        resp = client.post("/graph/testcol/rebuild-communities", headers=_auth(api_key))

        assert resp.status_code == 202
        assert resp.json()["job_id"] != crashed_job.job_id

        reloaded = asyncio.run(search_store.get_collection_meta("testcol", namespace="default"))
        assert reloaded.community_rebuild_job_id != crashed_job.job_id


# ---------------------------------------------------------------------------
# BE-7 (S15) — a GC rebuild holding the shared module-level rebuild lock (no
# community_rebuild_job_id set, since MaintenanceLoop's GC path never sets it)
# -> a user POST still returns 202 immediately, and its task only reaches DONE
# after the GC-held lock is released. Accepted trade-off (Mo4): 202-then-block,
# not a fast 409 reject -- but no corruption (both complete cleanly).
# ---------------------------------------------------------------------------


def test_user_request_during_gc_rebuild_returns_202_then_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GC rebuild holds the shared (ns, collection) rebuild lock directly
    (standing in for MaintenanceLoop._rebuild_communities_async mid-build,
    which sets no community_rebuild_job_id -- only the route does, for its
    own 409 guard). A user POST arriving while the lock is held:
    - passes the 409 guard (no community_rebuild_job_id is set) and returns
      202 immediately (BE-2's job creation/transition never touches the
      rebuild lock -- only the spawned task's CommunityBuilder.build() call
      does).
    - its background task then blocks inside CommunityBuilder.build() on the
      shared module-level lock until the GC holder releases it.
    - once released, the task proceeds and the job reaches DONE -- proving
      the two never write the community table concurrently (no corruption).

    The GC-lock-holder coroutine runs on the app's OWN event loop (via the
    TestClient's blocking portal, ``client.portal``) so it acquires the SAME
    lock object ``CommunityBuilder.build`` resolves inside the request's
    spawned task -- exactly mirroring production, where MaintenanceLoop's
    task and the route's spawned task are two independent asyncio tasks on
    one shared event loop.
    """
    pytest.importorskip("leidenalg", reason="leidenalg not installed; skipping BE-7 S15 integration test")
    from archon_search.community_builder import _get_rebuild_lock, _rebuild_locks
    from archon_search.types import JobStatus

    _install_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        asyncio.run(_seed_collection(cfg.db_path, "testcol"))
        asyncio.run(_seed_graph_node(cfg.db_path, "testcol", "default", "concept:alpha"))

        _rebuild_locks.clear()
        portal = client.portal

        release_event = asyncio.Event()
        held_event = asyncio.Event()
        events: list[str] = []

        async def _hold_gc_lock() -> None:
            """Stand in for MaintenanceLoop._rebuild_communities_async holding
            the shared (ns, collection) lock -- WITHOUT touching
            CollectionMeta.community_rebuild_job_id, exactly like the real
            GC path (which never sets it)."""
            lock = _get_rebuild_lock("default", "testcol")
            async with lock:
                events.append("gc-start")
                held_event.set()
                await release_event.wait()
                events.append("gc-end")

        # Start the GC-rebuild stand-in on the app's own event loop so it
        # acquires the SAME lock instance CommunityBuilder.build() resolves
        # from the shared module-level registry inside the request's task.
        gc_future = portal.start_task_soon(_hold_gc_lock)
        portal.call(held_event.wait)

        # Outer try/finally guarantees the module-global registry is cleared
        # on exit (pass or fail) so a loop-bound lock never leaks into
        # sibling tests on the same xdist worker. The GC-holder coroutine
        # must be released and awaited BEFORE clearing the registry, hence
        # the existing lock-release finally stays nested inside this one.
        try:
            try:
                # community_rebuild_job_id is NOT set (GC path never sets it) --
                # the user request must pass the 409 guard and return 202.
                resp = client.post("/graph/testcol/rebuild-communities", headers=_auth(api_key))
                events.append("route-202")
                assert resp.status_code == 202
                job_id = resp.json()["job_id"]
                assert resp.json()["status"] == "RUNNING"

                # The task is now blocked inside CommunityBuilder.build() waiting
                # on the lock the GC stand-in holds -- it must NOT reach a
                # terminal state while the lock is still held.
                job_store = client.app.state.job_store
                still_running = job_store.get(job_id)
                assert still_running.status == JobStatus.RUNNING, (
                    "user rebuild task completed before the GC-held lock was "
                    "released -- it should have been blocked on the shared lock"
                )
            finally:
                # Release the GC-held lock; the user's task can now proceed.
                portal.call(release_event.set)
                portal.call(gc_future.result)

            done = _poll_job(client, job_id, api_key, timeout_s=10.0)
            assert done["status"] == "DONE"
            assert done["result"] == {"communities_built": 1}

            # "route-202" happened while the GC lock was still held (gc-start
            # already recorded, gc-end not yet) -- proving the request was
            # accepted (not 409) DURING the GC rebuild, and only the underlying
            # task's completion was delayed until gc-end (no corruption: exactly
            # one community was written, not a partial/doubled concurrent write).
            assert events.index("gc-start") < events.index("route-202") < events.index("gc-end")

            search_store = client.app.state.search_store
            reloaded_meta = asyncio.run(search_store.get_collection_meta("testcol", namespace="default"))
            assert reloaded_meta.community_rebuild_job_id is None
        finally:
            _rebuild_locks.clear()


# ---------------------------------------------------------------------------
# ?namespace= query param (brief 2026-07-15-130, Guard 0)
# ---------------------------------------------------------------------------


def test_rebuild_namespace_query_param_matching_token_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """?namespace= matching the token namespace is accepted; proceeds to 202 (Guard 0 pass)."""
    _install_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        asyncio.run(_seed_collection(cfg.db_path, "testcol"))

        resp = client.post(
            "/graph/testcol/rebuild-communities?namespace=default",
            headers=_auth(api_key),
        )

    assert resp.status_code == 202
    assert resp.json()["namespace"] == "default"


def test_rebuild_namespace_query_param_mismatch_returns_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """?namespace= differing from the token namespace -> 422 with mismatch detail (Guard 0 fail).

    A token authorising 'default' combined with ?namespace=other_ns would
    target the wrong namespace; Guard 0 rejects it so the caller knows to use
    the correct API key.
    """
    _install_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        asyncio.run(_seed_collection(cfg.db_path, "testcol"))

        resp = client.post(
            "/graph/testcol/rebuild-communities?namespace=other_ns",
            headers=_auth(api_key),
        )

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "namespace mismatch" in detail
    assert "default" in detail
    assert "other_ns" in detail
