"""Regression tests for five startup / warm-up readiness gaps.

Every test in this file is written as a *failing* reproduction first: it encodes
the correct behavior, so it fails against the current code and passes once the
corresponding gap is closed.

1. ``collection_sync.sync()`` is awaited inline in the lifespan, so uvicorn cannot
   bind the port until a full corpus sync finishes.
2. ``app.state.warmup_result`` is never initialised in ``create_app``.
3. ``GET /ready`` reports ``models=ok`` while eager warm-up is still pending.
4. ``_server_connect_fail_msg()`` cannot probe ``/ready`` — it only asks the
   service manager, which is blind to a foreground ``archon-search serve``.
5. ``EmbedderCache.get_or_load`` waits on the loader event with no timeout, so a
   stalled loader hangs every concurrent waiter forever.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore


@pytest.fixture
def job_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


# --------------------------------------------------------------------------- #
# Bug 1 — startup sync blocks the lifespan yield
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_sync_blocks_lifespan_startup(tmp_path: Path, job_store: JobStore) -> None:
    """Startup collection sync must NOT be awaited inside the lifespan startup.

    uvicorn runs ``lifespan.startup()`` to completion *before* it binds the
    listening socket. ``app.py``'s lifespan awaits
    ``app.state.collection_sync.sync(all_cols)`` immediately before ``yield``, so on
    a populated corpus the port stays closed for the whole sync — clients get
    ``ConnectError``, not a 503.

    Oracle: ``SearchCollectionSync.sync`` is replaced with a coroutine parked on an
    event this test controls. Startup must still complete while it is parked, i.e.
    the sync has to be handed to ``asyncio.create_task``. The second assertion
    keeps the test honest: deleting the sync outright must not make it pass.
    """
    import asyncio  # noqa: PLC0415
    from unittest.mock import AsyncMock, patch  # noqa: PLC0415

    from archon_search.server.app import create_app  # noqa: PLC0415
    from archon_search.store import SearchStore  # noqa: PLC0415
    from archon_search.sync import SearchCollectionSync  # noqa: PLC0415

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = ["docs"]  # non-empty => the lifespan takes the sync branch

    release = asyncio.Event()
    sync_started = asyncio.Event()

    async def parked_sync(self: SearchCollectionSync, collections: list[str], progress_cb=None):
        sync_started.set()
        await release.wait()
        return None

    with (
        patch.object(SearchStore, "connect", new=AsyncMock()),
        patch.object(SearchStore, "_run_startup_migrations", new=AsyncMock()),
        patch.object(SearchStore, "disconnect", new=AsyncMock()),
        patch.object(SearchStore, "get_all_collections_meta", new=AsyncMock(return_value=[])),
        patch.object(SearchCollectionSync, "sync", new=parked_sync),
    ):
        app = create_app(cfg, job_store)
        startup_done = asyncio.Event()
        shutdown = asyncio.Event()

        # Enter AND exit the lifespan inside ONE task: the mounted FastMCP lifespan
        # resets a ContextVar on exit and raises if entry/exit run in different
        # contexts (as they do when only __aenter__ is wrapped in a task).
        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                startup_done.set()
                await shutdown.wait()

        lifespan_task = asyncio.create_task(run_lifespan())
        started_waiter = asyncio.create_task(sync_started.wait())
        startup_waiter = asyncio.create_task(startup_done.wait())
        try:
            # Wait until the parked sync has actually been entered, so the check
            # below cannot pass merely because startup has not reached sync yet.
            await asyncio.wait(
                {started_waiter, lifespan_task}, timeout=30.0, return_when=asyncio.FIRST_COMPLETED
            )
            sync_was_entered = sync_started.is_set()
            # The timeout elapses only on the FAILURE path (a blocking startup never
            # sets the event), so a generous budget is free when green.
            await asyncio.wait(
                {startup_waiter, lifespan_task}, timeout=30.0, return_when=asyncio.FIRST_COMPLETED
            )
            startup_completed_while_sync_parked = startup_done.is_set()
        finally:
            started_waiter.cancel()
            startup_waiter.cancel()
            release.set()
            shutdown.set()
            await lifespan_task

    assert sync_was_entered, "startup sync never ran at all"
    assert startup_completed_while_sync_parked, (
        "lifespan startup blocked on collection_sync.sync() — uvicorn cannot bind the "
        "port until startup returns, so clients get ConnectError for the whole sync "
        "window; the startup sync must run via asyncio.create_task()"
    )


# --------------------------------------------------------------------------- #
# Bug 2 — app.state.warmup_result is never initialised
# --------------------------------------------------------------------------- #
def test_warmup_result_attribute_missing(tmp_path: Path, job_store: JobStore) -> None:
    """``create_app`` must initialise ``app.state.warmup_result``.

    Readiness reporting needs a single, always-present source of truth for warm-up
    progress. Like ``app.state._warmup_task`` and ``app.state._background_tasks`` it
    has to be set in the ``create_app`` body — an attribute created only inside the
    ``if config.eager_load_embedders:`` lifespan branch AttributeErrors for every
    reader when the flag is off.
    """
    from archon_search.server.app import create_app  # noqa: PLC0415

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")

    app = create_app(cfg, job_store)

    sentinel = object()
    value = getattr(app.state, "warmup_result", sentinel)
    assert value is not sentinel, (
        "create_app() never sets app.state.warmup_result — /ready and /status have no "
        "way to tell that eager warm-up is still in progress, and any reader of the "
        "attribute raises AttributeError"
    )


# --------------------------------------------------------------------------- #
# Bug 3 — /ready returns 200 while eager warm-up is pending
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ready_returns_200_while_warmup_pending() -> None:
    """``/ready`` must report ``models=pending`` (503) while eager warm-up runs.

    ``model_validation`` completes in seconds (it only probes that the models are
    resolvable); eager warm-up takes minutes. Today ``_model_check_status`` looks at
    ``model_validation`` alone, so ``/ready`` answers 200/``models=ok`` while the
    first real search would still block on ONNX construction — an orchestrator
    routes traffic to a server that cannot serve it.
    """
    import json  # noqa: PLC0415
    from datetime import UTC, datetime  # noqa: PLC0415
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from archon_search.model_validation import ModelValidationResult  # noqa: PLC0415
    from archon_search.server.routes_ready import _model_check_status, ready  # noqa: PLC0415
    from archon_search.server.schemas import CheckStatus  # noqa: PLC0415

    cfg = SearchConfig()
    cfg.eager_load_embedders = True

    class FakeStore:
        ping = AsyncMock(return_value=True)

    class FakeState:
        model_validation = ModelValidationResult(
            embedder_ok=True,
            reranker_ok=True,
            provider_warnings=[],
            validated_at=datetime.now(UTC),
        )
        warmup_result = "pending"
        config = cfg
        search_store = FakeStore()

    class FakeApp:
        state = FakeState()

    class FakeRequest:
        app = FakeApp()

    request = FakeRequest()

    assert _model_check_status(request) is CheckStatus.PENDING, (
        "_model_check_status() ignores app.state.warmup_result — it reports OK while "
        "eager warm-up is still pending, because it only inspects model_validation"
    )

    response = await ready(request)
    body = json.loads(response.body)
    assert body["checks"]["models"] == CheckStatus.PENDING.value
    assert response.status_code == 503, (
        "/ready answered 200 while eager warm-up was still pending — load balancers "
        "will send real traffic to a server whose first search blocks on ONNX init"
    )


# --------------------------------------------------------------------------- #
# Bug 4 — _server_connect_fail_msg() cannot probe /ready
# --------------------------------------------------------------------------- #
def test_server_connect_fail_msg_probes_ready_endpoint() -> None:
    """``_server_connect_fail_msg()`` must take ``base_url`` and probe ``/ready``.

    The current implementation asks the *managed* service (launchd / systemd) only,
    so a foreground ``archon-search serve`` that is mid-warm-up is reported as "not
    running" — the operator is told to start a server that is already starting. A
    ``/ready`` probe answering 503 with ``checks.models == "pending"`` is the
    authoritative "still starting up" signal and works for every launch mode.
    """
    import inspect  # noqa: PLC0415
    from unittest.mock import MagicMock, patch  # noqa: PLC0415

    from archon_search.cli import _helpers  # noqa: PLC0415

    params = inspect.signature(_helpers._server_connect_fail_msg).parameters
    assert "base_url" in params, (
        "_server_connect_fail_msg() takes no base_url — it cannot probe /ready and "
        f"falls back to the launchd/systemd check, which is blind to a foreground "
        f"'archon-search serve'. Current signature: "
        f"{inspect.signature(_helpers._server_connect_fail_msg)}"
    )

    fake_resp = MagicMock()
    fake_resp.status_code = 503
    fake_resp.json.return_value = {"ready": False, "checks": {"models": "pending"}}

    with patch.object(_helpers.httpx, "get", return_value=fake_resp):
        msg = _helpers._server_connect_fail_msg(base_url="http://127.0.0.1:8765")

    assert "starting" in msg.lower(), (
        "/ready returned 503 with checks.models='pending' (the server is warming up) "
        f"but the CLI reported: {msg!r}"
    )


# --------------------------------------------------------------------------- #
# Bug 5 — get_or_load waits on the loader event without a timeout
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_or_load_concurrent_waiter_hangs_without_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent ``get_or_load`` waiter must fail fast, not hang forever.

    ``embedder_cache.py`` deduplicates concurrent loads through an
    ``asyncio.Event``; the waiter does a bare ``await event.wait()`` (line 60) with
    no timeout. If the active loader stalls (slow disk, wedged ONNX init, a load
    that never resolves), every waiter — i.e. every concurrent search for that
    model — blocks indefinitely with no diagnostic. The waiter needs its own
    timeout that raises a descriptive error.

    Oracle: the loader is parked, then a second ``get_or_load`` for the same model
    is run under a *generous* external ``wait_for`` budget. A bare, message-less
    ``TimeoutError`` means the external budget fired — the waiter hung. A
    descriptive error means the cache aborted the wait itself.

    The bound must live in a module-level constant (house style, cf.
    ``routes_search._SEARCH_TIMEOUT_SECONDS``) so this test can dial it down; the
    loop below rebinds every numeric ``*TIMEOUT*`` constant in the module to 0.1 s.
    A hardcoded inline timeout is not acceptable — it makes the behavior untestable
    and unconfigurable.
    """
    import asyncio  # noqa: PLC0415
    import threading  # noqa: PLC0415
    from unittest.mock import MagicMock, patch  # noqa: PLC0415

    from archon_search import embedder_cache as ec  # noqa: PLC0415

    # Dial any module-level timeout constant down so a realistic production value
    # (tens of seconds) does not make this test slow.
    for name in dir(ec):
        if "TIMEOUT" in name.upper() and isinstance(getattr(ec, name), (int, float)):
            monkeypatch.setattr(ec, name, 0.1)

    release = threading.Event()
    loader_entered = threading.Event()

    def stalled_make_embedder(model_name: str, providers=None):
        loader_entered.set()
        release.wait()  # park the loader thread; released in the finally below
        return MagicMock()

    cache = ec.EmbedderCache(max_size=2)

    with patch.object(ec, "make_embedder", new=stalled_make_embedder):
        loader_task = asyncio.create_task(cache.get_or_load("m"))
        try:
            # Let the loader register itself in _loading and enter make_embedder.
            for _ in range(500):
                if loader_entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert loader_entered.is_set(), "loader never started; test setup is wrong"

            exc: BaseException | None = None
            try:
                # 5 s only ever elapses on the FAILURE path (an unbounded wait); a
                # bounded waiter aborts in ~0.1 s.
                await asyncio.wait_for(cache.get_or_load("m"), timeout=5.0)
            except BaseException as caught:  # noqa: BLE001 — the exception IS the oracle
                exc = caught
        finally:
            release.set()
            loader_task.cancel()
            await asyncio.gather(loader_task, return_exceptions=True)

    assert exc is not None, "expected the concurrent waiter to fail fast, but it returned"
    assert str(exc), (
        "the concurrent get_or_load() waiter hung until the external 5s wait_for killed "
        "it (a bare, message-less TimeoutError from asyncio.wait_for). "
        "embedder_cache.py:60 `await event.wait()` has no timeout, so a stalled loader "
        "blocks every concurrent search for that model forever, with no diagnostic. "
        f"Got {type(exc).__name__}({str(exc)!r})"
    )
