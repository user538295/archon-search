# B6 — Production-Model Eval Lane
**Purpose**: Add a live-eval lane that measures ranking quality and latency with real fastembed + cross-encoder backends so regressions in production models are caught before release.
**Audience**: archon-search contributors implementing B6; reviewers of the resulting PRs.
**Status**: To Do

---

## Background

The eval harness (`pytest -m eval`) runs exclusively against deterministic stub backends (`EvalEmbedderBackend` / `EvalRerankerBackend`). Real model weights are never exercised in CI, so a ranking regression introduced by a fastembed or cross-encoder model upgrade can ship undetected. B6 adds a second eval lane (`pytest -m live_eval`) that runs the same corpus through real models, collects metrics, and generates a structured report. The lane is report-only in v1 — it does not block tag pushes.

The full design is in `Documentation/Backlog/b6-production-model-eval-lane-brief.md`. All architectural decisions are locked.

---

## Goal

After B6 ships: a tag push (or manual `workflow_dispatch`) triggers `archon-search-eval-live.yml`, which runs the corpus through real fastembed + cross-encoder, emits `live_eval_report.json` + `live_eval_report.md` as workflow artifacts, and records model versions alongside metrics. The release workflow continues independently. Team reviews the report asynchronously.

---

## Scope

### In Scope
- `live_eval` pytest marker in `pyproject.toml`
- `_build_pipeline_with_eval_backends(db_path, *, backend: Literal["deterministic", "live"])` parameterization using `ModelEmbedder` / `ModelReranker` for the live path; defense-in-depth `ARCHON_SEARCH_EVAL_BACKENDS` guard
- `run_eval_suite()` extended with `backend` parameter (default `"deterministic"` — backwards-compatible)
- `tests/eval/live/conftest.py` — isolated conftest that shadows parent autouse (no-op override of `_activate_deterministic_eval_backends`)
- `tests/eval/live_thresholds.toml` stub + `tests/eval/live_baselines/` directory layout
- `tests/eval/live/test_live_eval_suite.py` — smoke test marked `live_eval`
- `EvalBaseline` extension with 6 model-version optional fields; `load_baseline()` updated
- `archon_search/eval/live_report.py`: `MetricVerdict`, `LiveEvalReport`, `build_live_report()`, `load_live_thresholds()`, `write_live_report_json()`, `write_live_report_markdown()`
- `tests/eval/test_live_report_acceptance.py` — 5 pure-logic acceptance tests (marked `eval`)
- `tests/eval/live/test_live_acceptance.py` — 5 live-backend acceptance tests (marked `live_eval`)
- `.github/workflows/archon-search-eval-live.yml`
- Updates to `tests/eval/README.md` and `Documentation/Architecture/200_testing_strategy.md`

### Out of Scope
- Blocking releases on eval failure (report-only; future one-line workflow change)
- Scheduled nightly/weekly eval runs
- Per-collection model selection (C1)
- ADR for two-lane eval strategy (separate follow-up)
- Auto-commit of calibration baseline (CI never commits)
- PR comment posting (no PR context on tag-push triggers)

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 4.2 — Final verification & documentation update].

---

## What does NOT change
- `load_thresholds()` — reused unchanged by `load_live_thresholds()` wrapper
- `tests/eval/thresholds.toml` and `tests/eval/baselines/` — deterministic lane untouched
- `run_eval_suite()` default behaviour (`backend="deterministic"`) — existing eval tests pass without modification
- `assert_thresholds()` — unchanged; live lane uses non-raising `build_live_report()` instead
- `EvalEmbedderBackend` / `EvalRerankerBackend` — unchanged
- `archon-search-release.yml` — unchanged; live eval runs concurrently with no dependency

---

## Brief deviations

- **MetricVerdict field rename**: The brief defines `MetricVerdict` with a field named `floor: float | None`. This plan renames it to `threshold: float | None` to be semantically correct for both floor (quality) and ceiling (latency) thresholds. The brief's `floor` field name is superseded by this plan.

---

## Known limitations / accepted trade-offs
- Live latency thresholds are intentionally loose (1.5× calibration p95); CI tenancy variance (50–200%) makes fine-grained latency gating impractical
- Calibration is human-driven: first run produces a report-only artifact; a maintainer commits `live_baselines/baseline.json` + fills `live_thresholds.toml` after the outlier checklist
- Both the embedder and reranker use `importlib.metadata.version("fastembed")` for version tracking — `ModelReranker` wraps fastembed's `TextCrossEncoder`; the brief's reference to `sentence-transformers` does not apply to this codebase
- `EvalEmbedderBackend` produces 128-dim vectors; real fastembed `BAAI/bge-small-en-v1.5` produces 384-dim; tests use the correct dimensions
- Live-backend acceptance tests (brief tests 1, 3, 8, 9, 10) are placed in `tests/eval/live/` with `live_eval` marker rather than `eval + live`, to prevent the PR eval run (`-m eval tests/eval/`) from attempting model downloads; the brief's `eval + live` suggestion is impractical given the live conftest isolation

---

## Architecture

### New module: `archon_search/eval/live_report.py`

```python
@dataclass
class MetricVerdict:
    name: str
    actual: float | None
    threshold: float | None          # floor for quality, ceiling for latency; None = report-only
    kind: Literal["floor", "ceiling"]
    status: Literal["pass", "fail", "skipped"]
    delta_from_threshold: float | None
    baseline_value: float | None
    delta_from_baseline: float | None

@dataclass
class LiveEvalReport:
    verdicts: list[MetricVerdict]
    overall_status: Literal["pass", "fail", "report_only"]
    generated_at: datetime
    eval_report: EvalReport

def load_live_thresholds(path: Path) -> EvalThresholds | None: ...
def build_live_report(report: EvalReport) -> LiveEvalReport: ...
def write_live_report_json(r: LiveEvalReport, path: Path) -> None: ...
def write_live_report_markdown(r: LiveEvalReport, path: Path) -> None: ...
```

### `EvalBaseline` additions (in `archon_search/eval/runner.py`)

Six optional fields, all default `None`. Populated in `live_baselines/baseline.json`; absent in `baselines/baseline.json` (backward-compatible):

```python
embedding_model_id: str | None = None       # "BAAI/bge-small-en-v1.5"
embedding_model_version: str | None = None  # importlib.metadata.version("fastembed")
reranker_model_id: str | None = None        # "Xenova/ms-marco-MiniLM-L-6-v2"
reranker_model_version: str | None = None   # importlib.metadata.version("fastembed")
archon_search_version: str | None = None    # importlib.metadata.version("archon-search")
captured_at: str | None = None              # ISO-8601 UTC: datetime.now(UTC).isoformat().replace("+00:00", "Z")
```

### Signature changes to `archon_search/eval/runner.py`

```python
async def _build_pipeline_with_eval_backends(
    db_path: Path,
    *,
    backend: Literal["deterministic", "live"] = "deterministic",
    embedding_model_name: str = "BAAI/bge-small-en-v1.5",
    reranker_model_name: str = "Xenova/ms-marco-MiniLM-L-6-v2",
) -> SearchPipeline: ...

async def run_eval_suite(
    corpus_root: Path,
    runtime_config_path: Path,
    thresholds_path: Path | None = None,
    baseline_path: Path | None = None,
    *,
    backend: Literal["deterministic", "live"] = "deterministic",
) -> EvalReport: ...
```

### Test layout

| File | Marker | Content |
|---|---|---|
| `tests/eval/test_live_report_acceptance.py` | `eval` | Pure-logic tests 2, 4, 5, 6, 7 — run in PR eval suite |
| `tests/eval/live/test_live_eval_suite.py` | `live_eval` | End-to-end smoke test — requires model weights |
| `tests/eval/live/test_live_acceptance.py` | `live_eval` | Live-backend tests 1, 3, 8, 9, 10 — requires model weights |

### New directories / files

- `tests/eval/live/` — isolated conftest; parent autouse shadowed by no-op override
- `tests/eval/live_baselines/` — committed baseline (post-calibration); `_artifacts/` git-ignored
- `tests/eval/live_thresholds.toml` — empty stub; `load_live_thresholds()` returns `None` until populated

---

## Task breakdown

### Phase 1 — Real Models Execute End-to-End
> **Releasable**: after Task 1.4 — `pytest -m live_eval tests/eval/live/test_live_eval_suite.py` runs on a machine with cached model weights, drives the full corpus through real fastembed + cross-encoder, and prints the eval metrics report to console. No JSON/MD artifacts yet, but the core execution path is fully proven and the existing deterministic lane is unchanged.

#### Task 1.1 — `live_eval` marker + directory scaffolding
- [x] **File**: `pyproject.toml`, `tests/eval/live/__init__.py`, `tests/eval/live_thresholds.toml`, `tests/eval/live_baselines/.gitkeep`, `tests/eval/live_baselines/_artifacts/.gitignore`
- **Depends on**: nothing
- **Description**:
  - `pyproject.toml`: add `"live_eval: full live-model eval suite; excluded from all default runs; triggered only by archon-search-eval-live.yml or explicit -m live_eval"` to `markers`; add `live_eval` to the exclusion selector in `addopts`: `-m 'not live and not eval and not benchmark and not integration and not live_eval'`
  - `tests/eval/live/__init__.py`: empty file
  - `tests/eval/live_thresholds.toml`: comment-only stub explaining it will be populated post-calibration; no `[quality_floors]` section (intentional — `load_live_thresholds()` returns `None`)
  - `tests/eval/live_baselines/_artifacts/.gitignore`: ignore all except `.gitignore` itself
- **Releasable**: directory structure exists; `@pytest.mark.live_eval` is registerable without `--strict-markers` warnings.
- **Tests (TDD)** — `tests/eval/test_live_report_acceptance.py` (new file, marked `eval`):
  - Unit: `test_live_eval_marker_excluded_from_default_run` — parse `pyproject.toml` and verify `"live_eval"` appears in both `markers` and the exclusion expression in `addopts`
  - Checkpoint: `uv run pytest tests/eval/test_live_report_acceptance.py::test_live_eval_marker_excluded_from_default_run --no-cov`

#### Task 1.2 — Parameterize `_build_pipeline_with_eval_backends()` + extend `run_eval_suite(backend=...)`
- [x] **File**: `archon_search/eval/runner.py`
- **Depends on**: Task 1.1
- **Description**:
  - Add `backend: Literal["deterministic", "live"] = "deterministic"`, `embedding_model_name: str = "BAAI/bge-small-en-v1.5"`, `reranker_model_name: str = "Xenova/ms-marco-MiniLM-L-6-v2"` to `_build_pipeline_with_eval_backends()`
  - `backend == "deterministic"` path: body unchanged
  - `backend == "live"` path: first `if os.environ.get("ARCHON_SEARCH_EVAL_BACKENDS") == "1": raise RuntimeError("live backend invoked while ARCHON_SEARCH_EVAL_BACKENDS=1 is set — deterministic fixture is active")`; then `Embedder(ModelEmbedder(model_name=embedding_model_name))` and `Reranker(ModelReranker(model_name=reranker_model_name))`; same `DocumentChunker(chunk_size=256)` and `DocumentParser()`
  - Add `backend: Literal["deterministic", "live"] = "deterministic"` to `run_eval_suite()` and pass it through to `_build_pipeline_with_eval_backends()`
  - Add `from typing import Literal` if not already imported; add `import os` if not present
- **Releasable**: `run_eval_suite(backend="live")` is callable; all existing `run_eval_suite()` calls implicitly use `"deterministic"`.
- **Tests (TDD)** — `tests/eval/test_live_report_acceptance.py`:
  - Unit: `test_deterministic_backend_uses_stubs` (brief test 2) — call `await _build_pipeline_with_eval_backends(tmp_path)` (default backend); assert `isinstance(pipeline._embedder._backend, EvalEmbedderBackend)` and `isinstance(pipeline._reranker._backend, EvalRerankerBackend)`
  - Unit: `test_live_backend_guard_fires_when_eval_env_set` — `monkeypatch.setenv("ARCHON_SEARCH_EVAL_BACKENDS", "1")`; call `await _build_pipeline_with_eval_backends(tmp_path, backend="live")`; expect `RuntimeError`
  - Unit: `test_run_eval_suite_default_backend_is_deterministic` — call `await run_eval_suite(CORPUS_ROOT, RUNTIME_CFG_PATH)` (no `backend` kwarg); verify returns `EvalReport` with `recall_at_1 >= 0.0`; confirms no regression on existing deterministic path
  - Checkpoint: `uv run pytest tests/eval/test_live_report_acceptance.py -k "stubs or guard or default_backend" --no-cov`

#### Task 1.3 — `tests/eval/live/conftest.py` (isolated autouse shadow + fixtures)
- [x] **File**: `tests/eval/live/conftest.py`
- **Depends on**: Task 1.1
- **Description**:
  - Define `_activate_deterministic_eval_backends` as a no-op autouse fixture with the **exact same name** as the parent conftest's autouse fixture — pytest fixture override by name prevents the parent's autouse from setting `ARCHON_SEARCH_EVAL_BACKENDS=1` for any test in this directory:
    ```python
    @pytest.fixture(autouse=True)
    def _activate_deterministic_eval_backends() -> None:
        pass  # shadows parent; live tests must not activate deterministic stubs
    ```
  - `live_corpus_root` session fixture: `Path(__file__).resolve().parent.parent` (the `tests/eval/` dir)
  - `live_runtime_cfg_path` session fixture: `live_corpus_root / "runtime.toml"`
  - `live_thresholds_path` session fixture: `live_corpus_root / "live_thresholds.toml"`
  - `live_artifacts_dir` function fixture: creates and returns `live_corpus_root / "live_baselines" / "_artifacts"` (via `mkdir(parents=True, exist_ok=True)`)
  - Do NOT redefine `--thresholds-path` CLI option (already registered in parent conftest)
- **Releasable**: tests under `tests/eval/live/` run without `ARCHON_SEARCH_EVAL_BACKENDS` being set.
- **Tests (TDD)** — `tests/eval/live/test_live_eval_suite.py` (verified in Task 1.4):
  - The no-op shadow is validated by `test_fixture_isolation` (Task 3.1)
- **Checkpoint**: `uv run pytest tests/eval/live/ --collect-only --no-cov` (no collection errors, no warnings)

#### Task 1.4 — `tests/eval/live/test_live_eval_suite.py` v1 (run + assert metrics + print report)
- [x] **File**: `tests/eval/live/test_live_eval_suite.py`
- **Depends on**: Task 1.2, Task 1.3
- **Description**:
  - Single async test `test_live_eval_suite_runs_and_generates_report`, marked `@pytest.mark.live_eval`
  - Uses `live_corpus_root`, `live_runtime_cfg_path`, `live_thresholds_path`, `live_artifacts_dir` fixtures from the local conftest
  - Call `report = await run_eval_suite(live_corpus_root, live_runtime_cfg_path, backend="live")`
  - Print `render_report(report)` so the output is visible in the pytest run log
  - Assert: `report.document_count > 0`, `report.query_count > 0`, `report.metrics.recall_at_1 >= 0.0`, `report.metrics.latency_p95_ms > 0.0`
  - This test will be extended in Task 2.5 to also generate JSON/MD report files
- **Releasable**: `pytest -m live_eval tests/eval/live/test_live_eval_suite.py` runs end-to-end with real models and passes on a machine with cached weights.
- **Tests (TDD)**: this file IS the test.
- **Checkpoint**: `uv run pytest -m live_eval tests/eval/live/test_live_eval_suite.py -v --no-cov` (requires model weights)

---

### Phase 2 — Structured Report: Verdicts, JSON, Markdown
> **Releasable**: after Task 2.5 — `pytest -m live_eval` now writes `live_eval_report.json` + `live_eval_report.md` to `tests/eval/live_baselines/_artifacts/`. The JSON includes per-metric `MetricVerdict` objects with PASS/FAIL status; the markdown embeds the existing `render_report()` output alongside a verdict table. `EvalBaseline` schema accepts model-version fields from future live baselines without breaking existing deterministic baseline files.

#### Task 2.1 — Extend `EvalBaseline` with 6 model-version optional fields
- [x] **File**: `archon_search/eval/runner.py`
- **Depends on**: nothing (independent schema change)
- **Description**:
  - Add 6 optional fields (all `= None`) to `EvalBaseline` dataclass after `waiver_ids`: `embedding_model_id`, `embedding_model_version`, `reranker_model_id`, `reranker_model_version`, `archon_search_version`, `captured_at` — all typed `str | None`
  - Update `load_baseline()`: after parsing `waiver_ids`, read each of the 6 fields with `raw.get(field)`. Validate type: if the value is not `None` and not a `str`, raise `ValueError(f"Baseline field {field!r} must be a string or null")`
  - `_BASELINE_REQUIRED_FIELDS` is unchanged — the 6 new fields are never required
- **Releasable**: `load_baseline()` reads existing `baselines/baseline.json` unchanged (all 6 fields absent → `None`); also reads a future `live_baselines/baseline.json` with all 6 populated.
- **Tests (TDD)** — `tests/eval/test_baseline_contract.py` (add to existing):
  - Unit: `test_deterministic_baseline_model_fields_are_none` — `load_baseline(BASELINES_PATH / "baseline.json")`; assert all 6 new fields are `None`
  - Unit: `test_live_baseline_model_fields_populated` — synthesize JSON dict with all 6 fields as valid strings; `load_baseline(tmp_json)`; assert all 6 populated correctly
  - Unit: `test_load_baseline_rejects_non_string_model_field` — parametrize over all 6 model-version field names: `embedding_model_id`, `embedding_model_version`, `reranker_model_id`, `reranker_model_version`, `archon_search_version`, `captured_at`; for each: set field value to `42`; assert `ValueError`
  - Checkpoint: `uv run pytest tests/eval/test_baseline_contract.py --no-cov`

#### Task 2.2 — `load_live_thresholds(path: Path) -> EvalThresholds | None`
- [x] **File**: `archon_search/eval/live_report.py` (new — create the file with this function first)
- **Depends on**: Task 1.1 (live_thresholds.toml stub must exist)
- **Description**:
  - Create `archon_search/eval/live_report.py`
  - `load_live_thresholds(path: Path) -> EvalThresholds | None`:
    - If `path` does not exist: `logger.warning("live_thresholds.toml not found at %s — report-only mode", path)`; return `None`
    - Call `load_thresholds(path)` (reused unchanged)
    - Catch `ValueError` (missing required sections, empty file): `logger.warning("live_thresholds.toml missing required sections (%s) — report-only mode", exc)`; return `None`
    - On success: return `EvalThresholds`
  - Import `load_thresholds` from `archon_search.eval.runner`; do not duplicate parsing logic
- **Releasable**: callers get `EvalThresholds | None` without exception handling; empty `live_thresholds.toml` stub yields `None` (report-only mode).
- **Tests (TDD)** — `tests/eval/test_live_report_acceptance.py`:
  - Unit: `test_load_live_thresholds_with_valid_file` (brief test 4, with-section case) — write a minimal valid TOML with `[quality_floors]` to `tmp_path`; assert returned object is `EvalThresholds` with all floors present
  - Unit: `test_load_live_thresholds_empty_stub` (brief test 4, without-section case) — use the actual `tests/eval/live_thresholds.toml` stub; assert returns `None`, no exception
  - Unit: `test_load_live_thresholds_missing_file` — non-existent path; returns `None`, no exception
  - Unit: `test_load_live_thresholds_malformed_toml` — write a file containing invalid TOML syntax (e.g., `"[[[\n"`); assert returns `None`, no exception
  - Checkpoint: `uv run pytest tests/eval/test_live_report_acceptance.py -k "load_live_thresholds" --no-cov`

#### Task 2.3 — `MetricVerdict` + `LiveEvalReport` + `build_live_report()`
- [x] **File**: `archon_search/eval/live_report.py`
- **Depends on**: Task 2.2 (file created)
- **Description**:
  - Add `MetricVerdict` and `LiveEvalReport` dataclasses (see Architecture section)
  - `build_live_report(report: EvalReport) -> LiveEvalReport`:
    - If `report.thresholds is None`: `overall_status = "report_only"`, all verdicts have `status="skipped"`, `threshold=None`
    - Otherwise walk the complete metric list:
      - Quality floors (from `EvalQualityFloors`): `recall_at_1`, `recall_at_3`, `recall_at_5`, `mrr`, `ndcg_at_5`, `ndcg_at_10`; and conditionally (if non-None in thresholds): `routing_accuracy`, `routing_mrr_centroid`, `routing_mrr_hybrid`, `routing_precision_at_1_centroid`, `routing_precision_at_1_hybrid`
      - Latency ceilings (from `EvalLatencyCeilings`): `latency_p50_ms`, `latency_p95_ms`
      - Note: `reranker_lift` has no threshold in `EvalQualityFloors` and must NOT produce a verdict
      - For routing metrics: if both `threshold` is non-None AND `actual` is non-None, compute verdict; if either is None, `status="skipped"`
      Read metric actual values via `getattr(report.metrics, field_name)` (field names are Python identifiers like `"recall_at_1"`). `EvalBaseline.metrics` uses the same underscore key format — `report.baseline.metrics.get("recall_at_1")` is correct.
      Then for each metric:
      - `delta_from_threshold = actual - floor` (floors) or `ceiling - actual` (ceilings) — positive means passing
      - `status`: `"pass"` if `delta_from_threshold > 0` (strictly positive), `"fail"` if `delta_from_threshold <= 0` (including exact equality — when `actual == floor` exactly, `delta_from_threshold == 0.0`, which is NOT positive and status is `"fail"`), `"skipped"` if threshold is `None`
      - `baseline_value = report.baseline.metrics.get(name)` if baseline present, else `None`
      - `delta_from_baseline = actual - baseline_value` if both non-None, else `None`
    - When `actual` is `None` for a metric, the verdict must be `status="skipped"` with `delta_from_threshold=None`
    - `overall_status`: `"report_only"` if no thresholds; `"fail"` if any `"fail"` verdict; `"pass"` otherwise
    - Never raises — records failure as `status="fail"`
- **Releasable**: `build_live_report(report)` returns a complete `LiveEvalReport` for any `EvalReport`.
- **Tests (TDD)** — `tests/eval/test_live_report_acceptance.py`:
  - Unit: `test_threshold_comparison_passes` (brief test 5) — all metrics above floors; `overall_status == "pass"`; every verdict `"pass"`
  - Unit: `test_threshold_comparison_fails` (brief test 6) — only `recall_at_1` below floor; `overall_status == "fail"`; the verdict for `recall_at_1` has `status == "fail"` and `delta_from_threshold < 0`; AND all other verdicts have `status == "pass"` (explicit check that exactly one metric fails)
  - Unit: `test_report_only_when_no_thresholds` — `EvalReport` with `thresholds=None`; `overall_status == "report_only"`; all verdicts `"skipped"`
  - Unit: `test_build_live_report_never_raises` — all metrics below all floors; returns `LiveEvalReport`, no exception
  - Unit: `test_build_live_report_with_none_actual_metrics` — synthesize an `EvalReport` where routing or optional metric fields are `None` (e.g., `routing_accuracy=None`); assert `build_live_report()` returns without raising and those verdicts have `status="skipped"`
  - Unit: `test_ceiling_metric_delta_sign_convention` — set `latency_p95_ms` actual ABOVE ceiling (should fail, delta negative) and BELOW ceiling (should pass, delta positive); verify sign convention: `delta_from_threshold = ceiling - actual` is positive when passing and negative when failing
  - Unit: `test_threshold_comparison_boundary_exact_equality` — set one quality metric `actual == floor`; assert `status == "fail"` and `delta_from_threshold == 0.0`
  - Unit: `test_build_live_report_with_partial_thresholds` — thresholds present but `EvalLatencyCeilings.latency_p50_ms = None`; assert verdict for `latency_p50_ms` has `status="skipped"`
  - Unit: `test_build_live_report_with_baseline` — thresholds and baseline both present; assert at least one verdict has `baseline_value` non-None and `delta_from_baseline` non-None
  - Checkpoint: `uv run pytest tests/eval/test_live_report_acceptance.py -k "comparison or report_only or never_raises or none_actual or ceiling_delta or boundary or partial_thresholds or with_baseline" --no-cov`

#### Task 2.4 — `write_live_report_json()` + `write_live_report_markdown()`
- [x] **File**: `archon_search/eval/live_report.py`
- **Depends on**: Task 2.3
- **Description**:
  - `write_live_report_json(r: LiveEvalReport, path: Path) -> None`:
    - Serialize to JSON with `indent=2`; top-level keys: `verdicts` (list of dicts with all 8 `MetricVerdict` fields), `overall_status`, `generated_at` (ISO-8601 string), `eval_report`
    - The `eval_report` key is a projected dict with exactly 4 keys — do NOT serialize the full `EvalReport` dataclass (it contains `traces: list[QueryEvalTrace]`, `corpus_root: Path`, and other non-serializable fields). Extract: `metrics` (dict mapping each `EvalMetrics` field name to its float value, via `dataclasses.asdict(report.metrics)`), `query_count` (int), `document_count` (int), `generated_at` (ISO-8601 string — convert `datetime` to string before serializing)
    - All `datetime` values must be pre-converted to ISO-8601 strings before passing to `json.dumps()`
    - `path.parent.mkdir(parents=True, exist_ok=True)` before writing
  - `write_live_report_markdown(r: LiveEvalReport, path: Path) -> None`:
    - Header: `# Live Eval Report — {r.overall_status.upper()}\n\nGenerated: {r.generated_at.isoformat()}`
    - Verdict table: columns `Metric | Actual | Threshold | Δ Threshold | Status | Baseline | Δ Baseline`
    - Fenced code block containing `render_report(r.eval_report)` with the "deterministic eval backends" disclaimer line stripped — strip any line from the rendered output containing "deterministic eval backends" before writing to the fenced block (it is factually incorrect for the live lane)
    - `path.parent.mkdir(parents=True, exist_ok=True)` before writing
- **Releasable**: both functions write valid, self-contained artifacts to any path.
- **Tests (TDD)** — `tests/eval/test_live_report_acceptance.py`:
  - Unit: `test_report_generation_format` (brief test 7) — synthesize `LiveEvalReport` (include at least one `MetricVerdict` with `actual=None`, `threshold=None`); write both to `tmp_path`; assert JSON parses with keys `{verdicts, overall_status, generated_at, eval_report}`; assert JSON `generated_at` value is a string matching ISO-8601 format (e.g., regex `r'\d{4}-\d{2}-\d{2}T'`), not a raw Python datetime object; assert `None` values in `MetricVerdict` serialize as JSON `null` (not the string `"None"`); assert Markdown contains `# Live Eval Report`, verdict table header, and a fenced block containing `=== Archon Search Eval Report ===`; additionally assert: (1) the verdict table contains at least one data row (not just the header); (2) no cell contains the Python literal string `"None"` (None values should render as empty string or `"—"`); (3) the status column contains at least one of `"pass"`, `"fail"`, or `"skipped"`
  - Unit: `test_write_json_creates_parent_dirs` — write to `tmp_path / "nested" / "dir" / "report.json"`; assert file exists
  - Checkpoint: `uv run pytest tests/eval/test_live_report_acceptance.py -k "report_generation or parent_dirs" --no-cov`

#### Task 2.5 — Extend `test_live_eval_suite.py` to generate report artifacts
- [x] **File**: `tests/eval/live/test_live_eval_suite.py`
- **Depends on**: Task 1.4 (existing test), Task 2.3, Task 2.4, Task 2.2
- **Description**:
  - Extend `test_live_eval_suite_runs_and_generates_report` (do not rename; adds to existing assertions):
    1. Load thresholds first: `thresholds = load_live_thresholds(live_thresholds_path)`
    2. Resolve baseline path: `live_baseline_path = live_corpus_root / "live_baselines" / "baseline.json"`; pass it if it exists: `baseline_path=live_baseline_path if live_baseline_path.exists() else None`. If `tests/eval/live_baselines/baseline.json` exists (post-calibration), pass it as `baseline_path` to `run_eval_suite()` so that `delta_from_baseline` values are populated in the report. Pre-calibration: `baseline_path=None`, all `delta_from_baseline` are `None`.
    3. Call `run_eval_suite` with thresholds and baseline: `report = await run_eval_suite(live_corpus_root, live_runtime_cfg_path, thresholds_path=live_thresholds_path if thresholds else None, baseline_path=live_baseline_path if live_baseline_path.exists() else None, backend="live")`. Note: `run_eval_suite` re-parses `live_thresholds_path` internally (double-load). This is intentional — `load_live_thresholds()` is called first only to guard against passing a non-None path to `run_eval_suite` when the file is empty/missing. Passing the path directly to `run_eval_suite` without this guard would error if `thresholds_path` points to an empty stub.
    4. Then build and write reports:
       - `live_report = build_live_report(report)`
       - `write_live_report_json(live_report, live_artifacts_dir / "live_eval_report.json")`
       - `write_live_report_markdown(live_report, live_artifacts_dir / "live_eval_report.md")`
    - Add assertions: both files exist; JSON parses with correct top-level keys; `live_report.overall_status in ("pass", "fail", "report_only")`
  - Add necessary imports (`build_live_report`, `load_live_thresholds`, `write_live_report_json`, `write_live_report_markdown`)
- **Releasable**: `pytest -m live_eval` now produces `live_eval_report.json` + `live_eval_report.md` alongside the printed console report.
- **Tests (TDD)**: this file IS the test; the new assertions above are the spec.
- **Checkpoint**: `uv run pytest -m live_eval tests/eval/live/test_live_eval_suite.py -v --no-cov` (requires model weights)

---

### Phase 3 — Full Spec Validation
> **Releasable**: after Task 3.1 — all 10 required acceptance tests from the brief are implemented. The 5 pure-logic tests (2, 4, 5, 6, 7 from brief) are already passing in the PR eval suite from Phases 1–2. The 5 live-backend tests (1, 3, 8, 9, 10) pass when run with model weights, proving isolation, model-version recording, calibration procedure, and latency stability.

#### Task 3.1 — `tests/eval/live/test_live_acceptance.py` (live-backend tests 1, 3, 8, 9, 10)
- [x] **File**: `tests/eval/live/test_live_acceptance.py`
- **Depends on**: Task 1.2, Task 1.3, Task 2.1, Task 2.3
- **Description**:
  - All 5 tests marked `@pytest.mark.live_eval`; use `live_corpus_root`, `live_runtime_cfg_path`, `live_artifacts_dir` fixtures from the local conftest
  - **`test_live_backend_uses_real_models`** (brief test 1):
    - `pipeline = await _build_pipeline_with_eval_backends(tmp_path, backend="live")`
    - Encode a short text: `vecs = pipeline._embedder._backend.encode(["hello world"])`
    - Assert `len(vecs[0]) == 384` (fastembed `BAAI/bge-small-en-v1.5` output dim, not the stub's 128)
    - Assert `pipeline._embedder.model_name == "BAAI/bge-small-en-v1.5"`
  - **`test_model_versions_recorded_in_baseline`** (brief test 3):
    - `import importlib.metadata`; assert `importlib.metadata.version("fastembed")` is a non-empty string matching semver pattern `r"^\d+\.\d+"`
    - Assert `importlib.metadata.version("archon-search")` is non-empty (verifies the metadata APIs available for calibration scripts are accessible at runtime)
    - Run a live eval; call `build_live_report(report)`; construct a synthetic `live_baselines/baseline.json` dict with all 6 model-version fields populated from `importlib.metadata.version(...)` + `report.metrics`; parse it via `load_baseline(tmp_json)`; assert all 6 fields are non-empty strings
  - **`test_calibration_procedure`** (brief test 8):
    - Run `report = await run_eval_suite(live_corpus_root, live_runtime_cfg_path, backend="live")`
    - Build the full live baseline dict with model-version fields + metrics from `report.metrics`; write to a tmp file
    - Load via `load_baseline(tmp_path / "baseline.json")`; assert `captured_at` matches `r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"`; assert all 6 model-version fields non-empty
    - The test writes baseline only to `tmp_path` — do NOT assert that `tests/eval/live_baselines/baseline.json` does not exist; that assertion breaks once real calibration is committed. The test is scoped to the pre-calibration workflow only.
  - **`test_fixture_isolation`** (brief test 9):
    - Assert `os.environ.get("ARCHON_SEARCH_EVAL_BACKENDS")` is not `"1"` at test entry (confirms no-op shadow is active)
    - Call `_build_pipeline_with_eval_backends(tmp_path, backend="deterministic")`; assert `isinstance(pipeline._embedder._backend, EvalEmbedderBackend)` (confirms stubs work in live directory without parent autouse)
    - Then call `await run_eval_suite(corpus_root, cfg, backend="live")` once
    - Assert `os.environ.get("ARCHON_SEARCH_EVAL_BACKENDS")` is still not `"1"` after
    - Note: the previous design ran 3 full `run_eval_suite` calls and asserted `r1.metrics == r3.metrics` — both are removed; that design was expensive and proved nothing about the isolation mechanism
  - **`test_latency_stability`** (brief test 10):
    - Run live eval twice: `r1` and `r2`
    - Assert `abs(r2.metrics.latency_p95_ms - r1.metrics.latency_p95_ms) / max(r1.metrics.latency_p95_ms, 1.0) < 0.5` (50% tolerance per brief)
    - Assert both `latency_p95_ms > 0.0`
    - Note: the 50% tolerance is intentionally loose to survive CI tenancy variance. This test is a documentation artifact proving the live lane completes twice, not an effective latency regression guard. Mark with `@pytest.mark.xfail(strict=False, reason="CI latency variance makes 50% tolerance unreliable")` if it flakes in practice.
- **Releasable**: all 10 acceptance tests from the brief are implemented; 5 pure-logic already pass in CI.
- **Tests (TDD)**: this file contains the tests.
- **Checkpoint**: `uv run pytest -m live_eval tests/eval/live/test_live_acceptance.py -v --no-cov` (requires model weights)

---

### Phase 4 — CI + Documentation
> **Releasable**: after Task 4.2 — a tag push triggers `archon-search-eval-live.yml` concurrently with `archon-search-release.yml`; the live eval report appears as a downloadable workflow artifact within ~25 minutes; `tests/eval/README.md` and the architecture docs reflect the two-lane eval strategy.

#### Task 4.1 — `.github/workflows/archon-search-eval-live.yml`
- [x] **File**: `.github/workflows/archon-search-eval-live.yml` (new)
- **Depends on**: Task 1.4, Task 2.5
- **Description**:
  - Triggers: `push: { tags: ["*"] }` and `workflow_dispatch: {}` (no inputs — the `calibrate` input is removed; it did nothing in v1 and implied behavior that doesn't exist)
  - `concurrency: { group: eval-live, cancel-in-progress: false }`
  - Single job `eval-live` on `ubuntu-latest`, `timeout-minutes: 30`
  - Steps in order:
    1. `actions/checkout@v4` with `fetch-depth: 0`
    2. `actions/setup-python@v5` `python-version: "3.12"`
    3. `astral-sh/setup-uv@v3`
    4. `actions/cache@v4`: paths `~/.cache/huggingface/hub/` and `~/.cache/fastembed/`; key `fastembed-${{ hashFiles('uv.lock') }}-${{ runner.os }}`
    5. `uv sync --dev`
    6. Run suite (`continue-on-error: true` so report is always uploaded even on failure):
       `uv run pytest -o addopts= --strict-markers -m live_eval tests/eval/live/ -v --no-cov`
    7. `actions/upload-artifact@v4`: name `live-eval-report`, path `tests/eval/live_baselines/_artifacts/`, `if: always()`
  - Workflow `name: Live Eval`; no reference to a `calibrate` input (calibration = reviewing the artifact and committing manually)
  - No dependency from `archon-search-release.yml` in either direction
- **Releasable**: tag push triggers both workflows concurrently; report artifact downloadable after ~25 min.
- **Tests (TDD)**: N/A — YAML file; validated by CI run after merge.
- **Checkpoint**: `python -c "import yaml; yaml.safe_load(open('.github/workflows/archon-search-eval-live.yml'))"` (syntax check)

#### Task 4.2 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to discover all documentation in the project affected by B6 and update each file. Files that must be updated:
    - `tests/eval/README.md`: add live eval lane section covering the `live_eval` marker, `tests/eval/live/` directory and its conftest isolation pattern, calibration procedure and outlier checklist (from brief), threshold formula (1.5× for latency, −0.02pp for quality), CI latency variance caveat, `live_thresholds.toml` lifecycle
    - `Documentation/Architecture/200_testing_strategy.md`: add `live_eval` tier row to the marker table; add it to the Mermaid pyramid diagram; update "Adding tests by failure mode" table
    - `Documentation/Architecture/510_release_and_environment_strategy.md`: note that `archon-search-eval-live.yml` runs concurrently with release on tag push; report-only in v1; one-line change to block in future
    - `Documentation/Architecture/110_component_catalog_and_layer_breakdown.md`: add `archon_search/eval/live_report.py` module entry with its key public symbols
  - Verify all acceptance criteria below are met before marking complete.
- **Releasable**: B6 is fully delivered and documented.
- **Acceptance criteria** (must all pass):
  - [ ] `uv run pytest -o addopts= --strict-markers -m 'not live and not eval and not benchmark and not integration and not live_eval'` exits 0 with coverage ≥ 85% (default suite unchanged)
  - [ ] `uv run pytest -o addopts= --strict-markers --cov=archon_search --cov-append -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/` exits 0 (deterministic eval unchanged, pure-logic acceptance tests pass)
  - [ ] `uv run pytest tests/eval/test_live_report_acceptance.py -v --no-cov` exits 0 (all 5 pure-logic acceptance tests pass without model weights)
  - [ ] `uv run pytest tests/eval/test_baseline_contract.py -v --no-cov` exits 0 (existing deterministic baseline loads with new optional fields as `None`)
  - [ ] `uv run pytest tests/eval/live/ --collect-only --no-cov` collects tests without warnings or errors
  - [ ] `uv run pytest --co -q --no-cov -m live_eval` lists both live test files without `PytestUnknownMarkWarning`
  - [ ] On a machine with cached model weights: `uv run pytest -m live_eval tests/eval/live/ -v --no-cov` exits 0 with `live_eval_report.json` and `live_eval_report.md` written to `tests/eval/live_baselines/_artifacts/`
  - [ ] `live_eval_report.json` parses as valid JSON with top-level keys `{verdicts, overall_status, generated_at, eval_report}` and `overall_status == "report_only"` (no thresholds committed yet)
  - [ ] `.github/workflows/archon-search-eval-live.yml` is valid YAML; triggers on `push: { tags }` and `workflow_dispatch`; uploads artifact with `if: always()`
  - [ ] All 10 acceptance tests from the brief are implemented (5 in `test_live_report_acceptance.py`, 5 in `test_live_acceptance.py`)
  - [ ] `tests/eval/README.md` documents the live eval lane; `Documentation/Architecture/200_testing_strategy.md` pyramid and table include `live_eval`
- **Tests (TDD)**: N/A — verification and documentation task.
- **Checkpoint**: manually confirm every criterion above is checked.
