"""Tests for archon.rag.watcher — _DebounceHandler and CollectionWatcher."""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from archon.rag.watcher import CollectionWatcher, _DebounceHandler, _log_future_exception


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

        with patch("archon.rag.watcher._WATCHDOG_AVAILABLE", True), \
             patch("archon.rag.watcher.Observer", return_value=mock_observer), \
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

        with patch("archon.rag.watcher._WATCHDOG_AVAILABLE", True), \
             patch("archon.rag.watcher.Observer", return_value=mock_observer):
            watcher = CollectionWatcher("col", tmp_path, cb, loop, debounce_seconds=5.0)

            # Before start: False (no observer set)
            assert watcher.is_alive() is False

            watcher.start()
            assert watcher.is_alive() is True

            mock_observer.is_alive.return_value = False
            watcher.stop()
            assert watcher.is_alive() is False

        loop.close()

    def test_collection_watcher_start_no_watchdog(self, tmp_path: Path, caplog):
        """When _WATCHDOG_AVAILABLE is False, start() logs a warning and does nothing."""
        cb, _ = _make_async_callback()
        loop = asyncio.new_event_loop()

        with patch("archon.rag.watcher._WATCHDOG_AVAILABLE", False):
            with caplog.at_level(logging.WARNING, logger="archon"):
                watcher = CollectionWatcher("col", tmp_path, cb, loop, debounce_seconds=5.0)
                watcher.start()

        assert watcher.is_alive() is False
        assert len(caplog.records) > 0
        loop.close()

    def test_collection_watcher_start_nonexistent_directory(self, tmp_path: Path, caplog):
        """If Observer.start() raises OSError, start() logs a warning and does not raise."""
        cb, _ = _make_async_callback()
        loop = asyncio.new_event_loop()

        mock_observer = MagicMock()
        mock_observer.start.side_effect = OSError("no such file")
        mock_observer.is_alive.return_value = False

        with patch("archon.rag.watcher._WATCHDOG_AVAILABLE", True), \
             patch("archon.rag.watcher.Observer", return_value=mock_observer):
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

        with patch("archon.rag.watcher._WATCHDOG_AVAILABLE", True), \
             patch("archon.rag.watcher.Observer", return_value=mock_observer):
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

        with patch("archon.rag.watcher._WATCHDOG_AVAILABLE", True), \
             patch("archon.rag.watcher.Observer", return_value=mock_observer):
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

        with patch("archon.rag.watcher._WATCHDOG_AVAILABLE", True), \
             patch("archon.rag.watcher.Observer", return_value=mock_observer) as mock_observer_cls:
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

        with patch("archon.rag.watcher._WATCHDOG_AVAILABLE", True), \
             patch("archon.rag.watcher.Observer", return_value=mock_observer):
            watcher = CollectionWatcher("col", tmp_path, cb, loop, debounce_seconds=5.0)
            watcher.start()
            watcher.stop()  # must not raise

        loop.close()


# ---------------------------------------------------------------------------
# Integration test (optional)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_collection_watcher_integration(tmp_path: Path):
    """Real watcher: write a file, verify on_change is called within 0.5s."""
    try:
        from watchdog.observers import Observer  # noqa: F401
    except ImportError:
        pytest.skip("watchdog not installed")

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
