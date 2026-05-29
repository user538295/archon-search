"""Live eval suite smoke test — requires real model weights.

Run with: uv run pytest -m live_eval tests/eval/live/test_live_eval_suite.py -v --no-cov
"""
from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.eval.runner import render_report, run_eval_suite


@pytest.mark.live_eval
async def test_live_eval_suite_runs_and_generates_report(
    live_corpus_root: Path,
    live_runtime_cfg_path: Path,
    live_thresholds_path: Path,
    live_artifacts_dir: Path,
) -> None:
    report = await run_eval_suite(live_corpus_root, live_runtime_cfg_path, backend="live")
    print(render_report(report))

    assert report.document_count > 0
    assert report.query_count > 0
    assert report.metrics.recall_at_1 >= 0.0
    assert report.metrics.latency_p95_ms > 0.0
