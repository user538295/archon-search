"""TelemetryReader — file discovery and entry parsing (FEAT-039c Task 2.1)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from archon_search.telemetry.entry import TelemetryEntry

logger = logging.getLogger("archon.search")


class TelemetryReader:
    """Reads telemetry JSONL files from a log directory."""

    def __init__(self, log_dir: Path, retention_days: int) -> None:
        self._log_dir = log_dir
        self._retention_days = retention_days

    def resolve_dates(
        self, since: date | None, until: date | None
    ) -> tuple[date, date]:
        """Resolve and validate the [since, until] date range.

        - ``until`` defaults to today UTC if None.
        - ``since`` defaults to ``until - retention_days`` if None.
        - ``since`` is clamped to ``until - retention_days`` if earlier.
        - Raises ``ValueError`` if ``since > until`` after clamping.
        """
        if until is None:
            until = datetime.now(UTC).date()

        retention_floor = until - timedelta(days=self._retention_days)

        if since is None:
            since = retention_floor
        elif since < retention_floor:
            since = retention_floor

        if since > until:
            raise ValueError("since must be before until")

        return since, until

    def files_in_range(self, since: date, until: date) -> list[Path]:
        """Return sorted list of JSONL files whose stem date falls in [since, until].

        Returns ``[]`` if ``self._log_dir`` does not exist.
        """
        if not self._log_dir.exists():
            return []

        result: list[Path] = []
        for path in self._log_dir.glob("*.jsonl"):
            try:
                file_date = date.fromisoformat(path.stem)
            except ValueError:
                continue  # skip non-date filenames silently
            if since <= file_date <= until:
                result.append(path)

        result.sort()
        return result

    def read_entries(
        self, since: date, until: date
    ) -> tuple[list[TelemetryEntry], int]:
        """Parse all entries from files in [since, until].

        Returns ``(entries, skipped_lines)`` where ``skipped_lines`` counts
        lines that could not be parsed.
        """
        entries: list[TelemetryEntry] = []
        skipped_lines = 0

        for path in self.files_in_range(since, until):
            try:
                file = path.open(encoding="utf-8")
            except FileNotFoundError:
                logger.debug("telemetry: file not found, skipping: %s", path)
                continue
            except OSError as exc:
                logger.warning("telemetry: cannot open %s: %s", path, exc)
                continue

            with file:
                for line in file:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        entry = TelemetryEntry.model_validate(data)
                        entries.append(entry)
                    except Exception:
                        logger.warning(
                            "telemetry: skipping malformed line in %s", path
                        )
                        skipped_lines += 1

        return entries, skipped_lines
