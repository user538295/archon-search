"""IndexingNotificationMonitor — polls RAG indexing state and sends Telegram notification on completion."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from archon.config.loader import NotificationsConfig
from archon.search.progress import IndexingState, IndexingStateStore, IndexingStatus

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger("archon")


class IndexingNotificationMonitor:
    """Background task that polls indexing state and notifies Telegram when all collections reach terminal state."""

    def __init__(
        self,
        state_store: IndexingStateStore,
        bot: "Bot",
        allowed_user_ids: list[int],
        notifications_config: NotificationsConfig,
        poll_interval: float = 30.0,
    ) -> None:
        self._state_store = state_store
        self._bot = bot
        self._allowed_user_ids = allowed_user_ids
        self._notifications_config = notifications_config
        self._poll_interval = poll_interval

    async def run(self) -> None:
        """Infinite loop: sleep then check. CancelledError propagates to the caller."""
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self._check_and_notify()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("IndexingNotificationMonitor: unexpected error in poll cycle: %s", exc, exc_info=True)

    async def _check_and_notify(self) -> None:
        """Read state, detect all-terminal transition with notifiable trigger, send notification."""
        state = self._state_store.read()
        if state is None or not state.collections:
            return

        if state.trigger not in ("install", "update"):
            return

        terminal = (IndexingStatus.DONE, IndexingStatus.FAILED)
        if not all(cp.status in terminal for cp in state.collections.values()):
            return

        if self._notifications_config.mode == "quiet":
            return

        message = self._build_message(state)

        try:
            self._state_store.set_trigger(None)
        except Exception as exc:
            logger.warning("IndexingNotificationMonitor: failed to clear trigger: %s", exc)
            return

        await self._send_to_all(message)

    def _build_message(self, state: IndexingState) -> str:
        """Compose notification text from terminal collection states."""
        failed = [name for name, cp in state.collections.items() if cp.status == IndexingStatus.FAILED]
        done = [name for name, cp in state.collections.items() if cp.status == IndexingStatus.DONE]

        if not failed:
            return f"✅ RAG indexing complete — all {len(done)} collection(s) ready."
        if not done:
            return "❌ RAG indexing failed — no collections are ready. Run <code>archon rag status</code> for details."
        return f"⚠️ RAG indexing finished — {len(failed)} collection(s) failed. Run <code>archon rag status</code> for details."

    async def _send_to_all(self, message: str) -> None:
        """Send message to all allowed_user_ids; log and continue on failure."""
        if not self._allowed_user_ids:
            logger.warning("IndexingNotificationMonitor: no allowed_user_ids configured, skipping notification")
            return
        sent = 0
        for user_id in self._allowed_user_ids:
            try:
                await self._bot.send_message(user_id, message, parse_mode="HTML")
                sent += 1
            except Exception as exc:
                logger.warning(
                    "IndexingNotificationMonitor: failed to send notification to user %d: %s",
                    user_id,
                    exc,
                )
        if sent:
            logger.info("IndexingNotificationMonitor: sent RAG completion notification to %d user(s)", sent)
