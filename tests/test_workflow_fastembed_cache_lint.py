"""Static lint: a CI step that runs a real-model lane loads the fastembed cache CI restores.

The real-model lanes (``tests/eval/live/``, ``tests/eval/live_benchmark/``) load their
weights from ``get_fastembed_cache_dir()`` (S493): ``$FASTEMBED_CACHE_PATH`` when set,
else ``<ARCHON_SEARCH_DATA_DIR>/models`` (``~/.archon-search/models`` on a runner). A
workflow that caches ``~/.cache/fastembed`` must therefore export that path in every
step running one of those lanes, or the restored cache is never read and the lane
downloads the weights on every run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
CACHE_ACTION = "actions/cache@"
FASTEMBED_CACHE_DIR = "~/.cache/fastembed"
# Path prefix shared by `tests/eval/live/` and `tests/eval/live_benchmark/`.
REAL_MODEL_LANE_PATH = "tests/eval/live"
FASTEMBED_CACHE_EXPORT = 'export FASTEMBED_CACHE_PATH="$HOME/.cache/fastembed"'


def _steps(workflow: Path) -> list[dict]:
    doc = yaml.safe_load(workflow.read_text())
    return [step for job in doc["jobs"].values() for step in job.get("steps", [])]


def _caches_fastembed_dir(step: dict) -> bool:
    if not step.get("uses", "").startswith(CACHE_ACTION):
        return False
    return any(p.rstrip("/") == FASTEMBED_CACHE_DIR for p in step["with"]["path"].split())


def _workflows_caching_fastembed_dir() -> list[Path]:
    return [w for w in WORKFLOWS if any(_caches_fastembed_dir(s) for s in _steps(w))]


def test_scan_sees_both_real_model_lane_workflows() -> None:
    # Presence anchor: the per-workflow check below must not pass vacuously.
    names = {w.name for w in _workflows_caching_fastembed_dir()}
    assert {"archon-search-pr.yml", "archon-search-eval-live.yml"} <= names, names


@pytest.mark.parametrize("workflow", _workflows_caching_fastembed_dir(), ids=lambda p: p.name)
def test_real_model_lane_steps_export_cached_fastembed_dir(workflow: Path) -> None:
    lane_steps = [s for s in _steps(workflow) if REAL_MODEL_LANE_PATH in s.get("run", "")]
    assert lane_steps, f"{workflow.name} caches {FASTEMBED_CACHE_DIR} but runs no real-model lane"
    unexported = [s.get("name") for s in lane_steps if FASTEMBED_CACHE_EXPORT not in s["run"]]
    assert not unexported, (
        f"{workflow.name} restores {FASTEMBED_CACHE_DIR}, but steps {unexported!r} run a "
        f"real-model lane without `{FASTEMBED_CACHE_EXPORT}` — get_fastembed_cache_dir() "
        "then resolves ~/.archon-search/models and the restored cache is never read"
    )
