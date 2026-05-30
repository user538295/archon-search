"""Live eval suite smoke test — requires real model weights.

Run with: uv run pytest -m live_eval tests/eval/live/test_live_eval_suite.py -v --no-cov
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from archon_search.eval.live_report import (
    build_live_report,
    load_live_thresholds,
    write_live_report_json,
    write_live_report_markdown,
)
from archon_search.eval.runner import render_report, run_eval_suite


@pytest.mark.live_eval
async def test_live_eval_suite_runs_and_generates_report(
    live_corpus_root: Path,
    live_runtime_cfg_path: Path,
    live_thresholds_path: Path,
    live_artifacts_dir: Path,
) -> None:
    # Load thresholds (returns None when stub is empty — report-only mode)
    thresholds = load_live_thresholds(live_thresholds_path)

    # Resolve live baseline path — pass it only if the file exists (post-calibration)
    live_baseline_path = live_corpus_root / "live_baselines" / "baseline.json"

    report = await run_eval_suite(
        live_corpus_root,
        live_runtime_cfg_path,
        thresholds_path=live_thresholds_path if thresholds else None,
        baseline_path=live_baseline_path if live_baseline_path.exists() else None,
        backend="live",
    )
    print(render_report(report))

    assert report.document_count > 0
    assert report.query_count > 0
    assert report.metrics.recall_at_1 >= 0.0
    assert report.metrics.latency_p95_ms > 0.0

    # Build live report and write artifacts
    live_report = build_live_report(report)
    json_path = live_artifacts_dir / "live_eval_report.json"
    md_path = live_artifacts_dir / "live_eval_report.md"
    write_live_report_json(live_report, json_path)
    write_live_report_markdown(live_report, md_path)

    assert json_path.exists(), "live_eval_report.json was not written"
    assert md_path.exists(), "live_eval_report.md was not written"

    data = json.loads(json_path.read_text())
    assert set(data.keys()) >= {"verdicts", "overall_status", "generated_at", "eval_report"}

    assert live_report.overall_status in ("pass", "fail", "report_only")
