# Feature Brief: B6 — Production-Model Eval Lane

## Problem

The current eval harness runs only against deterministic backends (SHA-256 hashed embeddings, lexical reranker). Real models (fastembed for dense embeddings, cross-encoder for reranking) are never validated before release — a ranking regression in production models could ship undetected because the PR gate uses stubs.

## Goal

Validate that ranking quality (recall, MRR, nDCG) and latency (p50, p95) don't regress when the real embedder and reranker are used, before a release ships. Operators and the release team need confidence that production models perform acceptably on the corpus.

## Users & Context

- **Release engineers** cutting a new version (triggered by tag push or manual dispatch)
- **Team members** investigating ranking changes or model regressions
- **Evaluators** tuning thresholds and understanding production model performance

The team is in the state of having locked baseline deterministic metrics; now they need to know the production model story to unblock Phase C ranking features (HyDE, RAG Fusion, etc.).

## Core Flow

1. **Trigger**: Developer pushes a version tag (via `release.sh`) or manually triggers `archon-search-eval-live.yml`.
2. **Eval Lane Runs**: GitHub Actions runs the live eval lane (`pytest -m live_eval`) with real fastembed + cross-encoder backends.
3. **Metrics Collected**: Ranking (recall@1/3/5, MRR, nDCG@5/10) and latency (p50_ms, p95_ms) are measured against the committed corpus.
4. **Report Generated**: `archon_search.eval.live_report.build_live_report()` compares metrics against thresholds in `live_thresholds.toml`, emits a structured `LiveEvalReport` (pass/fail per metric, non-raising), and writes `live_eval_report.json` + `live_eval_report.md` to `tests/eval/live_baselines/_artifacts/`.
5. **Report Posted**: Workflow uploads artifacts; if triggered from a PR, markdown is posted as a comment.
6. **Release Proceeds**: Tag push is not blocked by eval results (report-only, by design). Team reviews report asynchronously; if a regression is critical, they investigate (downgrade model version to isolate root cause) and decide whether to proceed.

## In Scope

### Baselines & Thresholds

- `tests/eval/live_baselines/baseline.json` and `tests/eval/live_baselines/baseline.md` — production-model baseline (independent of deterministic `baselines/baseline.json`). Extends `EvalBaseline` schema with model version fields — see **Implementation Details → Model Version Tracking**.
- `tests/eval/live_thresholds.toml` — separate file from `thresholds.toml` (Approach A). Same TOML schema as the deterministic thresholds file: `[quality_floors]`, `[latency_ceilings]`, `[policy]`. All sections optional; missing/empty = report-only mode.

### Backend Selection Mechanism

- **Marker-based isolation**: add `live_eval` to pytest markers in `pyproject.toml`. Tests under `tests/eval/live/` carry `@pytest.mark.live_eval` (not the `eval` marker).
- **Directory isolation**: `tests/eval/live/conftest.py` (new, does NOT inherit the autose deterministic fixture from `tests/eval/conftest.py` — pytest's directory-scoped autouse semantics prevent cross-talk).
- **Test module**: `tests/eval/live/test_live_eval_suite.py` — mirrors `tests/eval/test_eval_suite.py` but calls `run_eval_suite(..., backend="live")`.
- **Pipeline factory**: refactor `_build_pipeline_with_eval_backends(db_path)` to accept `backend: Literal["deterministic", "live"] = "deterministic"`. When `backend == "live"`, constructs real `Embedder(FastembedBackend(...))` and `Reranker(CrossEncoderBackend(...))` (using production-ready factories already in `archon_search.embedder` / `archon_search.reranker`). No parallel function; single function, parameterized.
- **Defense-in-depth guard**: when `backend == "live"`, assert `os.environ.get("ARCHON_SEARCH_EVAL_BACKENDS") != "1"` — the env-var is set only by the deterministic autouse fixture. If live path sees it, crash loudly (prevents cross-wiring).

### Report Generation

- **Module**: new `archon_search/eval/live_report.py`. Public API:
  ```python
  @dataclass
  class MetricVerdict:
      name: str
      actual: float | None
      floor: float | None
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

  def build_live_report(report: EvalReport) -> LiveEvalReport: ...
  def write_live_report_json(r: LiveEvalReport, path: Path) -> None: ...
  def write_live_report_markdown(r: LiveEvalReport, path: Path) -> None: ...
  ```
- **Comparison logic**: walks metrics (quality floors, latency ceilings), produces one `MetricVerdict` per metric. Non-raising — records `status="fail"` instead of raising exceptions. Staleness errors become verdict entries.
- **Overall status**: `"report_only"` (no thresholds loaded), `"pass"` (all verdicts pass/skipped), or `"fail"` (any verdict fails).
- **Output**: JSON (authoritative, for blocking gate + tooling) + Markdown (human-readable, embeds existing `render_report()` as fenced block). Files written to `tests/eval/live_baselines/_artifacts/` (git-ignored directory; only baseline.json/baseline.md are committed).
- **GitHub Actions integration**: `.github/workflows/archon-search-eval-live.yml` triggers on `push: { tags: }` and `workflow_dispatch: { inputs: { calibrate: boolean } }`. Runs live suite, generates report, uploads artifacts. Workflow always exits 0 in v1 (report-only).

### Acceptance Test Suite

- Covering backend selection, threshold comparison, model version recording, calibration, fixture isolation, latency stability. See **Implementation Details → Test Plan** for full list of 10 required tests.
- All tests live under `tests/eval/` with the `eval` marker. Live-backend tests additionally carry a `live` marker (skipped by default on machines without cached models, run in the eval-live workflow).

### Calibration Procedure

Semi-automated via `workflow_dispatch` with mandatory human review gate before any threshold commit. See **Implementation Details → Calibration Procedure**.

## Out of Scope

- **Blocking releases on eval failure** — report-only for now; team decides whether to proceed. One-line workflow change to flip to blocking later.
- **Scheduled nightly/weekly eval runs** — deferred; tag-push + manual dispatch sufficient for now.
- **Per-collection model selection** — that is item C1; B6 evaluates one model pair per run.
- **ADR for two-lane eval strategy** — separate follow-up (document the deterministic + live split).
- **Hashed doc_id telemetry** — that is item D8; B6 does not change telemetry.
- **Latency gating on deterministic baseline** — remains report-only; only live thresholds gate latency.
- **PR comment posting** — out of scope for V1 (tag-push triggers have no PR context). Revisit if a PR-triggered variant is added.
- **Auto-commit of calibration baseline** — CI never commits to the repo; calibration results are uploaded as artifacts for human review and manual commit.

## Key Decisions

- **Two separate baselines** (`baselines/baseline.json` for deterministic, `live_baselines/baseline.json` for production models): Keeps the stories clean. Deterministic is regression guard for code changes; live is regression guard for model quality. Re-uses existing `EvalBaseline` schema (with model version additions).
- **Backend selection via separate pytest marker + directory isolation (Option A)**: Tests live in `tests/eval/live/` with marker `@pytest.mark.live_eval`. Pytest's directory-scoped autouse semantics prevent the parent `conftest.py` deterministic fixture from applying, providing structural isolation. More robust than flag-based conditionals in a shared autouse fixture. Defense-in-depth env-var assertion catches any cross-wiring at runtime.
- **Separate `live_thresholds.toml` (Approach A)**: Mirrors the separate-baselines philosophy. File has independent lifecycle. Reuses `load_thresholds()` unchanged — no forked parser.
- **Single parameterized factory** `_build_pipeline_with_eval_backends(db_path, *, backend=...)` (not parallel function): Surgical change; the function is small and orchestration is identical across backends.
- **Non-raising `build_live_report()` returning a struct**: Report-only mode is the v1 contract. Struct cleanly separates "did we measure metrics" (always true) from "did we gate" (flipped in future). Blocking gate is a one-line workflow change (call `assert_thresholds()` after report generation).
- **Markdown + JSON output, JSON authoritative**: JSON drives blocking gate + tooling; Markdown is for human review. Existing `render_report()` text embedded verbatim in Markdown avoids divergence.
- **Latest model versions** — Always test against latest fastembed + cross-encoder upstream. If regression detected, downgrade `pyproject.toml` to last-tested version locally to isolate code vs model.
- **Calibration is human-driven**, not auto-populated — first run produces a report-only artifact; a human reviews and commits `live_baselines/baseline.json` + `live_thresholds.toml`. Preserves the floor-drop waiver policy (matching deterministic lane's `baselines/regenerate.py` + manual review pattern). No CI commits.
- **Latency in CI is a coarse guard** — CI runner tenancy causes 50–200% variance. Live latency thresholds are intentionally loose (1.5x calibration p95) and will not catch small regressions. This is acceptable and explicitly documented; latency tuning belongs in local benchmarks.

## Implementation Details

### Model Version Tracking

**EvalBaseline schema additions**:

| Field | Type | Source | Example |
|---|---|---|---|
| `embedding_model_id` | `str` | `config.embedding.model_name` | `"BAAI/bge-small-en-v1.5"` |
| `embedding_model_version` | `str` | `importlib.metadata.version("fastembed")` | `"0.4.2"` |
| `reranker_model_id` | `str` | `config.reranker.model_name` | `"cross-encoder/ms-marco-MiniLM-L-6-v2"` |
| `reranker_model_version` | `str` | `importlib.metadata.version("sentence-transformers")` | `"3.0.1"` |
| `archon_search_version` | `str` | `importlib.metadata.version("archon-search")` | `"26.5.123"` |
| `captured_at` | ISO-8601 `str` | `datetime.utcnow().isoformat() + "Z"` | `"2026-05-29T14:22:01Z"` |

Deterministic baseline sets these to `null` or `"deterministic-stub"` (not version-relevant).

**`baseline.json` format (live)**:
```json
{
  "embedding_model_id": "BAAI/bge-small-en-v1.5",
  "embedding_model_version": "0.4.2",
  "reranker_model_id": "cross-encoder/ms-marco-MiniLM-L-6-v2",
  "reranker_model_version": "3.0.1",
  "archon_search_version": "26.5.123",
  "captured_at": "2026-05-29T14:22:01Z",
  "metrics": {
    "recall@1": 0.71,
    "recall@3": 0.88,
    "recall@5": 0.93,
    "mrr": 0.79,
    "ndcg@5": 0.82,
    "ndcg@10": 0.85,
    "p50_ms": 42.0,
    "p95_ms": 110.0
  }
}
```

**Rollback procedure** (manual, no new CLI flag):
1. Inspect failing run's report; note recorded model versions.
2. Edit `pyproject.toml` to pin `fastembed==<last-good-version>` (or reranker package similarly).
3. Run `uv sync`, then `uv run pytest -m eval --backend=live` locally.
4. If metrics recover → upstream regression; keep pin and open issue.
5. If metrics still fail → our code regression; revert pin and bisect.

### Latency Threshold Formula

**Precise formula** (computed once at calibration, written into `[latency_ceilings]` in `live_thresholds.toml`):

```
live_thresholds.p50_ms = calibration_p50_ms * 1.5
live_thresholds.p95_ms = calibration_p95_ms * 1.5
```

**Ranking metric tolerance** (real models have minor floating-point non-determinism):

```
live_thresholds.<ranking_metric> = calibration_value - 0.02   # 2 percentage point absolute floor
```

Empirically, fastembed + cross-encoder produce identical ranks for >98% of queries; the 2pp band absorbs residual variance and tie-boundary reordering. If calibration shows larger variance, widen to 3pp with documentation.

**CI variance note** (add to `tests/eval/README.md`):
> CI latency variance is 50–200% due to shared runner tenancy. The 1.5x live-latency thresholds are intentionally conservative and will not catch small regressions (<50%). Latency in this lane is a "smoke" guard against major slowdowns. For fine-grained latency tracking, use the `benchmark` marker on dedicated hardware.

### Test Plan

All tests under `tests/eval/`, marked `eval`. Live-backend tests additionally marked `live` (skipped by default, run in eval-live workflow).

| # | Test | Purpose | Setup | Assertions |
|---|---|---|---|---|
| 1 | `test_live_backend_uses_real_models` | Verify `backend="live"` loads fastembed + cross-encoder, not stubs | Invoke runner with `backend="live"` on fixture corpus; deterministic stub autouse must not apply | Embeddings have length matching fastembed model dim (e.g., 384, not 8); `embedding_model_version` recorded and matches `importlib.metadata.version("fastembed")` |
| 2 | `test_deterministic_backend_uses_stubs` | Verify default `backend="deterministic"` still uses SHA-256 stubs | Invoke runner with `backend="deterministic"`; autouse stub fixture active | Embeddings are 8-dim hashed vectors; `embedding_model_version` is `null` or `"deterministic-stub"` |
| 3 | `test_model_versions_recorded_in_baseline` | Verify all six model-id/version/timestamp fields populated on live run | Run live eval, capture baseline JSON | All six fields present, non-empty strings; `captured_at` is valid ISO-8601; versions match `importlib.metadata.version()` |
| 4 | `test_live_thresholds_loaded` | Verify loader parses `[live_thresholds]` and degrades gracefully when missing | Load `live_thresholds.toml` with and without `[quality_floors]` | With section: thresholds object has all 8 keys; without: loader returns "no thresholds" and emits warning, no exception |
| 5 | `test_threshold_comparison_passes` | Verify pass path of report | Synthetic metrics all >= thresholds; feed to report generator | Report `overall_status == "pass"`; every verdict has `status == "pass"`; exit 0 |
| 6 | `test_threshold_comparison_fails` | Verify fail path of report | Synthetic metrics with `recall@1` below threshold | Report `overall_status == "fail"`; only `recall@1` verdict fails; report names metric and delta; exit 0 (report-only mode) |
| 7 | `test_report_generation_format` | Verify JSON and Markdown well-formedness | Run report generator on synthetic result | JSON parses with keys `{verdicts, overall_status, generated_at, eval_report}`; Markdown contains metric table + status header |
| 8 | `test_calibration_procedure` | Verify calibration creates baseline.json with correct schema | Run live eval in calibration mode; capture output baseline | File exists; JSON validates (all six metadata fields + `metrics` block); existing baselines not overwritten silently |
| 9 | `test_fixture_isolation` | Verify deterministic and live runs don't cross-contaminate in same pytest session | Run deterministic eval, then live, then deterministic again | First and third deterministic runs produce identical metrics; live run between them doesn't mutate stub fixture state |
| 10 | `test_latency_stability` | Sanity-check latency tolerance on same hardware | Run live eval twice back-to-back | `abs(run2.p95_ms - run1.p95_ms) / run1.p95_ms < 0.5` (50% tolerance — loose, account for runner tenancy) |

Tests 1, 3, 8, 9, 10 require real model weights (marked `live`, skipped by default). Tests 2, 4, 5, 6, 7 are pure logic (marked `eval` only, run in default suite).

### Calibration Procedure

**Mode**: Semi-automated via `workflow_dispatch` with mandatory human review before commit. CI never commits to the repo.

**Steps**:
1. Maintainer triggers `archon-search-eval-live.yml` from Actions UI with input `calibrate: true`.
2. Workflow runs live eval, uploads `baseline.json`, `baseline.md`, and report as artifacts.
3. Maintainer downloads artifacts, inspects metrics, and applies outlier checklist (below).
4. If accepted, maintainer opens a PR adding `live_baselines/baseline.json`, `live_baselines/baseline.md`, and `[live_thresholds]` section (computed via formula above) to `thresholds.toml`.
5. CODEOWNERS-enforced review required on `tests/eval/live_baselines/` and `tests/eval/thresholds.toml`.
6. On merge, subsequent tag-push workflows compare against committed baseline.

**Outlier detection checklist**:
- If `p95_ms > 2.0 * <local dev-machine p95>` → re-trigger; suspect cold download or noisy runner.
- If any ranking metric >5pp below deterministic baseline → investigate before committing; likely model/config mismatch.
- If two consecutive calibration runs differ >10% on any metric → do not commit; investigate variance first.

**Approval gate**: Calibration baseline must be reviewed by a project maintainer (CODEOWNERS) before thresholds are merged.

### CI Configuration

**Model weight caching** (in `.github/workflows/archon-search-eval-live.yml`):

Use `actions/cache@v4` keyed on `fastembed-${{ hashFiles('uv.lock') }}-${{ runner.os }}` (uv.lock pins fastembed transitively via lock hash).

Cache paths:
- `~/.cache/huggingface/hub/` (HuggingFace model weights)
- `~/.cache/fastembed/` (fastembed's local cache directory — verify exact path against fastembed docs at implementation time)

Separate cache entry for cross-encoder under same key prefix.

**Runtime estimate / timeout**:
- Cold run (no cache): ~3 min model download + ~2 min ingest + ~2 min search/rerank ≈ 7 min total.
- Warm run (cache hit): ~1 min total.
- Workflow `timeout-minutes: 20` to absorb runner tenancy variance and retries.

**Workflow orchestration**:
- `archon-search-eval-live.yml` and `archon-search-release.yml` run **concurrently** on tag push. No dependency; they share no resources.
- Release workflow does NOT wait for eval (consistent with "report-only" decision).
- SLA: Eval report appears as artifact within ~25 minutes of tag push; release publishes independently.

## Edge Cases & Constraints

- **Model upstream changes**: Workflow always uses latest fastembed/cross-encoder. If regression detected, downgrade `pyproject.toml` to last-tested version locally (manual rollback procedure in **Implementation Details → Model Version Tracking**).
- **Latency variance**: p50/p95 vary across runs due to system load. Live thresholds set conservatively at 1.5× calibration value.
- **CI runner tenancy**: Shared GitHub runners exhibit 50–200% latency variance. Lane will not catch small regressions; this is accepted and documented in `tests/eval/README.md`.
- **Manual dispatch for debugging**: Workflow can be triggered via Actions UI, including with `calibrate: true` input for calibration runs.
- **Non-determinism in real models**: fastembed + cross-encoder have small floating-point variance. Ranking thresholds use 2pp absolute tolerance band.
- **Calibration outliers**: Single run can produce unrepresentative baseline (cold download, noisy runner). Outlier checklist (under **Calibration Procedure**) must be applied before committing; if violated, re-run rather than commit.
- **Cache miss on model upgrade**: First post-upgrade run is cold (~7 min); planned and inside 20-minute timeout.
- **Missing `[live_thresholds]` section**: Loader degrades gracefully (warning, no error). Useful during transition between B6 code landing and calibration PR merging.

## Open Questions

None — all architectural decisions locked.

## Future Iterations

- **Blocking gate** — Once the lane runs clean for 2–3 releases, add gating so eval failure prevents release (one-line workflow change; no code change required).
- **Scheduled nightly eval** — Add separate scheduled workflow (weekly or on-demand) to catch model drift between releases.
- **Per-model baselines** — Track separate baselines if per-collection model selection (C1) ships; one baseline per supported model pair.
- **Warm-up readiness signal** — B2 (deeper health) may provide a `warm_model_status` signal that gates the eval lane's initialization step.
- **Self-relative latency metric** — If absolute thresholds prove too noisy, switch to ratio (e.g., live p95 / deterministic p95 on same runner) which cancels much tenancy noise.
- **PR-comment integration** — If `pull_request` trigger is added (e.g., for changes to embedder/reranker), wire up bot comment with eval delta.

## Recommendation

**B6 is the right move now.** It directly unblocks Phase C ranking features (HyDE, RAG Fusion, multilingual); without the production-model eval lane, we can't validate whether those features improve ranking. Starting async/report-only is safe and pragmatic — the team can lift it to blocking once confidence grows. The calibration run is a one-time cost; ongoing maintenance is minimal (review reports after tag pushes). Latency metrics are especially valuable: they catch model upgrades that slow down ranking, a risk that purely deterministic eval can't see.

The team's willingness to downgrade models for root-cause analysis (code vs model) makes latest-model selection viable — you'll know quickly if a regression is yours or upstream's.

---

Run `/plan-maker Documentation/Backlog/b6-production-model-eval-lane-brief.md` to turn this into an implementation plan.
