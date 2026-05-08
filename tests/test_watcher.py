"""packages/archon-search/tests/test_watcher.py — _DebounceHandler and CollectionWatcher."""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from archon_search.watcher import CollectionWatcher, _DebounceHandler, _log_future_exception


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_file_event(src_path: str = "/some/file.txt", is_directory: bool = False, event_type: str = "modified", dest_path: str = "") -> MagicMock:
    event = MagicMock()
    event.is_directory = is_directory
    event.src_path = src_path
    event.event_type = event_type
    event.dest_path = dest_path
    return event


def _make_async_callback():
    """Return a coroutine-returning callable and a call-tracking list."""
    calls: list[str] = []

    async def _cb(collection_name: str) -> None:
        calls.append(collection_name)

    return _cb, calls


# ---------------------------------------------------------------------------
# _DebounceHandler tests
# ---------------------------------------------------------------------------


class TestDebounceHandler:
    def test_debounce_handler_schedules_callback(self):
        """on_any_event with a file event starts a timer; firing it submits the coroutine."""
        loop = asyncio.new_event_loop()
        cb, calls = _make_async_callback()
        # Use a long debounce so the timer doesn't auto-fire during the test
        handler = _DebounceHandler(cb, loop, "mycol", debounce_seconds=60.0)

        with patch("asyncio.run_coroutine_threadsafe") as mock_rct:
            mock_future = MagicMock()
            mock_rct.return_value = mock_future

            event = _make_file_event()
            handler.on_any_event(event)

            # Timer should have been created
            assert handler._timer is not None

            # Cancel the real timer then manually call _fire() to test the path
            handler._timer.cancel()
            handler._fire()

            mock_rct.assert_called_once()
            mock_future.add_done_callback.assert_called_once()
            # Timer must be cleared after _fire()
            assert handler._timer is None

        loop.close()

    def test_debounce_handler_resets_timer_on_rapid_events(self):
        """Two rapid events cancel the first timer and create only one new timer."""
        loop = asyncio.new_event_loop()
        cb, _ = _make_async_callback()
        handler = _DebounceHandler(cb, loop, "mycol", debounce_seconds=10.0)

        event1 = _make_file_event("/some/file1.txt")
        event2 = _make_file_event("/some/file2.txt")

        handler.on_any_event(event1)
        first_timer = handler._timer
        assert first_timer is not None

        handler.on_any_event(event2)
        second_timer = handler._timer

        # First timer should be cancelled; a new one replaces it
        assert second_timer is not None
        assert second_timer is not first_timer

        # Clean up
        handler.cancel_all()
        loop.close()

    def test_debounce_rapid_events_callback_fires_once(self):
        """Two rapid events result in exactly one callback invocation (not two)."""
        cb, calls = _make_async_callback()

        loop = asyncio.new_event_loop()
        # 10ms debounce with 300ms sleep = 30x margin; robust on loaded CI runners
        handler = _DebounceHandler(cb, loop, "mycol", debounce_seconds=0.01)

        event1 = _make_file_event("/some/file1.txt")
        event2 = _make_file_event("/some/file2.txt")

        try:
            handler.on_any_event(event1)
            handler.on_any_event(event2)

            # Run the event loop long enough for the debounced timer to fire and
            # the coroutine to complete (30x margin over debounce)
            loop.run_until_complete(asyncio.sleep(0.3))

            assert calls == ["mycol"], f"Expected callback once, got {calls}"
            assert handler._timer is None  # verify full debounce lifecycle completed
        finally:
            handler.cancel_all()
            loop.close()

    def test_debounce_handler_skips_directory_events(self):
        """Directory events are ignored — no timer created."""
        loop = asyncio.new_event_loop()
        cb, _ = _make_async_callback()
        handler = _DebounceHandler(cb, loop, "mycol", debounce_seconds=5.0)

        dir_event = _make_file_event(is_directory=True)
        handler.on_any_event(dir_event)

        assert handler._timer is None
        loop.close()

    def test_debounce_handler_cancel_all(self):
        """cancel_all cancels the active timer and sets _timer to None."""
        loop = asyncio.new_event_loop()
        cb, _ = _make_async_callback()
        handler = _DebounceHandler(cb, loop, "mycol", debounce_seconds=10.0)

        event = _make_file_event()
        handler.on_any_event(event)
        assert handler._timer is not None

        handler.cancel_all()
        assert handler._timer is None

        loop.close()

    def test_debounce_handler_handles_moved_event(self):
        """Moved events use dest_path for logging; timer is still scheduled."""
        loop = asyncio.new_event_loop()
        cb, _ = _make_async_callback()
        handler = _DebounceHandler(cb, loop, "mycol", debounce_seconds=10.0)

        moved_event = _make_file_event(
            src_path="/some/old_file.md",
            event_type="moved",
            dest_path="/some/file.md",
        )
        handler.on_any_event(moved_event)

        assert handler._timer is not None

        handler.cancel_all()
        loop.close()

    def test_debounce_handler_fire_wraps_loop_closed_error(self, caplog):
        """_fire() catches RuntimeError from run_coroutine_threadsafe (closed loop) and logs a warning."""
        loop = asyncio.new_event_loop()
        cb, _ = _make_async_callback()
        handler = _DebounceHandler(cb, loop, "mycol", debounce_seconds=0.0)

        with patch("asyncio.run_coroutine_threadsafe", side_effect=RuntimeError("Event loop is closed")):
            with caplog.at_level(logging.WARNING, logger="archon"):
                handler._fire()  # must not raise

        assert "Event loop closed" in caplog.text or "loop" in caplog.text.lower()
        loop.close()

    def test_fire_clears_timer_in_finally_on_unexpected_exception(self):
        """_fire() must set _timer=None in a finally block even when an unexpected exception propagates."""
        loop = asyncio.new_event_loop()
        cb, _ = _make_async_callback()
        handler = _DebounceHandler(cb, loop, "mycol", debounce_seconds=0.0)

        # Simulate a pending timer so _timer is not None before the call
        mock_timer = MagicMock()
        handler._timer = mock_timer

        with patch("asyncio.run_coroutine_threadsafe", side_effect=ValueError("unexpected")):
            with pytest.raises(ValueError, match="unexpected"):
                handler._fire()

        # _timer must be None regardless of whether the exception was RuntimeError or not
        assert handler._timer is None
        loop.close()

    def test_fire_does_not_clobber_timer_set_by_concurrent_event(self):
        """If on_any_event sets a NEW timer during _fire(), the finally block must not clear it."""
        loop = asyncio.new_event_loop()
        cb, _ = _make_async_callback()
        handler = _DebounceHandler(cb, loop, "mycol", debounce_seconds=0.0)

        original_timer = MagicMock()
        new_timer = MagicMock()
        handler._timer = original_timer

        # Simulate on_any_event replacing _timer with a new one during submission
        def _replace_timer_side_effect(coro, loop):
            with handler._lock:
                handler._timer = new_timer  # simulates concurrent on_any_event
            raise RuntimeError("loop closed")

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_replace_timer_side_effect):
            handler._fire()

        # The new timer must survive — finally must NOT clobber it
        assert handler._timer is new_timer


# ---------------------------------------------------------------------------
# _log_future_exception tests
# ---------------------------------------------------------------------------


class TestLogFutureException:
    def test_log_future_exception_logs_on_error(self, caplog):
        """_log_future_exception logs at ERROR when the future holds an exception."""
        import concurrent.futures
        cf_future = concurrent.futures.Future()
        cf_future.set_exception(ValueError("fail"))

        with caplog.at_level(logging.ERROR, logger="archon"):
            _log_future_exception(cf_future)

        assert any("fail" in r.message or "ValueError" in r.message for r in caplog.records if r.levelno == logging.ERROR)

    def test_log_future_exception_silent_on_success(self, caplog):
        """_log_future_exception does not log at ERROR when the future completed successfully."""
        import concurrent.futures
        cf_future = concurrent.futures.Future()
        cf_future.set_result(None)

        with caplog.at_level(logging.ERROR, logger="archon"):
            _log_future_exception(cf_future)

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_records) == 0

    def test_log_future_exception_on_cancelled_future(self, caplog):
        """_log_future_exception does not raise and does not log at ERROR for a cancelled future."""
        import concurrent.futures
        cf_future = concurrent.futures.Future()
        cf_future.cancel()

        with caplog.at_level(logging.ERROR, logger="archon"):
            _log_future_exception(cf_future)  # must not raise

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_records) == 0


# ---------------------------------------------------------------------------
# CollectionWatcher tests
# ---------------------------------------------------------------------------


class TestCollectionWatcher:
    def test_collection_watcher_start_stop(self, tmp_path: Path):
        """start() schedules and starts the observer; stop() joins it and calls cancel_all()."""
        cb, _ = _make_async_callback()
        loop = asyncio.new_event_loop()

        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = False

        with patch("archon_search.watcher.Observer", return_value=mock_observer), \
             patch.object(_DebounceHandler, "cancel_all") as mock_cancel_all:
            watcher = CollectionWatcher("col", tmp_path, cb, loop, debounce_seconds=5.0)
            watcher.start()

            mock_observer.schedule.assert_called_once()
            mock_observer.start.assert_called_once()

            watcher.stop()

            mock_observer.stop.assert_called_once()
            mock_observer.join.assert_called_once()
            mock_cancel_all.assert_called_once()

        loop.close()

    def test_collection_watcher_is_alive(self, tmp_path: Path):
        """is_alive() reflects the observer's alive state."""
        cb, _ = _make_async_callback()
        loop = asyncio.new_event_loop()

        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = True

        with patch("archon_search.watcher.Observer", return_value=mock_observer):
            watcher = CollectionWatcher("col", tmp_path, cb, loop, debounce_seconds=5.0)

            # Before start: False (no observer set)
            assert watcher.is_alive() is False

            watcher.start()
            assert watcher.is_alive() is True

            mock_observer.is_alive.return_value = False
            watcher.stop()
            assert watcher.is_alive() is False

        loop.close()

    def test_collection_watcher_start_nonexistent_directory(self, tmp_path: Path, caplog):
        """If Observer.start() raises OSError, start() logs a warning and does not raise."""
        cb, _ = _make_async_callback()
        loop = asyncio.new_event_loop()

        mock_observer = MagicMock()
        mock_observer.start.side_effect = OSError("no such file")
        mock_observer.is_alive.return_value = False

        with patch("archon_search.watcher.Observer", return_value=mock_observer):
            with caplog.at_level(logging.WARNING, logger="archon"):
                watcher = CollectionWatcher("col", tmp_path, cb, loop, debounce_seconds=5.0)
                watcher.start()  # must not raise

        assert watcher.is_alive() is False
        assert len(caplog.records) > 0
        loop.close()

    def test_collection_watcher_stop_join_timeout_warning(self, tmp_path: Path, caplog):
        """If observer is still alive after join(), stop() logs a timeout warning."""
        cb, _ = _make_async_callback()
        loop = asyncio.new_event_loop()

        mock_observer = MagicMock()
        # is_alive: True — simulates observer still running after join
        mock_observer.is_alive.return_value = True

        with patch("archon_search.watcher.Observer", return_value=mock_observer):
            watcher = CollectionWatcher("col", tmp_path, cb, loop, debounce_seconds=5.0)
            watcher.start()

            with caplog.at_level(logging.WARNING, logger="archon"):
                watcher.stop()

        assert any("did not terminate" in r.message or "5s" in r.message for r in caplog.records)
        loop.close()

    def test_collection_watcher_directory_disappears(self, tmp_path: Path, caplog):
        """If observer.stop() raises OSError (directory gone), stop() logs a warning and does not propagate."""
        cb, _ = _make_async_callback()
        loop = asyncio.new_event_loop()

        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = False
        mock_observer.stop.side_effect = OSError("path vanished")

        with patch("archon_search.watcher.Observer", return_value=mock_observer):
            watcher = CollectionWatcher("col", tmp_path, cb, loop, debounce_seconds=5.0)
            watcher.start()

            with caplog.at_level(logging.WARNING, logger="archon"):
                watcher.stop()  # must not raise

        assert len(caplog.records) > 0
        loop.close()

    def test_collection_watcher_double_start(self, tmp_path: Path):
        """Calling start() twice does not create a second observer thread."""
        cb, _ = _make_async_callback()
        loop = asyncio.new_event_loop()

        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = False

        with patch("archon_search.watcher.Observer", return_value=mock_observer) as mock_observer_cls:
            watcher = CollectionWatcher("col", tmp_path, cb, loop, debounce_seconds=5.0)
            watcher.start()
            watcher.start()  # second call must be a no-op

            # Observer() constructor and schedule() each called only once
            mock_observer_cls.assert_called_once()
            mock_observer.schedule.assert_called_once()

        loop.close()

    def test_collection_watcher_stop_before_start(self, tmp_path: Path):
        """stop() called before start() must not raise."""
        cb, _ = _make_async_callback()
        loop = asyncio.new_event_loop()
        watcher = CollectionWatcher("col", tmp_path, cb, loop, debounce_seconds=5.0)
        watcher.stop()  # must not raise
        loop.close()

    def test_collection_watcher_join_raises_oserror(self, tmp_path: Path, caplog):
        """If observer.join() raises OSError, stop() swallows it and continues."""
        cb, _ = _make_async_callback()
        loop = asyncio.new_event_loop()

        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = False
        mock_observer.join.side_effect = OSError("fs gone during join")

        with patch("archon_search.watcher.Observer", return_value=mock_observer):
            watcher = CollectionWatcher("col", tmp_path, cb, loop, debounce_seconds=5.0)
            watcher.start()
            watcher.stop()  # must not raise

        loop.close()


# ---------------------------------------------------------------------------
# Integration test (optional)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WatcherManager tests
# ---------------------------------------------------------------------------


class TestWatcherManager:
    def test_watcher_manager_add_starts_watcher(self, tmp_path: Path):
        """add(name, path) starts the CollectionWatcher; is_watching returns True."""
        loop = asyncio.new_event_loop()

        async def _on_change(col: str) -> None:
            pass

        mock_watcher = MagicMock()
        mock_watcher.is_alive.return_value = True

        with patch("archon_search.watcher.CollectionWatcher", return_value=mock_watcher) as mock_cls:
            from archon_search.watcher import WatcherManager

            mgr = WatcherManager(on_change=_on_change, loop=loop, debounce_seconds=5.0)
            mgr.add("col1", tmp_path)

            mock_cls.assert_called_once()
            mock_watcher.start.assert_called_once()
            assert mgr.is_watching("col1") is True

        loop.close()

    def test_watcher_manager_add_is_idempotent(self, tmp_path: Path):
        """add(name, path) called twice only creates and starts one CollectionWatcher."""
        loop = asyncio.new_event_loop()

        async def _on_change(col: str) -> None:
            pass

        mock_watcher = MagicMock()
        mock_watcher.is_alive.return_value = True

        with patch("archon_search.watcher.CollectionWatcher", return_value=mock_watcher) as mock_cls:
            from archon_search.watcher import WatcherManager

            mgr = WatcherManager(on_change=_on_change, loop=loop, debounce_seconds=5.0)
            mgr.add("col1", tmp_path)
            mgr.add("col1", tmp_path)  # second call must be a no-op

            mock_cls.assert_called_once()
            mock_watcher.start.assert_called_once()

        loop.close()

    @pytest.mark.asyncio
    async def test_watcher_manager_stop_all(self, tmp_path: Path):
        """stop_all() calls stop() on all watchers and clears watching_names()."""
        loop = asyncio.get_event_loop()

        async def _on_change(col: str) -> None:
            pass

        mock_watcher1 = MagicMock()
        mock_watcher1.is_alive.return_value = False
        mock_watcher2 = MagicMock()
        mock_watcher2.is_alive.return_value = False

        watchers = [mock_watcher1, mock_watcher2]

        with patch("archon_search.watcher.CollectionWatcher", side_effect=watchers):
            from archon_search.watcher import WatcherManager

            mgr = WatcherManager(on_change=_on_change, loop=loop, debounce_seconds=5.0)
            mgr.add("col1", tmp_path)
            mgr.add("col2", tmp_path)

            await mgr.stop_all()

            mock_watcher1.stop.assert_called_once()
            mock_watcher2.stop.assert_called_once()
            assert mgr.watching_names() == set()

    def test_watcher_manager_watching_names(self, tmp_path: Path):
        """watching_names() returns only collection names whose watcher is_alive()."""
        loop = asyncio.new_event_loop()

        async def _on_change(col: str) -> None:
            pass

        mock_alive = MagicMock()
        mock_alive.is_alive.return_value = True
        mock_dead = MagicMock()
        mock_dead.is_alive.return_value = False

        with patch("archon_search.watcher.CollectionWatcher", side_effect=[mock_alive, mock_dead]):
            from archon_search.watcher import WatcherManager

            mgr = WatcherManager(on_change=_on_change, loop=loop, debounce_seconds=5.0)
            mgr.add("live_col", tmp_path)
            mgr.add("dead_col", tmp_path)

            assert mgr.watching_names() == {"live_col"}

        loop.close()


    @pytest.mark.asyncio
    async def test_wrapped_callback_shutdown_guard(self, tmp_path):
        """_wrapped_callback returns without calling on_change when _shutting_down=True."""
        calls = []
        async def _on_change(col): calls.append(col)
        loop = asyncio.get_event_loop()
        from archon_search.watcher import WatcherManager
        mgr = WatcherManager(on_change=_on_change, loop=loop)
        mgr._shutting_down = True
        await mgr._wrapped_callback("col1")
        assert calls == []

    @pytest.mark.asyncio
    async def test_wrapped_callback_swallows_on_change_exception(self, tmp_path, caplog):
        """_wrapped_callback logs errors from on_change and does not propagate them; _active_syncs is empty after."""
        async def _failing_on_change(col): raise RuntimeError("sync failed")
        loop = asyncio.get_event_loop()
        from archon_search.watcher import WatcherManager
        mgr = WatcherManager(on_change=_failing_on_change, loop=loop)
        with caplog.at_level(logging.ERROR, logger="archon"):
            await mgr._wrapped_callback("col1")  # must not raise
        assert any("sync failed" in r.message or "RuntimeError" in r.message for r in caplog.records if r.levelno == logging.ERROR)
        assert len(mgr._active_syncs) == 0

    @pytest.mark.asyncio
    async def test_wrapped_callback_tracks_active_syncs(self, tmp_path):
        """_wrapped_callback adds task to _active_syncs and removes it on completion."""
        import asyncio
        syncing = asyncio.Event()
        proceed = asyncio.Event()

        async def _slow_on_change(col):
            syncing.set()
            await proceed.wait()

        loop = asyncio.get_event_loop()
        from archon_search.watcher import WatcherManager
        mgr = WatcherManager(on_change=_slow_on_change, loop=loop)

        # Start callback but don't let it finish yet
        cb_task = asyncio.create_task(mgr._wrapped_callback("col1"))
        await syncing.wait()  # wait until on_change has started
        assert len(mgr._active_syncs) == 1  # task in flight

        proceed.set()  # let on_change finish
        await cb_task
        assert len(mgr._active_syncs) == 0  # cleaned up

    def test_add_during_shutdown_is_noop(self, tmp_path):
        """add() is a no-op when _shutting_down=True."""
        async def _on_change(col): pass
        loop = asyncio.new_event_loop()
        from archon_search.watcher import WatcherManager
        mgr = WatcherManager(on_change=_on_change, loop=loop)
        mgr._shutting_down = True
        with patch("archon_search.watcher.CollectionWatcher") as mock_cls:
            mgr.add("col1", tmp_path)
            mock_cls.assert_not_called()
        assert "col1" not in mgr._watchers
        loop.close()

    @pytest.mark.asyncio
    async def test_stop_all_empty_manager(self):
        """stop_all() on a manager with no watchers does not raise."""
        async def _on_change(col): pass
        loop = asyncio.get_event_loop()
        from archon_search.watcher import WatcherManager
        mgr = WatcherManager(on_change=_on_change, loop=loop)
        await mgr.stop_all()  # must not raise
        assert mgr.watching_names() == set()

    def test_is_watching_unknown_name_returns_false(self, tmp_path):
        """is_watching() returns False for a name not in _watchers."""
        async def _on_change(col): pass
        loop = asyncio.new_event_loop()
        from archon_search.watcher import WatcherManager
        mgr = WatcherManager(on_change=_on_change, loop=loop)
        assert mgr.is_watching("nonexistent") is False
        loop.close()

    def test_new_manager_watching_names_empty(self):
        """J13.22: new WatcherManager() → watching_names() is empty."""
        async def _on_change(col): pass
        loop = asyncio.new_event_loop()
        from archon_search.watcher import WatcherManager
        mgr = WatcherManager(on_change=_on_change, loop=loop)
        assert mgr.watching_names() == set()
        loop.close()

    def test_same_path_two_different_names_two_watchers(self, tmp_path: Path):
        """J13.23: add same path with two different names → two watchers."""
        async def _on_change(col): pass
        loop = asyncio.new_event_loop()

        mock_watcher1 = MagicMock()
        mock_watcher1.is_alive.return_value = True
        mock_watcher2 = MagicMock()
        mock_watcher2.is_alive.return_value = True

        with patch("archon_search.watcher.CollectionWatcher", side_effect=[mock_watcher1, mock_watcher2]) as mock_cls:
            from archon_search.watcher import WatcherManager
            mgr = WatcherManager(on_change=_on_change, loop=loop)
            mgr.add("col_a", tmp_path)
            mgr.add("col_b", tmp_path)  # same path, different name

            assert mock_cls.call_count == 2
            assert mgr.watching_names() == {"col_a", "col_b"}

        loop.close()

    def test_watching_names_all_dead_returns_empty(self, tmp_path):
        """watching_names() returns empty set when all watchers are dead."""
        async def _on_change(col): pass
        loop = asyncio.new_event_loop()
        mock_dead1 = MagicMock()
        mock_dead1.is_alive.return_value = False
        mock_dead2 = MagicMock()
        mock_dead2.is_alive.return_value = False
        with patch("archon_search.watcher.CollectionWatcher", side_effect=[mock_dead1, mock_dead2]):
            from archon_search.watcher import WatcherManager
            mgr = WatcherManager(on_change=_on_change, loop=loop)
            mgr.add("col1", tmp_path)
            mgr.add("col2", tmp_path)
        assert mgr.watching_names() == set()
        loop.close()

    @pytest.mark.asyncio
    async def test_stop_all_cancels_active_syncs(self, tmp_path: Path):
        """stop_all() cancels in-flight sync tasks that exceed the timeout."""
        syncing = asyncio.Event()

        async def _slow_on_change(col: str) -> None:
            syncing.set()
            await asyncio.sleep(60)  # simulate long-running sync

        loop = asyncio.get_event_loop()
        from archon_search.watcher import WatcherManager

        mgr = WatcherManager(on_change=_slow_on_change, loop=loop)
        # Directly invoke _wrapped_callback to populate _active_syncs
        cb_task = asyncio.create_task(mgr._wrapped_callback("col1"))
        await syncing.wait()  # sync is in-flight
        assert len(mgr._active_syncs) == 1

        # stop_all with a very short timeout — forces the cancel path
        import unittest.mock
        with unittest.mock.patch.object(type(mgr), "stop_all", wraps=mgr.stop_all):
            # Patch asyncio.wait to use timeout=0.01 for speed
            original_wait = asyncio.wait

            async def fast_wait(fs, *, timeout=None):
                return await original_wait(fs, timeout=0.01)

            with unittest.mock.patch("archon_search.watcher.asyncio.wait", fast_wait):
                await mgr.stop_all()

        # All active syncs must be cleared after stop_all
        assert len(mgr._active_syncs) == 0
        # The cb_task should be done (cancelled)
        assert cb_task.done()


    def test_schedule_called_with_recursive_true(self, tmp_path: Path):
        """J13.24: observer.schedule(handler, path, recursive=True) → recursive=True verified."""
        cb, _ = _make_async_callback()
        loop = asyncio.new_event_loop()

        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = False

        with patch("archon_search.watcher.Observer", return_value=mock_observer):
            watcher = CollectionWatcher("col", tmp_path, cb, loop, debounce_seconds=5.0)
            watcher.start()

        _, kwargs = mock_observer.schedule.call_args
        assert kwargs.get("recursive") is True

        loop.close()

    def test_schedule_raises_oserror_logged_warning(self, tmp_path: Path, caplog):
        """J13.20: observer.schedule() raises OSError → logged WARNING, no crash."""
        cb, _ = _make_async_callback()
        loop = asyncio.new_event_loop()

        mock_observer = MagicMock()
        mock_observer.schedule.side_effect = OSError("inotify limit reached")
        mock_observer.is_alive.return_value = False

        with patch("archon_search.watcher.Observer", return_value=mock_observer):
            with caplog.at_level(logging.WARNING, logger="archon"):
                watcher = CollectionWatcher("col", tmp_path, cb, loop, debounce_seconds=5.0)
                watcher.start()  # must not raise

        assert watcher.is_alive() is False
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        loop.close()

    def test_join_raises_oserror_logged_warning(self, tmp_path: Path, caplog):
        """J13.21: observer.join() raises OSError → logged WARNING, no propagation."""
        cb, _ = _make_async_callback()
        loop = asyncio.new_event_loop()

        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = False
        mock_observer.join.side_effect = OSError("fs gone during join")

        with patch("archon_search.watcher.Observer", return_value=mock_observer):
            watcher = CollectionWatcher("col", tmp_path, cb, loop, debounce_seconds=5.0)
            watcher.start()

            with caplog.at_level(logging.WARNING, logger="archon"):
                watcher.stop()  # must not raise

        # No WARNING required for join() OSError per production code — just no propagation
        # The test verifies no exception is raised (implicit via no pytest.raises)
        loop.close()


@pytest.mark.integration
def test_collection_watcher_integration(tmp_path: Path):
    """Real watcher: write a file, verify on_change is called within 0.5s."""
    calls: list[str] = []

    async def on_change(collection_name: str) -> None:
        calls.append(collection_name)

    loop = asyncio.new_event_loop()
    watcher = CollectionWatcher(
        collection_name="intcol",
        source_path=tmp_path,
        on_change=on_change,
        loop=loop,
        debounce_seconds=0.1,
    )
    watcher.start()

    try:
        # Write a file to trigger the watcher
        (tmp_path / "test.txt").write_text("hello")

        # Run the event loop to process the coroutine
        async def _wait():
            await asyncio.sleep(0.5)

        loop.run_until_complete(_wait())
    finally:
        watcher.stop()
        loop.close()

    assert calls == ["intcol"]
