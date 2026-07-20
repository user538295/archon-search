**Purpose**: Define the test pyramid for `archon-search`, the markers that gate each layer, the coverage discipline, and the role of the evaluation harness as the regression gate for retrieval quality.
**Audience**: Contributors writing tests, reviewers gating PRs, and CI maintainers.
**Status**: Draft
**Last reviewed**: 2026-06-10
**Next review**: 2026-09-10

# Testing Strategy

Tests in `archon-search` are split across four pytest markers plus a default (unmarked) tier. The default tier runs on every push; the marked tiers are opt-in and target specific failure modes (live network, deterministic eval quality, performance, integration). This document is the canonical map. The retrieval-quality side of testing — fixtures, thresholds, baselines, waiver policy — is owned by `tests/eval/README.md`; this document only points at it.

## Principles

1. **TDD-first.** Per `CLAUDE.md` (Code Style): write tests first, start with happy paths, then edge cases. Maintain 85%+ coverage. Resolve all warnings.
2. **Default run includes every test except `live_benchmark`, `smoke`, `live_eval`, and `docling`.** `addopts` in `pyproject.toml` sets `-m "not live_benchmark and not smoke and not live_eval and not docling"` — these are the intentional marker exclusions. `live_benchmark` tests perform module-level `sys.modules` mutation to remove fastembed stubs; running them in the same process as the default suite would poison `sys.modules` for regular tests. `smoke` tests spawn a real `archon-search serve` subprocess and are excluded so the default suite never launches a live server. `live_eval` tests (`tests/eval/live/`) drive the full live-model eval with real fastembed weights (`backend="live"`) and are excluded (via `norecursedirs` + the `-m` filter) so the default suite never loads real models or hangs on inference. `docling` tests invoke the real docling parser / RapidOCR (PDF & image OCR), which takes minutes per parse on macOS (Metal/RapidOCR); they are excluded so the default suite never hangs on OCR. These run in dedicated CI steps or on demand instead. All other markers (`live`, `eval`, `benchmark`, `integration`) run on every `uv run pytest` and skip gracefully when the required infrastructure is absent (server-dependent benchmarks, `live` tests that gate on `ANTHROPIC_API_KEY` — which the autouse fixture in `tests/conftest.py` clears on every test, so these always skip on default runs even when the developer has the key exported in their shell; no clean shell-level workaround exists, see the `live` section below for how to run them), gated eval without `--thresholds-path`. ONNX downloads and tokenizer subprocesses are stubbed in `tests/conftest.py` before pytest discovery.
3. **Markers document intent, not exclusion — with four exceptions.** `live`, `eval`, `benchmark`, `integration` are registered markers used for explicit targeted runs (e.g. `uv run pytest -m integration`) but are NOT excluded from the default run. `live_benchmark`, `smoke`, `live_eval`, and `docling` are the four exceptions (see principle 2).
4. **Eval is the retrieval-quality gate, not the unit-test tier.** Retrieval, reranking, and routing changes are validated by the `eval` marker with committed thresholds in `tests/eval/thresholds.toml`. Latency in the harness is a regression guard, not a production indicator.
5. **Coverage gating is a single-run concept.** `--cov-fail-under=85` applies to the default single-run invocation. CI that runs multiple pytest invocations must accumulate coverage into a single dataset before applying the threshold — `.github/workflows/archon-search-pr.yml` does this by passing `--cov-append` to both the default and eval steps (writing into one `.coverage` file) and then running `coverage report --fail-under=85`. `coverage combine` is only needed when shards write parallel-mode files; the current PR workflow deliberately skips it. **Never bake `--no-cov` into `addopts`.**

## The pyramid

```mermaid
flowchart TB
  subgraph default[Default run — all markers included except live_benchmark, smoke, live_eval, and docling]
    U[Unit tests<br/>tests/**/test_*.py<br/>stubs from tests/_search_stubs.py]
    I[integration<br/>real components<br/>local infra]
    E[eval<br/>tests/eval/<br/>deterministic backends]
    B[benchmark<br/>xdist_group serialised<br/>server tests auto-skip]
    L[live / live_eval<br/>autouse clears ANTHROPIC_API_KEY<br/>always skip on default runs]
  end
  LB[live_benchmark<br/>dedicated CI step<br/>real fastembed + ONNX<br/>skips without model cache]
  SM[smoke<br/>real subprocess server<br/>CLI + REST assertions<br/>errors without model cache]
  default --> CI[CI: coverage gate ≥ 85%]
  LB --> CI2[CI: benchmark step<br/>p95/p90 gate]
  SM --> CI3[CI: manual/pre-release step<br/>no coverage gate]
```

### Default tier (unit)

- Invocation: `uv run pytest`.
- No marker selector — all tests run.
- Parallelism: the default run uses `pytest-xdist` with `-n 8 --dist=loadgroup` (raised from 4 on 2026-07-20 — the default suite stubs fastembed, so each worker is ~0.3 GB, measured; `-n 8` is memory-trivial on the 14-core/48 GB machine, ~177 s vs ~239 s at `-n 4`). Never `-n auto` (=14) and never bump `-n` for the real-model lanes: `-n auto` on those model-loading paths, where each worker holds ~2 GB of fastembed/onnxruntime/torch, OOM-crashed the 48 GB machine on 2026-07-05. `bash scripts/test-fast.sh` runs the suite on a macOS RAM disk (the suite is I/O-bound on temp LanceDB writes) for a further ~24 s. `--dist=loadgroup` distributes ungrouped tests individually across workers; tests with `pytestmark = pytest.mark.xdist_group("mcp")` (16 files that mutate `sys.modules["fastmcp"]`) are co-located on one worker to prevent `sys.modules` contamination; tests with `xdist_group("install")` (3 files that compete on `~/.archon-search/.install.lock`) are co-located on another. The `connected_store` fixture is session-scoped (one `SearchStore` per xdist worker); `col_name` mints a UUID-suffixed name to prevent cross-test collision.
- Test infra: `tests/conftest.py` installs ML stubs at module import time via `_search_stubs.install_stubs()`. It also injects `ARCHON_SEARCH_API_KEY = "0" * 64` so `create_app()` always sees a known key.
- Serial escape hatch: `uv run pytest -n0` produces identical results in serial mode. Use `-n0 -x` for fail-fast isolation (xdist workers continue until their current test finishes) and `-n0 -s` for stdout passthrough (suppressed by xdist).
- Coverage combining: `pytest-cov` natively supports xdist. Workers write `.coverage.workerN` files which the main process combines before applying `--cov-fail-under=85`. This applies to single-invocation runs only; CI requires `-n0` to avoid interference with multi-step `--cov-append` accumulation.
- Adding a test: place it under `tests/`, do not add a marker — the default selector will pick it up. Pipeline tests live under `tests/pipeline/` (`test_pipeline_ingest.py`, `test_pipeline_search.py`, `test_pipeline_multi.py`); shared helpers are in `tests/pipeline/conftest.py`.

### `tests/integration/` — multi-component integration and e2e tests

`tests/integration/` contains tests that exercise multiple real components collaborating end-to-end: real `SearchPipeline`, real `SearchStore`, real LanceDB in `tmp_path`, `TestClient` against a real FastAPI app. These are distinct from unit tests in `tests/` which rely on the ML stubs in `tests/conftest.py`.

**Why a separate directory?** Unit tests stub fastembed and the cross-encoder at `sys.modules` level (`tests/conftest.py`), which is correct for fast isolated testing but leaves the wiring between HTTP→middleware→route→pipeline→store→JSON serialization untested. `tests/integration/` fills that gap: each test exercises a real component chain without mocking the system under test.

All integration tests run in the default `uv run pytest` suite (no extra flags required) and are marked `integration` so they can also be run in isolation with `uv run pytest -m integration tests/integration/`.

**The six cross-cutting gap themes addressed** (from plan E1, which added 19 test files under `tests/integration/`):

1. **HTTP layer** (phases 1.1–1.6) — `SearchResultSchema.from_result()` wiring, filter SQL-escaping, multi-collection search, per-collection embedding model lifecycle, content-enrichment metadata in responses, HyDE/RAG-Fusion kill-switch and dependency-absent errors.
2. **Job dispatch & scheduler** (phases 2.1–2.2) — real `JobScheduler` dispatch through a full `create_app` lifespan: export/import round-trip, backup trigger with on-disk `.tar.gz` verification, user-before-backup job priority.
3. **MCP tool error paths** (phases 3.1–3.2) — validation errors, typed-exception mapping (`FanoutTimeoutError`, dependency-absent errors), and schema-contract tests (`McpSearchResultSchema`, `CollectionListItemSchema`, transient-field exclusion).
4. **CLI e2e** (phases 4.1–4.2) — real Click wiring: path safety enforcement, container serve path, `DATA_DIR` key-file routing, `configure_logging` stderr handler, wizard dry-run idempotency, TOML config output.
5. **Centroid and routing** (phases 5.1–5.2) — multi-batch accumulation correctness, reingest net-zero, delete centroid update, incremental vs. recomputed routing equivalence, hybrid routing end-to-end.
6. **Feature-specific cross-cutting** (phases 6.1–6.5) — FTS delete with no phantom hits, container env + disk I/O (`atomic_write_json`, `JobStore` JSON roundtrip, key-file mode 600), health/status/observability (`X-Request-Id`, stage timings, telemetry drain, no-raw-query invariant at HTTP wiring level), ACL/namespace isolation, collection lifecycle deletion.

**Shared helpers** live in `tests/integration/conftest.py` (never modify `tests/conftest.py` from this directory):

- `make_real_app(tmp_path, monkeypatch, *, backup_enabled=False, namespaces=None)` — returns `(TestClient, config, api_key)` backed by real `SearchStore` + `SearchPipeline` in `tmp_path`. Uses `monkeypatch.setenv` for both `ARCHON_SEARCH_DATA_DIR` and `ARCHON_SEARCH_API_KEY` to auto-revert env vars after each test.
- `ingest_doc(client, col, text, path, *, timeout_s=10)` — POST ingest + poll until job DONE.
- `ingest_file_via_path(client, col, path, *, timeout_s=10)` — POST ingest by filesystem path + poll until DONE.
- `search(client, col, query, **filters)` — POST `/search`, assert 200, return items.
- `make_real_pipeline(tmp_path, monkeypatch)` — async helper that creates a real connected `SearchStore` + `SearchPipeline` for direct async pipeline/store calls without going through `TestClient`.

### `integration` — `uv run pytest -m integration`

Integration tests exercise real components against local infrastructure. They run in the default suite. Use this marker when a test needs a real `SearchStore`, real LanceDB index on disk, or real OS service interactions — anything that the stubs in `conftest.py` deliberately replace.

- CI triggers: `archon-search-pr.yml` runs the integration suite on every PR, and `archon-search-release.yml` re-runs it on every tag push (both with a disk-backed `--basetemp=/var/tmp/archon-search-it` because the GitHub-hosted runner's `/tmp` is tmpfs and crash-injection tests skip on tmpfs). Both workflows pass `-m integration tests/` with `--cov-append`, so integration coverage rolls into the same `.coverage` file as the default + eval steps before the `--fail-under=85` gate runs.
- **D1/D2 export/import tests** are marked `integration`: `tests/test_routes_export.py`, `tests/test_jobs_list_resume.py`, `tests/test_mcp_export.py`, `tests/test_export_worker.py`, and `tests/test_import_worker.py`. These use a real `SearchStore` and LanceDB on disk. Unit-only tests (`tests/test_job_store_queued.py`, `tests/test_export_archive.py`, `tests/test_scheduler.py`, `tests/test_path_safety_export.py`, `tests/test_cli_export.py`) are unmarked and run in the default tier with stubs.

### `eval` — `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/`

The eval marker runs the offline retrieval-quality harness. It is the **authoritative regression gate** for the retrieval, reranking, and routing pipelines.

- **Backends are deterministic and label-blind** (`archon_search/eval/backends.py`). `EvalEmbedderBackend` produces SHA-256-based token-hash vectors; `EvalRerankerBackend` is a BM25-inspired lexical reranker. These are corpus-aware (scores vary with text) but never see labels, so the harness measures the *pipeline* against fixed data — not a hand-tuned model.
- **Fixtures** live under `tests/eval/`: `documents.jsonl`, `queries.jsonl`, `labels.jsonl`, `corpus/`, `routing/`, plus the committed `runtime.toml` used by the harness.
- **Thresholds** in `tests/eval/thresholds.toml` are *floors* (quality metrics) and *ceilings* (latency, when set). Floors must be `≤` the measured baseline in `tests/eval/baselines/baseline.json` — never above. Current floors: `recall_at_1 = 0.86`, `recall_at_3 = 0.94`, `recall_at_5 = 0.98`, `mrr = 1.0`, `ndcg_at_5 ≈ 0.976`, `ndcg_at_10 ≈ 0.979`, `routing_accuracy ≈ 0.926`. Latency ceilings are intentionally unset in v1 — latency is report-only until production-comparable backends exist.
- **Lowering a floor by more than `policy.max_floor_drop_without_waiver` (0.05)** requires a written waiver under `baselines/baseline.json::waiver_ids` and a rationale in `baselines/baseline.md`.
- **Read `tests/eval/README.md` before changing thresholds, fixtures, or baselines.** That document is the authoritative maintenance guide; this section only pins down its place in the strategy.

Unmarked eval units (the fast contract / fixture / metric tests under `tests/eval/`) run under the default selector — they are not gated by the marker.

**`[graph]`/`[code]` extras are never installed in any CI job (pre-existing, accepted gap).** All three CI workflows (`archon-search-pr.yml`, `archon-search-release.yml`, `archon-search-eval-live.yml`) run bare `uv sync --dev` with no `--extra graph`/`--extra code` flags. Every test file gating on `leidenalg`/`igraph` (community detection) or `tree_sitter` (AST chunking, def/ref extraction) uses a module-level `pytest.importorskip(...)` guard and skips gracefully — this includes `tests/eval/test_e2e_graph_eval_gate_v2.py` and `tests/eval/test_code_lane_eval_gate.py`. The practical consequence: the graph/code-lane eval gates (including BE-10's `code_defref_recall_at_5` floor) never execute in CI today — they run only in a developer venv with the extras synced (`uv sync --dev --extra graph --extra code`, never `--all-extras` — see `tests/eval/README.md`). No CI job installing these extras exists anywhere in this repository as of 2026-07-10; this is a known gap spanning the entire graph/code-lane feature surface (E2d–E2g), not something specific to any one gate.

**BE-10 code-lane collections deviate from the "deterministic, label-blind backend" invariant.** `code-chunking`/`code-defref` are the first eval collections ingested through a REAL feature pipeline (`archon_search.eval.runner._build_code_lane_ingest_pipeline` — real `ASTChunker`, real `DefRefExtractor`, real `GraphStore`) when `run_eval_suite` is called with `lancedb_root` set, instead of the deterministic SHA-256-hash embedder/BM25-lexical-reranker stub path every other collection uses. This is intentional: it is the only way to prove AST chunking and def/ref graph edges genuinely affect retrieval (a stub pipeline has no AST/graph logic to regress). The embedder/reranker themselves remain the deterministic eval backends in both paths — only the chunker and graph-extraction layer are real.

### `benchmark` — `uv run pytest -m benchmark`

Performance benchmarks. The flagship file is `tests/benchmark_routing_latency.py`, which compares in-process `MultiCollectionRouter.get_pre_context` against `POST /route` over 100 iterations after 3 warmups. It **auto-skips when the server is not reachable** (`pytest.skip` on a failed `GET /health` probe), so it is safe in CI. See `Architecture/210_performance_and_scalability.md` for the targets and the co-located-embedder fallback path.

### `live` — `uv run pytest -m live`

Tests that hit real network or external services. They are collected by the default suite but skip because the autouse fixture in `tests/conftest.py` clears `ANTHROPIC_API_KEY` on every test — `live` tests that gate on the key (via `_skip_if_no_api_key()` or equivalent) therefore always skip on default `uv run pytest` runs, even when the developer has the key exported in their shell. To run them against a real key, temporarily comment out the `monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)` line in `tests/conftest.py` for the invocation — the root conftest is always loaded regardless of which test directory is targeted, so no pytest flag combination bypasses the autouse.

### `live_eval` — `uv run pytest -m live_eval tests/eval/live/ -v --no-cov`

Runs the eval corpus through **real fastembed + cross-encoder model weights** — the same models used in production. Collected by the default suite but skips when model weights are unavailable OR when the test gates on `ANTHROPIC_API_KEY` (which the autouse fixture in `tests/conftest.py` clears on every test, so these always skip on default runs even when the developer has the key exported in their shell — to run them against a real key, temporarily comment out the `monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)` line in `tests/conftest.py` — the root conftest is always loaded, so no pytest flag combination bypasses the autouse).

- Tests live under `tests/eval/live/`. The directory has its own `conftest.py` that no-ops the parent `_activate_deterministic_eval_backends` autouse fixture, ensuring `ARCHON_SEARCH_EVAL_BACKENDS` is never set to `"1"` (which would inject deterministic stubs). The parent `_archon_isolated_data_dir` autouse fixture (which clears `ANTHROPIC_API_KEY`) is NOT shadowed and still fires for all live_eval tests.
- Thresholds live in `tests/eval/live_thresholds.toml`. Until calibrated, the file is a comment-only stub and `load_live_thresholds()` returns `None`, making all runs **report-only** (no gates fire).
- The live baseline (`tests/eval/live_baselines/baseline.json`) is absent until the first calibration run. See `tests/eval/README.md` for the calibration procedure and threshold formula (quality floors: −0.02 pp; latency ceiling: 1.5× baseline).
- CI triggers: `archon-search-eval-live.yml` runs on every tag push (concurrently with the release workflow) and on manual `workflow_dispatch`. The test step uses `continue-on-error: true` and uploads the report artifact regardless of outcome — pre-calibration report-only mode must never block a release.

### `live_benchmark` — `uv run pytest -m live_benchmark tests/eval/live_benchmark/ --no-cov`

CI-gated latency benchmark using **real fastembed BAAI/bge-small-en-v1.5 and Xenova/ms-marco-MiniLM-L-6-v2 ONNX models** — the production code path through fastembed and cross-encoder inference. This is one of only two markers **excluded from the default `addopts` run** (see principle 2; `smoke` is the other, below).

- Tests live under `tests/eval/live_benchmark/`. Two benchmark tests:
  - `test_real_model_search_steady_state_p95` — 100-iteration steady-state p95 latency for `pipeline.search()` end-to-end.
  - `test_real_model_search_cold_load_p90` — N=10 cold-load p90 latency for ONNX session construction (embedder + reranker together).
- **`xdist_group("live_benchmark")`** serialises both tests onto the same xdist worker, preventing concurrent ONNX session construction from inflating cold-load measurements.
- **Model-cache skip hook**: `tests/eval/live_benchmark/conftest.py` has a session-scoped autouse fixture `_require_model_cache` that calls `pytest.skip(...)` if the fastembed cache directory does not contain `bge-small*` and `ms-marco-MiniLM*` blobs. On developer machines without the cache, the entire session skips gracefully rather than failing.
- **Module-level stub removal**: the conftest removes `fastembed`, `fastembed.rerank`, and `fastembed.rerank.cross_encoder` from `sys.modules` at module load time, before any test import. This is why the marker cannot be included in the default suite — the `sys.modules.pop` runs unconditionally at conftest import, which would poison the stub state for all tests that run after it in the same process.
- Thresholds in `tests/eval/live_thresholds.toml` under `[real_model_search]`. Loaded by `load_benchmark_thresholds()` from `archon_search/eval/runner.py`. Unlike `load_live_thresholds()`, this function raises `ValueError` if the section is absent — the gate always requires explicit thresholds.
- CI step: `archon-search-pr.yml` runs `pytest -o addopts= ... -n0 -m live_benchmark` with `timeout-minutes: 3` after the integration step and before the coverage enforcement step. Uses `--no-cov` to avoid per-call coverage overhead biasing latency measurements. A `Verify benchmark tests ran` step after it parses the JUnit XML and fails if all tests were skipped (guards against a silently empty model cache).
- See `tests/eval/README.md` (live benchmark lane section) for calibration procedure and threshold update policy.

### `smoke` — `uv run pytest tests/smoke/ --no-cov`

Live subprocess smoke suite that spawns a real `archon-search serve` process and drives it with real CLI subprocess invocations and real HTTP requests — the tier that catches CLI-layer and process-lifecycle bugs (slow startup, raw Python repr output, blocking behaviour) that `TestClient`-based tests structurally cannot detect. This is one of only two markers **excluded from the default `addopts` run** (see principle 2; `live_benchmark` is the other, above).

- Tests live under `tests/smoke/`: `conftest.py` (session fixture), `test_cli.py` (CLI subprocess assertions), `test_conftest.py` (fixture-format and server-startup tests), `test_rest.py` (REST assertions via `httpx`).
- **Real subprocess, not `TestClient`**: the session fixture binds a free port (port-0 socket trick, no TOCTOU), starts `subprocess.Popen(["uv", "run", "archon-search", "serve"], ...)`, polls `GET /health` then `GET /ready`, pre-seeds a tiny real collection (3 text files ingested via `POST /collections/`), and tears down with SIGTERM (10 s) escalating to SIGKILL on timeout.
- **`xdist_group("smoke_e2e")`** is set as module-level `pytestmark` in every smoke test file, serialising all smoke tests onto one xdist worker — this prevents two workers from spawning concurrent server subprocesses against the same fixture.
- **Timing budgets**: CLI commands (`--help`, `config show`) budget 2 s; other CLI/REST calls budget 5 s. These catch gross startup/latency regressions, not tight P99 bounds.
- **Output-format assertions**: no `CollectionMeta(` repr, no raw embedding-vector arrays (`"[0."`), no Python stack traces in stdout/stderr/response bodies.
- **Exclusion mechanism**: `norecursedirs = ["tests/smoke"]` in `pyproject.toml` is what actually prevents pytest from auto-traversing the directory (so the fixture's real-subprocess spawn is never triggered by default collection); `-m "not smoke"` in `addopts` is a secondary, belt-and-suspenders guard. `tests/smoke/test_cli.py::test_smoke_marker_in_pyproject` asserts both guards plus the registered marker are present in `pyproject.toml` — but like every smoke test it is itself `norecursedirs`-excluded, so it only runs when `tests/smoke/` is invoked explicitly, not as a default-suite regression net.
- **Requires the fastembed model cache** (`~/.cache/fastembed`, ~2 GB) on the host; the subprocess env passes `FASTEMBED_CACHE_PATH` so the model is reused rather than re-downloaded. On a machine without the cache, the first run triggers a cold download and may exceed the 30 s readiness timeout.
- Run with `uv run pytest tests/smoke/ --no-cov` — not part of the coverage-gated default run; triggered manually or before a release, not on every PR.

### `docling` — `uv run pytest -m docling --no-cov`

The four tests that exercise the **real** docling parser / RapidOCR OCR path — PDF text extraction and image OCR — end-to-end. Excluded from the default `addopts` run because a single parse takes minutes on macOS: docling loads RapidOCR + onnxruntime and runs OCR on every page (Metal shader compilation on first use). Left in the default suite they hang the run well past any reasonable budget; one of the four had no timeout guard at all.

- Tests: `tests/test_parser.py::test_parse_with_docling_emits_page_marker`, `tests/test_fixtures.py::TestThreePagePdfFixture::test_three_page_pdf_contains_expected_text`, and two in `tests/integration/test_http_enrichment_metadata.py` (`test_pdf_page_number_in_search_response`, `test_image_file_assigns_page_start_one`).
- **Exclusion mechanism**: `-m "... and not docling"` in `addopts` **only** — the tests are scattered across three files rather than a dedicated directory, so there is no `norecursedirs` guard (unlike `live_benchmark`/`smoke`).
- **PDF fixtures are generated, not committed** — the reportlab-based `three_page_pdf` / `substantial_three_page_pdf` fixtures in `tests/conftest.py` write into `tmp_path`. The eval corpus deliberately contains **no** PDF/OCR document, so the deterministic eval harness (`tests/eval/`) never invokes docling; page-provenance parsing at the unit level is covered by `tests/test_enricher.py` without docling.
- Run with `uv run pytest -m docling --no-cov` — on demand, not part of the coverage-gated default run.

## Coverage

`pyproject.toml [tool.pytest.ini_options] addopts` includes `--cov=archon_search --cov-report=term-missing --cov-fail-under=85`.

- This threshold is intended for the **default single-run invocation**. A single `uv run pytest` produces a single coverage dataset, and 85% gates that dataset.
- **CI runs that invoke pytest more than once must accumulate coverage into one dataset before applying the threshold.** The PR workflow (`.github/workflows/archon-search-pr.yml`) does this by overriding `addopts` (`-o addopts=`) on each step, passing `--cov-append` so both the default and eval invocations write into the same `.coverage` file, and then running a separate `coverage report --fail-under=85`. CI also passes `-n0` explicitly to disable xdist parallelism — each invocation's internal combine step could otherwise overwrite `.coverage` with only the current run's worker shards, silently dropping coverage from prior invocations. If a future CI layout instead writes parallel-mode shard files (e.g. matrix shards each producing `.coverage.<id>`), it must `coverage combine` them first; the current workflow doesn't, because there's nothing to combine. Applying `--cov-fail-under=85` to an individual partial run would reject correct code, because each invocation only sees part of the codebase.
- **`--no-cov` is a local-only override.** Use it on the CLI when iterating (`uv run pytest --no-cov ...`). It must never be baked into `addopts` — doing so silently disables the coverage gate for every contributor.

## Adding tests by failure mode

| You want to test...                                | Tier        | Marker                  | Notes                                                                                |
| -------------------------------------------------- | ----------- | ----------------------- | ------------------------------------------------------------------------------------ |
| A pure function or a route in isolation            | Unit        | none                    | Use `auth_headers`, `connected_store`, `col_name` fixtures from `conftest.py`.       |
| Real LanceDB + real pipeline end-to-end            | Integration | `integration`           | Runs in default suite. |
| A retrieval / routing / reranking quality change   | Eval        | `eval`                  | Update `baseline.json` + `baseline.md` if measured metrics shift. See `tests/eval/README.md`. |
| Latency regression on `POST /route`                | Benchmark   | `benchmark`             | Server must be running; auto-skips otherwise.                                        |
| Behaviour that requires real network               | Live        | `live`                  | Justify the dependency in the PR description. #Unverified (marker registered; no in-tree `@pytest.mark.live` usage sampled) |
| Quality regression with real model weights         | Live eval   | `live_eval`             | Runs on tag push via `archon-search-eval-live.yml`. Requires model weight download. See `tests/eval/README.md` (live eval lane section). |
| Latency regression in real fastembed/ONNX path     | Live benchmark | `live_benchmark`     | Runs in dedicated CI step with model cache; skips on developer machines without cache. Excluded from default `addopts` due to module-level `sys.modules` mutation. See `tests/eval/live_benchmark/`. |
| CLI raw-repr output, slow startup, or blocking behaviour (the class of user-facing bugs 001–010 represent) | Smoke       | `smoke`                 | Real subprocess server + real CLI/HTTP calls; `TestClient` cannot detect these. Excluded from default `addopts` via the same dual-guard pattern as `live_benchmark`. Run with `uv run pytest tests/smoke/ --no-cov`. See `tests/smoke/`. |
| Real docling PDF/image OCR, end-to-end             | Docling     | `docling`               | Real OCR; minutes per parse on macOS. Excluded from default `addopts` via the `-m` filter only (tests are not in a dedicated directory). Run with `uv run pytest -m docling --no-cov`. |

## Parallel-test isolation (`archon_unset_data_dir` marker + `_archon_isolated_data_dir` autouse)

Every test runs under a per-worker, session-scoped `ARCHON_SEARCH_DATA_DIR` provided by the autouse fixture chain in `tests/conftest.py`:

- **`_archon_worker_data_dir` (session-scoped)** — calls `tmp_path_factory.mktemp("archon-data")` once per xdist worker. The result is a fresh temporary directory that is unique to that worker's lifetime.
- **`_archon_isolated_data_dir` (function-scoped, autouse)** — always clears `ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_PORT`, `ARCHON_SEARCH_CONTAINER`, `ARCHON_SEARCH_KEY_FILE`, `ARCHON_SEARCH_CONFIG`. By default it also sets `ARCHON_SEARCH_DATA_DIR` to `str(_archon_worker_data_dir)`, directing every path accessor (`get_data_dir()`, `key_manager.get_key_file()`, `jobs.get_jobs_file()`, etc.) to an isolated temporary tree. Tests that need to exercise the `Path.home() / ".archon-search"` default-fallback codepath (e.g. `test_default_returns_home_archon`) must opt out by applying `@pytest.mark.archon_unset_data_dir`; the fixture then deletes `ARCHON_SEARCH_DATA_DIR` from the environment instead of setting it.

The **`archon_unset_data_dir` marker** is registered in `[tool.pytest.ini_options].markers` in `pyproject.toml`. It is intentionally narrow: apply it only to tests that explicitly assert on the `Path.home() / ".archon-search"` default. The exact set of authorized tests is enforced by `test_archon_unset_data_dir_marker_scope` in `tests/test_no_hardcoded_path_home.py` (see below); any test outside the `MARKER_ALLOWLIST` that acquires this marker will fail CI.

### `Path.home()` ratchet (`tests/test_no_hardcoded_path_home.py`)

`tests/test_no_hardcoded_path_home.py` is a structural CI ratchet that prevents new hardcoded `Path.home()` callsites from being introduced under `archon_search/` outside `archon_search/paths.py` (the one legitimate caller). Three test functions:

1. **`test_path_home_ratchet`** — scans every `*.py` under `archon_search/` (excluding `paths.py`) for `Path.home(` via a line-level regex. Performs a *bidirectional assertion* against `tests/path_home_allowlist.txt` (one `<relative_path>:<line_no>:<sha256>` entry per allowlisted callsite): forward direction catches new unallowlisted callsites; reverse direction catches dead or hash-mismatched allowlist entries. A cosmetic edit to any grandfathered line changes its SHA-256, triggering the reverse direction and forcing an explicit allowlist update.

2. **Meta-tests** (`test_meta_positive_match`, `test_meta_no_parens_negative`, `test_meta_lowercase_negative`, `test_meta_string_literal_positive`) — exercise the regex against in-memory fixtures so a pattern weakening (e.g. dropping `\b` or `\s*\(`) fails immediately, independent of the codebase scan.

3. **`test_archon_unset_data_dir_marker_scope`** — AST-walks `tests/` and asserts that `@pytest.mark.archon_unset_data_dir` appears on exactly the tests named in `MARKER_ALLOWLIST`. Paired with `test_meta_ast_finds_pytest_mark_decorator`, which validates the AST walker itself.

Grandfathered callsites (those in `archon_search/install.py` lines 1214, 1215, 1358, 1547; `archon_search/config.py:144`; `archon_search/platform/linux.py` and `archon_search/platform/macos.py`) are pinned in `tests/path_home_allowlist.txt`. Lines 48, 377, and 1508 of `install.py` were migrated to `get_data_dir()` in C17 and removed from the allowlist. The sibling ratchet for SQL f-string injection is `tests/test_no_fstring_sql.py` + `store.py` guard (see `Architecture/130_data_architecture_and_persistence.md`).

## See also

- `tests/eval/README.md` — authoritative maintenance guide for the eval harness (fixtures, thresholds, baselines, waivers).
- `Architecture/100_system_architecture_overview.md` — what each module under test does in the runtime pipeline.
- `Architecture/140_error_handling_strategy.md` — failure-mode taxonomy that informs which tests to write.
- `Architecture/210_performance_and_scalability.md` — performance targets that the `benchmark` marker validates.
- `Architecture/510_release_and_environment_strategy.md` — how these tests gate releases.
