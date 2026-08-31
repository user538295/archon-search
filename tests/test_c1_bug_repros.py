"""Reproduction tests for the C1 bug batch.

Every test here is written as a *failing* reproduction first: it encodes the
correct behavior, so it fails against the current code and passes once the
corresponding bug is fixed. Each one is a permanent regression guard.

C1-I-1   startup sync runs without holding ``app.state.sync_lock``
C1-I-2   the lifespan ``finally`` disconnects the stores before cancelling tasks
C1-I-3   ``_server_connect_fail_msg(base_url=...)`` never consults the service manager
C1-I-4   ``EmbedderCache.get_or_load`` leaves ``_loading`` poisoned after a waiter timeout
C1-I-5   that timeout raises a bare ``RuntimeError`` (no not-ready exception type)
C1-I-6   eager warm-up has no terminal timeout and does not mark cancellation as failed
C1-I-7   ``GET /ready`` ignores the still-running startup sync task
C1-I-8   CLI connect-failure paths must not touch the network (probe must be patchable)
C1-I-9   nine CLI modules import ``_SERVER_NOT_RUNNING_MSG`` and never use it
C1-I-10  the ``/ready`` probe ignores ``checks.storage``
C1-I-12  ``app.state.warmup_result`` is not surfaced by ``GET /status``
C1-I-14  ``_warmup_pending()`` is evaluated twice per ``GET /ready``
C1-I-15  a ``"checks": null`` body makes the probe raise AttributeError into a blanket except
C1-I-16  probe succeeds with non-usable body but service-manager leak survives (sequential-add)
"""
from __future__ import annotations

import ast
import asyncio
import threading
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from archon_search.config import SearchConfig
from archon_search.jobs.model import JobStatus
from archon_search.jobs.store import JobStore

pytestmark = pytest.mark.xdist_group("c1_bugs")


@pytest.fixture
def job_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


@pytest.fixture(autouse=True)
def _isolate_sync_suppression_sentinel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Give every test in this module its own ``ARCHON_SEARCH_DATA_DIR``.

    Sticky suppression (fix-brief-C item 1) persists a sentinel file under
    ``get_data_dir()``. Without this override, a test here that triggers
    suppression would write it into the SESSION-scoped, per-xdist-worker
    directory that ``tests/conftest.py``'s ``_archon_isolated_data_dir`` points
    at by default — and this whole module is pinned to one worker via
    ``xdist_group("c1_bugs")`` above, so a leftover sentinel would leak from
    one test into the next one in declaration order and make suppression
    state non-deterministic across the file. ``make_real_app``-based tests
    already set this per-test (and set it to this same ``tmp_path``, so the
    two overrides never disagree); this fixture extends the same isolation to
    the direct ``create_app()`` boot tests, which do not.
    """
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))


def _enter_store_patches(stack: ExitStack) -> None:
    """Enter the patches that keep ``create_app``'s lifespan off real LanceDB I/O."""
    from archon_search.store import SearchStore  # noqa: PLC0415

    async def _fake_connect(self: SearchStore) -> None:
        # A bare AsyncMock() leaves self._db at its __init__ default (None), so
        # app.py's post-connect check_and_warn_legacy_graph_tables(store._db)
        # call fails with AttributeError on db.list_tables() — harmless (guarded
        # by its own except Exception -> logger.warning(exc_info=True)) but it
        # spams a WARNING traceback into every test's output/caplog. Stub _db
        # with a working list_tables() so the scan finds nothing instead.
        self._db = MagicMock(list_tables=AsyncMock(return_value=MagicMock(tables=[])))

    stack.enter_context(patch.object(SearchStore, "connect", new=_fake_connect))
    stack.enter_context(patch.object(SearchStore, "_run_startup_migrations", new=AsyncMock()))
    stack.enter_context(
        patch.object(SearchStore, "get_all_collections_meta", new=AsyncMock(return_value=[]))
    )


# --------------------------------------------------------------------------- #
# C1-I-1 — startup sync does not hold app.state.sync_lock
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_startup_sync_holds_sync_lock(tmp_path: Path, job_store: JobStore) -> None:
    """The lifespan startup sync must run under ``app.state.sync_lock``.

    ``POST /sync`` (``routes_sync.py``) serialises every operator-triggered sync
    through ``app.state.sync_lock`` and answers 409 while it is held. The startup
    sync spawned by ``app.py`` calls ``collection_sync.sync()`` directly, holding
    nothing — so a ``POST /sync`` arriving during startup runs a *second*
    concurrent sync over the same collections instead of being rejected.

    Oracle: ``SearchCollectionSync.sync`` records whether the lock was held at the
    moment it was entered. Nothing else can observe this without racing.
    """
    from archon_search.server.app import create_app  # noqa: PLC0415
    from archon_search.store import SearchStore  # noqa: PLC0415
    from archon_search.sync import SearchCollectionSync, SyncResult  # noqa: PLC0415

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = ["docs"]  # non-empty => the lifespan takes the startup-sync branch

    holder: dict[str, object] = {}
    lock_held_at_sync: list[bool] = []

    async def recording_sync(
        self: SearchCollectionSync, collections: list[str], progress_cb=None
    ) -> SyncResult:
        app = holder["app"]
        lock_held_at_sync.append(app.state.sync_lock.locked())  # type: ignore[attr-defined]
        return SyncResult()

    with ExitStack() as stack:
        _enter_store_patches(stack)
        stack.enter_context(patch.object(SearchStore, "disconnect", new=AsyncMock()))
        stack.enter_context(patch.object(SearchCollectionSync, "sync", new=recording_sync))

        app = create_app(cfg, job_store)
        holder["app"] = app
        startup_done = asyncio.Event()
        shutdown = asyncio.Event()

        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                startup_done.set()
                await shutdown.wait()

        lifespan_task = asyncio.create_task(run_lifespan())
        try:
            await asyncio.wait_for(startup_done.wait(), timeout=30.0)
            # The startup sync is a background task; give it up to 5 s to be entered.
            for _ in range(500):
                if lock_held_at_sync:
                    break
                await asyncio.sleep(0.01)
        finally:
            shutdown.set()
            await lifespan_task

    assert lock_held_at_sync, "startup sync never ran at all — test setup is wrong"
    assert lock_held_at_sync[0] is True, (
        "the startup sync called collection_sync.sync() without holding "
        "app.state.sync_lock, so a POST /sync during startup starts a second "
        "concurrent sync over the same collections instead of getting a 409"
    )


# --------------------------------------------------------------------------- #
# C1-I-2 — stores are disconnected before background tasks are cancelled
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_background_tasks_cancelled_before_store_disconnect(
    tmp_path: Path, job_store: JobStore
) -> None:
    """Shutdown must cancel background tasks BEFORE disconnecting the store.

    The lifespan ``finally`` block awaits ``search_store.disconnect()`` first and
    only then cancels ``app.state._background_tasks``. Any in-flight task (the
    startup sync, eager warm-up, model validation) therefore keeps issuing
    LanceDB calls against a store that is already closed.

    Oracle: ``SearchStore.disconnect`` records the not-yet-done background tasks
    at the instant it runs. A correct shutdown order leaves that list empty.
    """
    from archon_search.server.app import create_app  # noqa: PLC0415
    from archon_search.store import SearchStore  # noqa: PLC0415
    from archon_search.sync import SearchCollectionSync, SyncResult  # noqa: PLC0415

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = ["docs"]

    holder: dict[str, object] = {}
    live_at_disconnect: list[list[str]] = []
    release = asyncio.Event()
    sync_started = asyncio.Event()

    async def parked_sync(
        self: SearchCollectionSync, collections: list[str], progress_cb=None
    ) -> SyncResult:
        sync_started.set()
        await release.wait()  # keep the task alive across shutdown
        return SyncResult()

    async def recording_disconnect(self: SearchStore) -> None:
        app = holder["app"]
        live_at_disconnect.append(
            [repr(t) for t in app.state._background_tasks if not t.done()]  # type: ignore[attr-defined]
        )

    with ExitStack() as stack:
        _enter_store_patches(stack)
        stack.enter_context(patch.object(SearchStore, "disconnect", new=recording_disconnect))
        stack.enter_context(patch.object(SearchCollectionSync, "sync", new=parked_sync))

        app = create_app(cfg, job_store)
        holder["app"] = app
        startup_done = asyncio.Event()
        shutdown = asyncio.Event()

        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                startup_done.set()
                await shutdown.wait()

        lifespan_task = asyncio.create_task(run_lifespan())
        try:
            await asyncio.wait_for(startup_done.wait(), timeout=30.0)
            await asyncio.wait_for(sync_started.wait(), timeout=30.0)
        finally:
            shutdown.set()
            await lifespan_task
            release.set()

    assert live_at_disconnect, "SearchStore.disconnect() was never called during shutdown"
    assert live_at_disconnect[0] == [], (
        "search_store.disconnect() ran while background tasks were still alive: "
        f"{live_at_disconnect[0]}. Those tasks keep calling into a closed store — "
        "cancel and drain app.state._background_tasks first"
    )


# --------------------------------------------------------------------------- #
# C1-I-2 (second half) — telemetry must drain BEFORE background tasks are
# cancelled, not just before the store disconnects (the sibling test above only
# pins the latter half of the shutdown-order invariant)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_telemetry_drains_before_background_tasks_cancelled(
    tmp_path: Path, job_store: JobStore
) -> None:
    """Shutdown must drain telemetry BEFORE cancelling background tasks.

    ``test_background_tasks_cancelled_before_store_disconnect`` above only pins
    that background tasks are dead before ``search_store.disconnect()`` runs —
    it asserts nothing about telemetry, which is the constraint the original
    diff actually broke (the deleted "drain writer before cancelling background
    tasks" comment recorded it). Swapping the "drain telemetry" and "cancel
    background tasks" blocks in ``app.py``'s shutdown ``finally`` keeps that
    sibling test green, because both blocks still run before ``disconnect()``.

    This test deliberately does NOT rely on winning a callback-scheduling race
    between the enqueue and the shutdown signal: ``asyncio.Queue.put_nowait()``
    schedules the parked consumer's wake-up via ``call_soon`` at enqueue time,
    which can run before the shutdown-driven continuation gets a turn on the
    event loop regardless of code order in ``app.py`` — a purely timing-based
    oracle here could pass "by luck" under both the correct and the reverted
    ordering and prove nothing (cf. learnings.md on timing-dependent oracles
    being fragile). Instead it records ORDER directly: ``TelemetryWriter.drain_and_stop``
    is patched to append a marker when entered/exited, and the parked
    background task records a marker in its own ``except asyncio.CancelledError``
    handler. Because ``app.state._background_tasks`` includes the telemetry
    writer's own consumer task, cancelling background tasks before draining
    kills the consumer before ``drain_and_stop()``'s ``queue.join()`` can ever
    be satisfied for an entry still sitting in the queue — so under the
    reverted order the entry is provably lost (drain times out), and under the
    correct order it is provably written to disk before anything is cancelled.

    Confirmed by temporarily swapping the two blocks in ``app.py``'s shutdown
    ``finally`` and re-running this test: it fails (drain_end no longer
    precedes background_task_cancelled, and the marker never reaches disk).
    """
    from archon_search.server.app import create_app  # noqa: PLC0415
    from archon_search.store import SearchStore  # noqa: PLC0415
    from archon_search.sync import SearchCollectionSync, SyncResult  # noqa: PLC0415
    from archon_search.telemetry.entry import TelemetryEntry  # noqa: PLC0415
    from archon_search.telemetry.writer import TelemetryWriter  # noqa: PLC0415

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = ["docs"]
    cfg.telemetry.enabled = True
    cfg.telemetry.log_dir = str(tmp_path / "telemetry")

    order: list[str] = []
    release = asyncio.Event()
    sync_started = asyncio.Event()

    async def parked_sync(
        self: SearchCollectionSync, collections: list[str], progress_cb=None
    ) -> SyncResult:
        sync_started.set()
        try:
            await release.wait()  # keep the task alive across shutdown
        except asyncio.CancelledError:
            order.append("background_task_cancelled")
            raise
        return SyncResult()

    orig_drain_and_stop = TelemetryWriter.drain_and_stop

    async def recording_drain_and_stop(self: TelemetryWriter) -> None:
        order.append("drain_start")
        await orig_drain_and_stop(self)
        order.append("drain_end")

    with ExitStack() as stack:
        _enter_store_patches(stack)
        stack.enter_context(patch.object(SearchStore, "disconnect", new=AsyncMock()))
        stack.enter_context(patch.object(SearchCollectionSync, "sync", new=parked_sync))
        stack.enter_context(
            patch.object(TelemetryWriter, "drain_and_stop", new=recording_drain_and_stop)
        )

        app = create_app(cfg, job_store)
        startup_done = asyncio.Event()
        shutdown = asyncio.Event()

        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                startup_done.set()
                await shutdown.wait()

        lifespan_task = asyncio.create_task(run_lifespan())
        try:
            await asyncio.wait_for(startup_done.wait(), timeout=30.0)
            await asyncio.wait_for(sync_started.wait(), timeout=30.0)
            assert app.state.telemetry_writer is not None, (
                "telemetry was not wired onto app.state — test setup is wrong"
            )
            # No await between the enqueue and the shutdown signal: the entry
            # must still be sitting in the queue, undrained, at the instant
            # shutdown starts.
            app.state.telemetry_writer.enqueue(
                TelemetryEntry.from_error(
                    endpoint="search",
                    status="timeout",
                    error_kind="timeout",
                    latency_ms=1.0,
                    correlation_id="c1-i-2b-marker",
                )
            )
            shutdown.set()
            await asyncio.wait_for(lifespan_task, timeout=30.0)
        finally:
            release.set()

    assert order and order[0] == "drain_start", (
        f"telemetry drain never started first — recorded order: {order!r}"
    )
    assert "background_task_cancelled" in order, (
        "the parked background task was never cancelled — test setup is wrong"
    )
    assert "drain_end" in order, f"telemetry drain never completed — recorded order: {order!r}"
    assert order.index("drain_end") < order.index("background_task_cancelled"), (
        "telemetry drain did not finish before the background task was cancelled: "
        f"{order!r} — an entry enqueued right before shutdown can be lost if "
        "background tasks (including the writer's own consumer task) are "
        "cancelled before drain_and_stop() runs"
    )

    log_files = list((tmp_path / "telemetry").glob("*.jsonl"))
    assert log_files, "no telemetry log file was written during shutdown"
    contents = log_files[0].read_text()
    assert "c1-i-2b-marker" in contents, (
        "the telemetry entry enqueued right before shutdown never reached disk — "
        f"drain did not fully flush the queue before the process tore down. "
        f"Log contents: {contents!r}"
    )


# --------------------------------------------------------------------------- #
# C1-I-3 — the base_url branch never reaches the service-manager check
# --------------------------------------------------------------------------- #
def test_connect_fail_msg_with_base_url_does_not_consult_service_manager() -> None:
    """A failed ``/ready`` probe with ``base_url`` must return NOT_RUNNING_MSG.

    When ``--api-url`` names a specific server and the connection is refused,
    ``_server_connect_fail_msg(base_url)`` must return ``_SERVER_NOT_RUNNING_MSG``
    immediately — never consulting the local service manager. Consulting the
    manager would report the LOCAL instance's state when the operator asked about
    a different server (S530). The documented contract: "there is no in-process
    fallback" and connection refused always prints the not-running message.
    """
    from archon_search.cli import _helpers  # noqa: PLC0415

    service = MagicMock()
    service.status.return_value.running = True

    with (
        patch.object(_helpers.httpx, "get", side_effect=httpx.ConnectError("refused")),
        patch.object(_helpers, "_get_service", return_value=service),
    ):
        msg = _helpers._server_connect_fail_msg("http://127.0.0.1:8765")

    assert msg == _helpers._SERVER_NOT_RUNNING_MSG, (
        "connection refused to --api-url target must yield NOT_RUNNING_MSG — "
        "local service manager state must not leak into the message (S530). "
        f"Got {msg!r}"
    )
    service.status.assert_not_called()


# --------------------------------------------------------------------------- #
# C1-I-4 / C2-I-1 — a waiter timeout must not spawn a duplicate loader
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_waiter_timeout_does_not_spawn_duplicate_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out waiter must not cause a second ``make_embedder`` call.

    An earlier fix had the waiter delete its own (identity-guarded) stale
    registration from ``_loading`` on timeout. That closed only the narrow case
    of one waiter destroying a *newer* loader's registration — it did not stop
    the real defect: deleting the entry AT ALL empties ``_loading`` while the
    original loader is still running, so the very next caller takes the "we are
    the loader" branch and calls ``make_embedder`` a second time for the same
    model — two live ONNX sessions, with the first's copy uncached and
    therefore un-evictable.

    A waiter has no authority over the loader's lifecycle: only the loader
    itself may clear its registration (on every exit path, including
    cancellation — see ``test_cancelled_loader_leaves_cache_loadable_for_next_caller``).
    So the correct waiter-timeout behavior is to leave ``_loading`` untouched.

    Oracle: while the original loader L is still stalled inside
    ``make_embedder``, a waiter (W1) times out, and a fresh caller (W2) arrives
    — ``make_embedder`` must still have been called exactly once.
    """
    from archon_search import embedder_cache as ec  # noqa: PLC0415

    # Dial every module-level timeout constant down; a realistic production value
    # (120 s) would make this test unusable.
    for name in dir(ec):
        if "TIMEOUT" in name.upper() and isinstance(getattr(ec, name), (int, float)):
            monkeypatch.setattr(ec, name, 0.1)

    release = threading.Event()
    loader_entered = threading.Event()
    call_count = 0
    call_lock = threading.Lock()

    def counting_make_embedder(model_name: str, providers=None):
        nonlocal call_count
        with call_lock:
            call_count += 1
        loader_entered.set()
        release.wait()
        return MagicMock()

    cache = ec.EmbedderCache(max_size=2)
    second_waiter: asyncio.Task | None = None

    with patch.object(ec, "make_embedder", new=counting_make_embedder):
        loader_task = asyncio.create_task(cache.get_or_load("m"))
        try:
            for _ in range(500):
                if loader_entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert loader_entered.is_set(), "loader never started; test setup is wrong"
            event = cache._loading.get("m")
            assert event is not None, "loader did not register itself in _loading"

            with pytest.raises(ec.EmbedderNotReadyError):
                await asyncio.wait_for(cache.get_or_load("m"), timeout=5.0)

            assert call_count == 1, (
                f"make_embedder was called {call_count} times after only the first "
                "waiter's timeout — the original loader has not even finished yet"
            )

            second_waiter = asyncio.create_task(cache.get_or_load("m"))
            await asyncio.sleep(0.05)  # let W2 either become a waiter or a loader

            # Deterministic oracle, no wall clock needed: if W2 wrongly became a
            # SECOND loader it would have registered a brand-new Event under
            # cache._loading["m"], overwriting the original. Under reverted code
            # this alone proves the bug regardless of whether the 50 ms window
            # above was long enough for the background thread to also bump
            # call_count.
            assert cache._loading.get("m") is event, (
                "cache._loading['m'] no longer identity-matches the original "
                "loader's Event — W2 became a SECOND loader for the same model "
                "(registered a new Event) instead of a waiter on the original load"
            )
            assert call_count == 1, (
                f"make_embedder was called {call_count} times — a caller arriving "
                "after a waiter's timeout became a SECOND loader for the same "
                "model while the original loader was still in flight (a "
                "duplicate multi-hundred-MB ONNX load)"
            )
        finally:
            release.set()
            loader_task.cancel()
            await asyncio.gather(loader_task, return_exceptions=True)
            if second_waiter is not None:
                second_waiter.cancel()
                await asyncio.gather(second_waiter, return_exceptions=True)


# --------------------------------------------------------------------------- #
# C1-F-2(b) — a cancelled loader must leave the cache loadable by the next caller
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cancelled_loader_leaves_cache_loadable_for_next_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled loader must not permanently poison ``_loading`` for its model.

    ``get_or_load``'s loader-side cleanup previously caught only ``except
    Exception``, which does not catch ``asyncio.CancelledError`` (a
    ``BaseException``). A loader cancelled mid-load left ``_loading[model]``
    registered with its event never set, so the model stayed unloadable until
    some future caller burned the full ``_LOAD_WAIT_TIMEOUT_SECONDS`` to clean it
    up.

    Oracle: cancel the loader, then immediately issue a fresh ``get_or_load()``
    for the same model with a working ``make_embedder`` — it must succeed right
    away, not time out.
    """
    from archon_search import embedder_cache as ec  # noqa: PLC0415

    for name in dir(ec):
        if "TIMEOUT" in name.upper() and isinstance(getattr(ec, name), (int, float)):
            monkeypatch.setattr(ec, name, 0.2)

    release = threading.Event()
    loader_entered = threading.Event()

    def stalled_make_embedder(model_name: str, providers=None):
        loader_entered.set()
        release.wait()
        return MagicMock()

    cache = ec.EmbedderCache(max_size=2)

    with patch.object(ec, "make_embedder", new=stalled_make_embedder):
        loader_task = asyncio.create_task(cache.get_or_load("m"))
        try:
            for _ in range(500):
                if loader_entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert loader_entered.is_set(), "loader never started; test setup is wrong"

            loader_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loader_task
        finally:
            release.set()  # let the stalled thread return so it does not leak

    assert "m" not in cache._loading, (
        "a cancelled loader left 'm' registered in EmbedderCache._loading with its "
        "event never set — the model is now unloadable until some future caller "
        "burns the full timeout to clean it up"
    )

    fresh_embedder = MagicMock()
    with patch.object(ec, "make_embedder", return_value=fresh_embedder) as fresh_loader:
        # 5 s, not 1 s: this budgets the SUCCESS path (one default-executor hop
        # for a MagicMock-returning make_embedder), which is free on green and
        # should never be tight enough to flake under contention (cf.
        # learnings.md's wall-clock-budget entry).
        result = await asyncio.wait_for(cache.get_or_load("m"), timeout=5.0)

    assert result is fresh_embedder
    fresh_loader.assert_called_once()


# --------------------------------------------------------------------------- #
# C3-I-1 — a loader cancelled while parked acquiring self._lock on the SUCCESS
# path (after make_embedder already returned) must not permanently poison
# _loading either
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_loader_cancelled_while_acquiring_lock_on_success_path_leaves_cache_loadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loader cancelled while awaiting ``self._lock`` on the success path must
    not permanently poison ``_loading`` for its model.

    The cleanup ``try/except BaseException`` previously wrapped only
    ``asyncio.to_thread(make_embedder, ...)`` — it ended before the success
    path's ``async with self._lock:``. A cancellation delivered while parked
    acquiring that lock (the realistic shape of a firing warm-up timeout:
    ``preload()`` gathers N concurrent ``get_or_load`` calls that all contend on
    the same lock at their success path) escaped untouched: ``_loading[model]``
    stayed registered with its event never set, and the freshly-built embedder
    was discarded uncached. Every later caller for that model then blocked the
    full ``_LOAD_WAIT_TIMEOUT_SECONDS`` and got ``EmbedderNotReadyError`` — for
    the process lifetime (proven by execution in
    ``scratchpad/c3_probe_lockcancel.py``).

    Oracle: hold ``cache._lock`` from the test so the loader is guaranteed to
    queue as a waiter on it (``cache._lock._waiters`` non-empty — a
    deterministic check, not a timing guess), cancel the loader while it is
    parked there, release the lock, then assert the next caller loads
    successfully rather than timing out.
    """
    from archon_search import embedder_cache as ec  # noqa: PLC0415

    monkeypatch.setattr(ec, "_LOAD_WAIT_TIMEOUT_SECONDS", 0.2)

    release_make_embedder = threading.Event()

    def controlled_make_embedder(model_name: str, providers=None):
        release_make_embedder.wait()
        return MagicMock()

    cache = ec.EmbedderCache(max_size=2)
    lock_acquired_by_test = False

    with patch.object(ec, "make_embedder", new=controlled_make_embedder):
        loader_task = asyncio.create_task(cache.get_or_load("m"))
        try:
            for _ in range(500):
                if "m" in cache._loading:
                    break
                await asyncio.sleep(0.01)
            assert "m" in cache._loading, "loader never registered; test setup is wrong"

            # Contend the success-path lock BEFORE make_embedder returns, so
            # the loader is guaranteed to queue on it rather than race past.
            await cache._lock.acquire()
            lock_acquired_by_test = True
            release_make_embedder.set()

            for _ in range(500):
                if cache._lock._waiters and len(cache._lock._waiters) > 0:
                    break
                await asyncio.sleep(0.01)
            assert cache._lock._waiters and len(cache._lock._waiters) > 0, (
                "loader never parked acquiring self._lock on the success path; "
                "test setup is wrong"
            )

            loader_task.cancel()
            await asyncio.sleep(0)  # let the cancellation reach the parked acquire()
            cache._lock.release()
            lock_acquired_by_test = False
            with pytest.raises(asyncio.CancelledError):
                await loader_task
        finally:
            release_make_embedder.set()  # avoid leaking the executor thread on failure
            if lock_acquired_by_test:
                cache._lock.release()

    assert "m" not in cache._loading, (
        "a loader cancelled while acquiring self._lock on the success path left "
        "'m' registered in EmbedderCache._loading with its event never set — the "
        "model is now unloadable until some future caller burns the full timeout "
        "to clean it up"
    )

    fresh_embedder = MagicMock()
    with patch.object(ec, "make_embedder", return_value=fresh_embedder) as fresh_loader:
        result = await asyncio.wait_for(cache.get_or_load("m"), timeout=5.0)

    assert result is fresh_embedder
    fresh_loader.assert_called_once()


# --------------------------------------------------------------------------- #
# C1-F-2(a) — a timed-out waiter must not wake a concurrent waiter into a
# duplicate make_embedder call
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_waiter_timeout_does_not_wake_concurrent_waiter_into_duplicate_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One waiter's timeout must not wake a second, still-parked waiter early.

    An earlier version of this test created both waiters back-to-back and
    asserted only the final ``make_embedder`` call count. Because both
    waiters' ~0.2s deadlines land within microseconds of each other, the event
    loop's late wake-up under contention (>=1ms is common) meant BOTH timers
    were already expired by the time either one's handler ran — so the racing
    condition the test claims to create (waiter1 timing out while waiter2 is
    still genuinely parked) never actually happened, and the assertion passed
    even against the pre-fix ``ev.set()`` code this test is meant to catch.
    (C2-I-29 — a timing race made the test vacuous.)

    This version checks the mechanism directly instead of an indirect,
    timing-dependent symptom: after waiter1's own timeout fires, the loader's
    shared event must still be unset — true regardless of exactly when
    waiter2's own timeout later fires, so there is no race to lose.

    Oracle: two waiters share one wedged loader. After the first waiter times
    out, the loader's shared event must still be unset, and ``make_embedder``
    must have been called exactly once even after both waiters finish.
    """
    from archon_search import embedder_cache as ec  # noqa: PLC0415

    for name in dir(ec):
        if "TIMEOUT" in name.upper() and isinstance(getattr(ec, name), (int, float)):
            monkeypatch.setattr(ec, name, 0.2)

    release = threading.Event()
    loader_entered = threading.Event()
    call_count = 0
    call_lock = threading.Lock()

    def counting_make_embedder(model_name: str, providers=None):
        nonlocal call_count
        with call_lock:
            call_count += 1
        loader_entered.set()
        release.wait()
        return MagicMock()

    cache = ec.EmbedderCache(max_size=2)

    with patch.object(ec, "make_embedder", new=counting_make_embedder):
        loader_task = asyncio.create_task(cache.get_or_load("m"))
        try:
            for _ in range(500):
                if loader_entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert loader_entered.is_set(), "loader never started; test setup is wrong"

            event = cache._loading.get("m")
            assert event is not None, "loader did not register itself in _loading"

            waiter1 = asyncio.create_task(cache.get_or_load("m"))
            waiter2 = asyncio.create_task(cache.get_or_load("m"))

            with pytest.raises(ec.EmbedderNotReadyError):
                await asyncio.wait_for(waiter1, timeout=5.0)

            assert not event.is_set(), (
                "waiter1's timeout handling set the shared event — this would "
                "wake waiter2 (or any other concurrently-parked waiter) early, "
                "before its own timeout, letting it race in as a duplicate "
                "loader"
            )

            with pytest.raises(ec.EmbedderNotReadyError):
                await asyncio.wait_for(waiter2, timeout=5.0)
        finally:
            release.set()
            loader_task.cancel()
            await asyncio.gather(loader_task, return_exceptions=True)

    assert call_count == 1, (
        f"make_embedder was called {call_count} times — a timed-out waiter woke the "
        "other concurrent waiter early and it raced in as a duplicate loader "
        "before its own timeout"
    )


# --------------------------------------------------------------------------- #
# C1-I-5 — the timeout raises a bare RuntimeError
# --------------------------------------------------------------------------- #
def test_embedder_not_ready_error_type_exists() -> None:
    """A wedged load must raise a dedicated, mappable exception type.

    ``get_or_load`` raises a plain ``RuntimeError`` on timeout. Route handlers
    cannot distinguish that from a genuine bug, so a temporarily-unready model
    surfaces as HTTP 500 instead of 503 (retry later). The cache needs its own
    exception type for routes to map.
    """
    from archon_search import embedder_cache as ec  # noqa: PLC0415

    exc_type = getattr(ec, "EmbedderNotReadyError", None)
    assert exc_type is not None, (
        "archon_search.embedder_cache defines no EmbedderNotReadyError — the "
        "waiter timeout raises a bare RuntimeError, which routes can only map to "
        "a 500 even though the correct answer is 503 'not ready yet, retry'"
    )
    # RuntimeError, not just Exception: EmbedderNotReadyError deliberately
    # subclasses RuntimeError so pre-existing `except RuntimeError` callers keep
    # working — a rebase onto plain Exception would silently break them.
    assert isinstance(exc_type, type) and issubclass(exc_type, RuntimeError)


# --------------------------------------------------------------------------- #
# C1-F-8 — EmbedderNotReadyError must surface as HTTP 503 from POST /search
# --------------------------------------------------------------------------- #
def test_embedder_not_ready_error_returns_503_on_wire(tmp_path: Path) -> None:
    """A wedged model load must surface as HTTP 503 on ``POST /search``, not 500.

    ``routes_search.search`` calls ``embedder_cache.get_or_load(active_model)``
    BEFORE entering the ``asyncio.wait_for(_SEARCH_TIMEOUT_SECONDS)`` budget, so
    the ``except EmbedderNotReadyError`` handler IS reachable — this proves the
    mapping end to end instead of only checking that the exception type exists.
    """
    import os  # noqa: PLC0415
    from unittest.mock import AsyncMock, MagicMock  # noqa: PLC0415

    from fastapi.testclient import TestClient  # noqa: PLC0415

    from archon_search.collection_meta import CollectionMeta  # noqa: PLC0415
    from archon_search.embedder_cache import EMBEDDER_NOT_READY_DETAIL, EmbedderNotReadyError  # noqa: PLC0415
    from archon_search.server.app import create_app  # noqa: PLC0415

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    pipeline.warmup_models = AsyncMock()
    app.state.pipeline = pipeline

    cache = MagicMock()
    # A realistic wire-format internal message (class name + timeout constant +
    # model name) — this must NOT reach the response body verbatim (C6-3).
    cache.get_or_load = AsyncMock(
        side_effect=EmbedderNotReadyError(
            "EmbedderCache: timed out after 120.0s waiting for model 'BAAI/bge-small-en-v1.5' to load"
        )
    )
    app.state.embedder_cache = cache

    response = client.post("/search", json={"collection": "col", "query": "test"})

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "embedder_not_ready", (
        f"expected a machine-readable code='embedder_not_ready' (matching the "
        f"metadata_store_error convention used elsewhere in this file), got {body!r}"
    )
    assert body["detail"] == EMBEDDER_NOT_READY_DETAIL, (
        "the response detail must be the sanitized constant, not str(exc) — "
        f"str(exc) leaks the internal class name, timeout constant, and model "
        f"name. Got detail={body['detail']!r}"
    )


# --------------------------------------------------------------------------- #
# C6-1 — every other HTTP route that resolves an embedder must map
# EmbedderNotReadyError to the same 503, via the generic app-level handler
# --------------------------------------------------------------------------- #
def test_embedder_not_ready_error_returns_503_from_explain(tmp_path: Path) -> None:
    """``POST /explain`` must also surface a wedged model load as 503.

    Before the app-level ``EmbedderNotReadyError`` exception handler, only
    ``routes_search.search`` mapped this exception — every other route that
    calls ``embedder_cache.get_or_load`` (``/explain`` among them) let it
    escape uncaught, surfacing as HTTP 500. This proves the generic handler
    registered in ``create_app`` covers a route OTHER than ``/search``.
    """
    import os  # noqa: PLC0415
    from unittest.mock import AsyncMock, MagicMock  # noqa: PLC0415

    from fastapi.testclient import TestClient  # noqa: PLC0415

    from archon_search.collection_meta import CollectionMeta  # noqa: PLC0415
    from archon_search.embedder_cache import EmbedderNotReadyError  # noqa: PLC0415
    from archon_search.server.app import create_app  # noqa: PLC0415

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    app.state.pipeline = pipeline

    cache = MagicMock()
    cache.get_or_load = AsyncMock(side_effect=EmbedderNotReadyError("model still loading"))
    app.state.embedder_cache = cache

    response = client.post("/explain", json={"collection": "col", "query": "test"})

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "embedder_not_ready", (
        f"expected /explain to map EmbedderNotReadyError to code='embedder_not_ready' "
        f"via the generic app-level handler, got {body!r}"
    )


# --------------------------------------------------------------------------- #
# C1-I-6a — eager warm-up has no terminal timeout
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_eager_warmup_has_terminal_timeout(
    tmp_path: Path, job_store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged eager warm-up must terminate and mark itself failed.

    ``_run_eager_warmup`` awaits ``embedder_cache.preload`` with no
    ``asyncio.wait_for``. If model loading hangs (wedged ONNX init, stalled
    download), ``app.state.warmup_result`` stays ``"pending"`` forever and
    ``GET /ready`` answers 503 for the entire lifetime of the process with no
    diagnostic and no recovery.

    The bound must live in a module-level constant (house style, cf.
    ``routes_search._SEARCH_TIMEOUT_SECONDS``) so this test can dial it down —
    the loop below rebinds every numeric ``*TIMEOUT*`` constant in ``app.py``.
    """
    from archon_search.embedder_cache import EmbedderCache  # noqa: PLC0415
    from archon_search.server import app as app_module  # noqa: PLC0415
    from archon_search.server.app import create_app  # noqa: PLC0415
    from archon_search.store import SearchStore  # noqa: PLC0415

    for name in dir(app_module):
        if "TIMEOUT" in name.upper() and isinstance(getattr(app_module, name), (int, float)):
            monkeypatch.setattr(app_module, name, 0.1)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.eager_load_embedders = True
    cfg.collections = []

    preload_entered = asyncio.Event()

    async def parked_preload(self: EmbedderCache, model_names: list[str]) -> None:
        preload_entered.set()
        await asyncio.Event().wait()  # never resolves

    with ExitStack() as stack:
        _enter_store_patches(stack)
        stack.enter_context(patch.object(SearchStore, "disconnect", new=AsyncMock()))
        stack.enter_context(patch.object(EmbedderCache, "preload", new=parked_preload))

        app = create_app(cfg, job_store)
        startup_done = asyncio.Event()
        shutdown = asyncio.Event()

        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                startup_done.set()
                await shutdown.wait()

        lifespan_task = asyncio.create_task(run_lifespan())
        try:
            await asyncio.wait_for(startup_done.wait(), timeout=30.0)
            await asyncio.wait_for(preload_entered.wait(), timeout=30.0)
            # 5 s elapses only on the FAILURE path (an unbounded await); a bounded
            # warm-up gives up after the dialled-down constant (~0.1 s).
            for _ in range(500):
                if app.state.warmup_result != "pending":
                    break
                await asyncio.sleep(0.01)
            result = app.state.warmup_result
        finally:
            shutdown.set()
            await lifespan_task

    assert result == "failed", (
        "eager warm-up hung with no terminal timeout: app.state.warmup_result is "
        f"{result!r}, so /ready answers 503 forever with no diagnostic. "
        "_run_eager_warmup must bound preload()/warmup_models() with "
        "asyncio.wait_for(..., timeout=<module constant>) and record 'failed'"
    )


# --------------------------------------------------------------------------- #
# C1-I-6b — the CancelledError branch never records the outcome
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_eager_warmup_cancelled_sets_failed(tmp_path: Path, job_store: JobStore) -> None:
    """A cancelled eager warm-up must leave ``warmup_result`` terminal.

    ``_run_eager_warmup``'s ``except asyncio.CancelledError`` branch logs and
    re-raises without touching ``app.state.warmup_result``, which therefore stays
    ``"pending"`` — ``/ready`` and any operator reading it are told the warm-up is
    still progressing when it will never run again.
    """
    from archon_search.embedder_cache import EmbedderCache  # noqa: PLC0415
    from archon_search.server.app import create_app  # noqa: PLC0415
    from archon_search.store import SearchStore  # noqa: PLC0415

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.eager_load_embedders = True
    cfg.collections = []

    async def cancelling_preload(self: EmbedderCache, model_names: list[str]) -> None:
        raise asyncio.CancelledError()

    with ExitStack() as stack:
        _enter_store_patches(stack)
        stack.enter_context(patch.object(SearchStore, "disconnect", new=AsyncMock()))
        stack.enter_context(patch.object(EmbedderCache, "preload", new=cancelling_preload))

        app = create_app(cfg, job_store)
        startup_done = asyncio.Event()
        shutdown = asyncio.Event()

        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                startup_done.set()
                await shutdown.wait()

        lifespan_task = asyncio.create_task(run_lifespan())
        try:
            await asyncio.wait_for(startup_done.wait(), timeout=30.0)
            for _ in range(500):
                task = app.state._warmup_task
                if task is not None and task.done():
                    break
                await asyncio.sleep(0.01)
            warmup_task = app.state._warmup_task
            assert warmup_task is not None and warmup_task.done(), (
                "warm-up task never finished; test setup is wrong"
            )
            result = app.state.warmup_result
        finally:
            shutdown.set()
            await lifespan_task

    assert result == "failed", (
        "eager warm-up was cancelled but app.state.warmup_result is still "
        f"{result!r} — the CancelledError branch re-raises without recording a "
        "terminal outcome, so /ready reports a warm-up that will never complete"
    )


# --------------------------------------------------------------------------- #
# C1-I-7 — /ready ignores the startup sync task
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ready_gates_on_startup_sync_task() -> None:
    """``GET /ready`` must not report ready while the startup sync is running.

    ``ready_flag = storage_ok and not _warmup_pending(request)`` ignores
    ``app.state._startup_sync_task`` entirely. During the startup sync the index
    is still being (re)built — a load balancer routing traffic there gets stale
    or missing results, but the endpoint answers 200.
    """
    import json  # noqa: PLC0415

    from archon_search.server.routes_ready import ready  # noqa: PLC0415

    cfg = SearchConfig()
    cfg.eager_load_embedders = False

    unfinished_task = MagicMock()
    unfinished_task.done.return_value = False

    class FakeStore:
        ping = AsyncMock(return_value=True)

    class FakeState:
        model_validation = None
        warmup_result = None
        config = cfg
        search_store = FakeStore()
        _startup_sync_task = unfinished_task

    class FakeApp:
        state = FakeState()

    class FakeRequest:
        app = FakeApp()

    response = await ready(FakeRequest())
    body = json.loads(response.body)

    assert response.status_code == 503, (
        "/ready answered 200 while app.state._startup_sync_task was still running — "
        "the collections are mid-sync, so search results are incomplete"
    )
    assert body["ready"] is False
    assert body["checks"]["sync"] == "pending", (
        "a still-running startup sync must surface as checks.sync='pending', not "
        f"just a bare 503 — got {body['checks'].get('sync')!r}. Without this, an "
        "operator sees all-green checks with ready=false and no diagnosis (C1-F-6)"
    )


# --------------------------------------------------------------------------- #
# C1-F-8 — _startup_sync_pending: a completed task and a missing attribute
# must both yield /ready 200 (only the still-running case was covered above)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ready_returns_200_when_startup_sync_task_completed() -> None:
    """``GET /ready`` must report ready once the startup sync task is done."""
    import json  # noqa: PLC0415

    from archon_search.server.routes_ready import ready  # noqa: PLC0415

    cfg = SearchConfig()
    cfg.eager_load_embedders = False

    finished_task = MagicMock()
    finished_task.done.return_value = True

    class FakeStore:
        ping = AsyncMock(return_value=True)

    class FakeState:
        model_validation = None
        warmup_result = None
        config = cfg
        search_store = FakeStore()
        _startup_sync_task = finished_task

    class FakeApp:
        state = FakeState()

    class FakeRequest:
        app = FakeApp()

    response = await ready(FakeRequest())
    body = json.loads(response.body)

    assert response.status_code == 200, (
        f"/ready answered {response.status_code} while the startup sync task was "
        f"already done — body: {body!r}"
    )
    assert body["ready"] is True
    assert body["checks"]["sync"] == "ok", (
        f"a completed startup sync must report checks.sync='ok', got "
        f"{body['checks'].get('sync')!r}"
    )


# --------------------------------------------------------------------------- #
# C2-I-5 — a failed startup sync must report checks.sync='fail', not 'ok', and
# must not gate readiness
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ready_sync_fail_when_startup_sync_failed() -> None:
    """A crashed startup sync must report ``checks.sync == "fail"``, and ``ready``
    must stay True.

    ``_run_startup_sync`` swallows every failure (WARNING, no re-raise), so a
    crashed sync still leaves a ``done()`` task — indistinguishable from one
    that completed cleanly unless the failure is recorded separately. An
    operator following the incident runbook must not see a green sync check for
    a sync that never completed. ``ready`` must not gate on it: the failure is
    deliberately swallowed so a corrupted collection cannot wedge the pod's
    readiness forever.
    """
    import json  # noqa: PLC0415

    from archon_search.server.routes_ready import ready  # noqa: PLC0415

    cfg = SearchConfig()
    cfg.eager_load_embedders = False

    finished_task = MagicMock()
    finished_task.done.return_value = True

    class FakeStore:
        ping = AsyncMock(return_value=True)

    class FakeState:
        model_validation = None
        warmup_result = None
        config = cfg
        search_store = FakeStore()
        _startup_sync_task = finished_task
        _startup_sync_failed = True

    class FakeApp:
        state = FakeState()

    class FakeRequest:
        app = FakeApp()

    response = await ready(FakeRequest())
    body = json.loads(response.body)

    assert body["checks"]["sync"] == "fail", (
        "app.state._startup_sync_failed=True must report checks.sync='fail', "
        f"got {body['checks']['sync']!r}"
    )
    assert response.status_code == 200, (
        f"a failed sync must not gate readiness — got {response.status_code}, "
        f"body: {body!r}"
    )
    assert body["ready"] is True


@pytest.mark.asyncio
async def test_ready_returns_200_when_startup_sync_task_attribute_missing() -> None:
    """``GET /ready`` must report ready when ``_startup_sync_task`` was never set.

    App factories that never spawn a startup sync (e.g. no configured
    collections) never set ``app.state._startup_sync_task`` at all — the
    ``getattr`` guard in ``_startup_sync_pending`` must treat that the same as
    "not pending", not as an error.
    """
    import json  # noqa: PLC0415

    from archon_search.server.routes_ready import ready  # noqa: PLC0415

    cfg = SearchConfig()
    cfg.eager_load_embedders = False

    class FakeStore:
        ping = AsyncMock(return_value=True)

    class FakeState:
        model_validation = None
        warmup_result = None
        config = cfg
        search_store = FakeStore()
        # _startup_sync_task intentionally absent

    class FakeApp:
        state = FakeState()

    class FakeRequest:
        app = FakeApp()

    response = await ready(FakeRequest())
    body = json.loads(response.body)

    assert response.status_code == 200, (
        f"/ready answered {response.status_code} with no _startup_sync_task "
        f"attribute set at all — body: {body!r}"
    )
    assert body["ready"] is True
    assert body["checks"]["sync"] == "ok", (
        f"no _startup_sync_task attribute must report checks.sync='ok', got "
        f"{body['checks'].get('sync')!r}"
    )


# --------------------------------------------------------------------------- #
# C3-I-2 — an all-collections-failed startup sync must not report checks.sync='ok'
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_startup_sync_with_per_collection_errors_sets_startup_sync_failed(
    tmp_path: Path, job_store: JobStore
) -> None:
    """A startup sync that completes with per-collection errors must not be
    reported as ``checks.sync == "ok"``.

    ``SearchCollectionSync.sync()`` does not raise on a per-collection failure
    (missing path, ingest error, chunk-size reindex failure) — it accumulates
    messages in ``SyncResult.errors`` and returns normally. ``_run_startup_sync``
    previously discarded the return value entirely, so a sync in which every
    collection failed still logged "startup sync complete" and left
    ``app.state._startup_sync_failed`` at its default ``False`` — ``/ready``
    then answered 200 with ``checks.sync == "ok"`` for a sync that did not
    actually complete.

    Oracle: stub ``SearchCollectionSync.sync`` to return a ``SyncResult`` with a
    non-empty ``errors`` list (no exception) and assert the flag gets set and
    ``/ready`` reports ``checks.sync == "fail"`` — while ``ready`` stays True (a
    failed sync must never wedge the pod).
    """
    import json  # noqa: PLC0415

    from archon_search.server.app import create_app  # noqa: PLC0415
    from archon_search.server.routes_ready import ready  # noqa: PLC0415
    from archon_search.store import SearchStore  # noqa: PLC0415
    from archon_search.sync import SearchCollectionSync, SyncResult  # noqa: PLC0415

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = ["docs"]  # non-empty => the lifespan takes the startup-sync branch

    async def failing_sync(
        self: SearchCollectionSync, collections: list[str], progress_cb=None
    ) -> SyncResult:
        return SyncResult(errors=["collection 'docs': path does not exist"])

    with ExitStack() as stack:
        _enter_store_patches(stack)
        stack.enter_context(patch.object(SearchStore, "disconnect", new=AsyncMock()))
        stack.enter_context(patch.object(SearchStore, "ping", new=AsyncMock(return_value=True)))
        stack.enter_context(patch.object(SearchCollectionSync, "sync", new=failing_sync))

        app = create_app(cfg, job_store)
        startup_done = asyncio.Event()
        shutdown = asyncio.Event()

        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                startup_done.set()
                await shutdown.wait()

        lifespan_task = asyncio.create_task(run_lifespan())
        try:
            await asyncio.wait_for(startup_done.wait(), timeout=30.0)
            sync_task = app.state._startup_sync_task
            assert sync_task is not None, "startup sync task never spawned"
            await asyncio.wait_for(sync_task, timeout=30.0)

            assert app.state._startup_sync_failed is True, (
                "a startup sync that returned per-collection errors left "
                "_startup_sync_failed False — an all-collections-failed sync "
                "would report checks.sync='ok'"
            )

            # ready() calls store.ping(), which needs the patched SearchStore
            # above still active — call it before the ExitStack unwinds.
            built_app = app

            class FakeRequest:
                app = built_app

            response = await ready(FakeRequest())
            body = json.loads(response.body)
            assert body["checks"]["sync"] == "fail", (
                "a startup sync completed with per-collection errors must "
                f"report checks.sync='fail', got {body['checks'].get('sync')!r}"
            )
            assert response.status_code == 200
            assert body["ready"] is True, "a failed sync must never gate readiness"
        finally:
            shutdown.set()
            await lifespan_task


# --------------------------------------------------------------------------- #
# C6-4 — the startup sync must have a terminal timeout, mirroring the eager
# warm-up bound (_EAGER_WARMUP_TIMEOUT_SECONDS); otherwise /ready gates on it forever
# and Docker's HEALTHCHECK boot-loops an unhealthy container.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_startup_sync_has_terminal_timeout(
    tmp_path: Path, job_store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged startup sync must terminate and mark itself failed, not hang.

    ``_run_startup_sync`` previously awaited ``collection_sync.sync()`` with no
    bound. Since ``GET /ready`` gates on ``_startup_sync_pending``
    (``routes_ready.py``), a sync that never returns would leave ``/ready`` at
    503 for the process lifetime — combined with Docker's HEALTHCHECK
    (``--start-period=600s --interval=15s --retries=3``), an orchestrator that
    restarts unhealthy containers would boot-loop, since the sync restarts
    from the top on every restart and never converges on a large corpus.

    The bound must live in a module-level constant (house style, cf.
    ``_EAGER_WARMUP_TIMEOUT_SECONDS``) so this test can dial it down — the loop below
    rebinds every numeric ``*TIMEOUT*`` constant in ``app.py``.
    """
    from archon_search.server import app as app_module  # noqa: PLC0415
    from archon_search.server.app import create_app  # noqa: PLC0415
    from archon_search.store import SearchStore  # noqa: PLC0415
    from archon_search.sync import SearchCollectionSync  # noqa: PLC0415

    for name in dir(app_module):
        if "TIMEOUT" in name.upper() and isinstance(getattr(app_module, name), (int, float)):
            monkeypatch.setattr(app_module, name, 0.1)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = ["docs"]  # non-empty => the lifespan takes the startup-sync branch

    sync_entered = asyncio.Event()

    async def parked_sync(
        self: SearchCollectionSync, collections: list[str], progress_cb=None
    ):
        sync_entered.set()
        await asyncio.Event().wait()  # never resolves

    with ExitStack() as stack:
        _enter_store_patches(stack)
        stack.enter_context(patch.object(SearchStore, "disconnect", new=AsyncMock()))
        stack.enter_context(patch.object(SearchStore, "ping", new=AsyncMock(return_value=True)))
        stack.enter_context(patch.object(SearchCollectionSync, "sync", new=parked_sync))

        app = create_app(cfg, job_store)
        startup_done = asyncio.Event()
        shutdown = asyncio.Event()

        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                startup_done.set()
                await shutdown.wait()

        lifespan_task = asyncio.create_task(run_lifespan())
        try:
            await asyncio.wait_for(startup_done.wait(), timeout=30.0)
            await asyncio.wait_for(sync_entered.wait(), timeout=30.0)
            # 5 s elapses only on the FAILURE path (an unbounded await); a bounded
            # sync gives up after the dialled-down constant (~0.1 s).
            for _ in range(500):
                if app.state._startup_sync_failed:
                    break
                await asyncio.sleep(0.01)
            startup_sync_failed = app.state._startup_sync_failed
            sync_result = app.state.sync_result

            # /ready must never gate on this failure — 200 with checks.sync='fail'.
            from archon_search.server.routes_ready import ready  # noqa: PLC0415

            sync_task = app.state._startup_sync_task
            assert sync_task is not None and sync_task.done(), (
                "startup sync task never finished; test setup is wrong"
            )

            class _ReadyRequest:
                def __init__(self, real_app) -> None:
                    self.app = real_app

            response = await ready(_ReadyRequest(app))
            body = response.body
        finally:
            shutdown.set()
            await lifespan_task

    import json  # noqa: PLC0415

    assert startup_sync_failed is True, (
        "a wedged startup sync did not set app.state._startup_sync_failed=True "
        "after the (dialled-down) timeout elapsed — /ready would gate on it forever"
    )
    assert sync_result == "failed", (
        f"app.state.sync_result must be 'failed' after a timed-out startup sync, "
        f"got {sync_result!r}"
    )
    parsed = json.loads(body)
    assert parsed["checks"]["sync"] == "fail", (
        f"a timed-out startup sync must report checks.sync='fail', got {parsed['checks'].get('sync')!r}"
    )
    assert parsed["ready"] is True, "a timed-out startup sync must never gate readiness"


# --------------------------------------------------------------------------- #
# C6-5 — GET /status must surface startup-sync progress, mirroring warmup_result
# --------------------------------------------------------------------------- #
def test_status_surfaces_sync_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``GET /status`` must surface ``app.state.sync_result``.

    Before this fix, only ``warmup_result`` was surfaced on ``GET /status`` —
    ``sync_result`` (the phase most likely to hold ``/ready`` at 503 the
    longest, per C6-4) had no operator-visible counterpart at all.
    """
    from tests.integration.conftest import make_real_app  # noqa: PLC0415

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        client.app.state.sync_result = "pending"
        resp = client.get("/status", headers={"Authorization": f"Bearer {api_key}"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["sync_result"] == "pending", (
        "GET /status does not surface app.state.sync_result == 'pending'. "
        f"Got: {resp.json().get('sync_result')!r}"
    )


@pytest.mark.parametrize("value", ["done", "failed"])
def test_status_surfaces_sync_result_terminal_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """``GET /status`` must surface both terminal sync outcomes, not just 'pending'."""
    from tests.integration.conftest import make_real_app  # noqa: PLC0415

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        client.app.state.sync_result = value
        resp = client.get("/status", headers={"Authorization": f"Bearer {api_key}"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["sync_result"] == value, (
        f"GET /status does not surface app.state.sync_result == {value!r}. "
        f"Got: {resp.json().get('sync_result')!r}"
    )


# --------------------------------------------------------------------------- #
# C1-I-8 — CLI connect-failure paths must be fully patchable (no live probe)
# --------------------------------------------------------------------------- #
def test_cli_sync_connect_failure_uses_patched_probe_only() -> None:
    """The CLI connect-failure path must go through the patched ``/ready`` probe.

    ``archon-search sync`` calls ``_server_connect_fail_msg(base_url)`` on a
    ``ConnectError``, which issues a real ``httpx.get`` unless the test patches
    ``archon_search.cli._helpers.httpx.get``. Every such test must patch it — an
    unpatched one hits the developer's machine and its result flips with whether
    a local server happens to be up.

    Here the probe *is* patched (ConnectError) and the managed service reports
    running=True. The default URL is used (no ``--api-url``), so after the probe
    fails the managed-service check is consulted (c7829cbd): the output must be
    the "starting up" message, not "not running". For a custom ``--api-url``, the
    service manager is never consulted (S530).
    """
    from archon_search.cli import _helpers  # noqa: PLC0415
    from archon_search.cli.sync import sync as sync_cmd  # noqa: PLC0415

    service = MagicMock()
    service.status.return_value.running = True
    runner = CliRunner()

    with (
        patch("archon_search.cli.sync.httpx.post", side_effect=httpx.ConnectError("refused")),
        patch.object(
            _helpers.httpx, "get", side_effect=httpx.ConnectError("refused")
        ) as probe,
        patch.object(_helpers, "_get_service", return_value=service),
    ):
        result = runner.invoke(sync_cmd, ["--api-key", "test-key"])
        probe_calls = probe.call_args_list

    output = result.output + (result.stderr if hasattr(result, "stderr") else "")
    assert result.exit_code == 1
    assert len(probe_calls) == 1, (
        "the /ready probe was not routed through the patched "
        "archon_search.cli._helpers.httpx.get — a CLI connect-failure test that "
        f"does not patch it makes a live network call. Calls: {probe_calls!r}"
    )
    # Default URL + probe fail + service running → "starting up" (c7829cbd).
    # Custom --api-url + probe fail → "not running" regardless (S530).
    assert "starting up" in output.lower(), (
        "default URL: probe failed but managed service is alive — must report 'starting up'. "
        f"Got: {output!r}"
    )


# --------------------------------------------------------------------------- #
# C1-I-9 — dead _SERVER_NOT_RUNNING_MSG imports
# --------------------------------------------------------------------------- #
def test_no_cli_module_imports_unused_server_not_running_msg() -> None:
    """No CLI module may import ``_SERVER_NOT_RUNNING_MSG`` without using it.

    All nine CLI modules import the constant and none reference it: every one of
    them routes through ``_server_connect_fail_msg()`` instead. The imports are
    dead and are only kept alive by a ``hasattr`` assertion in
    ``tests/test_cli_260_connection_refused_ux.py``.
    """
    import archon_search.cli as cli_pkg  # noqa: PLC0415

    module_names = (
        "key_cmd",
        "backup_cmd",
        "maintenance_cmd",
        "sync",
        "ingest",
        "graph_cmd",
        "jobs_cmd",
        "collection",
        "export_cmd",
    )
    cli_dir = Path(cli_pkg.__file__).parent
    offenders: list[str] = []

    for mod_name in module_names:
        source = (cli_dir / f"{mod_name}.py").read_text()
        tree = ast.parse(source)
        imported = any(
            isinstance(node, ast.ImportFrom)
            and any(alias.name == "_SERVER_NOT_RUNNING_MSG" for alias in node.names)
            for node in ast.walk(tree)
        )
        used = any(
            isinstance(node, ast.Name) and node.id == "_SERVER_NOT_RUNNING_MSG"
            for node in ast.walk(tree)
        )
        if imported and not used:
            offenders.append(mod_name)

    assert offenders == [], (
        "these CLI modules import _SERVER_NOT_RUNNING_MSG but never reference it — "
        f"dead imports kept alive only by a hasattr() test: {offenders}"
    )


# --------------------------------------------------------------------------- #
# C1-I-10 — the probe ignores checks.storage
# --------------------------------------------------------------------------- #
def test_connect_fail_msg_requires_storage_ok_for_starting_hint() -> None:
    """"Starting up" requires ``checks.storage == "ok"``, not just pending models.

    The probe only inspects ``checks.models``. A server whose storage check
    *failed* answers 503 with ``models: pending`` too, and the operator is told
    to "wait for models to load" for a server that will never become ready.
    """
    from archon_search.cli import _helpers  # noqa: PLC0415

    resp = MagicMock()
    resp.status_code = 503
    resp.json.return_value = {"ready": False, "checks": {"models": "pending", "storage": "fail"}}

    with (
        patch.object(_helpers.httpx, "get", return_value=resp),
        patch.object(_helpers, "_get_service", side_effect=NotImplementedError),
    ):
        msg = _helpers._server_connect_fail_msg("http://127.0.0.1:8765")

    assert msg == _helpers._SERVER_NOT_RUNNING_MSG, (
        "/ready reported checks.storage='fail' — the server cannot become ready, "
        "so telling the operator to wait for model loading is wrong. "
        f"Got {msg!r}"
    )


# --------------------------------------------------------------------------- #
# C1-I-12 — warmup_result is not surfaced by GET /status
# --------------------------------------------------------------------------- #
def test_status_surfaces_warmup_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``GET /status`` must surface ``app.state.warmup_result``.

    ``create_app`` documents ``warmup_result`` as the warm-up signal "for /ready
    and /status", but only ``routes_ready`` reads it. ``/ready`` is a bare
    200/503 probe, so the authenticated status surface — the one an operator
    actually inspects — has no way to show that warm-up is in progress, done, or
    failed.
    """
    from tests.integration.conftest import make_real_app  # noqa: PLC0415

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        client.app.state.warmup_result = "pending"
        resp = client.get("/status", headers={"Authorization": f"Bearer {api_key}"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["warmup_result"] == "pending", (
        "GET /status does not surface app.state.warmup_result == 'pending' — "
        "operators cannot tell a warming server from a ready one. "
        f"Got: {resp.json().get('warmup_result')!r}"
    )


# --------------------------------------------------------------------------- #
# C1-F-8 — warmup_result reaching GET /status for the terminal states too
# (only "pending" was covered above)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", ["done", "failed"])
def test_status_surfaces_warmup_result_terminal_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """``GET /status`` must surface both terminal warm-up outcomes, not just 'pending'."""
    from tests.integration.conftest import make_real_app  # noqa: PLC0415

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        client.app.state.warmup_result = value
        resp = client.get("/status", headers={"Authorization": f"Bearer {api_key}"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["warmup_result"] == value, (
        f"GET /status does not surface app.state.warmup_result == {value!r}. "
        f"Got: {resp.json().get('warmup_result')!r}"
    )


# --------------------------------------------------------------------------- #
# C1-I-14 — _warmup_pending is evaluated twice per /ready request
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_warmup_pending_evaluated_once_per_ready_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/ready`` must evaluate ``_warmup_pending()`` exactly once per request.

    ``ready()`` calls it directly *and* through ``_model_check_status()``. The two
    reads of ``app.state.warmup_result`` are not atomic: a warm-up finishing
    between them yields a body with ``checks.models == "pending"`` and
    ``ready: true`` — a self-contradictory response.
    """
    from archon_search.server import routes_ready  # noqa: PLC0415

    cfg = SearchConfig()
    cfg.eager_load_embedders = True

    class FakeStore:
        ping = AsyncMock(return_value=True)

    class FakeState:
        model_validation = None
        warmup_result = "done"
        config = cfg
        search_store = FakeStore()
        _startup_sync_task = None

    class FakeApp:
        state = FakeState()

    class FakeRequest:
        app = FakeApp()

    original = routes_ready._warmup_pending
    calls: list[int] = []

    def counting_warmup_pending(request):
        calls.append(1)
        return original(request)

    monkeypatch.setattr(routes_ready, "_warmup_pending", counting_warmup_pending)
    await routes_ready.ready(FakeRequest())

    assert len(calls) == 1, (
        f"_warmup_pending() was evaluated {len(calls)} times in one GET /ready — "
        "ready() calls it directly and again inside _model_check_status(); a "
        "warm-up completing between the two reads produces a body with "
        "checks.models='pending' and ready=true"
    )


# --------------------------------------------------------------------------- #
# C1-I-15 — a null `checks` body raises AttributeError into the blanket except
# --------------------------------------------------------------------------- #
def test_connect_fail_msg_handles_null_checks_without_swallowing() -> None:
    """``"checks": null`` must be handled gracefully — not crash, not consult service manager.

    ``resp.json().get("checks", {}).get("models")`` guards a *missing* key but not
    a ``null`` value: ``None.get(...)`` raises ``AttributeError``, which the
    blanket ``except Exception`` swallows. After the C1-I-15 fix the body is
    type-checked before ``.get`` is called, so ``checks=null`` sets ``usable=False``
    without crashing.

    After C1-I-16: when ``base_url`` is given and ``usable=False`` (regardless of
    whether the probe failed or returned a non-usable body), the service manager is
    *never* consulted. The function returns ``_SERVER_NOT_RUNNING_MSG`` immediately.
    """
    from archon_search.cli import _helpers  # noqa: PLC0415

    resp = MagicMock()
    resp.status_code = 503
    resp.json.return_value = {"ready": False, "checks": None}

    service = MagicMock()
    service.status.return_value.running = True

    with (
        patch.object(_helpers.httpx, "get", return_value=resp),
        patch.object(_helpers, "_get_service", return_value=service),
    ):
        msg = _helpers._server_connect_fail_msg("http://127.0.0.1:8765")

    assert msg == _helpers._SERVER_NOT_RUNNING_MSG, (
        "A null checks body with base_url given must yield NOT_RUNNING_MSG — "
        "service manager must not be consulted (C1-I-16). "
        f"Got: {msg!r}"
    )
    service.status.assert_not_called()


# --------------------------------------------------------------------------- #
# C1-F-5 — the narrowed except must not swallow an unexpected internal error
# --------------------------------------------------------------------------- #
def test_connect_fail_msg_propagates_unexpected_internal_error() -> None:
    """An internal defect in the probe (not a network/parsing failure) must propagate.

    Narrowing the blanket ``except Exception`` to ``(httpx.HTTPError, ValueError)``
    means a genuine programming defect — e.g. ``resp.json()`` raising
    ``AttributeError`` for a reason unrelated to network I/O or JSON parsing —
    surfaces as a real exception instead of being silently treated as "probe
    unusable, fall back to the managed-service check".
    """
    from archon_search.cli import _helpers  # noqa: PLC0415

    resp = MagicMock()
    resp.status_code = 503
    resp.json.side_effect = AttributeError("boom")

    with patch.object(_helpers.httpx, "get", return_value=resp):
        with pytest.raises(AttributeError):
            _helpers._server_connect_fail_msg("http://127.0.0.1:8765")


# --------------------------------------------------------------------------- #
# C2-I-4 — a non-dict JSON body (null / array / bare string) must not crash the
# CLI with an AttributeError
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("body", [None, [], "not an object"])
def test_connect_fail_msg_handles_non_dict_body_without_crashing(body: object) -> None:
    """A body that is valid JSON but not an object must not crash the CLI.

    ``resp.json().get("checks")`` assumes the decoded body is a dict. A body of
    literal ``null``, a JSON array, or a bare JSON string — all valid JSON that a
    reverse proxy, load balancer error page, or captive portal can serve on
    ``/ready`` — would make ``.get`` raise ``AttributeError`` before the C1-I-15
    fix. After that fix the body is type-checked first, so these bodies produce
    ``usable=False`` without crashing.

    After C1-I-16: when ``base_url`` is given and ``usable=False``, the service
    manager is *never* consulted. The function returns ``_SERVER_NOT_RUNNING_MSG``
    immediately — whether the probe failed or returned a non-usable body.
    """
    from archon_search.cli import _helpers  # noqa: PLC0415

    resp = MagicMock()
    resp.status_code = 503
    resp.json.return_value = body

    service = MagicMock()
    service.status.return_value.running = True

    with (
        patch.object(_helpers.httpx, "get", return_value=resp),
        patch.object(_helpers, "_get_service", return_value=service),
    ):
        msg = _helpers._server_connect_fail_msg("http://127.0.0.1:8765")

    assert msg == _helpers._SERVER_NOT_RUNNING_MSG, (
        f"A non-dict body ({body!r}) with base_url given must yield NOT_RUNNING_MSG — "
        "service manager must not be consulted (C1-I-16). "
        f"Got: {msg!r}"
    )
    service.status.assert_not_called()


# --------------------------------------------------------------------------- #
# C2-I-7 — the "starting up" hint must recognize a pending startup sync, not
# just pending models
# --------------------------------------------------------------------------- #
def test_connect_fail_msg_recognizes_sync_pending_as_starting_up() -> None:
    """A 503 with ``models: "ok"``, ``storage: "ok"``, ``sync: "pending"`` must
    read as "starting up", not "not running".

    During the startup sync the server answers 503 with a healthy models check
    (eager warm-up already finished or is disabled) and a pending sync check.
    The old condition only inspected ``checks.models``, so it fell through to
    "archon-search serve is not running. Start it first" about a server that is
    up and mid-startup.

    ``_get_service`` is patched to raise if called: this probe is *usable*
    (a dict body with ``checks.models`` present), so the correct code path
    never reaches the service-manager fallback at all. Without this guard, a
    reversion of the ``sync == "pending"`` disjunct would silently fall
    through to the developer's real ``launchctl``/service status instead of
    failing the test outright, and the result would flip with whether a local
    service happens to be up.
    """
    from archon_search.cli import _helpers  # noqa: PLC0415

    resp = MagicMock()
    resp.status_code = 503
    resp.json.return_value = {
        "ready": False,
        "checks": {"models": "ok", "storage": "ok", "sync": "pending"},
    }

    with (
        patch.object(_helpers.httpx, "get", return_value=resp),
        patch.object(_helpers, "_get_service", side_effect=NotImplementedError),
    ):
        msg = _helpers._server_connect_fail_msg("http://127.0.0.1:8765")

    assert msg == _helpers._SERVER_STARTING_MSG, (
        "checks.sync == 'pending' with storage ok means the server is up and "
        f"mid-startup-sync — expected the 'starting up' hint, got {msg!r}"
    )


# --------------------------------------------------------------------------- #
# C1-I-16 — probe succeeds with non-usable body but service-manager still consulted
# --------------------------------------------------------------------------- #
def test_connect_fail_msg_probe_succeeds_non_usable_body_does_not_consult_service_manager() -> None:
    """When ``base_url`` is given and probe SUCCEEDS but body is not usable, service manager is NOT consulted.

    ca54533b fixed the case where the probe *fails* (raises HTTPError/ValueError): the
    function now returns ``_SERVER_NOT_RUNNING_MSG`` immediately. But the peer case —
    probe returns an HTTP response whose body has no ``checks.models`` (``usable=False``) —
    still falls through to ``_get_service().status().running``. If the managed service
    happens to be running (launchd / systemd), the function returns ``_SERVER_STARTING_MSG``
    even for the default localhost URL, which is the message the sequential-add scenario
    hits on the second call when the server is in a degraded/shutdown state.

    The contract (docstring, ca54533b): when ``base_url`` is given, the service manager
    is *never* consulted — not on probe failure, not on a non-usable probe response.
    """
    from archon_search.cli import _helpers  # noqa: PLC0415

    service = MagicMock()
    service.status.return_value = MagicMock(running=True)

    # Probe returns an HTTP 200 but with {"checks": null} — usable=False
    probe_resp = MagicMock()
    probe_resp.status_code = 200
    probe_resp.json.return_value = {"checks": None}

    with (
        patch.object(_helpers.httpx, "get", return_value=probe_resp),
        patch.object(_helpers, "_get_service", return_value=service),
    ):
        msg = _helpers._server_connect_fail_msg("http://127.0.0.1:8765")

    assert msg == _helpers._SERVER_NOT_RUNNING_MSG, (
        "Probe returned non-usable body with base_url given: "
        "service manager must not be consulted (same contract as probe-failure). "
        f"Got: {msg!r}"
    )
    service.status.assert_not_called()


def test_sequential_add_second_does_not_report_starting_up_when_probe_non_usable() -> None:
    """Second ``collection add`` must not say 'starting up' when probe returns non-usable body.

    Scenario (C1-I-16 integration): user runs two sequential ``add`` commands. The first
    succeeds. Between the calls the server enters a degraded state where ``POST /collections/``
    raises ``ConnectError`` but ``GET /ready`` returns a body with no ``checks.models``
    (``usable=False``). The managed service reports ``running=True``. Before the fix the
    second add outputs ``_SERVER_STARTING_MSG``; after the fix it outputs
    ``_SERVER_NOT_RUNNING_MSG``.
    """
    from archon_search.cli import _helpers  # noqa: PLC0415
    from archon_search.cli.collection import collection  # noqa: PLC0415

    runner = CliRunner()

    first_resp = MagicMock()
    first_resp.status_code = 202
    first_resp.json.return_value = {"job_id": "job-seq-001", "collection": "col_a"}

    service = MagicMock()
    service.status.return_value = MagicMock(running=True)

    probe_resp = MagicMock()
    probe_resp.status_code = 200
    probe_resp.json.return_value = {"checks": None}  # usable=False

    # First add: server healthy, succeeds
    with (
        patch("archon_search.cli.collection.httpx.post", return_value=first_resp),
        patch.object(_helpers, "_get_service", return_value=service),
    ):
        result1 = runner.invoke(collection, ["add", "/docs/A", "--api-key", "test-key"])

    assert result1.exit_code == 0, f"First add must succeed; got exit {result1.exit_code}: {result1.output}"

    # Second add: POST fails, probe returns non-usable body, managed service says running
    with (
        patch("archon_search.cli.collection.httpx.post", side_effect=httpx.ConnectError("refused")),
        patch.object(_helpers.httpx, "get", return_value=probe_resp),
        patch.object(_helpers, "_get_service", return_value=service),
    ):
        result2 = runner.invoke(collection, ["add", "/docs/B", "--api-key", "test-key"])

    output2 = result2.output + (result2.stderr if hasattr(result2, "stderr") else "")
    assert result2.exit_code == 1, f"Second add must fail; got exit {result2.exit_code}"
    assert "starting up" not in output2.lower(), (
        "Second add must not say 'starting up' when probe returns non-usable body "
        f"with base_url given — service manager state must not leak. Got: {output2!r}"
    )
    assert "not running" in output2.lower() or "start it first" in output2.lower(), (
        f"Second add must report the server as not running. Got: {output2!r}"
    )


# --------------------------------------------------------------------------- #
# 2026-08-19-020 — the startup sync must not auto-re-enter the ingest that just
# killed the process.
#
# `jobs/store.py` `_load()` already rewrites every RUNNING/CANCELLING job to
# `status=FAILED, error="process_restart"` on boot (:331-335) — the process died
# mid-ingest. `app.py`'s lifespan ignores that signal and creates
# `_run_startup_sync(all_cols)` unconditionally whenever any collection is
# configured (:706-756), so after an OOM/kill-9 death launchd restarts the
# server and the startup sync immediately re-runs the exact same machine-killing
# ingest, unattended. See
# Documentation/Backlog/2026-08-19-020-startup-sync-crash-loop-brief.md.
# --------------------------------------------------------------------------- #
def _seed_crashed_ingest_jobs_file(path: Path) -> None:
    """Write a jobs file whose ingest job is still RUNNING — an unclean death.

    Built through the real ``JobStore`` rather than hand-rolled JSON so the
    on-disk shape can never drift from the writer that produces it in
    production.
    """
    seeder = JobStore(path=path)
    job = seeder.create(path="/corpus/docs", collection="docs")
    seeder.update(job.job_id, status=JobStatus.RUNNING)


def _run_lifespan_and_capture_sync_task(app, sync_calls: list[list[str]]):
    """Enter ``app``'s lifespan, let any startup-sync task run, return the task.

    Returns a coroutine — the caller awaits it. ``sync_calls`` is polled only
    when a task was actually created, so the suppressed (fixed) path costs
    nothing.
    """

    async def _runner():
        startup_done = asyncio.Event()
        shutdown = asyncio.Event()

        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                startup_done.set()
                await shutdown.wait()

        lifespan_task = asyncio.create_task(run_lifespan())
        try:
            await asyncio.wait_for(startup_done.wait(), timeout=30.0)
            # The task is create_task'd, not awaited — give it a chance to
            # actually enter sync() so the caller can report what it re-ingested.
            sync_task = getattr(app.state, "_startup_sync_task", None)
            if sync_task is not None:
                # ~30s budget at a 10ms poll interval: the fast (already-entered)
                # path still returns almost immediately, but a loaded machine
                # under -n 8 xdist has enough scheduling slack that the previous
                # ~0.5s budget (range(50)) was a latent flake.
                for _ in range(3000):
                    if sync_calls:
                        break
                    await asyncio.sleep(0.01)
            return sync_task
        finally:
            shutdown.set()
            await lifespan_task

    return _runner()


def _make_recording_sync():
    """Build a ``SearchCollectionSync.sync`` stub that records the collections list.

    Extracted (fix-brief-C item 6e-4): the exact ``recording_sync`` closure
    below, plus the patch stack in ``_enter_startup_sync_lifespan_patches``,
    was duplicated byte-for-byte three times in this test block before this
    extraction — and the block is about to grow again with this brief's own
    sticky-suppression tests, so the duplication would only compound.
    """
    from archon_search.sync import SyncResult  # noqa: PLC0415

    sync_calls: list[list[str]] = []

    async def recording_sync(self, collections: list[str], progress_cb=None) -> SyncResult:
        sync_calls.append(list(collections))
        return SyncResult()

    return sync_calls, recording_sync


def _enter_startup_sync_lifespan_patches(stack: ExitStack, sync_impl=None) -> None:
    """Enter the standard patch stack for a real ``create_app()`` lifespan boot.

    Extends ``_enter_store_patches`` with the ``SearchStore.disconnect``/``ping``
    stubs every startup-sync boot test in this block needs, and (optionally) a
    ``SearchCollectionSync.sync`` stub — pass the ``recording_sync`` from
    ``_make_recording_sync()`` (or any other stub) via ``sync_impl``.
    """
    from archon_search.store import SearchStore  # noqa: PLC0415
    from archon_search.sync import SearchCollectionSync  # noqa: PLC0415

    _enter_store_patches(stack)
    stack.enter_context(patch.object(SearchStore, "disconnect", new=AsyncMock()))
    stack.enter_context(patch.object(SearchStore, "ping", new=AsyncMock(return_value=True)))
    if sync_impl is not None:
        stack.enter_context(patch.object(SearchCollectionSync, "sync", new=sync_impl))


# --------------------------------------------------------------------------- #
# fix-brief-C item 1 — the sentinel module itself (archon_search/sync_suppression.py):
# minimal JSON body, and fail-open on write/clear failure — a sentinel write
# failure must never fail startup, matching _start_startup_sync_job's posture.
# --------------------------------------------------------------------------- #
def test_write_sync_suppressed_sentinel_creates_minimal_json_body() -> None:
    import json  # noqa: PLC0415

    from archon_search.sync_suppression import (  # noqa: PLC0415
        get_sync_suppressed_file,
        write_sync_suppressed_sentinel,
    )

    write_sync_suppressed_sentinel()
    path = get_sync_suppressed_file()
    assert path.exists(), "write_sync_suppressed_sentinel did not create the sentinel file"
    body = json.loads(path.read_text())
    assert set(body.keys()) == {"suppressed_at"}, (
        f"the sentinel body must have exactly one field, suppressed_at; got {body!r}"
    )


def test_write_sync_suppressed_sentinel_fails_open_on_write_error() -> None:
    import archon_search.sync_suppression as ss_module  # noqa: PLC0415

    def _raising_atomic_write(path, data):
        raise OSError("disk full")

    with patch.object(ss_module, "atomic_write_json", new=_raising_atomic_write):
        ss_module.write_sync_suppressed_sentinel()  # must not raise
    assert not ss_module.get_sync_suppressed_file().exists(), (
        "test setup is wrong — the sentinel should not exist after a failed write"
    )


def test_clear_sync_suppressed_sentinel_fails_open_on_unlink_error() -> None:
    import archon_search.sync_suppression as ss_module  # noqa: PLC0415

    ss_module.write_sync_suppressed_sentinel()
    assert ss_module.get_sync_suppressed_file().exists(), "test setup is wrong"

    with patch.object(Path, "unlink", side_effect=OSError("permission denied")):
        ss_module.clear_sync_suppressed_sentinel()  # must not raise


def test_clear_sync_suppressed_sentinel_is_idempotent_when_absent() -> None:
    from archon_search.sync_suppression import (  # noqa: PLC0415
        clear_sync_suppressed_sentinel,
        get_sync_suppressed_file,
    )

    assert not get_sync_suppressed_file().exists(), "test setup is wrong"
    clear_sync_suppressed_sentinel()  # must not raise


def test_clean_sync_clears_stale_sentinel_even_when_sync_result_is_not_degraded() -> None:
    """A stale sentinel must be clearable on a server with no collections configured.

    The boot guard in ``app.py`` is gated on ``all_cols``, so a server with no
    ``[collections]`` never reaches ``SUPPRESSED`` — ``sync_result`` stays
    ``None``. If ``_clear_degraded_sync_state`` only cleared the sentinel inside
    its degraded-state branch, a sentinel left behind by an earlier
    configuration could never be removed by the sanctioned resume path, and
    would silently suppress the first boot after collections were added back.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    from archon_search.server.routes_sync import _clear_degraded_sync_state  # noqa: PLC0415
    from archon_search.sync_suppression import (  # noqa: PLC0415
        get_sync_suppressed_file,
        write_sync_suppressed_sentinel,
    )

    write_sync_suppressed_sentinel()
    assert get_sync_suppressed_file().exists(), "test setup is wrong"

    # The no-collections case: nothing ever set sync_result.
    app_state = SimpleNamespace(sync_result=None, _startup_sync_failed=False)
    _clear_degraded_sync_state(app_state)

    assert not get_sync_suppressed_file().exists(), (
        "a clean manual sync left a stale sentinel on disk when sync_result was not "
        "SUPPRESSED/FAILED — the next boot after collections are configured would be "
        "suppressed with no way to clear it short of deleting the file by hand"
    )
    assert app_state.sync_result is None, (
        "clearing the sentinel must not invent a sync_result transition"
    )


@pytest.mark.asyncio
async def test_startup_sync_suppressed_after_process_restart_marker(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A ``process_restart`` marker must suppress the automatic startup sync.

    Sequence reproduced here is the production one from the OOM incident:
    a jobs file left with a RUNNING ingest job (the process was killed mid-run),
    then a fresh boot. ``JobStore.__init__`` -> ``_load()`` rewrites that job to
    ``FAILED / "process_restart"`` — the server therefore *knows* the previous
    run died mid-ingest — and the lifespan then spawns the startup sync anyway,
    re-entering the same workload with no operator action.

    Oracle: ``app.state._startup_sync_task`` must not exist (or be ``None``) and
    ``SearchCollectionSync.sync`` must never be called, with a WARNING pointing
    the operator at the manual resume path.
    """
    import logging  # noqa: PLC0415

    from archon_search.server.app import create_app  # noqa: PLC0415

    jobs_path = tmp_path / "jobs.json"
    _seed_crashed_ingest_jobs_file(jobs_path)

    # The post-crash boot: a second JobStore over the same file.
    job_store = JobStore(path=jobs_path)
    assert [j.error for j in job_store.list()] == ["process_restart"], (
        "test setup is wrong — the seeded RUNNING job was not marked "
        f"process_restart on load; got {[j.error for j in job_store.list()]!r}"
    )

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = ["docs"]  # non-empty => the lifespan takes the startup-sync branch

    sync_calls, recording_sync = _make_recording_sync()

    with ExitStack() as stack:
        _enter_startup_sync_lifespan_patches(stack, recording_sync)

        app = create_app(cfg, job_store)
        with caplog.at_level(logging.WARNING, logger="archon_search.server.app"):
            sync_task = await _run_lifespan_and_capture_sync_task(app, sync_calls)

    assert sync_task is None, (
        "the lifespan created a startup-sync task even though the job store had "
        "just marked an ingest job FAILED / 'process_restart' — after an OOM or "
        "kill -9 death mid-ingest the server re-enters the same machine-killing "
        "workload unattended on every restart"
    )
    assert sync_calls == [], (
        f"the suppressed startup sync still called collection_sync.sync({sync_calls!r})"
    )

    warnings = [
        r.getMessage().lower()
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith("archon_search.server.app")
    ]
    assert any("sync" in m and "suppress" in m for m in warnings), (
        "suppressing the startup sync must log a prominent WARNING telling the "
        "operator to resume manually (POST /sync or `archon-search sync`); got "
        f"{warnings!r}"
    )
    # The resume-path half is operationally load-bearing (BREAKING.md and
    # OperatorGuide/90_incident_runbook.md both promise it) but was not
    # separately pinned above — the prior assertion passes even if this exact
    # phrase were deleted from the WARNING, as long as SOME "suppress" message
    # fired. Separate assertion per learnings.md's vacuity-trap guidance
    # (an `or` lets either half rot silently) — this one is new, the four
    # assertions above are untouched.
    assert any("resume manually with post /sync" in m for m in warnings), (
        "the suppression WARNING must name the resume path (POST /sync or "
        f"`archon-search sync`) so an operator knows how to recover; got {warnings!r}"
    )


@pytest.mark.asyncio
async def test_startup_sync_runs_when_no_process_restart_marker(tmp_path: Path) -> None:
    """A clean jobs file must leave the existing startup-sync behavior intact.

    Counterpart to the suppression test above: the crash-loop guard must key on
    the ``process_restart`` marker only, never disable the startup sync outright.
    """
    from archon_search.server.app import create_app  # noqa: PLC0415

    # No jobs file at all — the ordinary clean-shutdown boot.
    job_store = JobStore(path=tmp_path / "jobs.json")
    assert job_store.list() == [], "test setup is wrong — jobs file must be clean"

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = ["docs"]

    sync_calls, recording_sync = _make_recording_sync()

    with ExitStack() as stack:
        _enter_startup_sync_lifespan_patches(stack, recording_sync)

        app = create_app(cfg, job_store)
        sync_task = await _run_lifespan_and_capture_sync_task(app, sync_calls)

    assert sync_task is not None, (
        "the crash-loop guard suppressed the startup sync on a clean jobs file — "
        "it must key on the process_restart marker only"
    )
    assert sync_calls == [["docs"]], (
        f"a clean boot must still sync every configured collection; got {sync_calls!r}"
    )


@pytest.mark.asyncio
async def test_crashed_export_job_does_not_suppress_startup_sync(tmp_path: Path) -> None:
    """The guard must key on the ingest family only, not on any crashed job.

    An export that died mid-run says nothing about the ingest workload, so
    suppressing the startup sync for it would strand the index on a stale
    corpus for an unrelated reason.
    """
    from archon_search.server.app import create_app  # noqa: PLC0415

    jobs_path = tmp_path / "jobs.json"
    seeder = JobStore(path=jobs_path)
    export = seeder.create_export(
        collection="docs", output_path=str(tmp_path / "out.tar"), tmp_path=str(tmp_path)
    )
    seeder.update(export.job_id, status=JobStatus.RUNNING)

    job_store = JobStore(path=jobs_path)
    assert [j.error for j in job_store.list()] == ["process_restart"], (
        "test setup is wrong — the seeded RUNNING export was not marked process_restart"
    )
    assert job_store.crashed_ingest_on_load is False, (
        "a crashed export is not an ingest-family job and must not raise the "
        "crash-loop flag"
    )

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = ["docs"]

    sync_calls, recording_sync = _make_recording_sync()

    with ExitStack() as stack:
        _enter_startup_sync_lifespan_patches(stack, recording_sync)

        app = create_app(cfg, job_store)
        sync_task = await _run_lifespan_and_capture_sync_task(app, sync_calls)

    assert sync_task is not None, (
        "a crashed export job suppressed the startup sync — the guard is too wide"
    )
    assert sync_calls == [["docs"]], (
        f"the startup sync must still run after a crashed export; got {sync_calls!r}"
    )


def test_status_surfaces_suppressed_sync_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A suppressed startup sync must be visible on ``GET /status``, and ``/ready`` stay 200.

    End-to-end counterpart to the suppression test above: a real app booted over
    a jobs file left with a RUNNING ingest job must report
    ``sync_result == "suppressed"`` — the degraded-but-ready flag an operator
    reads to learn the index is stale and needs a manual ``POST /sync``.
    """
    from tests.integration.conftest import make_real_app  # noqa: PLC0415

    _seed_crashed_ingest_jobs_file(tmp_path / "jobs.json")

    with make_real_app(
        tmp_path,
        monkeypatch,
        toml_content='[collections]\ncollections = ["docs"]\n',
    ) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = client.get("/status", headers=headers)
        ready = client.get("/ready")

    assert resp.status_code == 200, resp.text
    assert resp.json()["sync_result"] == "suppressed", (
        "GET /status does not surface the suppressed startup sync. Got: "
        f"{resp.json().get('sync_result')!r}"
    )
    assert ready.status_code == 200, (
        f"a suppressed startup sync must stay ready, not 503; got {ready.status_code}"
    )


def test_ready_warns_but_stays_200_when_startup_sync_suppressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /ready`` must report ``checks.sync == "warn"`` and still answer 200.

    A bare ``"ok"`` would be an all-clear over a potentially stale index — the
    suppressed sync never ran, so neither ``"pending"`` (no task) nor ``"fail"``
    (``_startup_sync_failed`` was never set) fires. ``"warn"`` is informational
    only: it must not gate ``ready``, exactly like ``checks.models: "warn"``.
    """
    from tests.integration.conftest import make_real_app  # noqa: PLC0415

    _seed_crashed_ingest_jobs_file(tmp_path / "jobs.json")

    with make_real_app(
        tmp_path,
        monkeypatch,
        toml_content='[collections]\ncollections = ["docs"]\n',
    ) as (client, _cfg, _api_key):
        resp = client.get("/ready")

    body = resp.json()
    assert body["checks"]["sync"] == "warn", (
        "a suppressed startup sync must report checks.sync='warn', not a bare "
        f"all-clear; got {body['checks'].get('sync')!r}"
    )
    assert resp.status_code == 200, (
        f"checks.sync='warn' must not gate readiness; got {resp.status_code} {resp.text}"
    )
    assert body["ready"] is True, (
        f"a suppressed startup sync must report ready=true; got {body['ready']!r}"
    )


def test_successful_manual_sync_clears_suppressed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean ``POST /sync`` must clear the suppressed flag on ``/status`` and ``/ready``.

    The crash-loop guard's WARNING tells the operator to resume with
    ``POST /sync``; if the degraded flag survived that resume it would be stuck
    for the process lifetime and stop meaning anything.
    """
    import time  # noqa: PLC0415

    from tests.integration.conftest import make_real_app  # noqa: PLC0415

    _seed_crashed_ingest_jobs_file(tmp_path / "jobs.json")

    with make_real_app(
        tmp_path,
        monkeypatch,
        toml_content='[collections]\ncollections = ["docs"]\n',
    ) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        assert client.get("/status", headers=headers).json()["sync_result"] == "suppressed", (
            "test setup is wrong — the app did not boot into the suppressed state"
        )

        client.app.state.collection_sync.sync = AsyncMock(
            return_value=MagicMock(
                added=[], removed=[], unchanged=[], errors=[], skipped=[], updated=[]
            )
        )
        resp = client.post("/sync", headers=headers)
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            job = client.get(f"/jobs/{job_id}", headers=headers).json()
            if job["status"] in {"DONE", "FAILED", "CANCELLED"}:
                break
            time.sleep(0.05)
        assert job["status"] == "DONE", f"the manual sync did not succeed: {job!r}"

        status = client.get("/status", headers=headers).json()
        ready = client.get("/ready").json()

    assert status["sync_result"] == "done", (
        "a successful POST /sync must clear the suppressed flag; GET /status still "
        f"reports {status['sync_result']!r}"
    )
    assert ready["checks"]["sync"] == "ok", (
        "checks.sync must return to 'ok' once the operator has resumed; got "
        f"{ready['checks'].get('sync')!r}"
    )


# --------------------------------------------------------------------------- #
# fix-brief-C item 1 — suppression must be STICKY: it must survive a process
# restart, not last exactly one boot. Today _load() rewrites RUNNING -> FAILED
# and __init__ persists it, so the next boot sees a terminal row and syncs
# again — under any supervisor that restarts more than once this halves the
# crash loop instead of breaking it. Mechanism: a sentinel file under
# get_data_dir() (archon_search/sync_suppression.py), written when the guard
# suppresses and cleared only by a clean manual POST /sync.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_sync_suppression_persists_across_second_reboot(tmp_path: Path) -> None:
    """Suppression must survive a process restart.

    Boot 1 mirrors ``test_startup_sync_suppressed_after_process_restart_marker``:
    a RUNNING ingest job is rewritten to FAILED/"process_restart" on load, and
    the guard suppresses. By boot 2 that row is already terminal (FAILED), so
    a FRESH ``JobStore`` load's ``crashed_ingest_on_load`` is False — under the
    old (one-boot) behavior the guard would fire the startup sync again here,
    re-entering the same workload that killed the process. The sticky
    sentinel written during boot 1 must keep it suppressed on boot 2.
    """
    import json  # noqa: PLC0415

    from archon_search.server.app import create_app  # noqa: PLC0415
    from archon_search.sync_suppression import get_sync_suppressed_file  # noqa: PLC0415

    jobs_path = tmp_path / "jobs.json"
    _seed_crashed_ingest_jobs_file(jobs_path)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = ["docs"]

    # --- Boot 1: a fresh crash, detected via job_store.crashed_ingest_on_load ---
    boot1_store = JobStore(path=jobs_path)
    assert boot1_store.crashed_ingest_on_load is True, "test setup is wrong"

    sync_calls_1, recording_sync_1 = _make_recording_sync()
    with ExitStack() as stack:
        _enter_startup_sync_lifespan_patches(stack, recording_sync_1)
        app1 = create_app(cfg, boot1_store)
        sync_task_1 = await _run_lifespan_and_capture_sync_task(app1, sync_calls_1)

    assert sync_task_1 is None, "boot 1 setup is wrong — suppression did not fire"
    assert sync_calls_1 == [], "boot 1 setup is wrong — sync ran despite suppression"

    sentinel = get_sync_suppressed_file()
    assert sentinel.exists(), (
        "the guard suppressed on boot 1 but did not write the sticky sentinel file"
    )
    body = json.loads(sentinel.read_text())
    assert set(body.keys()) == {"suppressed_at"}, (
        f"the sentinel body must have exactly one field, suppressed_at; got {body!r}"
    )

    # --- Boot 2: the crashed row is already terminal (FAILED); a fresh load's
    # crashed_ingest_on_load is False, so only the sentinel can still suppress.
    boot2_store = JobStore(path=jobs_path)
    assert boot2_store.crashed_ingest_on_load is False, (
        "test setup is wrong — the row must already be terminal by boot 2"
    )

    sync_calls_2, recording_sync_2 = _make_recording_sync()
    with ExitStack() as stack:
        _enter_startup_sync_lifespan_patches(stack, recording_sync_2)
        app2 = create_app(cfg, boot2_store)
        sync_task_2 = await _run_lifespan_and_capture_sync_task(app2, sync_calls_2)

    assert sync_task_2 is None, (
        "suppression did not survive a second boot — the crashed job row is "
        "already terminal by boot 2, so only the sticky sentinel could still be "
        "suppressing; without it this is crash / skip / crash / skip under any "
        "supervisor that restarts more than once, not a broken loop"
    )
    assert sync_calls_2 == [], (
        f"boot 2's suppressed startup sync still called collection_sync.sync({sync_calls_2!r})"
    )


# --------------------------------------------------------------------------- #
# fix-brief-C item 2 — guard the WATCHER. _watch_callback re-entered ingest on
# file events while suppressed; the brief's own repro specifies watch=true, so
# the guard was bypassed in its documented configuration. Must key on the SAME
# live state a manual POST /sync clears (app.state.sync_result), not
# job_store.crashed_ingest_on_load (boot-time only) — otherwise a successful
# manual sync would clear GET /status while the watcher stayed blocked forever.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_watcher_guard_skips_while_suppressed_and_resumes_after_clean_sync(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file-change callback must be skipped while suppressed, and must
    resume once a clean manual sync has cleared the SAME live state.

    Both halves are mutation-checkable independently: reverting the watcher
    guard makes Part 1 fail (sync_collection gets called while suppressed);
    keying the guard on ``job_store.crashed_ingest_on_load`` instead of
    ``app.state.sync_result`` makes Part 2 fail (the watcher stays blocked
    forever even after a clean manual sync, since ``crashed_ingest_on_load``
    is a boot-time snapshot that a manual sync cannot touch).
    """
    import logging  # noqa: PLC0415

    from archon_search.server.app import create_app  # noqa: PLC0415
    from archon_search.server.routes_sync import _sync_task  # noqa: PLC0415
    from archon_search.server.schemas import StartupSyncResult  # noqa: PLC0415
    from archon_search.sync import SearchCollectionSync, SyncResult  # noqa: PLC0415

    jobs_path = tmp_path / "jobs.json"
    _seed_crashed_ingest_jobs_file(jobs_path)
    job_store = JobStore(path=jobs_path)

    source_dir = tmp_path / "docs"
    source_dir.mkdir()

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(source_dir)]
    cfg.watch = True

    sync_collection_calls: list[str] = []

    async def recording_sync_collection(self, collection_name, source_path) -> None:
        sync_collection_calls.append(collection_name)

    with ExitStack() as stack:
        _enter_startup_sync_lifespan_patches(stack)
        stack.enter_context(
            patch.object(SearchCollectionSync, "sync_collection", new=recording_sync_collection)
        )

        app = create_app(cfg, job_store)
        startup_done = asyncio.Event()
        shutdown = asyncio.Event()

        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                startup_done.set()
                await shutdown.wait()

        lifespan_task = asyncio.create_task(run_lifespan())
        col_name = ""
        try:
            await asyncio.wait_for(startup_done.wait(), timeout=30.0)
            assert app.state.sync_result == StartupSyncResult.SUPPRESSED, (
                "test setup is wrong — the boot did not suppress"
            )
            wm = app.state.watcher_manager
            assert wm is not None, "watcher was not started; test setup is wrong"
            col_name = next(iter(wm._watchers))

            # --- Part 1: a suppressed boot must skip the watcher-triggered ingest ---
            with caplog.at_level(logging.WARNING, logger="archon_search.server.app"):
                await wm._wrapped_callback(col_name)
            assert sync_collection_calls == [], (
                "the watcher called sync_collection while the startup sync was "
                f"suppressed: {sync_collection_calls!r}"
            )
            warnings = [
                r.getMessage().lower()
                for r in caplog.records
                if r.levelno >= logging.WARNING and r.name.startswith("archon_search.server.app")
            ]
            assert any("skip" in m for m in warnings), (
                f"skipping a watcher-triggered ingest must log a WARNING; got {warnings!r}"
            )
            assert any(col_name.lower() in m for m in warnings), (
                f"the WARNING must name the skipped collection {col_name!r}; got {warnings!r}"
            )

            # --- Part 2: a clean manual sync must clear the SAME state the
            # watcher reads, and the watcher must then resume ---
            class _CleanSync:
                async def sync(self, collections: list[str], progress_cb=None) -> SyncResult:
                    return SyncResult()

            manual_job = job_store.create_sync(namespace="default")
            running = job_store.transition(
                manual_job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING
            )
            await app.state.sync_lock.acquire()
            await _sync_task(
                job=running,
                job_store=job_store,
                collection_sync=_CleanSync(),
                collections=[str(source_dir)],
                lock=app.state.sync_lock,
                app_state=app.state,
            )
            assert app.state.sync_result == StartupSyncResult.DONE, (
                "test setup is wrong — the manual clean sync did not clear sync_result"
            )

            await wm._wrapped_callback(col_name)
        finally:
            shutdown.set()
            await lifespan_task

    assert sync_collection_calls == [col_name], (
        "the watcher did not resume calling sync_collection after a clean manual "
        f"sync cleared the suppressed state; sync_collection_calls={sync_collection_calls!r}"
    )


@pytest.mark.asyncio
async def test_crash_during_startup_sync_arms_the_guard_on_the_next_boot(
    tmp_path: Path,
) -> None:
    """The guard must arm itself: a crash *during the startup sync* must suppress the next boot.

    ``collection_sync.sync()`` writes no job row of its own, so before this fix a
    process killed during the unattended startup sync left nothing on disk —
    the next boot re-ran the same sync and crashed again, which is precisely the
    loop the incident recorded ("RapidOCR engines loading ... with no
    user-initiated job"). The guard only broke the first hop, from a job-backed
    ingest. Backing the startup sync with a SyncJob closes the loop.

    The kill -9 is simulated by snapshotting the jobs file *while the sync is
    still in flight* — that byte-for-byte is what a SIGKILLed process leaves
    behind — and booting a second app over the snapshot.
    """
    import json  # noqa: PLC0415

    from archon_search.server.app import create_app  # noqa: PLC0415
    from archon_search.store import SearchStore  # noqa: PLC0415
    from archon_search.sync import SearchCollectionSync, SyncResult  # noqa: PLC0415

    jobs_path = tmp_path / "jobs.json"
    job_store = JobStore(path=jobs_path)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = ["docs"]

    sync_calls: list[list[str]] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_sync(
        self: SearchCollectionSync, collections: list[str], progress_cb=None
    ) -> SyncResult:
        sync_calls.append(list(collections))
        entered.set()
        await release.wait()
        return SyncResult()

    with ExitStack() as stack:
        _enter_store_patches(stack)
        stack.enter_context(patch.object(SearchStore, "disconnect", new=AsyncMock()))
        stack.enter_context(patch.object(SearchStore, "ping", new=AsyncMock(return_value=True)))
        stack.enter_context(patch.object(SearchCollectionSync, "sync", new=blocking_sync))

        app = create_app(cfg, job_store)
        startup_done = asyncio.Event()
        shutdown = asyncio.Event()

        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                startup_done.set()
                await shutdown.wait()

        lifespan_task = asyncio.create_task(run_lifespan())
        crashed_jobs = tmp_path / "crashed-jobs.json"
        try:
            await asyncio.wait_for(startup_done.wait(), timeout=30.0)
            await asyncio.wait_for(entered.wait(), timeout=30.0)
            # The disk exactly as a SIGKILL mid-sync would leave it. The jobs file
            # is absent entirely when nothing records the sync — that is the bug,
            # so snapshot it as an empty store rather than raising here.
            crashed_jobs.write_text(jobs_path.read_text() if jobs_path.exists() else "[]")
        finally:
            release.set()
            shutdown.set()
            await lifespan_task

        on_disk = json.loads(crashed_jobs.read_text())
        assert [j["status"] for j in on_disk] == ["RUNNING"], (
            "the startup sync left no RUNNING job on disk, so a kill -9 mid-sync is "
            f"invisible to the next boot; jobs file held {on_disk!r}"
        )

        # The post-crash boot.
        reboot_store = JobStore(path=crashed_jobs)
        assert [j.error for j in reboot_store.list()] == ["process_restart"], (
            "the crashed startup-sync job was not marked process_restart on reload; got "
            f"{[j.error for j in reboot_store.list()]!r}"
        )
        assert reboot_store.crashed_ingest_on_load is True, (
            "a crashed startup sync must raise the crash-loop flag — a SyncJob is "
            "ingest-family"
        )

        reboot_app = create_app(cfg, reboot_store)
        reboot_task = await _run_lifespan_and_capture_sync_task(reboot_app, sync_calls)

    assert reboot_task is None, (
        "the boot after a crash *during the startup sync* still spawned a startup "
        "sync — the crash-loop guard does not arm itself and the loop is unbroken"
    )
    assert sync_calls == [["docs"]], (
        f"the post-crash boot re-entered the sync; calls were {sync_calls!r}"
    )


# --------------------------------------------------------------------------- #
# 2026-08-20 — fix-brief-A item 3: JobStore._load()'s corrupt-file except block
# must reset _crashed_ingest_on_load alongside self._jobs, or a file that
# parses far enough to hit a RUNNING ingest row before a later row throws
# leaves the crash-loop guard permanently armed (the corrupt file is never
# rewritten, since `modified` stays False, so every future boot reproduces the
# identical state).
# --------------------------------------------------------------------------- #
def test_load_corrupt_tail_after_running_ingest_row_does_not_leak_crashed_flag(
    tmp_path: Path,
) -> None:
    """A jobs file whose first row is RUNNING (ingest-family) and whose second
    row is malformed must not leave the crash-loop guard latched True.

    The first row sets ``self._crashed_ingest_on_load = True`` while the loop
    is still running; the second row's invalid ``status`` value raises
    ``ValueError`` inside the same ``try:`` block. The except clause resets
    ``self._jobs = {}`` — it must reset the flag too, or the guard stays armed
    forever with no jobs on record to ever un-arm it.
    """
    import json  # noqa: PLC0415

    jobs_path = tmp_path / "jobs.json"
    raw = [
        {
            "job_id": "job-1",
            "status": "RUNNING",
            "created_at": "2026-08-01T00:00:00+00:00",
            "updated_at": "2026-08-01T00:00:00+00:00",
            "namespace": "default",
            "source": "user",
            "source_path": "/corpus/docs",
            "collection": "docs",
            "retry_count": 0,
            "progress": None,
            "result": None,
            "error": None,
        },
        {
            "job_id": "job-2",
            "status": "not-a-real-status",
            "created_at": "2026-08-01T00:00:00+00:00",
            "updated_at": "2026-08-01T00:00:00+00:00",
            "namespace": "default",
        },
    ]
    jobs_path.write_text(json.dumps(raw))

    store = JobStore(path=jobs_path)

    assert store.crashed_ingest_on_load is False, (
        "a corrupt jobs file (first row RUNNING/ingest, second row malformed) "
        f"left the crash-loop guard latched True; got {store.crashed_ingest_on_load!r}"
    )


# --------------------------------------------------------------------------- #
# 2026-08-20 — fix-brief-A item 4: `_evict_old()` (store.py:358) must stay
# after the crash-marker loop (store.py:349-355), so the crash-loop guard
# latches before eviction runs.
#
# 2026-08-30 — superseded by fix-brief 2026-08-19-080: the crash-marker
# rewrite now also stamps `updated_at=_now_iso()` (store.py:361-367), so a
# stale RUNNING row recovered here is no longer immediately eviction-eligible
# — it survives this load with a refreshed timestamp instead of being
# silently dropped (the P1 data-loss bug that fix closed). This test now
# pins the guard-arms-and-the-row-survives shape rather than
# guard-arms-then-row-is-evicted.
# --------------------------------------------------------------------------- #
def test_stale_running_ingest_job_arms_guard_and_survives_recovery(
    tmp_path: Path,
) -> None:
    """A RUNNING ingest job older than ``_EVICTION_DAYS`` arms the crash-loop
    guard, and — since the crash rewrite refreshes ``updated_at`` — survives
    the very same load as a FAILED row rather than being evicted by it.

    ``JobStore.__init__`` calls ``self._write_atomic()`` whenever ``_load()``
    reports ``modified`` (true here — the crash rewrite sets it) — and
    ``_write_atomic()`` runs its own, separately-ordered eviction pass before
    serializing. That second pass would mask a reordering bug inside
    ``_load()`` itself, so ``_write_atomic`` is patched to a no-op here: this
    test isolates ``_load()``'s own in-memory result, which is the ordering
    fix-brief-A item 4 actually pins.
    """
    import json  # noqa: PLC0415
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    jobs_path = tmp_path / "jobs.json"
    stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    raw = [
        {
            "job_id": "job-stale",
            "status": "RUNNING",
            "created_at": stale,
            "updated_at": stale,
            "namespace": "default",
            "source": "user",
            "source_path": "/corpus/docs",
            "collection": "docs",
            "retry_count": 0,
            "progress": None,
            "result": None,
            "error": None,
        },
    ]
    jobs_path.write_text(json.dumps(raw))

    load_start = datetime.now(timezone.utc)
    with patch.object(JobStore, "_write_atomic", new=lambda self: None):
        store = JobStore(path=jobs_path)

    assert store.crashed_ingest_on_load is True, (
        "a RUNNING ingest job older than _EVICTION_DAYS must still arm the "
        "crash-loop guard on recovery"
    )
    jobs = store.list()
    assert [job.job_id for job in jobs] == ["job-stale"], (
        f"the stale crash row must survive recovery, not be evicted; got {jobs!r}"
    )
    job = jobs[0]
    assert job.status == JobStatus.FAILED
    assert job.error == "process_restart"
    assert datetime.fromisoformat(job.updated_at) >= load_start, (
        "updated_at must be refreshed to the recovery time, which is what keeps "
        "the row from being immediately re-eligible for eviction"
    )


def test_eviction_only_load_leaves_crashed_flag_false(tmp_path: Path) -> None:
    """The negative direction of the property above: a load that evicts an old
    *terminal* job with no crash rewrite anywhere must leave
    ``crashed_ingest_on_load`` False — it is narrower than the ``modified``
    flag ``_load()`` returns, which also covers plain age-eviction.
    """
    import json  # noqa: PLC0415
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    jobs_path = tmp_path / "jobs.json"
    stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    raw = [
        {
            "job_id": "job-done-stale",
            "status": "DONE",
            "created_at": stale,
            "updated_at": stale,
            "namespace": "default",
            "source": "user",
            "source_path": "/corpus/docs",
            "collection": "docs",
            "retry_count": 0,
            "progress": None,
            "result": None,
            "error": None,
        },
    ]
    jobs_path.write_text(json.dumps(raw))

    store = JobStore(path=jobs_path)

    assert store.crashed_ingest_on_load is False, (
        "a load that only aged out a terminal DONE job (no crash rewrite) "
        f"must not arm the crash-loop guard; got {store.crashed_ingest_on_load!r}"
    )
    assert store.list() == [], f"the stale DONE row must be evicted; got {store.list()!r}"


# --------------------------------------------------------------------------- #
# 2026-08-20 — fix-brief-A item 7: each sanitized ``_STARTUP_SYNC_ERROR_*``
# constant must actually reach the SyncJob's wire-facing ``error`` field, for
# every exit path of ``_run_startup_sync`` (app.py). These boot the real
# lifespan and read the resulting job back out of the job store — the same
# object ``GET /jobs`` serializes via ``job_to_dict``.
# --------------------------------------------------------------------------- #
async def _boot_and_get_sole_startup_sync_job(
    tmp_path: Path,
    job_store: JobStore,
    sync_impl,
    *,
    dial_down_timeouts: bool = False,
    monkeypatch: pytest.MonkeyPatch | None = None,
    wait_for=None,
):
    """Boot the real lifespan with a patched ``SearchCollectionSync.sync``,
    wait for ``wait_for`` (an ``asyncio.Event``) if given, then shut down and
    return the single job the startup sync created.
    """
    from archon_search.server import app as app_module  # noqa: PLC0415
    from archon_search.server.app import create_app  # noqa: PLC0415
    from archon_search.store import SearchStore  # noqa: PLC0415
    from archon_search.sync import SearchCollectionSync  # noqa: PLC0415

    if dial_down_timeouts:
        assert monkeypatch is not None
        for name in dir(app_module):
            if "TIMEOUT" in name.upper() and isinstance(getattr(app_module, name), (int, float)):
                monkeypatch.setattr(app_module, name, 0.1)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = ["docs"]

    with ExitStack() as stack:
        _enter_store_patches(stack)
        stack.enter_context(patch.object(SearchStore, "disconnect", new=AsyncMock()))
        stack.enter_context(patch.object(SearchStore, "ping", new=AsyncMock(return_value=True)))
        stack.enter_context(patch.object(SearchCollectionSync, "sync", new=sync_impl))

        app = create_app(cfg, job_store)
        startup_done = asyncio.Event()
        shutdown = asyncio.Event()

        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                startup_done.set()
                await shutdown.wait()

        lifespan_task = asyncio.create_task(run_lifespan())
        try:
            await asyncio.wait_for(startup_done.wait(), timeout=30.0)
            if wait_for is not None:
                await asyncio.wait_for(wait_for.wait(), timeout=30.0)
            sync_task = app.state._startup_sync_task
            if sync_task is not None:
                for _ in range(3000):
                    if sync_task.done():
                        break
                    await asyncio.sleep(0.01)
        finally:
            shutdown.set()
            await lifespan_task

    jobs = job_store.list()
    assert len(jobs) == 1, f"expected exactly one startup-sync job; got {jobs!r}"
    return jobs[0]


@pytest.mark.asyncio
async def test_startup_sync_partial_errors_records_sanitized_job_error(
    tmp_path: Path, job_store: JobStore
) -> None:
    from archon_search.server.app import _STARTUP_SYNC_ERROR_PARTIAL  # noqa: PLC0415
    from archon_search.sync import SearchCollectionSync, SyncResult  # noqa: PLC0415

    async def failing_sync(self: SearchCollectionSync, collections: list[str], progress_cb=None) -> SyncResult:
        return SyncResult(errors=["collection 'docs': path does not exist"])

    job = await _boot_and_get_sole_startup_sync_job(tmp_path, job_store, failing_sync)
    assert job.error == _STARTUP_SYNC_ERROR_PARTIAL, (
        f"expected job.error == {_STARTUP_SYNC_ERROR_PARTIAL!r}; got {job.error!r}"
    )


@pytest.mark.asyncio
async def test_startup_sync_timeout_records_sanitized_job_error(
    tmp_path: Path, job_store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from archon_search.server.app import _STARTUP_SYNC_ERROR_TIMEOUT  # noqa: PLC0415
    from archon_search.sync import SearchCollectionSync  # noqa: PLC0415

    sync_entered = asyncio.Event()

    async def parked_sync(self: SearchCollectionSync, collections: list[str], progress_cb=None):
        sync_entered.set()
        await asyncio.Event().wait()  # never resolves; asyncio.timeout() cuts it off

    job = await _boot_and_get_sole_startup_sync_job(
        tmp_path,
        job_store,
        parked_sync,
        dial_down_timeouts=True,
        monkeypatch=monkeypatch,
        wait_for=sync_entered,
    )
    assert job.error == _STARTUP_SYNC_ERROR_TIMEOUT, (
        f"expected job.error == {_STARTUP_SYNC_ERROR_TIMEOUT!r}; got {job.error!r}"
    )


@pytest.mark.asyncio
async def test_startup_sync_cancelled_during_shutdown_records_cancelled_not_failed(
    tmp_path: Path, job_store: JobStore
) -> None:
    """A clean shutdown mid-startup-sync must record ``CANCELLED``, not ``FAILED``.

    The job row is the ledger operators read during an incident, and ``status`` is
    what ``GET /jobs`` filtering and monitoring key on — a sanitized ``error``
    string reading "cancelled" would not undo a ``FAILED`` status. This mirrors
    ``routes_sync._sync_task``, which records the same event the same way.

    Not a behavioural guard: ``CANCELLED`` is terminal, so neither value arms the
    crash-loop guard. This pins correctness of the record.
    """
    from archon_search.sync import SearchCollectionSync, SyncResult  # noqa: PLC0415

    sync_entered = asyncio.Event()
    release = asyncio.Event()

    async def parked_sync(self: SearchCollectionSync, collections: list[str], progress_cb=None) -> SyncResult:
        sync_entered.set()
        await release.wait()  # kept alive until shutdown cancels it
        return SyncResult()

    job = await _boot_and_get_sole_startup_sync_job(
        tmp_path, job_store, parked_sync, wait_for=sync_entered
    )
    assert job.status == JobStatus.CANCELLED, (
        "a clean shutdown mid-startup-sync must leave the job CANCELLED, not FAILED — "
        f"GET /jobs filtering and monitoring key on status; got {job.status!r}"
    )
    assert job.error is None, (
        "a cancelled-by-shutdown job is not an error and must carry no error text; "
        f"got {job.error!r}"
    )


@pytest.mark.asyncio
async def test_startup_sync_generic_exception_records_sanitized_error_not_raw_message(
    tmp_path: Path, job_store: JobStore
) -> None:
    """Pins the CLAUDE.md invariant directly: an exception message carrying a
    recognizable token must not reach the wire-facing job ``error`` field —
    only the sanitized ``_STARTUP_SYNC_ERROR_FAILED`` constant may appear.
    """
    from archon_search.server.app import _STARTUP_SYNC_ERROR_FAILED  # noqa: PLC0415
    from archon_search.sync import SearchCollectionSync  # noqa: PLC0415

    _SECRET_TOKEN = "sk-live-do-not-leak-4f8a9c21"

    async def exploding_sync(self: SearchCollectionSync, collections: list[str], progress_cb=None):
        raise RuntimeError(f"internal failure, token={_SECRET_TOKEN}")

    job = await _boot_and_get_sole_startup_sync_job(tmp_path, job_store, exploding_sync)
    assert job.error == _STARTUP_SYNC_ERROR_FAILED, (
        f"expected job.error == {_STARTUP_SYNC_ERROR_FAILED!r}; got {job.error!r}"
    )
    assert _SECRET_TOKEN not in (job.error or ""), (
        f"the raw exception message leaked into the wire-facing job error: {job.error!r}"
    )


@pytest.mark.asyncio
async def test_start_startup_sync_job_store_failure_still_runs_the_sync(
    tmp_path: Path, job_store: JobStore
) -> None:
    """``_start_startup_sync_job`` returning None (job store write failed) must
    not abort the startup sync — the corpus sync still runs unmarked. This is
    the docstring promise fix-brief-A item 1 restores.

    Raises a plain ``RuntimeError`` — NOT ``OSError`` — from ``create_sync``:
    the pre-fix code only caught ``OSError`` in ``_start_startup_sync_job``, so
    this specifically pins the broadened ``except Exception``.
    """
    from archon_search.sync import SearchCollectionSync, SyncResult  # noqa: PLC0415

    sync_calls: list[list[str]] = []

    async def recording_sync(self: SearchCollectionSync, collections: list[str], progress_cb=None) -> SyncResult:
        sync_calls.append(list(collections))
        return SyncResult()

    def _raising_create_sync(*args, **kwargs):
        raise RuntimeError("job store backend unavailable")

    # Not using _boot_and_get_sole_startup_sync_job: it asserts exactly one job
    # exists, which does not hold here — create_sync always raises, so no job
    # is ever written. Drive the boot directly instead.
    from archon_search.server.app import create_app  # noqa: PLC0415
    from archon_search.store import SearchStore  # noqa: PLC0415

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = ["docs"]

    with ExitStack() as stack:
        _enter_store_patches(stack)
        stack.enter_context(patch.object(SearchStore, "disconnect", new=AsyncMock()))
        stack.enter_context(patch.object(SearchStore, "ping", new=AsyncMock(return_value=True)))
        stack.enter_context(patch.object(SearchCollectionSync, "sync", new=recording_sync))
        stack.enter_context(patch.object(JobStore, "create_sync", new=_raising_create_sync))

        app = create_app(cfg, job_store)
        startup_done = asyncio.Event()
        shutdown = asyncio.Event()

        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                startup_done.set()
                await shutdown.wait()

        lifespan_task = asyncio.create_task(run_lifespan())
        try:
            await asyncio.wait_for(startup_done.wait(), timeout=30.0)
            sync_task = app.state._startup_sync_task
            assert sync_task is not None, "startup sync task was never spawned"
            for _ in range(3000):
                if sync_calls:
                    break
                await asyncio.sleep(0.01)
        finally:
            shutdown.set()
            await lifespan_task

    assert sync_calls == [["docs"]], (
        "a job-store write failure in _start_startup_sync_job must not prevent "
        f"the corpus sync from running; sync_calls={sync_calls!r}"
    )
    assert job_store.list() == [], (
        "no job should have been persisted when create_sync always raises; got "
        f"{job_store.list()!r}"
    )


@pytest.mark.asyncio
async def test_start_startup_sync_job_base_exception_does_not_escape_the_task(
    tmp_path: Path, job_store: JobStore
) -> None:
    """A non-``Exception`` ``BaseException`` out of ``_start_startup_sync_job``
    must be caught by ``_run_startup_sync``'s final ``except BaseException``
    handler, not escape the task entirely.

    Belt-and-braces half of fix-brief-A item 1: ``job_id`` is initialized to
    ``None`` before the ``try:`` and the call moved inside it. Without that,
    the assignment statement sits outside the ``try:`` and a raised
    ``BaseException`` propagates straight out of ``_run_startup_sync`` —
    exactly the escape the module docstring's "never let the task escape"
    comment (app.py) exists to prevent.
    """
    from archon_search.server import app as app_module  # noqa: PLC0415
    from archon_search.server.app import create_app  # noqa: PLC0415
    from archon_search.server.schemas import StartupSyncResult  # noqa: PLC0415
    from archon_search.store import SearchStore  # noqa: PLC0415
    from archon_search.sync import SearchCollectionSync, SyncResult  # noqa: PLC0415

    class _ExoticBaseException(BaseException):
        """Simulates a non-Exception BaseException (not KeyboardInterrupt/
        SystemExit, which asyncio's task machinery re-raises specially)."""

    def _raising_start_job(store):
        raise _ExoticBaseException("simulated non-Exception failure")

    async def unreached_sync(self: SearchCollectionSync, collections: list[str], progress_cb=None) -> SyncResult:
        return SyncResult()

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = ["docs"]

    with ExitStack() as stack:
        _enter_store_patches(stack)
        stack.enter_context(patch.object(SearchStore, "disconnect", new=AsyncMock()))
        stack.enter_context(patch.object(SearchStore, "ping", new=AsyncMock(return_value=True)))
        stack.enter_context(patch.object(SearchCollectionSync, "sync", new=unreached_sync))
        stack.enter_context(patch.object(app_module, "_start_startup_sync_job", new=_raising_start_job))

        app = create_app(cfg, job_store)
        startup_done = asyncio.Event()
        shutdown = asyncio.Event()

        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                startup_done.set()
                await shutdown.wait()

        lifespan_task = asyncio.create_task(run_lifespan())
        try:
            await asyncio.wait_for(startup_done.wait(), timeout=30.0)
            sync_task = app.state._startup_sync_task
            assert sync_task is not None, "startup sync task was never spawned"
            for _ in range(3000):
                if sync_task.done():
                    break
                await asyncio.sleep(0.01)
        finally:
            shutdown.set()
            await lifespan_task

    assert sync_task.done(), "the startup sync task never finished"
    assert sync_task.exception() is None, (
        "a non-Exception BaseException out of _start_startup_sync_job escaped "
        f"the task instead of being caught: {sync_task.exception()!r}"
    )
    assert app.state._startup_sync_failed is True
    assert app.state.sync_result == StartupSyncResult.FAILED


# --------------------------------------------------------------------------- #
# fix-brief-C item 4 — when create_sync succeeds but the QUEUED -> RUNNING
# transition is rejected (job-store race), _start_startup_sync_job used to
# return None and leave the QUEUED row behind forever: QUEUED is non-terminal,
# so JobStore._evict_old never reclaims it, and every such boot leaks another
# row.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_start_startup_sync_job_transition_failure_does_not_orphan_queued_row(
    tmp_path: Path, job_store: JobStore
) -> None:
    """A rejected QUEUED -> RUNNING transition must not leave the job QUEUED
    forever. It must be moved to a terminal status (so eviction can reclaim
    it), and the corpus sync must still run unmarked — mirroring the
    job-store-write-failure case above.
    """
    from archon_search.server.app import _STARTUP_SYNC_ERROR_NEVER_STARTED  # noqa: PLC0415
    from archon_search.server.app import create_app  # noqa: PLC0415
    from archon_search.store import SearchStore  # noqa: PLC0415
    from archon_search.sync import SearchCollectionSync, SyncResult  # noqa: PLC0415

    sync_calls: list[list[str]] = []

    async def recording_sync(self: SearchCollectionSync, collections: list[str], progress_cb=None) -> SyncResult:
        sync_calls.append(list(collections))
        return SyncResult()

    def _rejecting_transition(self, job_id, from_statuses, to_status):
        return None

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = ["docs"]

    with ExitStack() as stack:
        _enter_startup_sync_lifespan_patches(stack, recording_sync)
        stack.enter_context(patch.object(JobStore, "transition", new=_rejecting_transition))

        app = create_app(cfg, job_store)
        startup_done = asyncio.Event()
        shutdown = asyncio.Event()

        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                startup_done.set()
                await shutdown.wait()

        lifespan_task = asyncio.create_task(run_lifespan())
        try:
            await asyncio.wait_for(startup_done.wait(), timeout=30.0)
            for _ in range(3000):
                if sync_calls:
                    break
                await asyncio.sleep(0.01)
        finally:
            shutdown.set()
            await lifespan_task

    assert sync_calls == [["docs"]], (
        "a rejected QUEUED -> RUNNING transition must not prevent the corpus "
        f"sync from running unmarked; sync_calls={sync_calls!r}"
    )
    jobs = job_store.list()
    assert len(jobs) == 1, f"expected exactly one job to be created; got {jobs!r}"
    assert jobs[0].status != JobStatus.QUEUED, (
        "the startup-sync job was left QUEUED after its RUNNING transition was "
        "rejected — QUEUED is non-terminal, so JobStore._evict_old never "
        f"reclaims it and every such boot leaks another row. Got {jobs[0]!r}"
    )
    assert jobs[0].status == JobStatus.FAILED, (
        f"expected the orphaned job to be marked FAILED; got {jobs[0].status!r}"
    )
    assert jobs[0].error == _STARTUP_SYNC_ERROR_NEVER_STARTED, (
        f"expected the sanitized orphan error; got {jobs[0].error!r}"
    )


# --------------------------------------------------------------------------- #
# fix-brief-C item 5 — _run_startup_sync must be a module-level function, not
# a closure captured inside create_app's lifespan. The closure directly caused
# the app.py boundary bug (a shutdown mid-sync misclassified), and hoisting
# shrinks the diff's own risk surface by making every dependency an explicit
# parameter instead of a hidden capture.
# --------------------------------------------------------------------------- #
def test_run_startup_sync_hoisted_to_module_level() -> None:
    import inspect  # noqa: PLC0415

    from archon_search.server import app as app_module  # noqa: PLC0415

    fn = getattr(app_module, "_run_startup_sync", None)
    assert fn is not None, (
        "_run_startup_sync is not a module-level attribute of archon_search.server.app "
        "— it must be hoisted out of create_app's lifespan closure"
    )
    sig = inspect.signature(fn)
    assert list(sig.parameters) == ["job_store", "app_state", "cols"], (
        "_run_startup_sync must take (job_store, app_state, cols) as explicit "
        f"parameters rather than capturing them via closure; got signature {sig}"
    )


# --------------------------------------------------------------------------- #
# 2026-08-20 — fix-brief-A item 5 & item 7 (bullet 4): routes_sync._sync_task
# must not leak str(exc) into the wire-facing SyncJob ``error`` field
# (CLAUDE.md hard invariant), and the SUPPRESSED->DONE ``sync_result``
# transition must be single-direction — a clean sync clears it, but nothing
# else rewrites ``sync_result``. Direct unit tests against ``_sync_task``,
# no lifespan needed.
# --------------------------------------------------------------------------- #
def _make_running_sync_job(job_store: JobStore):
    job = job_store.create_sync(namespace="default")
    running = job_store.transition(job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)
    assert running is not None, "test setup is wrong — QUEUED -> RUNNING transition failed"
    return running


@pytest.mark.asyncio
async def test_sync_task_generic_exception_records_sanitized_error_not_raw_message(
    job_store: JobStore,
) -> None:
    """Pins the CLAUDE.md invariant directly for the manual-sync path: an
    exception message carrying a recognizable token must not reach the
    wire-facing job ``error`` field.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    from archon_search.server.routes_sync import _SYNC_TASK_ERROR_FAILED, _sync_task  # noqa: PLC0415

    _SECRET_TOKEN = "sk-live-do-not-leak-4f8a9c21"
    running_job = _make_running_sync_job(job_store)
    lock = asyncio.Lock()
    await lock.acquire()

    class _ExplodingSync:
        async def sync(self, collections: list[str], progress_cb=None):
            raise RuntimeError(f"internal failure, token={_SECRET_TOKEN}")

    await _sync_task(
        job=running_job,
        job_store=job_store,
        collection_sync=_ExplodingSync(),
        collections=["docs"],
        lock=lock,
        app_state=SimpleNamespace(sync_result=None),
    )

    job = job_store.get(running_job.job_id)
    assert job.status == JobStatus.FAILED
    assert job.error == _SYNC_TASK_ERROR_FAILED, (
        f"expected job.error == {_SYNC_TASK_ERROR_FAILED!r}; got {job.error!r}"
    )
    assert _SECRET_TOKEN not in (job.error or ""), (
        f"the raw exception message leaked into the wire-facing job error: {job.error!r}"
    )
    assert not lock.locked(), "_sync_task must release the lock in its finally block"


# --------------------------------------------------------------------------- #
# fix-brief-C item 3 — _sync_task must not swallow asyncio.CancelledError (a
# BaseException, not caught by "except Exception") into a false crash marker.
# A clean shutdown during a manual POST /sync used to leave the job RUNNING,
# which JobStore._load() rewrites to FAILED/"process_restart" on the next
# boot — a false arm of the crash-loop guard for a shutdown that was never a
# crash. Under sticky suppression (item 1) that false arm now persists until a
# manual sync instead of clearing on the next boot, so this is Critical.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_sync_task_cancelled_records_cancelled_not_running(
    job_store: JobStore,
) -> None:
    """Cancelling ``_sync_task`` mid-sync must record ``CANCELLED``, release the
    lock, and must NOT arm the crash-loop guard on a fresh load of the same
    jobs file (that would happen if the job were left ``RUNNING``).
    """
    from types import SimpleNamespace  # noqa: PLC0415

    from archon_search.server.routes_sync import _sync_task  # noqa: PLC0415

    running_job = _make_running_sync_job(job_store)
    lock = asyncio.Lock()
    await lock.acquire()

    entered = asyncio.Event()

    class _ParkedSync:
        async def sync(self, collections: list[str], progress_cb=None):
            entered.set()
            await asyncio.Event().wait()  # never resolves; cancelled from outside

    task = asyncio.create_task(
        _sync_task(
            job=running_job,
            job_store=job_store,
            collection_sync=_ParkedSync(),
            collections=["docs"],
            lock=lock,
            app_state=SimpleNamespace(sync_result=None),
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    job = job_store.get(running_job.job_id)
    assert job.status == JobStatus.CANCELLED, (
        f"a cancelled _sync_task must record CANCELLED, not leave the job "
        f"{job.status.value!r} — a RUNNING row would be rewritten to "
        "FAILED/'process_restart' on the next boot, falsely arming the "
        "crash-loop guard for an ordinary shutdown"
    )
    assert not lock.locked(), "_sync_task must release the lock even when cancelled"

    # The job store's own persisted view: reload it fresh, exactly as the next
    # boot would, and confirm the crash-loop guard does NOT arm.
    reloaded = JobStore(path=job_store._path)
    assert reloaded.crashed_ingest_on_load is False, (
        "a cancelled manual sync left a marker that arms the crash-loop guard "
        "on the next boot — an ordinary clean shutdown must not look like a crash"
    )


@pytest.mark.asyncio
async def test_sync_task_clean_sync_does_not_rewrite_non_suppressed_sync_result(
    job_store: JobStore,
) -> None:
    """Negative half of the (now generalized) single-transition rule: a clean
    manual sync must rewrite ``sync_result`` ONLY when it started at
    ``SUPPRESSED`` or ``FAILED``. ``None`` (never run), ``PENDING``, and
    ``DONE`` must all be left untouched — ``sync_result`` is the *startup*
    sync's record.

    ``FAILED`` is deliberately NOT in this negative list (fix-brief-C item 6 /
    C1-B-4 generalized the rule from "SUPPRESSED -> DONE only" to "SUPPRESSED
    or FAILED -> DONE"); its positive coverage lives in
    ``test_sync_task_clean_sync_clears_symmetric_degraded_state`` below.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    from archon_search.server.routes_sync import _sync_task  # noqa: PLC0415
    from archon_search.server.schemas import StartupSyncResult  # noqa: PLC0415
    from archon_search.sync import SyncResult  # noqa: PLC0415

    class _CleanSync:
        async def sync(self, collections: list[str], progress_cb=None) -> SyncResult:
            return SyncResult()

    for starting_state in (
        None,
        StartupSyncResult.PENDING,
        StartupSyncResult.DONE,
    ):
        running_job = _make_running_sync_job(job_store)
        lock = asyncio.Lock()
        await lock.acquire()
        app_state = SimpleNamespace(sync_result=starting_state)

        await _sync_task(
            job=running_job,
            job_store=job_store,
            collection_sync=_CleanSync(),
            collections=["docs"],
            lock=lock,
            app_state=app_state,
        )

        assert app_state.sync_result == starting_state, (
            "a clean manual sync rewrote sync_result even though it started at "
            f"{starting_state!r} (only SUPPRESSED/FAILED -> DONE is sanctioned); got "
            f"{app_state.sync_result!r}"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "starting_state",
    ["SUPPRESSED", "FAILED"],
)
async def test_sync_task_clean_sync_clears_symmetric_degraded_state(
    job_store: JobStore, starting_state: str
) -> None:
    """Positive half of the generalized rule (fix-brief-C item 6 / C1-B-4): a
    clean manual sync must move ``sync_result`` to ``DONE`` from EITHER
    ``SUPPRESSED`` or ``FAILED`` — and must clear ``app_state._startup_sync_failed``
    in the same step. The two fields must move together: ``GET /ready`` reads
    ``_startup_sync_failed`` and ``GET /status`` reads ``sync_result``, so
    clearing one without the other would make the two endpoints contradict
    each other.

    Also covers the sticky sentinel (fix-brief-C item 1): a clean sync must
    unlink it, or the NEXT boot would still see it on disk and re-suppress
    despite the operator having just resumed.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    from archon_search.server.routes_sync import _sync_task  # noqa: PLC0415
    from archon_search.server.schemas import StartupSyncResult  # noqa: PLC0415
    from archon_search.sync import SyncResult  # noqa: PLC0415
    from archon_search.sync_suppression import (  # noqa: PLC0415
        get_sync_suppressed_file,
        write_sync_suppressed_sentinel,
    )

    class _CleanSync:
        async def sync(self, collections: list[str], progress_cb=None) -> SyncResult:
            return SyncResult()

    write_sync_suppressed_sentinel()
    assert get_sync_suppressed_file().exists(), "test setup is wrong — sentinel was not written"

    running_job = _make_running_sync_job(job_store)
    lock = asyncio.Lock()
    await lock.acquire()
    app_state = SimpleNamespace(
        sync_result=StartupSyncResult[starting_state],
        _startup_sync_failed=True,
    )

    await _sync_task(
        job=running_job,
        job_store=job_store,
        collection_sync=_CleanSync(),
        collections=["docs"],
        lock=lock,
        app_state=app_state,
    )

    assert app_state.sync_result == StartupSyncResult.DONE, (
        f"a clean manual sync starting at {starting_state} must move sync_result "
        f"to DONE; got {app_state.sync_result!r}"
    )
    assert app_state._startup_sync_failed is False, (
        "a clean manual sync moved sync_result to DONE but left "
        "_startup_sync_failed True — /ready and /status now contradict each other"
    )
    assert not get_sync_suppressed_file().exists(), (
        "a clean manual sync did not clear the sticky sentinel file — the next "
        "boot would still see it on disk and re-suppress"
    )


@pytest.mark.asyncio
async def test_sync_task_with_errors_does_not_clear_suppressed(job_store: JobStore) -> None:
    """The other negative half: a manual sync that completes with non-empty
    ``result.errors`` (no exception) must NOT clear a ``SUPPRESSED``
    ``sync_result`` — only a clean sync (no errors) is the sanctioned resume path.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    from archon_search.server.routes_sync import _sync_task  # noqa: PLC0415
    from archon_search.server.schemas import StartupSyncResult  # noqa: PLC0415
    from archon_search.sync import SyncResult  # noqa: PLC0415

    running_job = _make_running_sync_job(job_store)
    lock = asyncio.Lock()
    await lock.acquire()

    class _PartialSync:
        async def sync(self, collections: list[str], progress_cb=None) -> SyncResult:
            return SyncResult(errors=["collection 'docs': path does not exist"])

    app_state = SimpleNamespace(sync_result=StartupSyncResult.SUPPRESSED)
    await _sync_task(
        job=running_job,
        job_store=job_store,
        collection_sync=_PartialSync(),
        collections=["docs"],
        lock=lock,
        app_state=app_state,
    )
    assert app_state.sync_result == StartupSyncResult.SUPPRESSED, (
        "a manual sync that completed with per-collection errors must not "
        f"clear SUPPRESSED; got {app_state.sync_result!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "starting_state",
    ["SUPPRESSED", "FAILED"],
)
async def test_sync_task_with_errors_clears_neither_field_under_new_rule(
    job_store: JobStore, starting_state: str
) -> None:
    """Negative half of the generalized rule (fix-brief-C item 6e-2), written
    against the NEW rule from C1-B-4 — not the old SUPPRESSED-only docstring.

    A manual sync that completes with non-empty ``result.errors`` (no
    exception) must clear NEITHER ``sync_result`` NOR
    ``app_state._startup_sync_failed`` — starting from either degraded state
    (``SUPPRESSED`` or, newly, ``FAILED``). Only a clean sync (no errors) is
    the sanctioned resume path; the sibling test above already covers
    SUPPRESSED without ``_startup_sync_failed`` — this one additionally covers
    FAILED and pins that the symmetric flag stays put too.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    from archon_search.server.routes_sync import _sync_task  # noqa: PLC0415
    from archon_search.server.schemas import StartupSyncResult  # noqa: PLC0415
    from archon_search.sync import SyncResult  # noqa: PLC0415

    running_job = _make_running_sync_job(job_store)
    lock = asyncio.Lock()
    await lock.acquire()

    class _PartialSync:
        async def sync(self, collections: list[str], progress_cb=None) -> SyncResult:
            return SyncResult(errors=["collection 'docs': path does not exist"])

    starting = StartupSyncResult[starting_state]
    app_state = SimpleNamespace(sync_result=starting, _startup_sync_failed=True)
    await _sync_task(
        job=running_job,
        job_store=job_store,
        collection_sync=_PartialSync(),
        collections=["docs"],
        lock=lock,
        app_state=app_state,
    )
    assert app_state.sync_result == starting, (
        f"a manual sync that completed with per-collection errors must not clear "
        f"{starting_state}; got {app_state.sync_result!r}"
    )
    assert app_state._startup_sync_failed is True, (
        "a manual sync that completed with per-collection errors must not clear "
        f"_startup_sync_failed; got {app_state._startup_sync_failed!r}"
    )


# --------------------------------------------------------------------------- #
# 2026-08-20 — fix-brief-A item 6: ``_KIND_TYPE_MAP`` (routes_jobs.py) was
# missing "sync", so ``kind_types`` ended up empty for a ``?kind=sync``
# filter and every job was silently dropped.
# --------------------------------------------------------------------------- #
def test_get_jobs_kind_sync_filter_returns_sync_jobs(
    tmp_path: Path, job_store: JobStore, auth_headers: dict[str, str]
) -> None:
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from archon_search.server.app import create_app  # noqa: PLC0415

    sync_job = job_store.create_sync(namespace="default")
    ingest_job = job_store.create(collection="docs")

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, job_store)
    client = TestClient(app, headers=auth_headers)

    response = client.get("/jobs", params={"kind": "sync"})
    assert response.status_code == 200
    body = response.json()
    job_ids = [item["job_id"] for item in body["items"]]
    assert sync_job.job_id in job_ids, (
        f"GET /jobs?kind=sync did not return the sync job; got job_ids={job_ids!r}"
    )
    assert ingest_job.job_id not in job_ids, (
        f"GET /jobs?kind=sync must not return non-sync jobs; got job_ids={job_ids!r}"
    )


# --------------------------------------------------------------------------- #
# 2026-08-20 — fix-brief-A item 2: ``_finish_startup_sync_job``'s docstring
# says "Never raises into the task." but the catch set was only
# ``(KeyError, OSError)``.
# --------------------------------------------------------------------------- #
def test_finish_startup_sync_job_never_raises_into_task() -> None:
    from archon_search.server.app import _finish_startup_sync_job  # noqa: PLC0415

    class _ExplodingStore:
        def update(self, job_id, **fields):
            raise RuntimeError("unexpected store failure — not KeyError or OSError")

    # Must not raise — the docstring's promise, now backed by except Exception.
    _finish_startup_sync_job(_ExplodingStore(), "job-1", status=JobStatus.DONE)


# --------------------------------------------------------------------------- #
# fix-brief-C item 6b — DELETE /jobs/{id} on the startup-sync job must return
# 409, not transition it to CANCELLING. Nothing actually cancels the lifespan
# task that runs it, so the sync runs on and _finish_startup_sync_job
# overwrites the status — the operator's cancel would be silently ignored.
# Worse, CANCELLING is a crash status (jobs/store.py _CRASH_STATUSES), so
# leaving the row there would arm the (sticky, since item 1) crash-loop guard
# on the next boot for an operator action that was never a crash.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_delete_startup_sync_job_returns_409(job_store: JobStore) -> None:
    import json  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    from fastapi import Response  # noqa: PLC0415
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    from archon_search.server.routes_jobs import (  # noqa: PLC0415
        _STARTUP_SYNC_JOB_NOT_CANCELLABLE_DETAIL,
        delete_job,
    )

    job = job_store.create_sync(namespace="default")
    running = job_store.transition(job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)
    assert running is not None, "test setup is wrong"

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(job_store=job_store, _startup_sync_job_id=job.job_id)
        ),
        state=SimpleNamespace(namespace="default"),
    )

    result = await delete_job(job.job_id, request, Response())

    assert isinstance(result, JSONResponse), f"expected a JSONResponse; got {result!r}"
    assert result.status_code == 409, (
        f"DELETE on the startup-sync job must return 409; got {result.status_code}"
    )
    body = json.loads(result.body)
    assert body["detail"] == _STARTUP_SYNC_JOB_NOT_CANCELLABLE_DETAIL, (
        f"expected the sanitized detail constant; got {body!r}"
    )

    reloaded = job_store.get(job.job_id)
    assert reloaded.status == JobStatus.RUNNING, (
        "DELETE on the startup-sync job must not transition it — a CANCELLING "
        f"row would arm the crash-loop guard on the next boot; got {reloaded.status!r}"
    )


@pytest.mark.asyncio
async def test_delete_non_startup_sync_job_still_cancels_normally(job_store: JobStore) -> None:
    """Pins that the startup-sync guard clause does not over-match: a manual
    ``SyncJob`` — same job TYPE, different job_id — must still cancel normally.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    from fastapi import Response  # noqa: PLC0415

    from archon_search.server.routes_jobs import delete_job  # noqa: PLC0415

    startup_job = job_store.create_sync(namespace="default")
    job_store.transition(startup_job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)

    manual_job = job_store.create_sync(namespace="default")
    running_manual = job_store.transition(manual_job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)
    assert running_manual is not None, "test setup is wrong"

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(job_store=job_store, _startup_sync_job_id=startup_job.job_id)
        ),
        state=SimpleNamespace(namespace="default"),
    )

    response_obj = Response()
    await delete_job(manual_job.job_id, request, response_obj)

    assert response_obj.status_code == 202, (
        f"a non-startup-sync job must still cancel normally; got {response_obj.status_code}"
    )
    reloaded = job_store.get(manual_job.job_id)
    assert reloaded.status == JobStatus.CANCELLING, (
        f"a non-startup-sync job DELETE must transition to CANCELLING; got {reloaded.status!r}"
    )
