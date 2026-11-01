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

import re
from pathlib import Path

import pytest
import yaml

from archon_search.paths import TOKEN_COUNTER_MODEL_REVISION
from tests import _live_model_cache as live_cache
from tests._live_model_cache import live_specs

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
CACHE_ACTION = "actions/cache@"
MODELS_ROOT = "~/.archon-search/models"
TOKEN_COUNTER_DIR = f"{MODELS_ROOT}/token-counter"
PR_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "archon-search-pr.yml"
PREFETCH_COMMAND = "python -m tests._live_model_cache"
# Path prefix shared by `tests/eval/live/` and `tests/eval/live_benchmark/`.
REAL_MODEL_LANE_PATH = "tests/eval/live"
BENCHMARK_LANE_MARKER = "-m live_benchmark"
OFFLINE_EXPORT = "export HF_HUB_OFFLINE=1"
# The one workflow that still installs fastembed: the CPU benchmark gate's comparison baseline.
BASELINE_WORKFLOW = "archon-search-runtime-bench.yml"
PARITY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "archon-search-runtime-parity.yml"
UNLOCKED_DEPS_SCRIPT = REPO_ROOT / "scripts" / "test-unlocked-deps.sh"
# Either command downloads the pinned gpt2 tokenizer: the narrow script (1.3 MB) or the live-lane prefetch, whose `__main__` ends with it.
TOKENIZER_PREFETCH_COMMANDS = ("scripts/prefetch-token-counter.py", PREFETCH_COMMAND)
PYTEST_INVOCATION = re.compile(r"(?:uv run |python -m )pytest\b")
MARKER_EXPRESSION = re.compile(r"""(?<!\S)-m\s+(?:"([^"]*)"|(\S+))""")
UNLOCKED_DEPS_DEFAULT_SUITE = re.compile(r"test-unlocked-deps\.sh\s+(?:suite|all)\b")
UNLOCKED_DEPS_PREFETCH_FUNCTION = "prefetch_token_counter"
UNLOCKED_DEPS_PREFETCH_COMMAND = '"$PY" scripts/prefetch-token-counter.py'


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


def _pr_cache_step() -> dict:
    (cache_step,) = [s for s in _steps(PR_WORKFLOW) if _caches_live_models(s)]
    return cache_step


def test_pr_workflow_caches_the_pinned_token_counter_tokenizer() -> None:
    # The real-gpt2 tests fail (not skip) on CI when the tokenizer is missing, so the PR job must restore and prefetch it.
    step = _pr_cache_step()

    assert TOKEN_COUNTER_DIR in {p.rstrip("/") for p in step["with"]["path"].split()}
    assert TOKEN_COUNTER_MODEL_REVISION in step["with"]["key"]


# --- every lane that runs the default suite fetches the pinned gpt2 tokenizer ------------------------------------------
# `tests/test_llm_client_token_ceiling.py` is unmarked, so any lane that runs the default suite also runs its real-gpt2
# tests, which fail (not skip) on GitHub (`CI` set) when the tokenizer is not cached.


def _runs_the_default_suite(run: str) -> bool:
    """True when a pytest call selects unmarked tests: no ``-m`` filter, or one made only of ``not <marker>`` terms."""
    if not PYTEST_INVOCATION.search(run):
        return False
    match = MARKER_EXPRESSION.search(run)
    if match is None:
        return True
    terms = re.split(r"\s+(?:and|or)\s+", match.group(1) or match.group(2))
    return all(term.startswith("not ") for term in terms)


def _shell_function_code(script: str, name: str) -> list[str] | None:
    """The non-comment, non-blank lines of shell function *name* (stripped), or ``None`` when it is not defined."""
    match = re.search(rf"^{name}\(\) \{{\n(?P<body>.*?)^\}}$", script, re.DOTALL | re.MULTILINE)
    if match is None:
        return None
    lines = (line.strip() for line in match.group("body").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _unlocked_deps_suite_prefetches_first(script: str) -> bool:
    """True when ``suite()`` calls ``prefetch_token_counter`` before pytest and that function really runs the prefetch.

    Comments never count, and the command must sit inside the function ``suite()`` calls, in the venv under test.
    """
    suite = _shell_function_code(script, "suite")
    assert suite is not None, "scripts/test-unlocked-deps.sh has no suite() function"
    prefetch = _shell_function_code(script, UNLOCKED_DEPS_PREFETCH_FUNCTION)
    if prefetch is None or UNLOCKED_DEPS_PREFETCH_FUNCTION not in suite:
        return False
    pytest_run = next((i for i, line in enumerate(suite) if "-m pytest" in line), None)
    return (
        pytest_run is not None
        and suite.index(UNLOCKED_DEPS_PREFETCH_FUNCTION) < pytest_run
        and any(line.startswith(UNLOCKED_DEPS_PREFETCH_COMMAND) for line in prefetch)
    )


def _default_suite_steps_without_the_tokenizer(workflow: Path, unlocked_deps_script: str) -> list[str]:
    """``job: step`` of every step that runs the default suite with no tokenizer prefetch before it in its job."""
    missing: list[str] = []
    for job_name, job in yaml.safe_load(workflow.read_text())["jobs"].items():
        fetched = False
        for step in job.get("steps", []):
            run = step.get("run", "")
            if UNLOCKED_DEPS_DEFAULT_SUITE.search(run):
                covered = _unlocked_deps_suite_prefetches_first(unlocked_deps_script)
            elif _runs_the_default_suite(run):
                covered = fetched
            else:
                fetched = fetched or any(command in run for command in TOKENIZER_PREFETCH_COMMANDS)
                continue
            if not covered:
                missing.append(f"{job_name}: {step.get('name')}")
    return missing


def _workflow_text(*steps: str) -> str:
    return "jobs:\n  test:\n    steps:\n" + "".join(f"      - {step}\n" for step in steps)


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        ('uv run pytest -o addopts= -n0 -m "not live and not eval and not integration"', True),
        ("uv run pytest tests/", True),
        ('uv run pytest -m "integration and not eval" tests/', False),
        ("uv run pytest -m docling tests/", False),
        ("uv run pytest --strict-markers -m live_eval tests/eval/live/", False),
        ('uv run python -c "import pytest_cov"', False),
        ("uv run python -m tests._live_model_cache", False),
    ],
)
def test_a_pytest_call_runs_the_default_suite_only_without_a_positive_marker(run: str, expected: bool) -> None:
    assert _runs_the_default_suite(run) is expected


def test_scan_finds_every_lane_that_runs_the_default_suite() -> None:
    # Presence anchor: the per-workflow check below must not pass vacuously, nor sweep in the marker-selected lanes.
    def runs_it(workflow: Path) -> bool:
        runs = [step.get("run", "") for step in _steps(workflow)]
        return any(_runs_the_default_suite(r) or UNLOCKED_DEPS_DEFAULT_SUITE.search(r) for r in runs)

    names = {w.name for w in WORKFLOWS if runs_it(w)}
    assert names == {"archon-search-pr.yml", "archon-search-release.yml", "archon-search-unlocked-deps.yml"}, names


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_lane_running_the_default_suite_fetches_the_pinned_tokenizer(workflow: Path) -> None:
    missing = _default_suite_steps_without_the_tokenizer(workflow, UNLOCKED_DEPS_SCRIPT.read_text())

    assert not missing, (
        f"{workflow.name}: {missing} run the default suite, whose real-gpt2 tests fail on CI without the pinned "
        f"tokenizer; add a step running one of {TOKENIZER_PREFETCH_COMMANDS} before it"
    )


def test_the_scan_flags_a_default_suite_step_without_a_prefetch(tmp_path: Path) -> None:
    workflow = tmp_path / "lane.yml"
    workflow.write_text(_workflow_text("name: Default suite\n        run: uv run pytest -m \"not live\""))

    assert _default_suite_steps_without_the_tokenizer(workflow, "") == ["test: Default suite"]


def test_the_scan_flags_a_prefetch_that_runs_after_the_suite(tmp_path: Path) -> None:
    workflow = tmp_path / "lane.yml"
    workflow.write_text(
        _workflow_text(
            "name: Default suite\n        run: uv run pytest -m \"not live\"",
            "name: Prefetch\n        run: uv run python scripts/prefetch-token-counter.py",
        )
    )

    assert _default_suite_steps_without_the_tokenizer(workflow, "") == ["test: Default suite"]


@pytest.mark.parametrize("command", TOKENIZER_PREFETCH_COMMANDS)
def test_the_scan_accepts_a_prefetch_before_the_suite(tmp_path: Path, command: str) -> None:
    workflow = tmp_path / "lane.yml"
    workflow.write_text(
        _workflow_text(
            f"name: Prefetch\n        run: uv run python {command}",
            "name: Default suite\n        run: uv run pytest -m \"not live\"",
        )
    )

    assert _default_suite_steps_without_the_tokenizer(workflow, "") == []


def test_the_scan_leaves_a_marker_selected_lane_alone(tmp_path: Path) -> None:
    workflow = tmp_path / "lane.yml"
    workflow.write_text(_workflow_text("name: Docling\n        run: uv run pytest -m docling tests/"))

    assert _default_suite_steps_without_the_tokenizer(workflow, "") == []


def test_the_scan_delegates_an_unlocked_deps_suite_step_to_the_script(tmp_path: Path) -> None:
    workflow = tmp_path / "lane.yml"
    workflow.write_text(_workflow_text("name: Suite\n        run: bash scripts/test-unlocked-deps.sh suite"))
    script = UNLOCKED_DEPS_SCRIPT.read_text()
    bare_script = script.replace(UNLOCKED_DEPS_PREFETCH_FUNCTION, "true")

    assert _default_suite_steps_without_the_tokenizer(workflow, script) == []
    assert _default_suite_steps_without_the_tokenizer(workflow, bare_script) == ["test: Suite"]


def test_the_unlocked_deps_suite_prefetches_the_tokenizer_inside_the_venv_before_pytest() -> None:
    script = UNLOCKED_DEPS_SCRIPT.read_text()

    assert _unlocked_deps_suite_prefetches_first(script), "suite() must call prefetch_token_counter, which runs the venv under test's python"


_PREFETCH_COMMAND_LINE = '    "$PY" scripts/prefetch-token-counter.py && return 0\n'
_SUITE_WITH_CALL = '    prefetch_token_counter\n    "$PY" -m pytest tests/\n'


def _unlocked_deps_script(
    *, prefetch_body: str = _PREFETCH_COMMAND_LINE, suite_body: str = _SUITE_WITH_CALL, elsewhere: str = ""
) -> str:
    return f"{elsewhere}prefetch_token_counter() {{\n{prefetch_body}}}\n\nsuite() {{\n{suite_body}}}\n"


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        pytest.param(_unlocked_deps_script(), True, id="real call in the function, function called before pytest"),
        pytest.param(
            _unlocked_deps_script(prefetch_body="    # runs: \"$PY\" scripts/prefetch-token-counter.py\n    return 0\n"),
            False,
            id="command only in a comment inside the function",
        ),
        pytest.param(
            _unlocked_deps_script(
                prefetch_body="    return 0\n",
                elsewhere='lane() {\n    "$PY" scripts/prefetch-token-counter.py\n}\n\n',
            ),
            False,
            id="command in another function, not in the one suite() calls",
        ),
        pytest.param(
            _unlocked_deps_script(suite_body='    # prefetch_token_counter\n    "$PY" -m pytest tests/\n'),
            False,
            id="the function is only named in a comment of suite()",
        ),
        pytest.param(
            _unlocked_deps_script(suite_body='    "$PY" -m pytest tests/\n    prefetch_token_counter\n'),
            False,
            id="the function is called after pytest",
        ),
        pytest.param(
            _unlocked_deps_script(prefetch_body="    uv run python scripts/prefetch-token-counter.py\n"),
            False,
            id="uv's python, not the venv under test",
        ),
    ],
)
def test_the_unlocked_deps_scan_wants_a_real_prefetch_call_inside_the_function_suite_calls(
    script: str, expected: bool
) -> None:
    assert _unlocked_deps_suite_prefetches_first(script) is expected


def test_prefetch_downloads_the_token_counter_tokenizer(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded: list[bool] = []
    monkeypatch.setattr(live_cache, "load_gpt2_tokenizer", lambda: loaded.append(True))

    live_cache.prefetch_token_counter_tokenizer()

    assert loaded == [True]


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
