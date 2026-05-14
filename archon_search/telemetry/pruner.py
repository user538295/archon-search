"""Telemetry log pruner — filename-based daily retention (FEAT-039b, Task 2.3)."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("archon.search")


class Pruner:
    """Deletes JSONL telemetry files older than `retention_days` using filename-based age."""

    def __init__(self, log_dir: Path, retention_days: int) -> None:
        self._log_dir = log_dir
        self._retention_days = retention_days

    def prune_once(self, *, now: date | None = None) -> int:
        """Synchronous. Returns count of files deleted.

        Scans `log_dir` for `*.jsonl` files. Parses the stem as `YYYY-MM-DD`.
        Deletes those where `file_date < now - timedelta(days=retention_days)`
        AND `file_date != now`. Today's file is never deleted.
        """
        if now is None:
            now = datetime.now(UTC).date()

        cutoff = now - timedelta(days=self._retention_days)
        deleted = 0

        if not self._log_dir.exists():
            return 0

        for path in self._log_dir.glob("*.jsonl"):
            try:
                file_date = date.fromisoformat(path.stem)
            except ValueError:
                logger.debug("pruner: skipping malformed filename %s", path.name)
                continue

            if file_date == now:
                continue

            if file_date < cutoff:
                try:
                    path.unlink()
                    deleted += 1
                except OSError:
                    logger.warning("pruner: failed to delete %s", path)

        return deleted
