"""Regenerate baseline.json + baseline.md from the committed eval corpus.

Run from the package root:

    cd packages/archon-search
    uv run python tests/eval/baselines/regenerate.py

This is a maintenance utility, not part of the pytest run (per spec).

IMPORTANT: installs the same ML stubs that pytest uses (fake chonkie, fake fastembed)
so calibrated values are consistent with what the gated CI tests measure.
"""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

# Install stubs BEFORE any archon_search imports to mirror the pytest conftest
# environment. Without this, regenerate.py uses the real chonkie tokenizer while
# the pytest gated test uses the fake word-count chunker, producing different
# chunk boundaries and thus different routing centroids.
_TESTS_DIR = Path(__file__).resolve().parent.parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
from _search_stubs import install_stubs  # noqa: E402

install_stubs()

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

    # Preserve any manually-set waivers from the existing baseline so that
    # regenerate.py doesn't silently drop them and break gated tests.
    existing_baseline_path = BASELINES_DIR / "baseline.json"
    existing_waivers: dict[str, str] = {}
    if existing_baseline_path.exists():
        try:
            existing_waivers = json.loads(existing_baseline_path.read_text()).get(
                "waiver_ids", {}
            )
        except (json.JSONDecodeError, KeyError):
            pass

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
        "waiver_ids": existing_waivers,
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
