"""Regenerate baseline.json + baseline.md from the committed eval corpus.

Run from the package root:

    cd packages/archon-search
    uv run python tests/eval/baselines/regenerate.py

This is a maintenance utility, not part of the pytest run (per FEAT-039 spec).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from archon_search.eval._hashing import (
    compute_eval_hash,
    compute_runtime_config_hash,
    compute_thresholds_hash,
)
from archon_search.eval.runner import render_report, run_eval_suite

EVAL_DIR = Path(__file__).resolve().parent.parent
BASELINES_DIR = Path(__file__).resolve().parent
RUNTIME_TOML = EVAL_DIR / "runtime.toml"
THRESHOLDS_TOML = EVAL_DIR / "thresholds.toml"

CALIBRATION_COMMAND = (
    "pytest -m eval tests/eval/test_eval_suite.py::test_eval_suite_report_only_smoke"
)


async def main() -> None:
    report = await run_eval_suite(
        EVAL_DIR,
        RUNTIME_TOML,
        thresholds_path=None,
        baseline_path=None,
    )

    metrics_dict: dict[str, float | None] = {}
    for k, v in asdict(report.metrics).items():
        metrics_dict[k] = None if v is None else float(v)
    # Latency is wall-clock — strip from baseline.json so re-running calibration
    # produces identical content given identical inputs. (Rendered report keeps
    # the measured latencies for human review.)
    metrics_dict["latency_p50_ms"] = None
    metrics_dict["latency_p95_ms"] = None

    baseline: dict[str, object] = {
        "eval_hash": compute_eval_hash(EVAL_DIR),
        "runtime_config_hash": compute_runtime_config_hash(RUNTIME_TOML),
        "thresholds_hash": (
            compute_thresholds_hash(THRESHOLDS_TOML)
            if THRESHOLDS_TOML.exists()
            else None
        ),
        "command": CALIBRATION_COMMAND,
        "metrics": metrics_dict,
        "waiver_ids": {},
    }

    BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    (BASELINES_DIR / "baseline.json").write_text(
        json.dumps(baseline, indent=2, sort_keys=True) + "\n"
    )
    (BASELINES_DIR / "baseline.md").write_text(render_report(report) + "\n")
    print("wrote baseline.json + baseline.md")
    print(json.dumps(metrics_dict, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
