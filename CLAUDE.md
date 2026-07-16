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

# Full test suite — all markers except live_benchmark and smoke
uv run pytest

# Serial execution — reserved for developer debugging only (fail-fast, stdout passthrough)
uv run pytest tests/test_router.py::test_name -n0 -x
uv run pytest -n0 -s

# Skip coverage locally (developer override only — never bake into addopts)
uv run pytest --no-cov

# live_benchmark suite — excluded from default runs; skips gracefully without the fastembed model cache
uv run pytest -m live_benchmark tests/eval/live_benchmark/ --no-cov

# smoke suite — excluded from default runs; spawns a real archon-search serve subprocess; requires the fastembed model cache
uv run pytest tests/smoke/ --no-cov

# Cut a release (tag + push; CI runs eval + publishes to PyPI via OIDC)
bash release.sh           # interactive
bash release.sh -y        # non-interactive
bash release.sh --dry-run
```

Test markers (all run by default except `live_benchmark` and `smoke`; full rules under Repository conventions):

- `integration` — run by default; no server or network required.
- `benchmark` — serialized via `xdist_group("benchmark")`; server-dependent tests auto-skip.
- `eval` — gated eval tests **run by default**: `--thresholds-path tests/eval/thresholds.toml` is wired into `addopts`. They skip only when invoked without that flag.
- `live` / `live_eval` — skip gracefully when live infrastructure is absent.
- `live_benchmark` — excluded from default runs primarily via `norecursedirs`, with `-m` addopts filter as a secondary guard (see Repository conventions); run separately with the command above.
- `smoke` — excluded from default runs the same way as `live_benchmark` (`norecursedirs` primary, `-m` addopts filter secondary); every smoke test file sets `pytestmark = pytest.mark.xdist_group("smoke_e2e")` at module level to serialize onto one worker and prevent concurrent server subprocess instances; run separately with the command above.

**PARALLEL TESTS ARE MANDATORY: Always run `uv run pytest` without `-n0`. The `addopts` in `pyproject.toml` already sets `-n 4 --dist=loadgroup`. The worker count is deliberately capped at 4 — never raise it back to `-n auto`: auto meant 14 workers on this 14-core machine, and on model-loading paths each worker holds ~2 GB (fastembed/onnxruntime/torch), which OOM-crashed the 48 GB machine on 2026-07-05. Never add `-n0` — it disables parallelism and is reserved for developer debugging only. To see more failure detail, use `--tb=short` or `--tb=long`, never `-n0 -s`.**

**ONE TEST SUITE AT A TIME: Never launch `uv run pytest` via a background mechanism (e.g. `run_in_background`), and never start a new suite run while a previous one may still be alive. Before any test run, `pgrep -fl pytest` must return nothing; if a run timed out or produced no output, wait for its workers to exit before re-running — stacked suite runs are what OOM-crashed the machine on 2026-07-05. While iterating, run scoped paths (`uv run pytest tests/test_x.py --no-cov`); run the full suite once, in the foreground, at task completion.**

Release CI (`archon-search-release.yml`, `archon-search-pr.yml`) passes `-n0` explicitly: CI uses multi-step `--cov-append` across separate pytest invocations, and xdist's per-invocation combine step would corrupt the accumulated `.coverage` file.

`git-cliff >= 2.4` is a release-only prerequisite (not needed for development): `brew install git-cliff` (macOS) or `cargo install git-cliff --version '>=2.4'` (cross-platform).

Plain pushes to `main` do **not** publish. Only a tag push (typically via `release.sh`) triggers `archon-search-release.yml`.

## Architecture

Only rules, invariants, and non-obvious behavior are recorded here. For module inventories, API surfaces, and exact signatures, read the source (single source of truth) or the docs listed in the Documentation map.

### Core retrieval pipeline (`archon_search/`)

The runtime is a layered pipeline: `parser.py` → `chunker.py` → `embedder.py` → `store.py` (LanceDB vector + FTS) → `reranker.py` (cross-encoder second stage) → `pipeline.py` (`SearchPipeline` orchestrates ingest, search, and context retrieval). `router.py` does centroid pre-ranking across collections; `watcher.py` + `sync.py` keep on-disk corpora and the index in sync.

- `pipeline.py` — graph hooks: post-ingest entity extraction and `graph_mode` search paths (`"naive"` query expansion; `"local"`/`"global"` community retrieval, which require communities built via `CommunityBuilder`, else `GraphCommunitiesNotBuiltError`; `"ppr"` Personalised PageRank walk via `PPRWalker`, seeded from query-matched entities and their mention-row counts, falling back to hybrid when no entities match). TTL precedence at ingest: request `chunk_ttl_seconds` > collection `default_ttl_seconds` > null. `scope_filter` semantics: exact scopes are pushed into the SQL predicate; trailing-`*` wildcards are post-filtered Python-side on the top-k set. Post-persist auxiliary writes (graph extraction after ingest, mention cleanup on delete) never propagate errors — they log WARNING and return, so a bad graph write cannot fail an ingest or delete.
- `collection_meta.py` / `description_generator.py` / `acl.py` — gotchas: description samples are non-deterministic (`sample_chunk_texts` shuffles in-process); `list_documents` cursor pagination is a Python-side `doc_id > cursor` filter, not a SQL predicate; `default_ttl_seconds` is forward-only (new chunks only); ACL sidecars > 64 KB are rejected with a warning surfaced via `IngestResult.warnings` (CLI prints warnings to stderr; async ingest exposes them in `GET /jobs/{id}`).
- Graph subsystem — `graph_types.py` (dataclasses + deterministic stable-ID helpers; E2g adds `RelationshipType.calls/imports/defines/inherits`, `ImpactResult`/`ImpactGroup`/`ImpactEdge` for traversal results, `DEFAULT_IMPACT_DEPTH=2`/`MAX_IMPACT_DEPTH=5`/`MAX_IMPACT_GROUP_SIZE=50` constants), `graph_store.py` (per-collection LanceDB tables; E2g adds `compute_impact` BFS traversal, `write_pagerank_scores`, `pagerank_score`), `graph_extractor.py` (spaCy NER in `asyncio.to_thread`), `defref_extractor.py` (E2g: AST-aware def/ref extraction for 9 languages via tree-sitter; same-file edges = `"extracted"`, cross-file = `"inferred"`; `code_symbol` node IDs are file-qualified, `entity_name` stays bare), `pagerank_builder.py` (E2g: background PageRank over code-symbol edges; persisted to nodes table; debounce-triggered by `MaintenanceLoop`), `graph_expander.py` (naive expansion; capped at `[graph].naive_max_expansion_terms` (default 20) before deduplication — E2h), `community_builder.py` (Leiden), `graph_inspector.py` (JSON/GraphML inspection; E2g adds `"importance"` sort mode ordered by persisted PageRank, nulls-last), `synonym_detector.py` (E2f: embedding-based synonym edge detection, depends on `GraphStoreProtocol`), `alias_loader.py` (E2f: TOML alias file loader, produces manual `synonym_of` edges and a `skip_pairs` set for the detector), `ppr_walker.py` (E2h: `PPRWalker` Use Cases component that runs Personalised PageRank over the collection graph at query time via `networkx.pagerank` in `asyncio.to_thread`; seeded from `get_mentions_for_entity_ids` row counts; returns ranked chunk IDs). E2j: `GET /graph/{collection}/view` serves a self-contained HTML graph viewer (`server/graph_viewer.html`) — auth via `Authorization: Bearer` header or `?token=` query param (middleware exemption; handler-validated); nodes are colored by `entity_type` and sized by `salience`; edges have thickness proportional to `weight`; `GraphNodeResponse` gains `entity_type: str` (E2j BE-1). Rules: graph tables are named `_archon_graph_{ns}__{col}_nodes|edges|communities|mentions` (double `__` separator; pre-namespacing `_archon_graph_{col}_*` tables are orphans after upgrade — a startup WARNING lists them, delete manually); `GraphStore` adds `get_mentions_for_entity_ids(collection, entity_ids, ns)` (E2h BE-4: returns mention rows filtered to the given entity ID list, preserving duplicates for PPR personalization weighting); all `GraphStore` public methods take `ns` as the LAST parameter; graph extras are optional (`archon-search[graph]`) and `graph.enabled = true` without them raises `ConfigError` at startup; code-parser extras (`archon-search[code]`) are separate — `graph.enabled = true` with code parsers absent causes a WARNING logged once per unsupported file extension by `code_enricher`, and `DefRefExtractor` additionally surfaces a per-file warning in `IngestResult.warnings` for each skipped code file; server still starts and prose graphing still works. Enrichment invariant (E2f): the post-ingest synonym enrichment callback (`pipeline.on_synonym_edges_written`) is called inside a `try/except` block and NEVER propagates exceptions — enrichment failure logs WARNING and the ingest result is returned normally. `enrichment_auto = false` in `[graph]` config prevents automatic triggering. `GraphConfig` synonym fields: `synonym_threshold: float = 0.85`, `alias_file: str | None = None`, `enrichment_auto: bool = True`. E2h PPR fields: `ppr_damping: float = 0.85`, `ppr_top_entities: int = 20`, `naive_max_expansion_terms: int = 20`. E2g def/ref invariant: `"extracted"` always wins over `"inferred"` (never downgraded) — a pre-read-and-override step in `write_graph` enforces this before the bulk `merge_insert`. Existing collections do not retroactively gain def/ref edges — re-ingest is the only path.
- `jobs/` — async job store plus in-process `BackupLoop` and `MaintenanceLoop` (backup export/rotation; FTS optimize, orphan cleanup, expired-chunk pruning, graph GC + async community rebuild, failed-ingest retry). Rules: backup-sourced jobs always sort behind `source="user"` jobs; FAILED jobs that age out (`retry_max_age_hours`) or exhaust retries (`retry_max_attempts`) become terminal `FAILED_EXPIRED` — never re-enqueued, surfaced in `GET /jobs?status=FAILED_EXPIRED` and `/status`. Loop state persists in `.backup-state.json` / `.maintenance-state.json` under the data dir; `POST /maintenance/trigger` forces an immediate pass.
- `key_manager.py` — key bootstrap (`ARCHON_SEARCH_API_KEY` overrides the key file; `ARCHON_SEARCH_KEY_FILE` redirects it) and `KeyStore` (`keys.json`). Invariant: raw bearer tokens are **never** persisted — only SHA-256 hashes. `active_keys()` re-reads disk on every call (no cache).
- `model_validation.py` — background provider/model probe spawned by the app lifespan; never raises and never blocks startup; surfaces in `GET /status` and `GET /ready`.
- `paths.py` — `get_data_dir()` (env `ARCHON_SEARCH_DATA_DIR`, default `~/.archon-search/`) is the single source of truth every path accessor derives from: one env var relocates the entire runtime tree (the Docker image mounts `/data`).
- `config.py` + `constants.py` — load `~/.archon-search/archon-search.toml`; each TOML section (`[search]`, `[jobs]`, `[backup]`, `[maintenance]`, `[database]`, `[auth]`, `[mcp]`, `[ingest]`, `[telemetry]`, `[graph]`, `[openai_shim]`) maps to a dataclass on `SearchConfig`; defaults live in `config.py`, documented in `archon-search.toml.example`. Non-obvious: `_validate_collection`/`_validate_namespace` reject names containing `__` or with leading/trailing `_` (graph table-name injection guard); the `[ingest] max_file_mb` guard is universal because REST (413), MCP (`file_too_large`), CLI, and the watcher all pass through `pipeline.ingest_file()`'s pre-check; `load_config(serve=True)` flips the host default to `0.0.0.0`.
- `logging_setup` — `ARCHON_SEARCH_CONTAINER=1` attaches a stderr handler so `docker logs` captures output even with an empty `log_file`.
- `platform/` + `install.py` — OS service install/uninstall. Secrets: both the Linux unit and the macOS `run-server.sh` launchd wrapper source `~/.archon-search/.secrets.env` (created 0600 by the wizard when HyDE or RAG Fusion is enabled; absent file is a no-op).

### Server (`archon_search/server/`)

`app.py` builds the FastAPI app; routes are split per resource (`routes_*.py`); `schemas.py` + `schemas_telemetry.py` hold the Pydantic models. All endpoints except `GET /health` and `GET /ready` require a `Bearer` token. `GET /openapi.json` is the authoritative API contract — keep it in sync, and record breaking changes in `BREAKING.md`.

MCP (`mcp.py`): mounted at `/mcp` on the REST port when `mcp.enabled = true` (default) — no second uvicorn, no second port; in serve mode it therefore binds `0.0.0.0:{port}/mcp`. Traps: the mount must be wrapped in an explicit `mcp_starlette.router.lifespan_context(app)` delegation or FastMCP's session task group never starts; a mount failure logs a warning and must never block REST startup; `app.state.mcp_bound` is the single source of truth for MCP status (with `enabled = false`, `/status` and `/health` report `mcp: null`). Tool names do not mirror REST routes 1:1 — `mcp.py` is the source of truth; MCP validation mirrors REST rules; namespace auth reaches every tool via `request.state.namespace`. Permanent design decision (not deferred work): `search_with_context` rejects any non-null `graph_mode` — use `search` instead. See ADR-09 for the mount and namespace-propagation spike.

OpenAI shim (`routes_openai_shim.py` + `schemas_openai.py`): G9 — mounted at `/v1` on the existing REST port when `openai_shim.enabled = true` (`[openai_shim]` TOML section, disabled by default). `GET /v1/models` returns one `ModelObject` per namespace-visible collection plus a catch-all `archon-search` entry. `POST /v1/chat/completions` extracts the last `role="user"` message as a query, runs retrieval via `SearchPipeline`, and returns results in OpenAI chat-completion format. `OpenAI401Middleware` rewrites bodyless 401 responses on `/v1/*` to OpenAI error shape; it is added AFTER `APIKeyMiddleware` in `create_app()` (Starlette LIFO — sits outside `APIKeyMiddleware`, intercepts outgoing 401s). The `include_router` and `add_middleware(OpenAI401Middleware)` calls are in two separate `if config.openai_shim.enabled:` guards in `app.py`. When `enabled = false`, no `/v1` routes are registered — disabled shim is a true no-op (not even a 404 handler for the path). `OpenAIShimConfig` fields: `enabled: bool = False`, `inject_citations: bool = True`, `top_k: int = 5` (accepted, not yet forwarded to pipeline — reserved for future `search()` runtime top_k parameter).

### CLI (`archon_search/cli/`)

`main.py` is the `archon-search` Click entry point; `_helpers.py` is shared CLI infrastructure. `serve.py` runs the server in the foreground with the host default flipped to `0.0.0.0` and never touches launchd/systemd.

### Telemetry (`archon_search/telemetry/`)

Opt-in and **disabled by default**; one JSONL line per call under `~/.archon-search/search-logs/`. Invariants: factory methods in `entry.py` do not accept a `query` parameter — raw query strings must never be logged (structural guarantee); `export_enabled = true` is not implemented in v1 — the config loader warns and coerces it to `false`, and no external transmission occurs. `hash_doc_ids = true` HMAC-hashes `result_doc_ids` before write; this protects logs shared separately, not the full data directory — the salt (`.telemetry-salt`) is co-located with LanceDB, which stores raw `source_path`.

### Evaluation harness (`archon_search/eval/` + `tests/eval/`)

`tests/eval/` is the sanctioned regression gate for retrieval / reranking / routing / latency changes. Fixtures: `documents.jsonl`, `queries.jsonl`, `labels.jsonl`, `corpus/`, `routing/`. Thresholds in `thresholds.toml`, baseline in `baselines/baseline.{md,json}`. The harness uses **deterministic, corpus-aware but label-blind backends** (`archon_search/eval/backends.py`) so metrics are stable without real model weights; latency p50/p95 is a regression guard, not a production SLA. The maintenance guide (fixture schemas, threshold-lowering policy, waivers) is `tests/eval/README.md` — read it before changing thresholds or fixtures.

## Repository conventions

- Default pytest run includes **all** markers except `live_benchmark` and `smoke`, each excluded at **two levels**: (1) `norecursedirs = ["tests/eval/live_benchmark", "tests/smoke"]` in `pyproject.toml` prevents pytest from auto-traversing those directories — for `live_benchmark` this keeps its conftest (which removes fastembed stubs at module level) **never imported** during default collection, the critical isolation (without it, every xdist worker would poison `sys.modules["fastembed"]` for all subsequent tests); for `smoke` this prevents the real `archon-search serve` subprocess fixture from ever being collected by default; (2) `-m "not live_benchmark and not smoke"` in `addopts` is a secondary guard for both. Gated `eval` tests run by default because `--thresholds-path tests/eval/thresholds.toml` is wired into `addopts`; they skip only when invoked without that flag. `live`/`live_eval` tests gating on `ANTHROPIC_API_KEY` always skip on default runs because the autouse fixture in `tests/conftest.py` clears the key on every test (eliminates the 30 s SDK-timeout floor); to run live tests against a real key, temporarily comment out the `monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)` line in `tests/conftest.py` — there is no clean shell-level workaround since every invocation loads the root conftest. Coverage gate (`--cov-fail-under=85`) applies to the default single-run invocation; split/matrix CI runs MUST `coverage combine` before applying the threshold; never bake `--no-cov` into `addopts`.
- `tests/integration/` contains multi-component integration/e2e tests exercising real components (real `SearchStore`, real `SearchPipeline`, real LanceDB in `tmp_path`, `TestClient` against a real FastAPI app), distinct from unit tests in `tests/` which use the ML stubs from `tests/conftest.py`. They are marked `integration` and run in the default suite; isolate with `uv run pytest -m integration tests/integration/`. Shared helpers (`make_real_app`, `ingest_doc`, `ingest_file_via_path`, `search`, `make_real_pipeline`) live in `tests/integration/conftest.py` — do NOT modify `tests/conftest.py` when adding integration tests.
- The package directory is `archon_search/` (underscore), the distribution is `archon-search` (hyphen). `pyproject.toml` `[tool.hatch.build.targets.wheel].packages` is explicit about this — don't "fix" it.
- Breaking REST/MCP changes go in `BREAKING.md`.
- Telemetry's no-raw-query guarantee is structural: do not add a `query` parameter to telemetry entry constructors.
- `store.py` SQL predicates must be built via `_where_eq`/`_where_in` (which quote through `_sql_quote_str` in `store_filters.py`), never f-strings; the `tests/test_no_fstring_sql.py` CI guard fails the build if an f-string-wrapped `.where(`/`.delete(`/`.count_rows(` reappears in `store.py`.
- **`STORE_SCHEMA_VERSION` bump policy:** increment `STORE_SCHEMA_VERSION` in `store.py` whenever a structural change to the shared `_schema()` (chunk-table) or `_meta_schema()` (collection-metadata) requires existing rows to be migrated. **Exception:** per-collection chunk-table-only changes (e.g. `migrate_acl`) do NOT bump it. Every bump must add a corresponding `MigrationSpec` to `SearchStore._all_migrations()`. Current value: `STORE_SCHEMA_VERSION = 1` — version 0 covers the five startup migrations formalised in D3 (`introduced_at = 0`); version 1 adds `migrate_expires_at_and_scopes` and `migrate_default_ttl_seconds` (`introduced_at = 1`), which are NOT applied at startup — operators must run `POST /collections/{name}/migrate` after upgrading.

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
"What Has Failed." While working, note which entries you directly acted on
(took a specific action, avoided a specific pattern, or made a specific code
choice traceable to that entry) — you will increment their `N` at task close.

**After completing any task:**
You MUST update `learnings.md`. Format for new entries:

**[Date] (×N) — [Task type]**
- [action-first observation]

New entries start at `(×1)`. For each entry you acted on this session
(see above), increment its `N`. Entries without `(×N)` (pre-migration) count
as `(×1)` for eviction. If no new observations arose, skip adding — but still
scan for entries you acted on and increment their `N`.

Be specific. "Avoid relative imports in /utils — the build step
resolves them incorrectly" is useful. "Be careful with imports" is not.

Do not add:
- Observations already captured in the file
- General best practices (only project-specific ones)
- Redundant restatements of existing entries

**Size cap — 150 lines, non-negotiable:**
`learnings.md` must always stay **under 150 lines**. If it is already at or
above 150 lines when you open it, compact before adding anything. After every
update review the whole file and re-compact so it stays under the cap:
- Merge overlapping or same-theme entries into one dense entry.
- Delete entries that are stale, superseded, or now enforced elsewhere (code guards, tests, this CLAUDE.md).
- Compress kept entries — collapse to a single action-first bullet (keep the date + `(×N)` tag) when space demands it.
- Only evict when the file would still exceed 150 lines after compression — never proactively. When eviction is needed, remove lowest `N` first; break ties by oldest date. Add new entries after eviction, not before.
If an update would push the file past 150 lines, compress first, then add.
