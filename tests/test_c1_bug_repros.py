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
from archon_search.jobs.store import JobStore

pytestmark = pytest.mark.xdist_group("c1_bugs")


@pytest.fixture
def job_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


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
    a local server happens to be up. Here the probe *is* patched (ConnectError),
    so connection refused to the target URL must produce the NOT_RUNNING message
    regardless of local service manager state (S530: no in-process fallback).
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
    assert "not running" in output.lower(), (
        "connection refused to the target URL must report not-running — "
        f"local service manager state must not influence the message (S530). Got: {output!r}"
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
    """``"checks": null`` must be handled, not thrown into the blanket ``except``.

    ``resp.json().get("checks", {}).get("models")`` guards a *missing* key but not
    a ``null`` value: ``None.get(...)`` raises ``AttributeError``, which the
    blanket ``except Exception`` swallows. The probe result is discarded and the
    service-manager fallback is skipped entirely.

    Oracle: with the null handled, the function finds no "pending" signal and
    falls through to ``_get_service()`` — which is never reached today.
    """
    from archon_search.cli import _helpers  # noqa: PLC0415

    resp = MagicMock()
    resp.status_code = 503
    resp.json.return_value = {"ready": False, "checks": None}

    service = MagicMock()
    service.status.return_value.running = True

    with (
        patch.object(_helpers.httpx, "get", return_value=resp),
        patch.object(_helpers, "_get_service", return_value=service) as get_service,
    ):
        msg = _helpers._server_connect_fail_msg("http://127.0.0.1:8765")

    assert get_service.called, (
        "a null `checks` body raised AttributeError inside the probe and was "
        "swallowed by the blanket `except Exception`, so the service-manager "
        "fallback never ran"
    )
    assert msg == _helpers._SERVER_STARTING_MSG, (
        "/ready answered but carried no usable checks, and the service manager "
        f"reports the server as running — expected the 'starting up' hint, got {msg!r}"
    )


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
    ``/ready`` — makes ``.get`` raise ``AttributeError``, which is neither
    ``httpx.HTTPError`` nor ``ValueError`` and escapes the narrowed except,
    aborting the CLI with a traceback on the exact path whose job is to print a
    friendly hint after a connection failure.

    Oracle: with the body type-checked before ``.get`` is called, the probe
    finds no usable ``checks`` and falls through to the service-manager
    fallback, exactly like the null-``checks``-value case above.
    """
    from archon_search.cli import _helpers  # noqa: PLC0415

    resp = MagicMock()
    resp.status_code = 503
    resp.json.return_value = body

    service = MagicMock()
    service.status.return_value.running = True

    with (
        patch.object(_helpers.httpx, "get", return_value=resp),
        patch.object(_helpers, "_get_service", return_value=service) as get_service,
    ):
        msg = _helpers._server_connect_fail_msg("http://127.0.0.1:8765")

    assert get_service.called, (
        f"a non-dict body ({body!r}) raised AttributeError inside the probe and "
        "was NOT caught by the narrowed except, so the service-manager fallback "
        "never ran"
    )
    assert msg == _helpers._SERVER_STARTING_MSG, (
        f"/ready answered with a non-dict body ({body!r}), and the service "
        f"manager reports the server as running — expected the 'starting up' "
        f"hint, got {msg!r}"
    )


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
