"""Tests for archon.rag.notification_monitor — IndexingNotificationMonitor."""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from archon.config.loader import NotificationsConfig
from archon.search.notification_monitor import IndexingNotificationMonitor
from archon.search.progress import (
    CollectionProgress,
    IndexingState,
    IndexingStateStore,
    IndexingStatus,
)


def _make_monitor(
    state: IndexingState | None = None,
    trigger: str | None = "install",
    user_ids: list[int] | None = None,
    mode: str = "normal",
    poll_interval: float = 0.0,
) -> tuple[IndexingNotificationMonitor, MagicMock, MagicMock]:
    """Helper: build a monitor with mocked store and bot."""
    store = MagicMock(spec=IndexingStateStore)
    if state is None:
        store.read.return_value = None
    else:
        state.trigger = trigger
        store.read.return_value = state
    bot = AsyncMock()
    cfg = NotificationsConfig(mode=mode)
    monitor = IndexingNotificationMonitor(
        state_store=store,
        bot=bot,
        allowed_user_ids=user_ids if user_ids is not None else [111, 222],
        notifications_config=cfg,
        poll_interval=poll_interval,
    )
    return monitor, store, bot


def _terminal_state(
    done: int = 1,
    failed: int = 0,
    trigger: str | None = "install",
) -> IndexingState:
    collections: dict[str, CollectionProgress] = {}
    for i in range(done):
        collections[f"done_{i}"] = CollectionProgress(status=IndexingStatus.DONE)
    for i in range(failed):
        collections[f"failed_{i}"] = CollectionProgress(status=IndexingStatus.FAILED)
    return IndexingState(collections=collections, trigger=trigger)


class TestCheckAndNotify:
    @pytest.mark.asyncio
    async def test_no_notification_when_state_absent(self) -> None:
        monitor, store, bot = _make_monitor(state=None)
        await monitor._check_and_notify()
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_notification_when_no_collections(self) -> None:
        state = IndexingState(collections={}, trigger="install")
        monitor, store, bot = _make_monitor(state=state)
        await monitor._check_and_notify()
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_notification_when_trigger_manual(self) -> None:
        state = _terminal_state(done=2, trigger="manual")
        monitor, store, bot = _make_monitor(state=state, trigger="manual")
        await monitor._check_and_notify()
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_notification_when_trigger_none(self) -> None:
        state = _terminal_state(done=2, trigger=None)
        monitor, store, bot = _make_monitor(state=state, trigger=None)
        await monitor._check_and_notify()
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_notification_when_in_progress(self) -> None:
        state = IndexingState(
            collections={
                "col_a": CollectionProgress(status=IndexingStatus.DONE),
                "col_b": CollectionProgress(status=IndexingStatus.IN_PROGRESS),
            },
            trigger="install",
        )
        monitor, store, bot = _make_monitor(state=state)
        await monitor._check_and_notify()
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_notification_when_pending(self) -> None:
        state = IndexingState(
            collections={
                "col_a": CollectionProgress(status=IndexingStatus.DONE),
                "col_b": CollectionProgress(status=IndexingStatus.PENDING),
            },
            trigger="install",
        )
        monitor, store, bot = _make_monitor(state=state)
        await monitor._check_and_notify()
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_notification_when_all_pending(self) -> None:
        state = IndexingState(
            collections={
                "col_a": CollectionProgress(status=IndexingStatus.PENDING),
                "col_b": CollectionProgress(status=IndexingStatus.PENDING),
            },
            trigger="install",
        )
        monitor, store, bot = _make_monitor(state=state)
        await monitor._check_and_notify()
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_success_notification(self) -> None:
        state = _terminal_state(done=2, failed=0, trigger="install")
        monitor, store, bot = _make_monitor(state=state)
        await monitor._check_and_notify()
        expected = "✅ RAG indexing complete — all 2 collection(s) ready."
        assert bot.send_message.call_count == 2
        bot.send_message.assert_any_call(111, expected, parse_mode="HTML")
        bot.send_message.assert_any_call(222, expected, parse_mode="HTML")

    @pytest.mark.asyncio
    async def test_sends_partial_failure_notification(self) -> None:
        state = _terminal_state(done=1, failed=1, trigger="install")
        monitor, store, bot = _make_monitor(state=state)
        await monitor._check_and_notify()
        expected = "⚠️ RAG indexing finished — 1 collection(s) failed. Run <code>archon rag status</code> for details."
        assert bot.send_message.call_count == 2
        bot.send_message.assert_any_call(111, expected, parse_mode="HTML")
        bot.send_message.assert_any_call(222, expected, parse_mode="HTML")

    @pytest.mark.asyncio
    async def test_sends_total_failure_notification(self) -> None:
        state = _terminal_state(done=0, failed=2, trigger="install")
        monitor, store, bot = _make_monitor(state=state)
        await monitor._check_and_notify()
        expected = "❌ RAG indexing failed — no collections are ready. Run <code>archon rag status</code> for details."
        assert bot.send_message.call_count == 2
        bot.send_message.assert_any_call(111, expected, parse_mode="HTML")
        bot.send_message.assert_any_call(222, expected, parse_mode="HTML")

    @pytest.mark.asyncio
    async def test_clears_trigger_before_send(self) -> None:
        """set_trigger(None) must be called BEFORE _send_to_all."""
        state = _terminal_state(done=1, trigger="install")
        monitor, store, bot = _make_monitor(state=state)

        call_order: list[str] = []
        store.set_trigger.side_effect = lambda _: call_order.append("set_trigger")
        bot.send_message.side_effect = lambda *a, **kw: call_order.append("send_message")

        await monitor._check_and_notify()
        assert call_order[0] == "set_trigger"
        assert "send_message" in call_order
        store.set_trigger.assert_called_once_with(None)

    @pytest.mark.asyncio
    async def test_no_double_notify(self) -> None:
        """After trigger cleared (store.read returns trigger=None), second poll must not re-send."""
        state_with_trigger = _terminal_state(done=1, trigger="install")
        state_no_trigger = _terminal_state(done=1, trigger=None)

        store = MagicMock(spec=IndexingStateStore)
        store.read.side_effect = [state_with_trigger, state_no_trigger]
        bot = AsyncMock()
        cfg = NotificationsConfig(mode="normal")
        monitor = IndexingNotificationMonitor(
            state_store=store,
            bot=bot,
            allowed_user_ids=[111],
            notifications_config=cfg,
            poll_interval=0.0,
        )
        await monitor._check_and_notify()
        await monitor._check_and_notify()
        assert bot.send_message.call_count == 1

    @pytest.mark.asyncio
    async def test_quiet_mode_suppresses(self) -> None:
        state = _terminal_state(done=1, trigger="install")
        monitor, store, bot = _make_monitor(state=state, mode="quiet")
        await monitor._check_and_notify()
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_normal_mode_sends(self) -> None:
        state = _terminal_state(done=1, trigger="install")
        monitor, store, bot = _make_monitor(state=state, mode="normal", user_ids=[111])
        await monitor._check_and_notify()
        assert bot.send_message.call_count == 1

    @pytest.mark.asyncio
    async def test_verbose_mode_sends(self) -> None:
        state = _terminal_state(done=1, trigger="install")
        monitor, store, bot = _make_monitor(state=state, mode="verbose", user_ids=[111])
        await monitor._check_and_notify()
        assert bot.send_message.call_count == 1

    @pytest.mark.asyncio
    async def test_debug_mode_sends(self) -> None:
        state = _terminal_state(done=1, trigger="install")
        monitor, store, bot = _make_monitor(state=state, mode="debug", user_ids=[111])
        await monitor._check_and_notify()
        assert bot.send_message.call_count == 1

    @pytest.mark.asyncio
    async def test_send_failure_is_caught(self) -> None:
        """bot.send_message raises — _check_and_notify must not re-raise."""
        state = _terminal_state(done=1, trigger="install")
        monitor, store, bot = _make_monitor(state=state, user_ids=[111])
        bot.send_message.side_effect = RuntimeError("network failure")
        # Should not raise
        await monitor._check_and_notify()

    @pytest.mark.asyncio
    async def test_monitor_set_trigger_failure_caught(self, caplog: pytest.LogCaptureFixture) -> None:
        """If set_trigger raises OSError, exception is caught, _send_to_all not called."""
        state = _terminal_state(done=1, trigger="install")
        monitor, store, bot = _make_monitor(state=state)
        store.set_trigger.side_effect = OSError("disk full")
        with caplog.at_level(logging.WARNING, logger="archon"):
            await monitor._check_and_notify()
        bot.send_message.assert_not_called()
        assert any("disk full" in r.message or "disk full" in str(r.args) for r in caplog.records)

    @pytest.mark.asyncio
    async def test_no_send_when_no_users(self, caplog: pytest.LogCaptureFixture) -> None:
        """Empty allowed_user_ids: trigger cleared, no send, WARNING logged."""
        state = _terminal_state(done=1, trigger="install")
        monitor, store, bot = _make_monitor(state=state, user_ids=[])
        with caplog.at_level(logging.WARNING, logger="archon"):
            await monitor._check_and_notify()
        bot.send_message.assert_not_called()
        store.set_trigger.assert_called_once_with(None)
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    @pytest.mark.asyncio
    async def test_send_to_second_user_after_first_fails(self) -> None:
        """First user send raises, second user still receives message."""
        state = _terminal_state(done=1, trigger="install")
        monitor, store, bot = _make_monitor(state=state, user_ids=[111, 222])
        bot.send_message.side_effect = [RuntimeError("fail"), None]
        await monitor._check_and_notify()
        assert bot.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_sends_notification_when_trigger_update(self) -> None:
        """trigger='update' is forward-compatible — notification fires same as 'install'."""
        state = _terminal_state(done=1, failed=0, trigger="update")
        monitor, store, bot = _make_monitor(state=state, trigger="update", user_ids=[111])
        await monitor._check_and_notify()
        assert bot.send_message.call_count == 1


class TestBuildMessage:
    def _monitor(self) -> IndexingNotificationMonitor:
        store = MagicMock(spec=IndexingStateStore)
        bot = AsyncMock()
        return IndexingNotificationMonitor(
            state_store=store,
            bot=bot,
            allowed_user_ids=[111],
            notifications_config=NotificationsConfig(),
        )

    def test_build_message_success(self) -> None:
        monitor = self._monitor()
        state = _terminal_state(done=3, failed=0)
        msg = monitor._build_message(state)
        assert msg == "✅ RAG indexing complete — all 3 collection(s) ready."

    def test_build_message_partial_failure(self) -> None:
        monitor = self._monitor()
        state = _terminal_state(done=2, failed=1)
        msg = monitor._build_message(state)
        assert msg == "⚠️ RAG indexing finished — 1 collection(s) failed. Run <code>archon rag status</code> for details."

    def test_build_message_total_failure(self) -> None:
        monitor = self._monitor()
        state = _terminal_state(done=0, failed=2)
        msg = monitor._build_message(state)
        assert msg == "❌ RAG indexing failed — no collections are ready. Run <code>archon rag status</code> for details."


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_run_survives_unexpected_exception(self) -> None:
        """Unexpected exception in _check_and_notify must not kill the run() loop."""
        store = MagicMock(spec=IndexingStateStore)
        store.read.return_value = None
        bot = AsyncMock()
        monitor = IndexingNotificationMonitor(
            state_store=store,
            bot=bot,
            allowed_user_ids=[111],
            notifications_config=NotificationsConfig(),
            poll_interval=0.0,
        )
        event = asyncio.Event()
        call_count = 0

        async def _raises_then_sets_event() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("unexpected failure")
            event.set()

        monitor._check_and_notify = _raises_then_sets_event  # type: ignore[method-assign]
        task = asyncio.create_task(monitor.run())
        await asyncio.wait_for(event.wait(), timeout=1.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert call_count >= 2

    @pytest.mark.asyncio
    async def test_run_calls_check_and_notify(self) -> None:
        """run() calls _check_and_notify. Use asyncio.Event for determinism."""
        store = MagicMock(spec=IndexingStateStore)
        store.read.return_value = None
        bot = AsyncMock()
        monitor = IndexingNotificationMonitor(
            state_store=store,
            bot=bot,
            allowed_user_ids=[111],
            notifications_config=NotificationsConfig(),
            poll_interval=0.0,
        )
        event = asyncio.Event()

        original = monitor._check_and_notify

        async def _patched() -> None:
            event.set()
            await original()

        monitor._check_and_notify = _patched  # type: ignore[method-assign]
        task = asyncio.create_task(monitor.run())
        await asyncio.wait_for(event.wait(), timeout=1.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_run_exits_cleanly_on_cancelled_error(self) -> None:
        """Cancelling run() task propagates CancelledError to the caller (standard asyncio task cancellation)."""
        store = MagicMock(spec=IndexingStateStore)
        store.read.return_value = None
        bot = AsyncMock()
        monitor = IndexingNotificationMonitor(
            state_store=store,
            bot=bot,
            allowed_user_ids=[111],
            notifications_config=NotificationsConfig(),
            poll_interval=100.0,
        )
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0)
        task.cancel()
        # Should not raise CancelledError or any other exception
        with pytest.raises(asyncio.CancelledError):
            await task
