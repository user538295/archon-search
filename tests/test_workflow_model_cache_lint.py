"""Static lint: CI restores and fills the model cache the real-model lanes load from.

The real-model lanes (``tests/eval/live/``, ``tests/eval/live_benchmark/``) load their weights
through ``archon_search.model_store`` from ``<ARCHON_SEARCH_DATA_DIR>/models`` (``~/.archon-search/models``
on a runner), in the Hugging Face hub layout, at the revisions pinned in ``model_specs``. A workflow
that runs a lane must therefore cache those snapshot directories under a key that changes with the
pinned revisions, and download the weights through ``python -m tests._live_model_cache`` before the
lane; the latency benchmark runs offline so a cache mismatch fails instead of downloading inside
its time budget.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests._live_model_cache import live_specs

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
CACHE_ACTION = "actions/cache@"
MODELS_ROOT = "~/.archon-search/models"
PREFETCH_COMMAND = "python -m tests._live_model_cache"
# Path prefix shared by `tests/eval/live/` and `tests/eval/live_benchmark/`.
REAL_MODEL_LANE_PATH = "tests/eval/live"
BENCHMARK_LANE_MARKER = "-m live_benchmark"
OFFLINE_EXPORT = "export HF_HUB_OFFLINE=1"
# The one workflow that still installs fastembed: the CPU benchmark gate's comparison baseline.
BASELINE_WORKFLOW = "archon-search-runtime-bench.yml"
PARITY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "archon-search-runtime-parity.yml"


def _steps(workflow: Path) -> list[dict]:
    doc = yaml.safe_load(workflow.read_text())
    return [step for job in doc["jobs"].values() for step in job.get("steps", [])]


def _live_cache_dirs() -> list[str]:
    return [f"{MODELS_ROOT}/models--{spec.repo.replace('/', '--')}" for spec in live_specs()]


def _caches_live_models(step: dict) -> bool:
    if not step.get("uses", "").startswith(CACHE_ACTION):
        return False
    return bool(set(_live_cache_dirs()) & {p.rstrip("/") for p in step["with"]["path"].split()})


def _workflows_caching_live_models() -> list[Path]:
    return [w for w in WORKFLOWS if any(_caches_live_models(s) for s in _steps(w))]


def _index(steps: list[dict], predicate) -> list[int]:
    return [i for i, step in enumerate(steps) if predicate(step)]


def test_scan_sees_both_real_model_lane_workflows() -> None:
    # Presence anchor: the per-workflow checks below must not pass vacuously.
    names = {w.name for w in _workflows_caching_live_models()}
    assert {"archon-search-pr.yml", "archon-search-eval-live.yml"} <= names, names


@pytest.mark.parametrize("workflow", _workflows_caching_live_models(), ids=lambda p: p.name)
def test_cache_step_covers_every_live_model_directory(workflow: Path) -> None:
    (cache_step,) = [s for s in _steps(workflow) if _caches_live_models(s)]

    cached = {p.rstrip("/") for p in cache_step["with"]["path"].split()}
    assert set(_live_cache_dirs()) <= cached, f"{workflow.name} does not cache {_live_cache_dirs()} (got {cached})"


@pytest.mark.parametrize("workflow", _workflows_caching_live_models(), ids=lambda p: p.name)
def test_cache_key_changes_with_the_pinned_revisions(workflow: Path) -> None:
    (cache_step,) = [s for s in _steps(workflow) if _caches_live_models(s)]

    key = cache_step["with"]["key"]
    stale = [spec.revision for spec in live_specs() if spec.revision not in key]
    assert not stale, (
        f"{workflow.name}: cache key {key!r} lacks the pinned revisions {stale}; a revision bump in "
        "model_specs would keep restoring the old snapshots"
    )


@pytest.mark.parametrize("workflow", _workflows_caching_live_models(), ids=lambda p: p.name)
def test_models_are_downloaded_after_the_cache_restore_and_before_every_lane(workflow: Path) -> None:
    steps = _steps(workflow)
    (restore,) = _index(steps, _caches_live_models)
    prefetches = _index(steps, lambda s: PREFETCH_COMMAND in s.get("run", ""))
    lanes = _index(steps, lambda s: REAL_MODEL_LANE_PATH in s.get("run", ""))

    assert prefetches, f"{workflow.name} restores the live-model cache but never runs `{PREFETCH_COMMAND}`"
    assert lanes, f"{workflow.name} caches the live models but runs no real-model lane"
    assert restore < prefetches[0] < min(lanes), (
        f"{workflow.name}: order must be cache restore ({restore}), prefetch ({prefetches[0]}), "
        f"then the lanes ({lanes})"
    )


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_latency_benchmark_runs_offline(workflow: Path) -> None:
    benchmark_steps = [s for s in _steps(workflow) if BENCHMARK_LANE_MARKER in s.get("run", "")]
    online = [s.get("name") for s in benchmark_steps if OFFLINE_EXPORT not in s["run"]]
    assert not online, f"{workflow.name}: steps {online!r} run the latency benchmark without `{OFFLINE_EXPORT}`"


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_only_the_runtime_bench_workflow_mentions_fastembed(workflow: Path) -> None:
    text = workflow.read_text().lower()
    if workflow.name == BASELINE_WORKFLOW:
        assert "fastembed" in text, "the benchmark gate installs fastembed as its baseline"
    else:
        assert "fastembed" not in text


def test_runtime_parity_lane_runs_nightly_and_on_demand_on_cpu() -> None:
    doc = yaml.safe_load(PARITY_WORKFLOW.read_text())
    triggers = doc.get("on", doc.get(True))  # YAML 1.1 parses a bare `on` key as True
    assert "schedule" in triggers and "workflow_dispatch" in triggers
    assert "pull_request" not in triggers, "the lane downloads ~5 GB of weights; it must not run per PR"

    (job,) = doc["jobs"].values()
    assert job["runs-on"] == "ubuntu-latest"
    runs = [s["run"] for s in job["steps"] if "run" in s]
    (lane,) = [r for r in runs if "-m runtime_parity" in r]
    assert "tests/runtime_parity" in lane and "-o addopts=" in lane
    assert "ARCHON_RUNTIME_PARITY_DATA_DIR" in lane or "ARCHON_RUNTIME_PARITY_DATA_DIR" in str(job.get("env", {}))
