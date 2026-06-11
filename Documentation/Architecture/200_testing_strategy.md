**Purpose**: Define the test pyramid for `archon-search`, the markers that gate each layer, the coverage discipline, and the role of the evaluation harness as the regression gate for retrieval quality.
**Audience**: Contributors writing tests, reviewers gating PRs, and CI maintainers.
**Status**: Draft
**Last reviewed**: 2026-06-10
**Next review**: 2026-09-10

# Testing Strategy

Tests in `archon-search` are split across four pytest markers plus a default (unmarked) tier. The default tier runs on every push; the marked tiers are opt-in and target specific failure modes (live network, deterministic eval quality, performance, integration). This document is the canonical map. The retrieval-quality side of testing — fixtures, thresholds, baselines, waiver policy — is owned by `tests/eval/README.md`; this document only points at it.

## Principles

1. **TDD-first.** Per `CLAUDE.md` (Code Style): write tests first, start with happy paths, then edge cases. Maintain 85%+ coverage. Resolve all warnings.
2. **Default run is fast and hermetic.** No network, no real ML models, no running server. ONNX downloads and tokenizer subprocesses are stubbed in `tests/conftest.py` before pytest discovery.
3. **Slower or environment-dependent tests are marker-gated.** `live`, `eval`, `benchmark`, `integration` are excluded from the default run (`pyproject.toml [tool.pytest.ini_options]`). Run them explicitly.
4. **Eval is the retrieval-quality gate, not the unit-test tier.** Retrieval, reranking, and routing changes are validated by the `eval` marker with committed thresholds in `tests/eval/thresholds.toml`. Latency in the harness is a regression guard, not a production indicator. #Unverified (intent statement; consistent with `baseline.json` leaving latency metrics `null`, but not falsifiable from code alone)
5. **Coverage gating is a single-run concept.** `--cov-fail-under=85` applies to the default single-run invocation. CI that runs multiple pytest invocations must accumulate coverage into a single dataset before applying the threshold — `.github/workflows/archon-search-pr.yml` does this by passing `--cov-append` to both the default and eval steps (writing into one `.coverage` file) and then running `coverage report --fail-under=85`. `coverage combine` is only needed when shards write parallel-mode files; the current PR workflow deliberately skips it. **Never bake `--no-cov` into `addopts`.**

## The pyramid

```mermaid
flowchart TB
  subgraph default[Default run — &#40;not live and not eval and not benchmark and not integration and not live_eval&#41;]
    U[Unit tests<br/>tests/**/test_*.py<br/>stubs from tests/_search_stubs.py]
  end
  subgraph gated[Marker-gated]
    I[integration<br/>real components<br/>local infra]
    E[eval<br/>tests/eval/<br/>deterministic backends]
    B[benchmark<br/>requires running server<br/>auto-skips if unreachable]
    L[live<br/>real network / external services]
    LE[live_eval<br/>tests/eval/live/<br/>real model weights]
  end
  default --> CI[CI: coverage gate ≥ 85%]
  gated -.-> CI
```

### Default tier (unit)

- Invocation: `uv run pytest`.
- Selector (from `pyproject.toml`): `-m 'not live and not eval and not benchmark and not integration'`.
- Parallelism: the default run uses `pytest-xdist` with `-n auto --dist=loadgroup`. `--dist=loadgroup` distributes ungrouped tests individually across workers; tests with `pytestmark = pytest.mark.xdist_group("mcp")` (16 files that mutate `sys.modules["fastmcp"]`) are co-located on one worker to prevent `sys.modules` contamination; tests with `xdist_group("install")` (3 files that compete on `~/.archon-search/.install.lock`) are co-located on another. The `connected_store` fixture is session-scoped (one `SearchStore` per xdist worker); `col_name` mints a UUID-suffixed name to prevent cross-test collision.
- Test infra: `tests/conftest.py` installs ML stubs at module import time via `_search_stubs.install_stubs()`. It also injects `ARCHON_SEARCH_API_KEY = "0" * 64` so `create_app()` always sees a known key.
- Serial escape hatch: `uv run pytest -n0` produces identical results in serial mode. Use `-n0 -x` for fail-fast isolation (xdist workers continue until their current test finishes) and `-n0 -s` for stdout passthrough (suppressed by xdist).
- Coverage combining: `pytest-cov` natively supports xdist. Workers write `.coverage.workerN` files which the main process combines before applying `--cov-fail-under=85`. This applies to single-invocation runs only; CI requires `-n0` to avoid interference with multi-step `--cov-append` accumulation.
- Adding a test: place it under `tests/`, do not add a marker — the default selector will pick it up. Pipeline tests live under `tests/pipeline/` (`test_pipeline_ingest.py`, `test_pipeline_search.py`, `test_pipeline_multi.py`); shared helpers are in `tests/pipeline/conftest.py`.

### `integration` — `uv run pytest -m integration`

Integration tests exercise real components against local infrastructure. They are excluded from the default run because they are slower and may require local setup. Use this marker when a test needs a real `SearchStore`, real LanceDB index on disk, or real OS service interactions — anything that the stubs in `conftest.py` deliberately replace.

- CI triggers: `archon-search-pr.yml` runs the integration suite on every PR, and `archon-search-release.yml` re-runs it on every tag push (both with a disk-backed `--basetemp=/var/tmp/archon-search-it` because the GitHub-hosted runner's `/tmp` is tmpfs and crash-injection tests skip on tmpfs). Both workflows pass `-m integration tests/` with `--cov-append`, so integration coverage rolls into the same `.coverage` file as the default + eval steps before the `--fail-under=85` gate runs.

### `eval` — `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/`

The eval marker runs the offline retrieval-quality harness. It is the **authoritative regression gate** for the retrieval, reranking, and routing pipelines.

- **Backends are deterministic and label-blind** (`archon_search/eval/backends.py`). `EvalEmbedderBackend` produces SHA-256-based token-hash vectors; `EvalRerankerBackend` is a BM25-inspired lexical reranker. These are corpus-aware (scores vary with text) but never see labels, so the harness measures the *pipeline* against fixed data — not a hand-tuned model.
- **Fixtures** live under `tests/eval/`: `documents.jsonl`, `queries.jsonl`, `labels.jsonl`, `corpus/`, `routing/`, plus the committed `runtime.toml` used by the harness.
- **Thresholds** in `tests/eval/thresholds.toml` are *floors* (quality metrics) and *ceilings* (latency, when set). Floors must be `≤` the measured baseline in `tests/eval/baselines/baseline.json` — never above. Current floors: `recall_at_1 = 0.86`, `recall_at_3 = 0.94`, `recall_at_5 = 0.98`, `mrr = 1.0`, `ndcg_at_5 ≈ 0.976`, `ndcg_at_10 ≈ 0.979`, `routing_accuracy ≈ 0.926`. Latency ceilings are intentionally unset in v1 — latency is report-only until production-comparable backends exist.
- **Lowering a floor by more than `policy.max_floor_drop_without_waiver` (0.05)** requires a written waiver under `baselines/baseline.json::waiver_ids` and a rationale in `baselines/baseline.md`.
- **Read `tests/eval/README.md` before changing thresholds, fixtures, or baselines.** That document is the authoritative maintenance guide; this section only pins down its place in the strategy.

Unmarked eval units (the fast contract / fixture / metric tests under `tests/eval/`) run under the default selector — they are not gated by the marker.

### `benchmark` — `uv run pytest -m benchmark`

Performance benchmarks. The flagship file is `tests/benchmark_routing_latency.py`, which compares in-process `MultiCollectionRouter.get_pre_context` against `POST /route` over 100 iterations after 3 warmups. It **auto-skips when the server is not reachable** (`pytest.skip` on a failed `GET /health` probe), so it is safe in CI. See `Architecture/210_performance_and_scalability.md` for the targets and the co-located-embedder fallback path.

### `live` — `uv run pytest -m live`

Tests that hit real network or external services. Excluded from the default run for the same reasons as `integration`, but specifically called out so reviewers know a test depends on something outside the sandbox.

### `live_eval` — `uv run pytest -m live_eval tests/eval/live/ -v --no-cov`

Runs the eval corpus through **real fastembed + cross-encoder model weights** — the same models used in production. Excluded from the default run because it requires model downloads (~200 MB) and GPU/CPU inference time.

- Tests live under `tests/eval/live/`. The directory has its own `conftest.py` that no-ops the parent autouse fixture, ensuring `ARCHON_SEARCH_EVAL_BACKENDS` is never set to `"1"` (which would inject deterministic stubs).
- Thresholds live in `tests/eval/live_thresholds.toml`. Until calibrated, the file is a comment-only stub and `load_live_thresholds()` returns `None`, making all runs **report-only** (no gates fire).
- The live baseline (`tests/eval/live_baselines/baseline.json`) is absent until the first calibration run. See `tests/eval/README.md` for the calibration procedure and threshold formula (quality floors: −0.02 pp; latency ceiling: 1.5× baseline).
- CI triggers: `archon-search-eval-live.yml` runs on every tag push (concurrently with the release workflow) and on manual `workflow_dispatch`. The test step uses `continue-on-error: true` and uploads the report artifact regardless of outcome — pre-calibration report-only mode must never block a release.

## Coverage

`pyproject.toml [tool.pytest.ini_options] addopts` includes `--cov=archon_search --cov-report=term-missing --cov-fail-under=85`.

- This threshold is intended for the **default single-run invocation**. A single `uv run pytest` produces a single coverage dataset, and 85% gates that dataset.
- **CI runs that invoke pytest more than once must accumulate coverage into one dataset before applying the threshold.** The PR workflow (`.github/workflows/archon-search-pr.yml`) does this by overriding `addopts` (`-o addopts=`) on each step, passing `--cov-append` so both the default and eval invocations write into the same `.coverage` file, and then running a separate `coverage report --fail-under=85`. CI also passes `-n0` explicitly to disable xdist parallelism — each invocation's internal combine step could otherwise overwrite `.coverage` with only the current run's worker shards, silently dropping coverage from prior invocations. If a future CI layout instead writes parallel-mode shard files (e.g. matrix shards each producing `.coverage.<id>`), it must `coverage combine` them first; the current workflow doesn't, because there's nothing to combine. Applying `--cov-fail-under=85` to an individual partial run would reject correct code, because each invocation only sees part of the codebase.
- **`--no-cov` is a local-only override.** Use it on the CLI when iterating (`uv run pytest --no-cov ...`). It must never be baked into `addopts` — doing so silently disables the coverage gate for every contributor.

## Adding tests by failure mode

| You want to test...                                | Tier        | Marker                  | Notes                                                                                |
| -------------------------------------------------- | ----------- | ----------------------- | ------------------------------------------------------------------------------------ |
| A pure function or a route in isolation            | Unit        | none                    | Use `auth_headers`, `connected_store`, `col_name` fixtures from `conftest.py`.       |
| Real LanceDB + real pipeline end-to-end            | Integration | `integration`           | Excluded from default to keep CI fast. #Unverified (marker registered and excluded; adoption in current tests not sampled) |
| A retrieval / routing / reranking quality change   | Eval        | `eval`                  | Update `baseline.json` + `baseline.md` if measured metrics shift. See `tests/eval/README.md`. |
| Latency regression on `POST /route`                | Benchmark   | `benchmark`             | Server must be running; auto-skips otherwise.                                        |
| Behaviour that requires real network               | Live        | `live`                  | Justify the dependency in the PR description. #Unverified (marker registered; no in-tree `@pytest.mark.live` usage sampled) |
| Quality regression with real model weights         | Live eval   | `live_eval`             | Runs on tag push via `archon-search-eval-live.yml`. Requires model weight download. See `tests/eval/README.md` (live eval lane section). |

## See also

- `tests/eval/README.md` — authoritative maintenance guide for the eval harness (fixtures, thresholds, baselines, waivers).
- `Architecture/100_system_architecture_overview.md` — what each module under test does in the runtime pipeline.
- `Architecture/140_error_handling_strategy.md` — failure-mode taxonomy that informs which tests to write.
- `Architecture/210_performance_and_scalability.md` — performance targets that the `benchmark` marker validates.
- `Architecture/510_release_and_environment_strategy.md` — how these tests gate releases.
