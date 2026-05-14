"""TelemetryReader — file discovery and entry parsing (FEAT-039c Task 2.1)."""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from archon_search.telemetry.entry import TelemetryEntry

_ALL_ERROR_KINDS = (
    "empty_query",
    "slot_out_of_range",
    "timeout",
    "internal_error",
    "validation_error",
    "other",
)

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

    def compute_stats(
        self,
        entries: list[TelemetryEntry],
        since: date,
        until: date,
        skipped_lines: int,
    ) -> dict[str, Any]:
        """Compute aggregate statistics from a list of telemetry entries."""
        total_queries = len(entries)

        # success_rate
        if total_queries > 0:
            ok_count = sum(1 for e in entries if e.status == "ok")
            success_rate: float | None = ok_count / total_queries
        else:
            success_rate = None

        # latency percentiles (nearest-rank)
        if entries:
            sorted_latencies = sorted(e.latency_ms for e in entries)
            n = len(sorted_latencies)
            p50_idx = math.ceil(50 / 100 * n) - 1
            p95_idx = math.ceil(95 / 100 * n) - 1
            latency_ms: dict[str, float | None] = {
                "p50": sorted_latencies[p50_idx],
                "p95": sorted_latencies[p95_idx],
            }
        else:
            latency_ms = {"p50": None, "p95": None}

        # by_endpoint
        by_endpoint: dict[str, dict[str, int]] = {}
        for e in entries:
            ep = by_endpoint.setdefault(e.endpoint, {"total": 0, "ok": 0, "error": 0})
            ep["total"] += 1
            if e.status == "ok":
                ep["ok"] += 1
            else:
                ep["error"] += 1

        # by_collection
        by_collection: dict[str, dict[str, int]] = {}
        for e in entries:
            if e.collection is not None:
                cols = [e.collection]
            elif e.collections is not None:
                cols = e.collections
            else:
                continue  # error entry with no collection info
            for col in cols:
                rec = by_collection.setdefault(col, {"total": 0, "ok": 0})
                rec["total"] += 1
                if e.status == "ok":
                    rec["ok"] += 1

        # error_breakdown (all 6 kinds pre-populated at 0)
        error_breakdown: dict[str, int] = {k: 0 for k in _ALL_ERROR_KINDS}
        for e in entries:
            if e.error_kind is not None:
                error_breakdown[e.error_kind] += 1

        return {
            "schema_version": 1,
            "enabled": True,
            "since": since.isoformat(),
            "until": until.isoformat(),
            "total_queries": total_queries,
            "success_rate": success_rate,
            "skipped_lines": skipped_lines,
            "latency_ms": latency_ms,
            "by_endpoint": by_endpoint,
            "by_collection": by_collection,
            "error_breakdown": error_breakdown,
        }
