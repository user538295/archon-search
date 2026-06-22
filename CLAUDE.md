# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`archon-search` is a standalone hybrid retrieval + routing server: LanceDB vector store, fastembed dense embeddings, a cross-encoder reranker, a multi-collection router, FastAPI HTTP control plane, and an MCP endpoint sharing the same auth. Runtime state lives under `~/.archon-search/`.

- Python `>=3.12`, managed with `uv`.
- Package name: `archon-search` (PyPI), import name: `archon_search`.
- Version is derived from git tags via `hatch-vcs` (CalVer `YY.M.<rev-count>`); never hardcode versions.

## Common commands

```bash
# Dev install
uv sync --dev

# Run the server (entry point archon_search.cli.main:main)
uv run archon-search

# Run the server in the foreground (container / direct-run mode; defaults to 0.0.0.0).
# Does not invoke launchd/systemd; blocks until SIGTERM/Ctrl-C.
uv run archon-search serve

# Full test suite — all markers except live_benchmark (excluded by addopts; requires model cache)
uv run pytest

# Serial execution — required for fail-fast (-x) and stdout passthrough (-s)
uv run pytest -n0
uv run pytest -n0 -x          # stop on first failure
uv run pytest -n0 -s          # show stdout (suppressed by xdist)

# Single test file / single test
uv run pytest tests/test_router.py
uv run pytest tests/test_router.py::test_name -n0 -x

# Skip coverage locally (developer override only — never bake into addopts)
uv run pytest --no-cov

# Gated eval suite (requires --thresholds-path; skips gracefully without it)
uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py

# Notes on markers:
# - integration: all run by default; no server or network required
# - benchmark: serialized via xdist_group("benchmark"); server-dependent tests auto-skip
# - eval: report-only tests always run; gated tests skip gracefully without --thresholds-path
# - live / live_eval: skip gracefully when live infrastructure is absent
# - live_benchmark: EXCLUDED at two levels — (1) norecursedirs in pyproject.toml prevents
#   pytest from auto-traversing tests/eval/live_benchmark/ so its conftest (which removes
#   fastembed stubs at module-level) is never imported during default runs; (2) -m "not
#   live_benchmark" in addopts filters any items that do get collected. Run separately:
#   uv run pytest -m live_benchmark tests/eval/live_benchmark/ --no-cov
#   (skips gracefully if fastembed model cache absent)

# Cut a release (tag + push; CI runs eval + publishes to PyPI via OIDC)
bash release.sh           # interactive
bash release.sh -y        # non-interactive
bash release.sh --dry-run
```

Note: release CI (`archon-search-release.yml` and `archon-search-pr.yml`) passes `-n0` explicitly to disable xdist parallelism. CI uses multi-step `--cov-append` across separate pytest invocations; xdist's per-invocation combine step would corrupt the accumulated `.coverage` file.

**PARALLEL TESTS ARE MANDATORY: Always run `uv run pytest` without `-n0`. The `addopts` in `pyproject.toml` already sets `-n auto --dist=loadgroup`. Never add `-n0` — it disables parallelism and is reserved for developer debugging only. To see more failure detail, use `--tb=short` or `--tb=long`, never `-n0 -s`.**

`git-cliff >= 2.4` is a release-only prerequisite (not needed for development): `brew install git-cliff` (macOS) or `cargo install git-cliff --version '>=2.4'` (cross-platform).

Plain pushes to `main` do **not** publish. Only a tag push (typically via `release.sh`) triggers `archon-search-release.yml`.

## Architecture

### Core retrieval pipeline (`archon_search/`)

The runtime is a layered pipeline; understanding the seam between these modules is the fastest way to navigate the codebase:

- `parser.py` → `chunker.py` → `embedder.py` → `store.py` (LanceDB vector + FTS) → `reranker.py` (cross-encoder second stage) → `pipeline.py` (`SearchPipeline` orchestrates ingest, search, and context retrieval).
- `router.py` (`MultiCollectionRouter`) does centroid pre-ranking across collections to pick which to query for a given prompt.
- `collection_meta.py`, `description_generator.py`, `acl.py` add per-collection metadata, auto-generated descriptions, and access control.
- `watcher.py` + `sync.py` keep on-disk corpora and the index in sync (watchdog-driven).
- `jobs/` is the async job store (model + store) used by long-running ingest/reindex operations exposed over the API. `jobs/backup_loop.py` runs an in-process `BackupLoop` (trigger + completion async loops via `asyncio.gather`) that enqueues `ExportJob`s with `source="backup"` at the configured interval, persists last-backup-at in `~/.archon-search/.backup-state.json`, and rotates archives under `output_dir/{namespace}/`. `jobs/maintenance_loop.py` runs an in-process `MaintenanceLoop` (single trigger loop) that executes three configurable policies per non-excluded collection — FTS index optimization (`store.optimize_fts`), orphan chunk cleanup (`store.list_chunks_raw` + `store.delete_by_source_path`), and failed-ingest retry (re-enqueues FAILED `IngestJob`s via `JobStore.create(source="maintenance")`) — then writes `.maintenance-state.json` atomically after each pass; `POST /maintenance/trigger` sets `_trigger_event` for an immediate on-demand pass.
- `key_manager.py` owns the API-key bootstrap (auto-generates the key file with mode 600 on first start; `ARCHON_SEARCH_API_KEY` env overrides; `ARCHON_SEARCH_KEY_FILE` redirects the path; otherwise the file lives at `get_data_dir() / ".search.env"` — see `paths.py`).
- `paths.py` is the single source of truth for the runtime data directory: `get_data_dir()` reads `ARCHON_SEARCH_DATA_DIR` and falls back to `~/.archon-search/`. All path accessors (`key_manager.get_key_file()`, `jobs.get_jobs_file()`, `language_detector.get_fasttext_models_dir()`, `cli/ingest.py` history default, `config.load_config()` overrides for `db_path`/`log_file`/`telemetry.log_dir`) derive from it, so a single env var relocates the entire runtime tree — used by the Docker image to mount everything under `/data`.
- `config.py` + `constants.py` load `~/.archon-search/archon-search.toml` (see `archon-search.toml.example`). `load_config()` accepts env-var overrides for `ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_PORT`, and `ARCHON_SEARCH_DATA_DIR`, and a `serve: bool` kwarg that flips the host default to `0.0.0.0` (used by `archon-search serve`). The `[jobs]` TOML section configures bulk job concurrency: `max_concurrent_bulk` (default `1`) and `checkpoint_interval` (default `100`). The `[backup]` TOML section configures scheduled backup: `interval_hours` (default `0` = disabled), `keep` (default `7`), `exclude` (list of `{col}` or `{ns}/{col}` patterns), and `output_dir` (default `get_data_dir() / "backups"`). When enabled, `BackupLoop` enqueues `ExportJob`s with `source="backup"`; `list_queued_bulk()` sorts these behind `source="user"` jobs so manual operations always win. The `[maintenance]` TOML section (D5) configures scheduled maintenance: `interval_hours` (default `0` = disabled), `fts_optimize` (default `true`), `orphan_cleanup` (default `true`), `failed_ingest_retry` (default `true`), `retry_max_attempts` (default `3`), `retry_max_age_hours` (default `72`), and `exclude` (same bare/qualified pattern syntax as backup). `MaintenanceConfig` is the corresponding dataclass in `config.py`.
- `logging_setup.configure_logging()` attaches a `StreamHandler(sys.stderr)` to the `archon_search` logger when `ARCHON_SEARCH_CONTAINER=1`, so `docker logs` captures application output even with an empty `log_file`.
- `platform/` (`runtime.py`, `service.py`, `macos.py`, `linux.py`, `windows.py`) handles OS-specific service install/uninstall; `install.py` + `cli/install_cmd.py` wire it to the CLI.

### Server (`archon_search/server/`)

`app.py` builds the FastAPI app; `mcp.py` exposes the same control-plane tools over MCP using the shared auth middleware (`middleware_auth.py`). All endpoints except `GET /health` require a `Bearer` token.

Routes are split per resource: `routes_health.py`, `routes_state.py`, `routes_status.py`, `routes_search.py`, `routes_route.py`, `routes_collections.py`, `routes_jobs.py`, `routes_export.py`, `routes_backup.py`, `routes_telemetry.py`, `routes_explain.py`, `routes_maintenance.py` (D5). `schemas.py` + `schemas_telemetry.py` are the Pydantic request/response models. `GET /openapi.json` is the authoritative API contract — keep it in sync, and record breaking changes in `BREAKING.md`.

MCP tools (registered in `mcp.py`, 13 total): `search`, `search_with_context`, `explain`, `ingest_file`, `ingest_directory`, `list_collections`, `get_collections_meta`, `get_collection_meta`, `list_documents`, `delete_document`, `update_collection`, `export_collection`, `import_collection`. They share the REST auth layer but their names do not mirror the REST routes 1:1 — consult `mcp.py` as the source of truth.

### CLI (`archon_search/cli/`)

`main.py` is the `archon-search` entry point (Click). Subcommands: `start`, `stop`, `status`, `serve`, `ingest`, `sync`, `collection`, `config_cmd`, `install_cmd`, `export`, `import`, `backup`, `maintenance` (D5). `_helpers.py` is shared CLI infrastructure. `serve.py` is the container / direct-run entry point: it loads config with `serve=True` (so the host default is `0.0.0.0`), then calls `run_server(config)` in the foreground — it never touches `launchd`/`systemd`. `cli/maintenance_cmd.py` (D5) implements the `maintenance` Click group: `status` (reads `.maintenance-state.json` offline-capable + optionally merges live data from `GET /status`; `--json` flag) and `run` (POSTs `/maintenance/trigger`; `--wait` polls `GET /status` until `maintenance.last_run_at` changes).

### Telemetry (`archon_search/telemetry/`)

Opt-in and **disabled by default**. `writer.py` appends one JSONL line per call to `~/.archon-search/search-logs/`, `reader.py` powers `/telemetry/stats` and `/telemetry/entries`, `pruner.py` enforces `retention_days`. **Structural invariant: factory methods in `entry.py` do not accept a `query` parameter — raw query strings must never be logged.** `export_enabled = true` is not implemented in v1: the config loader logs a warning and silently coerces it to `false` (see `config.py`). No external transmission occurs in v1. `doc_id`s are path-derived and may leak filesystem paths when telemetry is on; this is documented as accepted risk.

### Evaluation harness (`archon_search/eval/` + `tests/eval/`)

`tests/eval/` is the sanctioned regression gate for retrieval / reranking / routing / latency changes. Fixtures: `documents.jsonl`, `queries.jsonl`, `labels.jsonl`, `corpus/`, `routing/`. Thresholds in `thresholds.toml`, baseline in `baselines/baseline.{md,json}`. The harness uses **deterministic, corpus-aware but label-blind backends** (`archon_search/eval/backends.py`) so metrics are stable without real model weights; latency p50/p95 is a regression guard, not a production SLA. The maintenance guide (fixture schemas, threshold-lowering policy, waivers) is `tests/eval/README.md` — read it before changing thresholds or fixtures.

## Repository conventions

- Default pytest run includes **all** markers except `live_benchmark`. `live_benchmark` is excluded at **two levels**: (1) `norecursedirs = ["tests/eval/live_benchmark"]` in `pyproject.toml` prevents pytest from auto-traversing that directory so its conftest (which removes fastembed stubs at module-level) is **never imported** during default collection — the critical isolation; (2) `-m "not live_benchmark"` in `addopts` is a secondary guard. Without `norecursedirs`, every xdist worker would import `live_benchmark/conftest.py` during collection and poison `sys.modules["fastembed"]` for all subsequent tests. All other markers run on every `uv run pytest`. `benchmark` tests are serialised via `xdist_group("benchmark")`; server-dependent ones auto-skip when unreachable. Gated `eval` tests skip gracefully without `--thresholds-path` (wired into `addopts`). `live`/`live_eval` tests that gate on `ANTHROPIC_API_KEY` always skip on default runs because the autouse fixture in `tests/conftest.py` clears the key on every test (C18 fix — eliminates the 30 s SDK-timeout floor when developers have the key exported in their shell); to run live tests against a real key, temporarily comment out the `monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)` line in `tests/conftest.py` for the invocation — any pytest invocation still loads the root conftest, so there is no clean shell-level workaround. Coverage gate (`--cov-fail-under=85`) applies to the default single-run invocation. Split / matrix CI runs MUST `coverage combine` before applying the threshold; never bake `--no-cov` into `addopts`.
- `tests/integration/` contains multi-component integration and e2e tests that exercise real components end-to-end (real `SearchStore`, real `SearchPipeline`, real LanceDB in `tmp_path`, `TestClient` against a real FastAPI app). These are distinct from unit tests in `tests/` which use ML stubs from `tests/conftest.py`. Integration tests are marked `integration` and run in the default suite; run them in isolation with `uv run pytest -m integration tests/integration/`. Shared helpers (`make_real_app`, `ingest_doc`, `ingest_file_via_path`, `search`, `make_real_pipeline`) are in `tests/integration/conftest.py` — do NOT modify `tests/conftest.py` when adding integration tests.
- The package directory is `archon_search/` (underscore), the distribution is `archon-search` (hyphen). `pyproject.toml` `[tool.hatch.build.targets.wheel].packages` is explicit about this — don't "fix" it.
- Breaking REST/MCP changes go in `BREAKING.md`.
- Telemetry's no-raw-query guarantee is structural: do not add a `query` parameter to telemetry entry constructors.
- `store.py` SQL predicates must be built via `_where_eq`/`_where_in` (which quote through `_sql_quote_str` in `store_filters.py`), never f-strings; the `tests/test_no_fstring_sql.py` CI guard fails the build if an f-string-wrapped `.where(`/`.delete(`/`.count_rows(` reappears in `store.py`.
- **`STORE_SCHEMA_VERSION` bump policy (D3):** increment `STORE_SCHEMA_VERSION` in `store.py` whenever a structural change to `_meta_schema()` (collection-metadata columns) or `_schema()` (the shared chunk-table schema) requires existing rows to be migrated. **Exception:** per-collection chunk-table-only changes (e.g. `migrate_acl`) do NOT bump the version — only changes to the shared `_schema()` or `_meta_schema()` require it. Every bump must also add a corresponding `MigrationSpec` entry to `SearchStore._all_migrations()`. `STORE_SCHEMA_VERSION = 0` covers all five startup migrations formalised in D3 (all have `introduced_at = 0`). The first feature that adds a real new column after D3 must set `introduced_at = 1` and bump the constant to `1`.

## Documentation map

Full project documentation lives under `Documentation/`. Treat the **source code as the single source of truth**; the docs are explanatory and may lag. When a doc and the code disagree, fix the doc.

Start here when picking up a task:

- `Documentation/Architecture/990_documentation_index_and_contribution_guide.md` — index of every doc with one-line descriptions and review cadence.
- `Documentation/Architecture/100_system_architecture_overview.md` — C4 diagrams and the layered pipeline overview.
- `Documentation/Architecture/110_component_catalog_and_layer_breakdown.md` — every module mapped to its layer with key public symbols.
- `Documentation/Architecture/600_api_reference_or_public_interface.md` — exhaustive REST + MCP + CLI surface (the live OpenAPI at `GET /openapi.json` is authoritative for HTTP).

Topic-specific entry points:

| Topic | Doc |
|---|---|
| Vision, non-goals | `Architecture/000_introduction_and_guiding_principles.md` |
| Hard invariants (CalVer, coverage, no-raw-query) | `Architecture/010_engineering_principles_and_constraints.md` |
| Sync/async integrations (HTTP, MCP, watcher, jobs) | `Architecture/120_services_and_integration_architecture.md` |
| LanceDB schema, FTS, RRF, persistence layout | `Architecture/130_data_architecture_and_persistence.md` |
| Error taxonomy and HTTP-status mapping | `Architecture/140_error_handling_strategy.md` |
| Auth, ACL, telemetry privacy | `Architecture/150_security_and_privacy_architecture.md` |
| Health/status surface, service install, runbooks | `Architecture/160_operational_readiness_monitoring_and_reliability.md` |
| Test markers and coverage rules | `Architecture/200_testing_strategy.md` |
| Routing knobs and latency regression guards | `Architecture/210_performance_and_scalability.md` |
| CLI a11y posture, i18n scope (English-only by design) | `Architecture/220_accessibility_and_internationalization.md` |
| Dev workflow, package vs distribution naming | `Architecture/500_development_workflows_and_conventions.md` |
| `release.sh`, CalVer, CI workflows | `Architecture/510_release_and_environment_strategy.md` |
| OpenAPI as authoritative, MCP/REST relationship | `Architecture/520_api_design_and_contracts.md` |
| Tech-debt register | `Architecture/530_technical_debt_refactoring_roadmap.md` |
| ADRs (LanceDB, fastembed, reranker, router, telemetry) | `Documentation/ADRs/` |
| Roadmap, planned work | `Documentation/roadmap.md`, `Documentation/Backlog/` |
| Onboarding (devs) | `Documentation/quick_start.md`, `contributing.md` |
| End-user / operator guides | `Documentation/UserManual/` |

When generating, refactoring, or reviewing code: open the relevant Architecture doc first for context, then verify against source. When making a behavior change that the docs describe (auth, telemetry, routing, schemas, API surface), update the docs in the same PR. ADRs are append-only — supersede with a new ADR rather than editing accepted ones.

## Memory and Learning

**Before starting any task:**
You MUST Read `learnings.md` in full. Apply all entries under "What Has Worked"
and "Patterns and Preferences." Avoid all patterns listed under
"What Has Failed."

**After completing any task:**
You MUST Update `learnings.md` with new observations using this format:

**[Date] — [Task type]**
- Observation: [what you noticed]
- Action: [what to do or avoid going forward]
- Confidence: [high / medium / low]

Be specific. "Avoid relative imports in /utils — the build step
resolves them incorrectly" is useful. "Be careful with imports" is not.

Do not add:
- Observations already captured in the file
- General best practices (only project-specific ones)
- Redundant restatements of existing entries

You MUST update `learnings.md` before ending the session. This is required even if nothing new was discovered. If existing patterns held, add a brief note confirming that.

