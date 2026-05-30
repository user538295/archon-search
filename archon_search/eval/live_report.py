"""Live eval report module.

Provides load_live_thresholds() and (in later tasks) MetricVerdict,
LiveEvalReport, build_live_report(), write_live_report_json(), and
write_live_report_markdown().
"""
from __future__ import annotations

import logging
from pathlib import Path

from archon_search.eval.runner import EvalThresholds, load_thresholds

logger = logging.getLogger("archon")


def load_live_thresholds(path: Path) -> EvalThresholds | None:
    """Load live eval thresholds from *path*, returning None in report-only mode.

    Returns None (instead of raising) when:
    - The file does not exist
    - The TOML is malformed or missing required sections
    """
    if not path.exists():
        logger.warning("live_thresholds.toml not found at %s — report-only mode", path)
        return None
    try:
        return load_thresholds(path)
    except ValueError as exc:
        logger.warning(
            "live_thresholds.toml missing required sections (%s) — report-only mode", exc
        )
        return None
