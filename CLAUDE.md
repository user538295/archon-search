# CLAUDE.md

Guidance for Claude Code in this repository. **Deliberately thin** — it carries only what you cannot
grep for. Module inventories, API surfaces, and signatures live in the source (single source of truth)
and in `Documentation/`; see the map at the bottom. Testing rules live in `tests/CLAUDE.md`.

## Project

`archon-search` is a standalone hybrid retrieval + routing server: LanceDB vector store, fastembed dense
embeddings, a cross-encoder reranker, a multi-collection router, FastAPI HTTP control plane, and an MCP
endpoint sharing the same auth. Runtime state lives under `~/.archon-search/`.

- Python `>=3.12`, managed with `uv`.
- Package directory `archon_search/` (underscore), distribution `archon-search` (hyphen).
  `pyproject.toml` `[tool.hatch.build.targets.wheel].packages` is explicit about this — don't "fix" it.
- Version comes from git tags via `hatch-vcs` (CalVer `YY.M.<rev-count>`); never hardcode versions.

## Common commands

```bash
uv sync --dev                  # dev install
uv run archon-search           # entry point archon_search.cli.main:main
uv run archon-search serve     # foreground (container / direct-run); host defaults to 0.0.0.0,
                               # never touches launchd/systemd, blocks until SIGTERM/Ctrl-C
uv run pytest                  # full suite — see tests/CLAUDE.md before running
bash release.sh                # tag + push; CI runs eval + publishes to PyPI via OIDC
                               # `-y` non-interactive, `--dry-run` preview
```

Plain pushes to `main` do **not** publish — only a tag push triggers `archon-search-release.yml`.
`git-cliff >= 2.4` is a release-only prerequisite: `brew install git-cliff`, or
`cargo install git-cliff --version '>=2.4'`.

## Architecture at a glance

Layered pipeline: `parser.py` → `chunker.py` → `embedder.py` → `store.py` (LanceDB vector + FTS) →
`reranker.py` → `pipeline.py` (`SearchPipeline` orchestrates ingest, search, context retrieval).
`router.py` does centroid pre-ranking across collections; `watcher.py` + `sync.py` keep on-disk corpora
and the index in sync. `server/` splits routes per resource (`routes_*.py`) behind `app.py`; MCP mounts at
`/mcp` and the optional OpenAI shim at `/v1`, both on the REST port. `jobs/` holds the async job store plus
the `BackupLoop` / `MaintenanceLoop`. `install/` is the OS-service installer package. Graph, telemetry, and
eval subsystems are documented in the map below.

Per-module detail: `Documentation/Architecture/110_component_catalog_and_layer_breakdown.md`.

## Hard invariants

These are the rules that are not discoverable by reading one file. Everything else: read the source.

- **`GET /openapi.json` is the authoritative HTTP contract.** Keep it in sync; record breaking REST/MCP
  changes in `BREAKING.md`.
- **`paths.py` `get_data_dir()`** (env `ARCHON_SEARCH_DATA_DIR`, default `~/.archon-search/`) is the single
  source every path accessor derives from — one env var relocates the whole runtime tree.
- **Lifespan startup never awaits slow work.** uvicorn binds the listening socket only after lifespan
  startup returns, so anything awaited there keeps the port closed (clients get `ConnectError`, not a
  served 503). Spawn with `asyncio.create_task` and track in `app.state._background_tasks`.
- **Auxiliary writes never fail their primary operation.** Post-ingest graph extraction, mention cleanup on
  delete, and the synonym-enrichment callback are all wrapped in `try/except` that logs WARNING and
  returns — a bad graph write must never fail an ingest or delete.
- **Never put `str(exc)` in a wire-facing `detail` or job `error` field** for a caught exception whose
  message embeds internals. Define one sanitized `DETAIL`/`CODE` constant pair next to the exception.
- **`store.py` SQL predicates go through `_where_eq` / `_where_in`** (which quote via `_sql_quote_str` in
  `store_filters.py`), never f-strings. `tests/test_no_fstring_sql.py` fails the build otherwise.
- **`STORE_SCHEMA_VERSION` bump policy:** increment it in `store.py` whenever a structural change to the
  shared `_schema()` (chunk table) or `_meta_schema()` (collection metadata) requires migrating existing
  rows, and add the matching `MigrationSpec` to `SearchStore._all_migrations()`. **Exception:**
  per-collection chunk-table-only changes (e.g. `migrate_acl`) do NOT bump it. Current value: `1` —
  version 1's migrations are NOT applied at startup; operators run `POST /collections/{name}/migrate`.
- **Collection/namespace names** may not contain `__` or lead/trail with `_` (`_validate_collection` /
  `_validate_namespace`) — this guards the `_archon_graph_{ns}__{col}_*` graph table names.
- **All `GraphStore` public methods take `ns` as the LAST parameter.**
- **Telemetry never sees a raw query.** Factory methods in `telemetry/entry.py` must not accept a `query`
  parameter — the guarantee is structural, not a policy check.
- **`search_with_context` rejects any non-null `graph_mode`** — permanent design decision, not deferred
  work. Use `search` instead.
- Every new `Path.home()` callsite needs a `tests/path_home_allowlist.txt` entry (file, lineno, sha).

## Documentation map

Full documentation lives under `Documentation/`. Treat the **source as the single source of truth** — when
a doc and the code disagree, fix the doc. When you change behavior a doc describes, update it in the same
PR. ADRs are append-only: supersede with a new ADR, never edit an accepted one.

Start here: `Architecture/990_documentation_index_and_contribution_guide.md` (index of every doc),
`Architecture/100_system_architecture_overview.md` (C4 + pipeline),
`Architecture/110_component_catalog_and_layer_breakdown.md` (every module → layer → public symbols),
`Architecture/600_api_reference_or_public_interface.md` (REST + MCP + CLI surface).

| Topic | Doc |
|---|---|
| Vision, non-goals | `Architecture/000_introduction_and_guiding_principles.md` |
| Hard invariants (CalVer, coverage, no-raw-query) | `Architecture/010_engineering_principles_and_constraints.md` |
| Sync/async integrations (HTTP, MCP, watcher, jobs) | `Architecture/120_services_and_integration_architecture.md` |
| LanceDB schema, FTS, RRF, persistence, graph tables | `Architecture/130_data_architecture_and_persistence.md` |
| Error taxonomy and HTTP-status mapping | `Architecture/140_error_handling_strategy.md` |
| Auth, ACL, telemetry privacy | `Architecture/150_security_and_privacy_architecture.md` |
| Health/status surface, service install, runbooks | `Architecture/160_operational_readiness_monitoring_and_reliability.md` |
| Test markers and coverage rules | `Architecture/200_testing_strategy.md`, `tests/CLAUDE.md` |
| Routing knobs and latency regression guards | `Architecture/210_performance_and_scalability.md` |
| CLI a11y posture, i18n scope (English-only by design) | `Architecture/220_accessibility_and_internationalization.md` |
| Dev workflow, package vs distribution naming | `Architecture/500_development_workflows_and_conventions.md` |
| `release.sh`, CalVer, CI workflows | `Architecture/510_release_and_environment_strategy.md` |
| OpenAPI as authoritative, MCP/REST relationship | `Architecture/520_api_design_and_contracts.md` |
| Tech-debt register | `Architecture/530_technical_debt_refactoring_roadmap.md` |
| MCP mount + namespace propagation | `ADRs/09_mcp_http_mount_and_namespace_propagation.md` (see `ADRs/` for LanceDB, fastembed, reranker, router, telemetry) |
| Graph operations, community rebuilds | `OperatorGuide/60_graph_operations.md`, `UserManual/65_graph_search.md` |
| Config reference | `UserManual/30_configuration.md`, `archon-search.toml.example` |
| Roadmap, planned work | `Documentation/Backlog/03_world_class_roadmap.md`, `Documentation/Backlog/` |
| Onboarding | `Documentation/quick_start.md`, `contributing.md` (repo root) |
| End-user / operator guides | `Documentation/UserManual/`, `Documentation/OperatorGuide/` |

`Documentation/Completed/` is an archive — off-limits for editing; trashing superseded files is sanctioned.

## TypeSpec contracts

`tsp_contract/` holds all `.tsp` contract files and their generated `openapi.yaml`. When designing a new
API surface or integration seam, save the `.tsp` there, and the compiled output (`tsp compile .`) beside
it. Related planning docs (brief, tasks, team-plan) live alongside.

## Memory and learning

**Before any task:** read `learnings.md` in full and apply it — the "What Has Worked" patterns, and the
"What Has Failed" ones to avoid. Note which entries you act on.

**After any task:** update `learnings.md`. Increment `(×N)` and refresh the date on every entry you acted
on. New entries start at `(×1)`, format `**[Date] (×N) — [Task type]**`, action-first and project-specific
("Avoid relative imports in /utils — the build step resolves them incorrectly", not "be careful with
imports"). Skip adding if nothing new arose, but still increment what you used. Never add general best
practices or restatements of existing entries.

**Hard caps, non-negotiable: under 30 lines AND under 256 chars per line.** Compact before adding if
either is breached: merge same-theme entries, compress to a single bullet, delete what is stale or now
enforced by a code guard, a test, or this file. Only evict when compression is not enough — lowest `N`
first, ties broken by oldest date. Full detail for compacted entries lives in `learnings-archive.md`
(grep it, never read it whole).
