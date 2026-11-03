# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project

`archon-search` is a standalone hybrid retrieval + routing server: LanceDB vector store, PyTorch dense embeddings, a cross-encoder reranker, a multi-collection router, FastAPI HTTP control plane, and an MCP endpoint sharing the same auth. Runtime state lives under `~/.archon-search/`.

- Python `>=3.13`, managed with `uv`.
- Package name: `archon-search` (PyPI), import name: `archon_search`.
- Version is derived from git tags via `hatch-vcs` (CalVer `YY.M.<rev-count>`); never hardcode versions.

## Common commands

```bash
# Dev install
uv sync --dev

# Run the server (entry point archon_search.cli.main:main)
uv run archon-search

# Run the server in the foreground (binds 127.0.0.1 natively; 0.0.0.0 only with ARCHON_SEARCH_CONTAINER=1)
uv run archon-search serve

# Full test suite (default addopts enforce --cov-fail-under=85)
uv run pytest

# Lint, zero findings (rule set pinned in pyproject.toml [tool.ruff.lint]; CI runs the same command)
uvx ruff@0.17.3 check .

# Single test file / single test
uv run pytest tests/test_router.py
uv run pytest tests/test_router.py::test_name -x

# Skip coverage locally (developer override only — never bake into addopts)
uv run pytest --no-cov

# Marker-selectable suites (eval, integration, live and benchmark are part of the default run: live and benchmark skip without their prerequisites; the rest below are excluded by the `-m` filter or `norecursedirs`)
uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py
uv run pytest -m integration
uv run pytest -m live
uv run pytest -m benchmark   # needs a running server; auto-skips if unreachable
uv run pytest -m live_benchmark tests/eval/live_benchmark/ --no-cov   # real-model latency; needs the model cache
uv run pytest tests/eval/live/ --no-cov                               # live_eval: real model weights
uv run pytest tests/smoke/ --no-cov                                   # smoke: spawns a real `archon-search serve`
ARCHON_RUNTIME_PARITY_DATA_DIR=/tmp/rt_data uv run pytest -m runtime_parity tests/runtime_parity/ --no-cov -q -p no:cacheprovider -o addopts=   # PyTorch vs frozen fastembed outputs
# Docker-gated tests (real image build): ARCHON_SEARCH_RUN_DOCKER_SMOKE=1 uv run pytest tests/test_docker_smoke.py -n0 -o addopts= --no-cov

# Cut a release (tag + push; CI runs eval + publishes to PyPI via OIDC)
bash release.sh           # interactive
bash release.sh -y        # non-interactive
bash release.sh --dry-run
```

Plain pushes to `main` do **not** publish. Only a tag push (typically via `release.sh`) triggers `archon-search-release.yml`.

## Architecture

### Core retrieval pipeline (`archon_search/`)

The runtime is a layered pipeline; understanding the seam between these modules is the fastest way to navigate the codebase:

- `parser.py` → `chunker.py` → `embedder.py` → `store.py` (LanceDB vector + FTS) → `reranker.py` (cross-encoder second stage) → `pipeline.py` (`SearchPipeline` orchestrates ingest, search, and context retrieval).
- `router.py` (`MultiCollectionRouter`) does centroid pre-ranking across collections to pick which to query for a given prompt.
- `services/` is the application layer shared by the REST, MCP and OpenAI-shim adapters: `search_budget.py`, `request_hyde.py`, `validators.py`, `service_error.py` (`ServiceError` and the wire text the adapters share), `search_command.py` (`SearchCommand` and the one request normaliser), `search_service.py` (`SearchService.search` for the single and the multi collection path, `SearchOutcome`, `AclGate`), `container.py` (the one factory that builds `SearchService` for every adapter) and `authz.py` (the transport-free authorization policy). It imports the core and never `server/`, `cli/` or a web, CLI or HTTP-client package; `tests/test_layer_import_boundary.py` holds the rules (and keeps route modules, MCP and the CLI command modules apart, with a ratcheting allowlist), and `tests/parity/` fails when a REST route, MCP tool or CLI command is neither claimed in its registry nor waived. The CLI is an HTTP client of REST and never imports `services/` or `server/` (the lazy `run_server` in `cli/serve.py` is the one exception). See `Documentation/ADRs/20_application_service_layer_with_enforced_boundaries_and_interface_parity.md`.
- `collection_meta.py` and `acl.py` add per-collection metadata and access control. `description_generator.py` (`DescriptionGenerator`) turns a collection's open chunks into a description with the configured model and never raises (it returns `DescriptionGenerated` or `DescriptionFailed`); `description_lifecycle.py` (`DescriptionLifecycle`) decides when and in what order generations run (ingest, `POST /collections/{name}/describe`, startup backfill); `description_writer.py`, `description_write.py` and `description_change.py` write a description (generated, manual set/clear, import) through one guarded `SearchStore.write_description`; `description_sample.py` is the sample read port. Descriptions are off by default (`[description]`).
- `llm_client/` is the shared LLM client every model call goes through (HyDE, RAG Fusion, graph community summaries, descriptions): a consumer-owned `LlmClient` Protocol (`protocol.py`) and one wrapper per provider (`ollama_client.py`, `llama_cpp_client.py`, `anthropic_client.py`, `openai_client.py`, `claude_cli_client.py`), plus input budgeting and fitting (`budget.py`, `token_counter.py`, `fit.py`: the head of a query that fits the window), `rate_limit.py` and `once_per_process.py` (`warn_once`). Clients raise `LlmCallFailure`; every consumer catches it and falls back. `server/llm_composition.py` builds one client per feature from its own config section; there is no `claude-agent-sdk`.
- `watcher.py` + `sync.py` keep on-disk corpora and the index in sync (watchdog-driven).
- `jobs/` is the async job store (model + store) used by long-running ingest/reindex operations exposed over the API.
- `key_manager.py` owns the API-key bootstrap (auto-generates `~/.archon-search/.search.env` with mode 600 on first start; `ARCHON_SEARCH_API_KEY` env overrides and must be at least 32 lowercase hex characters; `ARCHON_SEARCH_KEY_FILE` redirects).
- `torch_runtime.py`, `model_specs.py`, `model_store.py`, `reranker_loading.py`, `text_batching.py` are the PyTorch runtime (device resolution, `accelerator_lock`, `TRANSFORMERS_LOCK`, model download, batching). `warmup_coordinator.py` (`WarmupCoordinator`) single-flights model builds; `embedder_cache.py` is the per-collection embedder LRU (the default embedder is pinned); `model_validation.py` probes the pipeline's own embedder/reranker; `memory_budget.py` estimates model memory against the cgroup/RAM limit; `lance_session.py` creates LanceDB connections with capped caches.
- `ingest_path_policy.py` + `_path_safety.py` decide which client-supplied ingest paths are readable (deny list, `[ingest].allowed_roots`, non-admin refusal when the list is empty); `_durable_io.py` owns private (0700/0600) atomic writes.
- `config.py` + `constants.py` load `<data-dir>/archon-search.toml` (default `~/.archon-search/archon-search.toml`; `ARCHON_SEARCH_CONFIG` overrides) (see `archon-search.toml.example`).
- `platform/` (`runtime.py`, `service.py`, `macos.py`, `linux.py`, `windows.py`) handles OS-specific service install/uninstall; the `install/` package (`installer.py`, `wizard.py`, `config_writer.py`, `extras.py`, ...) + `cli/install_cmd.py` wire it to the CLI.

### Server (`archon_search/server/`)

`app.py` builds the FastAPI app; `mcp.py` exposes the same control-plane tools over MCP using the shared auth middleware (`middleware_auth.py`). All endpoints except `GET /health` and `GET /ready` require a `Bearer` token. `authz.py` holds the FastAPI half of the admin-scope check (`identity_of`, `require_admin_dependency`, `caller_identity`, `ADMIN_ONLY_RESPONSES`); the transport-free policy REST and MCP share is `services/authz.py`; `middleware_body_limit.py` enforces `[server].max_request_body_mb` (413) ahead of auth; `_ingest_admission.py` enforces `[ingest].max_concurrent_tasks` (503 `ingest_busy`); `graph_view_token.py` mints the short-lived `view_token` for the graph viewer; `warmup.py` runs warm-up and the background `/ready` recovery; `llm_composition.py` composes the per-feature LLM clients and resolves their context windows in a background task; `description_backfill.py` (reachability probe, startup backfill), `description_startup.py` (startup notices and WARNINGs) and `provider_dependency.py` (provider-package guard) serve the description feature.

Routes are split per resource, one `routes_*.py` each (health, ready, state, status, search, explain, route, collections, jobs, export, graph, keys, sync, backup, maintenance, telemetry, and the OpenAI shim). `disconnect.py` (`CancelOnDisconnectMiddleware`: `POST /search` and `POST /explain`) and `mcp_disconnect.py` (`CancelMcpToolOnDisconnectMiddleware`: the `search`, `search_with_context` and `explain` tools) cancel an abandoned search; `middleware_auth.py` binds each MCP session to its credential (`bind_sessions`). `schemas.py` + `schemas_telemetry.py` are the Pydantic request/response models. `GET /openapi.json` is the authoritative API contract — keep it in sync, and record breaking changes in `BREAKING.md`.

MCP tools (registered in `mcp.py`, 22 total): `search`, `search_with_context`, `explain`, `ingest_file`, `ingest_directory`, `list_collections`, `get_collections_meta`, `get_collection_meta`, `list_documents`, `delete_document`, `update_collection`, `set_collection_description`, `clear_collection_description`, `export_collection`, `import_collection`, `create_key`, `list_keys`, `revoke_key`, `rotate_key`, `get_graph`, `get_graph_cross_collection`, `graph_impact`. They share the REST auth layer but their names do not mirror the REST routes 1:1 — consult `mcp.py` as the source of truth.

### CLI (`archon_search/cli/`)

`main.py` is the `archon-search` entry point (Click). Subcommands: `start`, `serve`, `stop`, `status`, `wizard`, `install`, `uninstall`, `ingest`, `sync`, `collection`, `config`, `export`, `import`, `backup`, `maintenance`, `key`, `jobs`, `search`, `graph` (`install_cmd.py` holds `install`, `uninstall` and `wizard`; `config_cmd.py` holds `config`; `search.py` holds `search`, an HTTP client of `POST /search`). `_helpers.py` is shared CLI infrastructure.

### Telemetry (`archon_search/telemetry/`)

Opt-in and **disabled by default**. `writer.py` appends one JSONL line per call to `~/.archon-search/search-logs/`, `reader.py` powers `/telemetry/stats` and `/telemetry/entries`, `pruner.py` enforces `retention_days`. **Structural invariant: factory methods in `entry.py` do not accept a `query` parameter — raw query strings must never be logged.** `export_enabled = true` is not implemented in v1: the config loader logs a warning and silently coerces it to `false` (see `config.py`). No external transmission occurs in v1. `doc_id`s are path-derived and may leak filesystem paths when telemetry is on; this is documented as accepted risk.

### Evaluation harness (`archon_search/eval/` + `tests/eval/`)

`tests/eval/` is the sanctioned regression gate for retrieval / reranking / routing / latency changes. Fixtures: `documents.jsonl`, `queries.jsonl`, `labels.jsonl`, `corpus/`, `routing/`. Thresholds in `thresholds.toml`, baseline in `baselines/baseline.{md,json}`. The harness uses **deterministic, corpus-aware but label-blind backends** (`archon_search/eval/backends.py`) so metrics are stable without real model weights; latency p50/p95 is a regression guard, not a production SLA. The maintenance guide (fixture schemas, threshold-lowering policy, waivers) is `tests/eval/README.md` — read it before changing thresholds or fixtures.

## Repository conventions

- Default pytest run excludes the `live_benchmark`, `smoke`, `live_eval`, `docling`, `graph_real_artifact` and `runtime_parity` markers (`-m` filter in `addopts`; the heavyweight directories are also in `norecursedirs`); `live` tests skip because the autouse fixture clears `ANTHROPIC_API_KEY`, and docker-gated tests skip unless `ARCHON_SEARCH_RUN_DOCKER_SMOKE=1`. The default suite stubs the PyTorch model backends (`real_torch_backends` opts out). Coverage gate (`--cov-fail-under=85`) applies to the default single-run invocation. Split / matrix CI runs MUST `coverage combine` before applying the threshold; never bake `--no-cov` into `addopts`. `xdist_group` pins only work under pytest < 9.2 with pytest-xdist 3.8.0 (DEP-5 in doc 530; `tests/test_xdist_loadgroup.py`). Strategy: `Documentation/Architecture/200_testing_strategy.md`.
- The package directory is `archon_search/` (underscore), the distribution is `archon-search` (hyphen). `pyproject.toml` `[tool.hatch.build.targets.wheel].packages` is explicit about this — don't "fix" it.
- Breaking REST/MCP changes go in `BREAKING.md`.
- Telemetry's no-raw-query guarantee is structural: do not add a `query` parameter to telemetry entry constructors.
- Default-lane tests are kept off real model providers by the session guard `_block_model_providers` (`tests/_model_host_guard.py`), which refuses real `anthropic`/`openai`/`ollama` clients, non-loopback `httpx`/`httpx2` model traffic and `claude` spawns. It has known gaps (`urllib`, `requests`, `aiohttp`, `hf_xet`, `env claude`, and live-marked tests, which lift most guards), so it is a safety net, not a proof. Use `FakeModelServer` (`tests/_fake_model_server.py`; Ollama and llama.cpp endpoints, `unreachable_port()`), `_fake_claude` (`tests/_fake_claude.py`; a real subprocess standing in for `claude`) and `_cli_testclient_bridge` (`cli_against_app`; runs CLI commands against an in-process app). Details and gaps: `tests/CLAUDE.md`.

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
| ADRs (LanceDB, embedder, reranker, router, telemetry) | `Documentation/ADRs/` |
| Roadmap, planned work | `Documentation/Backlog/03_world_class_roadmap.md`, `Documentation/Backlog/` |
| Onboarding (devs) | `Documentation/quick_start.md`, `contributing.md` |
| End-user / operator guides | `Documentation/UserManual/` |

When generating, refactoring, or reviewing code: open the relevant Architecture doc first for context, then verify against source. When making a behavior change that the docs describe (auth, telemetry, routing, schemas, API surface), update the docs in the same PR. ADRs are append-only — supersede with a new ADR rather than editing accepted ones.
