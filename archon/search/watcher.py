"""archon/rag/watcher.py — filesystem watcher for RAG collections.

Uses watchdog to watch a directory and debounce rapid changes before calling
an async sync callback on the asyncio event loop.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Coroutine
from pathlib import Path

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileSystemEvent  # noqa: F401

    _WATCHDOG_AVAILABLE = True
except ImportError:  # pragma: no cover
    _WATCHDOG_AVAILABLE = False
    FileSystemEventHandler = object  # type: ignore[assignment,misc]

    class Observer:  # type: ignore[no-redef]
        """Stub when watchdog is not installed."""

        def schedule(self, *args: object, **kwargs: object) -> None:  # pragma: no cover
            pass

        def start(self) -> None:  # pragma: no cover
            pass

        def stop(self) -> None:  # pragma: no cover
            pass

        def join(self, timeout: float = 0.0) -> None:  # pragma: no cover
            pass

        def is_alive(self) -> bool:  # pragma: no cover
            return False

_log = logging.getLogger("archon")


# ---------------------------------------------------------------------------
# Module-level helper for future exception logging
# ---------------------------------------------------------------------------


def _log_future_exception(future: object) -> None:
    """Done-callback: log at ERROR if the future completed with an exception."""
    if future.cancelled():  # type: ignore[union-attr]
        return
    exc = future.exception()  # type: ignore[union-attr]
    if exc is not None:
        _log.error("Watch-triggered sync raised an exception: %r", exc)


# ---------------------------------------------------------------------------
# Debounce handler
# ---------------------------------------------------------------------------


class _DebounceHandler(FileSystemEventHandler):  # type: ignore[misc]
    """FileSystemEventHandler that debounces rapid events per collection."""

    def __init__(
        self,
        async_callback: Callable[[str], Coroutine],
        loop: asyncio.AbstractEventLoop,
        collection_name: str,
        debounce_seconds: float = 5.0,
    ) -> None:
        super().__init__()
        self._async_callback = async_callback
        self._loop = loop
        self._collection_name = collection_name
        self._debounce_seconds = debounce_seconds
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def on_any_event(self, event: object) -> None:
        """Called by watchdog on any filesystem event."""
        if event.is_directory:  # type: ignore[union-attr]
            return

        path = (
            event.dest_path  # type: ignore[union-attr]
            if event.event_type == "moved"  # type: ignore[union-attr]
            else event.src_path  # type: ignore[union-attr]
        )
        _log.debug("Watcher event for collection %r: %s", self._collection_name, path)

        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_seconds, self._fire)
            self._timer.start()

    def _fire(self) -> None:
        """Called from the timer thread — submit coroutine to the event loop."""
        coro = self._async_callback(self._collection_name)
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            future.add_done_callback(_log_future_exception)
        except RuntimeError:
            coro.close()
            _log.warning(
                "Event loop closed, skipping watch-triggered sync for %r",
                self._collection_name,
            )
        finally:
            with self._lock:
                self._timer = None

    def cancel_all(self) -> None:
        """Cancel any pending debounce timer."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


# ---------------------------------------------------------------------------
# CollectionWatcher
# ---------------------------------------------------------------------------


class CollectionWatcher:
    """Watches a source directory and calls *on_change* when files change."""

    def __init__(
        self,
        collection_name: str,
        source_path: Path,
        on_change: Callable[[str], Coroutine],
        loop: asyncio.AbstractEventLoop,
        debounce_seconds: float = 5.0,
    ) -> None:
        self._collection_name = collection_name
        self._source_path = source_path
        self._on_change = on_change
        self._loop = loop
        self._debounce_seconds = debounce_seconds
        self._handler: _DebounceHandler | None = None
        self._observer: object | None = None  # watchdog Observer or None

    def start(self) -> None:
        """Start watching the source directory."""
        if self._observer is not None:
            return  # already watching

        if not _WATCHDOG_AVAILABLE:
            _log.warning(
                "watchdog is not installed; file watching disabled for collection %r",
                self._collection_name,
            )
            return

        handler = _DebounceHandler(
            self._on_change, self._loop, self._collection_name, self._debounce_seconds
        )
        observer = Observer()
        observer.schedule(handler, str(self._source_path), recursive=True)

        try:
            observer.start()
        except OSError as exc:
            _log.warning(
                "Failed to start filesystem observer for collection %r: %s",
                self._collection_name,
                exc,
            )
            return

        self._handler = handler
        self._observer = observer

    def stop(self) -> None:
        """Stop watching and clean up resources."""
        if self._handler is not None:
            self._handler.cancel_all()

        if self._observer is not None:
            try:
                self._observer.stop()  # type: ignore[union-attr]
            except OSError as exc:
                _log.warning(
                    "OSError while stopping observer for collection %r: %s",
                    self._collection_name,
                    exc,
                )
            # still attempt join even after stop() failure — best-effort cleanup
            try:
                self._observer.join(timeout=5.0)  # type: ignore[union-attr]
            except OSError:
                pass

            if self._observer.is_alive():  # type: ignore[union-attr]
                _log.warning(
                    "Observer thread did not terminate within 5s for collection %r",
                    self._collection_name,
                )

            self._observer = None

    def is_alive(self) -> bool:
        """Return True if the observer thread is running."""
        return self._observer is not None and self._observer.is_alive()  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# WatcherManager
# ---------------------------------------------------------------------------


class WatcherManager:
    """Manages multiple CollectionWatchers and their lifecycle."""

    def __init__(
        self,
        on_change: Callable[[str], Coroutine],
        loop: asyncio.AbstractEventLoop,
        debounce_seconds: float = 5.0,
    ) -> None:
        self._on_change = on_change
        self._loop = loop
        self._debounce_seconds = debounce_seconds
        self._watchers: dict[str, CollectionWatcher] = {}
        self._active_syncs: set[asyncio.Task[None]] = set()
        self._shutting_down: bool = False

        async def _wrapped_callback(col_name: str) -> None:
            if self._shutting_down:
                return
            task: asyncio.Task[None] = asyncio.create_task(on_change(col_name))
            self._active_syncs.add(task)
            try:
                await task
            except Exception as exc:
                _log.error("Watch-triggered sync for %r raised: %r", col_name, exc)
            finally:
                self._active_syncs.discard(task)

        self._wrapped_callback = _wrapped_callback

    def add(self, collection_name: str, source_path: Path) -> None:
        """Start watching *source_path* for *collection_name*. No-op if already watching."""
        if self._shutting_down:
            return
        if collection_name in self._watchers:
            return
        watcher = CollectionWatcher(
            collection_name,
            source_path,
            self._wrapped_callback,
            self._loop,
            self._debounce_seconds,
        )
        watcher.start()
        self._watchers[collection_name] = watcher

    async def stop_all(self) -> None:
        """Stop all watchers and wait for in-flight syncs to complete."""
        self._shutting_down = True
        await asyncio.gather(*(asyncio.to_thread(w.stop) for w in self._watchers.values()))
        if self._active_syncs:
            _, pending = await asyncio.wait(self._active_syncs, timeout=10.0)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._watchers.clear()

    def is_watching(self, collection_name: str) -> bool:
        """Return True if *collection_name* has a live watcher."""
        watcher = self._watchers.get(collection_name)
        return watcher is not None and watcher.is_alive()

    def watching_names(self) -> set[str]:
        """Return the set of collection names with live watchers."""
        return {name for name, w in self._watchers.items() if w.is_alive()}
