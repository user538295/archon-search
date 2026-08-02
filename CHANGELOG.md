# Changelog


## [26.8.1800] - 2026-08-02

canary

**Installation wizard + Docker improvements + metadata safety**

**Installation & Service Management**
- Wizard's `--config` path now correctly threaded to started service (previously service ignored the flag and read default config, causing 401 errors on custom keys and timeout on non-default ports)
- Legacy service file no longer removed on install abort (failed runs now leave the existing service intact rather than requiring manual recovery)
- Readiness gate tolerates first-launch crashes with extended timeout (installer now survives one `ModuleNotFoundError` restart and surfaces the error on timeout)

**Docker**
- CPU base image now resolves CPU-only torch (removed ~6.2 GB of unnecessary CUDA/nvidia packages from CPU deployments)
- Healthcheck start-period extended from 360s to 600s to accommodate network-bound first-start extras install

**API & Configuration**
- `POST /collections/{name}/reindex-metadata` with `dry_run: true` is now purely read-only (previously wrote to meta table, causing transient 404s during concurrent search)
- Empty `log_file` setting now preserved under `ARCHON_SEARCH_DATA_DIR` (environment variable no longer clobbers explicit empty-string opt-out)
- Dry-run CLI output marked with `[DRY RUN]` prefix for clarity

**Documentation**
- `GET /jobs` response envelope documented with pagination details (`next_cursor` field)
- Fixed quotes of server-not-running error message and dry-run examples (added missing `--wait` flag)


## [26.8.1751] - 2026-08-01

**Reliable service shutdown, safe dry-run rehearsals, and installation refactoring**

**Service lifecycle**
- `archon-search stop` now waits for the process to fully terminate (up to ~10 seconds) before returning, fixing a race where `GET /health` still returned 200 while the server was shutting down and accepting requests.
- `GET /health` returns 503 during shutdown to signal clients to stop routing work to the instance.

**Setup wizard**
- `archon-search wizard --dry-run` now properly previews all operations without executing them—previously it removed the legacy service config even in dry-run mode.
- Validates `--db-path` write permissions during dry-run, surfacing errors during preview instead of silently skipping the check.

**Installation**
- Refactored the monolithic 2857-line `install.py` into a focused `archon_search/install/` package with modules like `installer.py`, `config_writer.py`, `wizard.py`, and `service_ops.py`. Existing `from archon_search.install import X` imports remain unchanged via re-export.
- Enforced dry-run correctness via strategy pattern: all system mutations route through abstract methods, allowing `DryRunInstaller` to preview operations while `RealInstaller` executes them, preventing accidental state changes in rehearsal mode.


## [26.7.1738] - 2026-07-29

**Multi-platform Docker, reindex isolation, and test reliability**

- Added `linux/arm64` to the Docker buildx platform matrix alongside `linux/amd64`, so ARM64 hosts (Apple Silicon, AWS Graviton) can pull the native image. The slim image now ships multi-arch manifests; the NVIDIA CUDA variant remains `amd64`-only.

- Fixed `reindex_collection` to skip directory scans when a collection has no configured source path (e.g., collections created via single-file `POST /ingest`). Previously, the reindex task would scan the server's current working directory, causing hangs or unintended file ingestion. Meta-only collections now complete cleanly.

- Extended the Docker test runner's graph server startup timeout from 30 s to 90 s to account for spaCy model loading. Local development timeout remains 30 s.

- Fixed `archon-search wizard --dry-run` to leave the filesystem completely untouched—previously it created `~/.archon-search/` and subdirectories even in dry-run mode.

- Improved test coverage for single-file ingest collection visibility, reindex regression guards, and Docker smoke suite CLI handling.


## [26.7.1727] - 2026-07-28

**Single-file ingest collections now fully functional; installation wizard improvements**

**Collections and routing**
- Single-file `POST /ingest` collections can now be listed (`GET /collections/`), inspected, deleted, reindexed (`POST /collections/{name}/reindex`), and routed via `POST /route` — previously these returned 404 or empty results because they relied on `config.collections` paths that single-file ingest never creates. The router now seeds its collection cache with `initial_metadata` from the store on startup instead of fetching it via an incorrect internal HTTP call.

**Installation wizard**
- Multilingual profiles with `--skip-preload` now download the required ~1 MB language-detection model (`lid.176.ftz`) instead of deferring it; download failure gracefully falls back to English-only.
- spaCy model (`en_core_web_sm`) now installs via `uv pip` in tool-install contexts instead of `python -m spacy download`, fixing "No virtual environment found" errors when no active venv exists.
- `--dry-run` mode no longer registers or starts the service; the wizard prints "[DRY RUN]" and skips mutations, making behavior predictable regardless of platform layer.
- fastembed's mean-pooling UserWarning (harmless, multilingual embedders only) is now suppressed during model prewarm to reduce alarm during setup.

**Documentation and testing**
- `GET /status` endpoint now correctly documented: `path`, `doc_count`, and `chunk_count` return real values sourced from configuration, cached metadata, and live chunk counts — not hard-coded empty.
- Test suite improvements: benchmark clock test de-flaked under parallel concurrency, 23 previously-skipped tests now run (optional deps `textual`, tree-sitter, `xlwt` now provision on `uv sync`), Leiden local/global recall test fully implemented, CI integration step now excludes eval-gated tests to prevent fixture failures.


## [26.7.1725] - 2026-07-28

**Single-file ingest collections now fully integrated; install wizard reliability improvements**

**Collection visibility and operations**

- `GET /collections/` and `GET /collections/{name}` now show collections created by single-file `POST /ingest`, which previously only persisted metadata but remained invisible. The endpoints now union config-based collections with metadata-only rows from the store.
- `DELETE /collections/{name}` and `POST /collections/{name}/reindex` now accept metadata-only single-file ingest collections instead of returning 404.
- `POST /route` now includes single-file ingest collections in `routable_names`, using direct metadata instead of an ineffective self-referencing HTTP fetch.

**Install wizard**

- Multilingual profiles now always download the required `lid.176.ftz` language-detection model even with `--skip-preload` (which defers only heavy embedder/reranker weights). Failed downloads gracefully degrade to English-only instead of aborting.
- spaCy model downloads now use `uv pip install` instead of `python -m spacy download`, fixing failures when the install subprocess lacks virtual environment context.
- `--dry-run` flag now correctly skips service registration and startup entirely, printing `[DRY RUN] Would register and start the search service.` instead of relying on platform-layer short-circuits.
- Suppressed harmless fastembed `UserWarning` about mean pooling in multilingual embedders during model pre-download.

**Test suite and documentation**

- `GET /status` documentation corrected to reflect actual behavior: `path`, `doc_count`, and `chunk_count` return config-resolved values and live counts, not hard-coded zeros.
- Default test suite now installs all feature extras (`textual`, `xlwt`, language grammars, `leidenalg`) on `uv sync`, fixing 23 previously-skipped tests.
- Implemented graph-based local and global recall evaluation test.
- De-flaked benchmark clock test under parallel load and migrated data-path callsites to centralized config helpers.


## [26.7.1708] - 2026-07-27

canary

**All `collection` subcommands now proxy the server; Docker dev/test workflows; container-mode CLI guards; spaCy resilience**

**CLI: collection list → HTTP proxy + key management**

`archon-search collection list` now proxies `GET /collections/` instead of opening LanceDB directly, making all `collection` subcommands server-based. This fixes startup failures in Docker when `/data` is not yet mounted. The `--config` flag is removed; use `--api-key` / `ARCHON_SEARCH_API_KEY` / the key file instead (same as every other command). New CLI functions `load_key()` (returns key from env or file, never generates) and `persist_key(key)` (atomic key file write) let commands work in read-only mounts. When a key comes from the environment, it's now persisted to disk so CLI processes in the same shell don't need the env var re-exported.

**Container mode: install / uninstall / start / stop / status guards**

CLI commands that modify system services now detect container environments (`ARCHON_SEARCH_CONTAINER=1`) and exit cleanly with an instructional message instead of failing. Affected: `install`, `uninstall`, `start`, `stop`. The `status` command suppresses the "stopped" service line when containerized (HTTP telemetry still shows if the server is reachable). The `maintenance run` command exits 0 when the server is not running, making it safe for provisioning scripts.

**Docker: test runner, dev shell, and optimized image**

New `docker-compose.override.yml` services for development: `archon-test-runner` runs the full suite including smoke tests in a clean Linux environment, and `archon-dev-shell` offers an interactive shell. Both mount source at `/workspace` and reuse named volumes for incremental builds. A new `Dockerfile.test` (Python 3.12 bookworm-slim with uv) removes the need for local Python when running tests or developing on macOS. The production image now installs graph/code/multilingual extras at runtime via an entrypoint script instead of baking them in, reducing base-image size for core-only deployments.

**Docker smoke tests: CLI behavior proofs**

New test suite at `tests/smoke/docker/` verifies archon-search CLI behavior inside containers: `--help` / `--version` / `config show` work offline, `serve` starts and shuts down cleanly, `install` / `uninstall` / `start` / `stop` emit clean container-mode messages, `status` renders telemetry payloads correctly. Smoke tests run serially in the `archon-test-runner` service; the full suite (`7842` passed) includes both fast parallel tests and the new serial Docker proofs.

**Graph: spaCy download failures no longer crash the server**

`spacy.cli.download()` calls `sys.exit(1)` when no package installer is available (common in `uv tool install` environments). This `SystemExit` bypassed exception handlers and killed the server, leaving ingest jobs unretrieved. Now caught and re-raised as `RuntimeError` with an actionable message, so ingest failures are logged as graceful errors and the server stays alive.

**Other**

- Fixed spurious WARNING logs on `GET /status` for fresh collections with no graph data yet: LanceDB raises `ValueError` (not `FileNotFoundError`) when graph tables don't exist; `node_count` and `edge_count` now catch both.
- `.dockerignore` extended to exclude large agent-state directories (`.claude/`, `.tokensave/`, `.hypothesis/`, etc.) from Docker builds.
- `.gitignore` now excludes `.tokensave/` agent state.


## [26.7.1682] - 2026-07-22

**Permission-Aware Search Snippets + ACL provenance tracking + improved release tooling**

**Permission-Aware Search (G15)**

`POST /search` and `POST /explain` now expose ACL context and decision provenance via the `acl_gate` field (when `acl_context: true` is passed to search). The `acl_gate` field includes `allowed_principals`, `source` (one of `"frontmatter"`, `"sidecar"`, `"collection_default"`, `None`), `sidecar_path`, and structured `warnings` — letting clients understand why a chunk was accessible and which ACL rule applied. `POST /explain` always populates `acl_gate` unconditionally on every `ExplainResult` (near-misses do not carry the field). All fail-open branches in ACL parsing (`parse_acl_value` and `read_acl_sidecar`) now surface structured warnings: invalid types, non-string elements, invalid namespace names, symlink detection, UTF-8 decode failures, and deny-all edge cases. Three new nullable columns (`acl_source`, `acl_sidecar_path`, `acl_warning`) are added to the store via startup migration and propagated through `ChunkRecord`, `SearchResult`, and `ScoredSearchCandidate` — the full pipeline from ingest through search carries the provenance.

**Release tooling**

`release.sh` now prints which synthesis path it is using (`"release notes: using Anthropic API key"` or `"release notes: using claude -p CLI"`) to stderr before attempting to generate notes, raising the Claude CLI synthesis timeout from 2 to 5 minutes, replacing `head` with `awk` to avoid SIGPIPE under `set -o pipefail`, and running synthesis in dry-run mode to catch errors before commit. The `synthesize_release_notes.py` script receives visibility into both success and fallback paths, eliminating silent failures.

**Testing and CI**

Fixed a long-standing gap where tests calling `leidenalg`-dependent code at runtime (e.g. `CommunityBuilder.build()`) raised `ImportError` in CI even when they never imported `leidenalg` directly — the lazy import fires at the call site, not module load. Any test body that triggers a leidenalg code path now uses `pytest.importorskip("leidenalg")` as its first line, regardless of whether it imports the package directly. This keeps the suite green in CI when the `[graph]` extra is absent.

**Textual UI examples**

The `examples/` prototype wizard was rebuilt as a two-step setup flow: device calibration (step 01) and core matrix selection (step 02) on the new colour system (`#141414` bg, `#80C0F8` blue, `#F09850` orange, `#62C9C3` cyan, `#86C08A` green). Step 01 runs a 5-phase animated benchmark (probe → throughput → headroom → disk → derive factor) with braille spinner and live measured values, then shows the factor derivation and MAX SAFE LOAD verdict. Step 02 selects corpora and profiles with animated gauge sweeps and live EST LOAD sparkline in braille dots (`⣀⣤⣶⣿`). Animation was split into `sweep_corpus` and `sweep_profile` clocks so changing the corpus only re-animates the corpus section, leaving profile gauges stationary — fixing jittery reveals where unrelated widgets flickered on every input. Tests rewritten to cover calibration completion, device cycling, cross-screen navigation, per-section animation isolation, and braille EST LOAD rendering.

**CLI**

`archon-search collection info <name>` is now an HTTP proxy to `GET /collections/{name}` instead of dumping a raw Python `CollectionMeta(...)` repr (which included 384-element embedding vectors and internal details). Output is human-readable and consistent with the REST API: 13 fixed fields in order (`name`, `description`, `namespace`, `doc_count`, `chunk_count`, `active_embedding_model`, `pending_embedding_model`, `needs_reindex`, `reindex_job_id`, `last_indexed`, `default_ttl_seconds`, `schema_version`, `centroid_present`), with null/empty fields omitted and `last_indexed: never` when the server returns null.

**Documentation**

Graduated 63 completed briefs, plans, and tasks from `Documentation/Backlog/` to `Documentation/Completed/` (features shipped across briefs 010–350: smoke suite, wizard fixes, CLI proxy, community rebuild, collection-add-async, permission-aware snippets, and every UX/error fix through brief 330). Updated cross-references in docs and roadmap links to point to the new locations, keeping the active queue focused on in-flight work.


## [26.7.1681] - 2026-07-22

**Test CI robustness for optional graph extras**

- Graph-dependent tests now skip gracefully when the `[graph]` extra (`leidenalg`, `igraph`) is absent, instead of raising `ImportError` at runtime. Tests that invoke graph code paths (e.g., `CommunityBuilder.build()`) now use `pytest.importorskip("leidenalg")` as their first line to catch the missing dependency early, ensuring CI reliability across different installation configurations.


## [26.7.1678] - 2026-07-22

**Permission-aware search snippets, Textual UI wizard, and CLI collection info proxy**

**Permission-Aware Search Snippets**

G15 adds end-to-end ACL provenance tracking through ingest and search:

- Added `acl_source`, `acl_sidecar_path`, `acl_warning` fields to `ChunkRecord`, `SearchResult`, and `ScoredSearchCandidate` to track how each chunk's ACL was resolved (frontmatter, sidecar file, or collection default)
- Refactored `resolve_acl()`, `parse_acl_value()`, and `read_acl_sidecar()` to return structured `AclResolutionResult` with warnings for all fail-open branches (invalid types, non-string list elements, invalid namespace names, UTF-8 decode failures)
- Added three new nullable columns (`acl_source`, `acl_sidecar_path`, `acl_warning`) to the LanceDB chunk table via startup migration `migrate_acl_provenance()`
- New `AclGateSchema` in `POST /search` — pass `acl_context: true` to receive `acl_gate` with `allowed_principals`, `source`, `sidecar_path`, and `warnings` on each result (null when omitted)
- `POST /explain` now unconditionally includes `acl_gate` on every `ExplainResult`; near-misses carry no `acl_gate` field
- All ACL parsing warnings are now surfaced as structured data, making ACL resolution transparent to clients

**Textual Examples Wizard**

Rebuilt the `examples/` prototype as a two-screen calibration→core-selection wizard:

- New color palette from design handoff: `#141414` background, `#80C0F8` blue, `#F09850` orange, `#62C9C3` cyan, `#86C08A` green
- New `textual_calibration.py` — step 01 device benchmark with 5-phase animated sequence (probe → throughput → headroom → disk → derive factor), live measured values, braille spinner, and MAX SAFE LOAD verdict; press `r` to re-run, `d` to cycle device, `n` to advance
- Rewrote `textual_core_matrix.py` — step 02 core matrix (2 corpus + 3 profile choices, cursor-follows-info, gauge sweep, EST LOAD sparkline, breadcrumb, LOCKED flash) plus `WizardApp` wiring both screens with NEXT/BACK navigation
- Split animation clocks into `sweep_corpus` and `sweep_profile` so only the changed section re-animates
- Render EST LOAD sparkline with braille dots (`⣀⣤⣶⣿` ramp) instead of block bars

**CLI Collection Info**

`archon-search collection info <name>` is now an HTTP proxy to `GET /collections/{name}`:

- Displays 13 human-readable fields in fixed order: `name`, `description`, `namespace`, `doc_count`, `chunk_count`, `active_embedding_model`, `pending_embedding_model`, `needs_reindex`, `reindex_job_id`, `last_indexed`, `default_ttl_seconds`, `schema_version`, `centroid_present`
- Omits null or empty fields; shows `last_indexed: never` when null
- Accepts `--api-url` / `--api-key` options consistent with other proxy commands


## [26.7.1533] - 2026-07-14

**Add dry_run support to service lifecycle and implement get_search_service**

- Add dry_run support to service lifecycle and implement get_search_service
- Slim install command to register-and-start only
- Fix task 12.1 plan based on spike findings
- Add [multilingual] optional extra with fasttext-wheel to pyproject.toml
- Add language_detection_confidence_threshold config key
- Fix store.py read path to preserve three-state language value
- Implement languagedetector module (task 3.1)
- Add _prompt_fasttext_license() license gate to install.py (task 4.1)
- Add _download_fasttext_model() and fasttext_model_url to install.py
- Add --accept-fasttext-license cli flag to wizard command
- Add _check_multilingual_deps() startup guard in app.py
- Add language parameter to documentchunker.chunk() (task 6.1)
- Wire languagedetector into pipeline.py ingest_file (task 6.2)
- Unlock searchfilters.language validator (task 7.1)
- Unlock language filter in store_filters.py build_where (task 7.2)
- Add language_filter_used boolean to filterflags telemetry
- Add /status warning for untagged chunks in multilingual mode
- Update mcp search tool descriptions for language filter (task 10.1)
- Add multilingual eval fixtures and thresholds (task 11.1)
- Wire language-aware fts tokenization in store.py (task 12.1)
- Final verification and documentation update (task f.1)
- Mark c2 shipped, update last-reviewed date
- Add c3a/c3b/c3c briefs, c3a/c3b plans, link from roadmap
- Add transient start_offset/end_offset fields to chunkrecord (task 1.1)
- Propagate chonkie character offsets to chunkrecord (task 1.2)
- Implement markdownenricher with prepare() and enrich_chunk() (tasks 2.1 & 2.2)
- Wire markdownenricher into pipeline.py ingest_file() (task 3.1)
- Final verification and documentation update (task 4.1)
- Add page_break_marker constant and export to enricher.py (task 1.1)
- Implement tasks 1.2 + 5.1 together (bundled commit)
- Implement task 2.1 — _extract_page_breaks pre-removal page table builder
- Add _transform_page_table coordinate-transform pure function (task 2.2)
- Implement task 2.3 — markdownenricher._excise_markers
- Implement task 2.4 — markdownenricher.preprocess entry point
- Implement task 2.5 — enrich_chunk page resolution
- Implement task 3.1 — _source_subtype map and is_docling_source helper
- Implement task 4.1 — wire preprocess into ingest_file
- Add task 4.2 per-chunk page metadata merge tests (pipeline.py)
- Implement task 5.2 — eval query, label, and eval corpus fixture
- Final verification and documentation update (task f.1)
- Implement task 1.1 — [code] optional dep group and code_enricher module
- Implement task 1.2 — scopeentry, scopetable, code_extensions tests
- Implement task 2.1 — _module_path helper with full test coverage
- Implement task 3.1 — grammar registry with lazy loading and graceful degradation
- Implement task 4.1 — python fixture for scope-table builder tests
- Implement task 4.2 — typescript fixture for scope-table builder tests
- Implement task 5.1 — _build_scope_table tests (python + typescript)
- Implement task 6.1 — codeenricher.prepare() tests (tdd)
- Implement task 7.1 — dispatch codeenricher in pipeline.py
- Task 8.1 — add code corpus eval queries and labels
- Final verification and documentation update (task 8.2)
- Task 1.1 — hydeconfig dataclass + [hyde] toml loader
- Task 1.2 — hydegenerator class + optional import guard + rate limiter
- Task 2.1 — searchpipeline.search() gains query_vector parameter
- Task 2.2 — searchpipeline.search_many() gains query_vector parameter
- Task 2.3 — searchpipeline.search_with_context() gains query_vector parameter
- Task 3.1 — searchrequest/searchresponse gain hyde fields
- Update breaking.md, openapi snapshot, and acl schema test for task 3.1
- Task 3.2 — explainrequest/explainresponse gain hyde fields
- Task 4.1 — hydegenerator init in app.py + pyproject.toml optional dep
- Task 4.2 — wire resolve_hyde_vector into routes_search.py handler
- Task 4.3 — wire resolve_hyde_vector into routes_explain.py handler
- Task 5.1 — wire mcp search, search_with_context, explain tools for hyde
- Task 6.1 — telemetry invariant ci guard for hyde.py
- Task 6.3 — eval harness hyde regression scenario + latency threshold
- Task 7.1 — final verification, adr, and documentation update
- Task 1.1 — add ragfusionconfig to config.py
- Task 1.1 — ragfusionconfig dataclass + [rag_fusion] toml loader
- Task 1.2 — ragfusiongenerator core module
- Mark task 1.2 complete in plan
- Task 2.1 — _fuse_rag_fusion_results() second-pass rrf in pipeline.py
- Task 2.2 — wire ragfusiongenerator into searchpipeline.search()
- Mark task 2.2 complete in plan
- Task 2.3 — search_many() gains rag fusion orchestration
- Task 2.4 — explainpipelineresult rag fusion fields + pipeline.explain() orchestration
- Task 3.1 — searchrequest.rag_fusion + searchresponse rag fusion fields
- Task 3.2 — explainrequest + explainresponse + ragfusionsubqueryresult schema
- Task 3.3 — telemetryentry rag fusion fields
- Task 4.1 — wire ragfusiongenerator into app startup + toml.example
- Mark task 4.1 complete in plan
- Task 4.2 — wire routes_search.py rest handler for rag fusion
- Task 4.3 — routes_explain.py end-to-end rag fusion wiring
- Mark task 4.3 complete in plan
- Task 4.4 — wire mcp search, search_with_context, explain for rag fusion
- Mark task 5.1 complete in plan
- Task 6.1 — ci guard for no-raw-query invariant in rag_fusion.py
- Task 7.1 — final verification, adr corrections, and documentation update
- Task 1.1 — spike script and integration tests verifying table.optimize() gates
- Task 1.2 — fts_optimize_removes_deleted constant and supports_incremental_fts_delete property on searchstore
- Task 2.1 — add store.optimize_fts() for incremental fts maintenance
- Task 2.2 — fts maintenance hook in store.delete_document()
- Task 3.1 — ingest_file uses optimize_fts with fallback to rebuild_fts_index
- Replace rebuild_fts_index with optimize_fts
- Replace rebuild_fts_index with optimize_fts at batch end
- Task 4.1 — remove redundant rebuild_fts_index from reindex_metadata
- Task f.1 — final verification, consistency test, and documentation update
- Task 1.1 — install pytest-xdist dependency
- Task 1.2 — fix three_page_pdf fixture for xdist safety
- Task 1.3 — fix test_update_description_timeout_skips_write
- Task 2.1 — enable parallel addopts (-n auto --dist=loadfile)
- Task 3.1 — add -n0 to ci default-run pytest commands
- Task f.1 — final verification, acceptance criteria check, and documentation update
- Phase 1 — introduce mcp_schemas.py with all mcp pydantic schemas
- Task 2.1 — _err_schema constant + migrate search tool to mcpsearchresponse
- Task 2.2 — migrate search_with_context to pydantic schemas
- Task 2.3 — add validationerror catch to explain tool (schema drift guard)
- Task 2.4 — migrate ingest_file and ingest_directory to ingestresultschema
- Task 2.5 — migrate list_collections and get_collections_meta to pydantic schemas
- Task 2.5 — add outer exception handler tests for list_collections and get_collections_meta
- Task 2.6 — migrate get_collection_meta and update_collection to collectiondetailschema
- Task 2.7 — migrate list_documents and delete_document to pydantic schemas
- Task 3.1 — add breaking.md entries for five field-narrowing mcp tools
- Task f.1 — final verification and documentation update
- Task 1.1 — add wizardfeatures dataclass to install.py
- Task 1.1 — create tests/pipeline/ scaffolding with conftest.py
- Task 2.1 — create tests/pipeline/test_pipeline_ingest.py
- Task 2.2 — create tests/pipeline/test_pipeline_search.py
- Task 2.3 — create tests/pipeline/test_pipeline_multi.py
- Reorganise documentation — move completed items to completed/, add new backlog entries
- Task 3.1 — delete tests/test_pipeline.py
- Task f.1 — final verification and documentation update
- Task 1.1 — promote connected_store to session scope
- Task 1.2 — switch addopts to --dist=loadgroup
- Task 1.3 — add xdist_group("mcp") marker to 16 files
- Task 2.1 — measure wall time and add xdist_group("install") fix
- Task 3.1 — final verification and documentation update
- Brief — bypass fts-index rebuild in tests that don't query fts
- Normalize status field to "done" across completed/ plans
- Bring both roadmaps up to date through c7 + active backlog
- Add _prompt_multilingual() function (c8 task 1.2)
- Add _prompt_optional_features() for 7 optional wizard questions
- Add _prompt_gpu_confirm() for interactive gpu acceleration prompt (c8-1.4)
- Implement _apply_wizard_features_to_toml()
- Extend _write_profile_config and _profile_toml with wizardfeatures
- Add _install_code_extra() with uv/pip fallback (c8-2.3)
- Extend _render_summary() to display optional features (c8-2.4)
- Wire optional-feature prompts and gpu confirm into searchinstaller.run()
- Add 8 wizard-only cli flags to install_cmd.py (task 3.2)
- Final verification and documentation update for wizard optional features
- Add comprehensive user-facing wizard guide (02_wizard.md)
- Gate branch b filesystem writes and fix stale self.cfg (c14 task 1.1)
- Gate branch c filesystem writes behind dry_run check (c14 task 1.2)
- Gate fasttext download, prewarm, and force-reinstall .bak on dry_run (c14 task 1.3)
- Convert --multilingual to tri-state flag-pair with --no-multilingual
- Reorder wizard prompts — gpu confirm before licenses, optional features after
- Add explanation print blocks to optional-feature prompts (c14 task 4.1)
- Add recommended annotation to balanced profile in table (c14-5.1)
- Expand summary screen and add next steps block
- Add _detect_config_hand_edits for wizard overwrite protection
- Integrate overwrite warning into branch c of wizard run()
- Final verification and documentation update for wizard ux improvements
- Add 9 new wizardfeatures fields for tier 1 flags and hyde/rag fusion
- Implement _apply_wizard_features_to_toml() write logic for 9 new fields
- Add 10 new keyword params to searchinstaller.run()
- Add 7 tier 1 click options to wizard with validation
- Print full api key with source in wizard success output
- Extract _install_extra() helper from _install_code_extra()
- Add hyde/rag fusion prompt to _prompt_optional_features() and cli flags
- Add --enable-hyde and --enable-rag-fusion help tests to test_install_cmd.py
- Add --server-key click option with _hexkeyparamtype validation
- Add server-key integration tests to e2e wizard suite
- Final verification and documentation update for wizard configurability expansion
- Print [dry-run] message for --server-key in dry-run mode
- Show correct host/port in wizard summary when --host/--port passed
- Allow --dry-run to proceed past model-mismatch guard
- Clarify scope, fix false claims, and resolve open questions
- Add rebuild_fts parameter to ingest_directory and unit tests
- Switch eligible ingest_directory calls to rebuild_fts=false in test_pipeline_ingest.py
- Graduate c8/c14/c15 to completed, iterative-review c9 plan
- Remove graduated plans from backlog, update c9 plan
- Verify all acceptance criteria, update roadmap, and move to completed
- Add missing language param to stubchunker in test_sync.py
- Update integration tests to use current collectionmeta and store api
- Update test_sync_fts integration tests to match current api
- Restore purpose comments stripped during threshold update
- Fix stale marker-exclusion claims found by devil's advocate review
- Add archon_search_host/port env overrides and serve kwarg
- Add archon_search.paths.get_data_dir() single source of truth
- Harden get_data_dir() per iterative da review
- Route db_path, log_file, telemetry.log_dir through archon_search_data_dir
- Clarify env var override block per iterative da review
- Add real-model search latency benchmark brief
- Replace key_file constant with lazy get_key_file()
- Add c16 real-model search latency benchmark plan
- Rename brief to c16 prefix; add env var scope note to c9
- Harden archon_search_key_file validation per iterative da review
- Replace jobs_file constant with lazy get_jobs_file()
- Harden get_jobs_file() laziness tests per iterative da review
- Replace fasttext_models_dir with lazy get_fasttext_models_dir()
- Harden task 2.5 lazy fasttext path per iterative da review
- Replace path.home() with lazy get_data_dir() for history sessions default
- Document archon_search_data_dir redirection for history sessions default per iterative da review
- Add `serve` subcommand for foreground/container deployment
- Add archon_search_container stderr handler for container deployments
- Tighten task 3.2 helpers, parity tests, transition test per iterative da review
- Add dockerfile and .dockerignore for cpu/gpu container build
- Add docker-compose dev/test/prod stack with isolated volumes
- Document lazy key file resolution and data_dir override
- Add c17 install-lock parallel isolation brief
- Add 30+ collections and flaky-test brainstorm notes
- Add onboarding.md teammate onboarding guide
- Finalize c9 — running with docker + serve/data_dir coverage across the doc tree (task 5.2)
- Seed tests/path_home_allowlist.txt with 15 hash-pinned path.home() callsites
- Register archon_unset_data_dir pytest marker in pyproject.toml (c17 task 2.1)
- Migrate install.py path.home() callsites to get_data_dir() and shrink allowlist (task 3.1)
- Add archon_unset_data_dir marker + path.home() ratchet to testing strategy (task 4.1)
- Move completed c9 and c17 briefs and plans to completed/
- Mark c9 and c17 as complete, add c17 to test-suite infra section
- Add live_benchmark marker, benchmarkthresholds, real-model latency tests, and ci gate
- Calibrate live_thresholds.toml with darwin-derived estimates and add workflow_dispatch
- Add live_benchmark documentation across strategy, perf, and eval guide
- Correct return type annotation and p95 unit test formula coupling
- Move completed c16 brief and plan to completed/
- Mark c8/c13/c14/c15/c16 complete, add missing items to status snapshot
- Add collection export/import feature brief (d1+d2)
- Add implementation plan for collection export/import (d1+d2)
- Fix 20 issues in plan after iterative-review (2 cycles)
- Add queued status, progress field, exportjob and importjob (task 1.1)
- Add [jobs] config section (jobsconfig) to searchconfig
- Add validate_export_path() and validate_archive_members() to _path_safety
- Add exportarchivewriter, importarchivereader, export_schema_version
- Update jobstore with eviction guard, export/import factories, serialization, and update_progress
- Add progress field to job_to_dict() and jobresponse
- Add jobscheduler with 5-second tick loop and fifo queued dispatch
- Register jobscheduler in fastapi lifespan with no-op dispatch
- Add _export_task() worker and list_chunks_raw() to searchstore
- Add integration tests for post /collections/{name}/export
- Add _import_task() worker to routes_export.py
- Add post /collections/{name}/import rest endpoint
- Add get /jobs list endpoint with cursor pagination
- Add post /jobs/{job_id}/resume endpoint
- Add export_collection and import_collection mcp tools
- Add archon-search export cli command
- Add archon-search import cli command
- Update all documentation for d1/d2 export/import feature
- Add backupconfig dataclass with validation and toml.example section
- Add source field to exportjob/importjob
- Priority sort backup jobs behind user jobs + get /jobs source filter
- Job_to_dict subclass fields + lancedb_version in manifest
- Wire real export/import dispatch closure in lifespan
- Add backuploop scheduled backup orchestrator
- Wire backuploop into create_app lifespan
- Add post /backup/trigger endpoint
- Extend get /status with backup state object
- Add archon-search backup cli group
- Document scheduled backup feature across all docs
- Add d2 scheduled backup feature brief
- Eliminate xdist parallel flakes from live_benchmark sys.modules pollution
- Iterative-review fixes for mcp error paths tests
- Iterative-review fixes for mcp schema contract tests
- Iterative-review fixes for multi-collection http tests
- Iterative-review fixes for wizard e2e tests
- Iterative-review fixes for dispatch scheduler e2e tests
- Iterative-review fixes for enrichment metadata tests
- Iterative-review fixes for per-collection model tests
- Iterative-review fixes for http filters round-trip tests
- Iterative-review fixes for routing integration tests
- Final verification and documentation update for e1 integration test plan
- Serialize docling tests with xdist_group to eliminate parallel flakiness
- Add brief and plan to clear anthropic_api_key in conftest autouse
- Record hypothesis confirmation measurements in brief
- Enumerate affected tests via with-key vs without-key durations
- Capture pre-fix wall-clock baseline (p50 167.59 s, range 57.78 s)
- Capture pre-change baselines (4722 collected, 4713 passed, 152 targeted, 95% description_generator coverage)
- Final verification and documentation update for c18 fix 1
- Correct comment inaccuracies in autouse anthropic_api_key block
- Harden guard-detection regex and add 4 regex meta-tests
- Fix 6 documentation issues in c18 testing docs
- Add learnings.md memory protocol and scaffold file
- Plug coroutine leaks on exception paths in watcher and tests
- Graduate c18, d1-d2, d2, e1 plans from backlog to completed
- Record session observations from c18 review and fixes
- Graduate d1/d2 roadmap items; add d3–d8 backlog briefs and d3 team plan
- Harden d3 plan via review; resolve k1, graduate to planned
- Add migrationjob, migrationspec, migrationkind entities; rewrite d3 typespec contracts
- Add store_schema_version constant and schema_version column (d3 be-2)
- Add pending_migrations() and _all_migrations() catalog (d3 be-3)
- Add get /collections/{name}/migrations/pending (d3 be-4)
- Add `collection migrate` subcommand with dry-run pending view (d3 be-5)
- Add apply_in_place_migrations() and consolidate startup migrations (d3 be-6)
- Add be-6 session observations (d3)
- Add post /collections/{name}/migrate in-place path (d3 be-7)
- Add --apply flag to collection migrate command (be-8)
- Add apply_rewrite_migration with per-collection lock and progress callbacks (d3 be-9)
- Add migrationjob to jobstore — discriminator, factory, bulk list, and job_to_dict (d3 be-10)
- Add migrationjob fields to jobresponse; regenerate openapi snapshot (d3 be-11)
- Add post /migrate rewrite async path; fix scheduler dispatch contract (d3 be-12)
- Accept migrationjob in post /jobs/{id}/resume; add coverage (d3 be-13)
- Add --backup-first and --wait flags to collection migrate (d3 be-14)
- Add store_schema_version and collections_schema_behind to get /status (d3 be-15)
- Widen jobresponse.result to str|dict|none; complete d3 t-6 close-out
- Fix two parallel-xdist flakiness sources in d3 migration tests
- Complete k1 kickoff — agree contracts c1/c2, scenarios s1–s12, q1 resolution
- Add _ingest_chunk_batch_size = 512 for d4 batch-emit
- Batch-emit ingest in ≤512-chunk batches; add _is_continuation + sample_chunk_texts
- Be-3 — remove all_vectors/all_chunks accumulators from ingest_directory(); guard metadata on all-failures
- Remove centroid_incremental_enabled; b5 incremental path is now unconditional
- T-3 close-out — update component catalog and data architecture docs
- Complete k1 kickoff — resolve open questions, add team plan and typespec contracts
- Complete k1 kickoff — harden typespec contracts and plan
- Add maintenanceconfig dataclass and [maintenance] toml section (d5 be-1)
- Add maintenanceloop skeleton with trigger loop, state file, and lifespan wiring
- Add d5 maintenance schema models to schemas.py (be-3)
- Add post /maintenance/trigger route and status builder (d5 be-4)
- Implement _run_fts_optimize policy (d5 be-5)
- Implement _run_orphan_cleanup policy (be-6)
- Add source, source_path, collection, retry_count to ingestjob base class (be-7)
- Add be-7 observations on pydantic nullable field staleness and literal runtime enforcement
- Implement _run_failed_ingest_retry pass-level policy (be-8)
- Add `archon-search maintenance` cli group (d5 fe-1)
- Close-out t-6 — acceptance fact-check passed, all criteria verified
- Add team plan, typespec contracts, and resolved brief
- Apply investigation-agent findings to team plan
- Apply iterative-review findings to provider-validation team plan
- Ratify k1 contracts and resolve scenario gaps
- Add validate_models_async and shared provider check
- Add modelvalidationstatus to statusresponse
- Add validation_timeout_seconds to searchconfig
- Record d6 be-2 pydantic and timestamp-type notes
- Spawn background model validation in app lifespan
- Surface model_validation in get /status response
- Add checkstatus.pending/warn and readinesschecks.models
- Populate /ready checks.models from model validation
- Delegate validate_providers to validate_providers_shared
- Validate reranker provider after model pre-warm (fe-1)
- Render model_validation block in maintenance status
- Document validation_timeout_seconds in toml.example
- Close out provider-validation feature and align all docs
- Record d6 be-9/fe-2/t-1/t-2 task patterns
- Add team plan, typespec contracts, and resolve brief gaps
- Ratify contracts c1-c3 and close k1 kickoff; fix typespec seam signatures
- Implement keyrecord, keystore.create/load, and authconfig (be-1)
- Add key_store param to apikeymiddleware; managed-key dispatch with hmac.compare_digest
- Wire keystore into app.py and mcp.py; add toml synthetic records
- Record d7 be-3 task patterns
- Add post /keys endpoint and keycreaterequest/keycreateresponse schemas
- Add `key create` subcommand with duration parser (d7 fe-1)
- Add keystore.revoke() and list_keys() — d7 be-5
- Add get /keys and delete /keys/{id} endpoints (d7 be-6)
- Add key list and key revoke subcommands (d7 fe-2)
- Add keystore.rotate_default_key() with da hardening — d7 be-7
- Harden rotate_default_key() input validation and return safety
- Add post /keys/rotate with safe write order, dynamic api_key; harden middleware
- Offload atomic_write_bytes to thread; use new_raw_token directly
- Add create_key, list_keys, revoke_key, rotate_key mcp tools (d7 be-9)
- Add key rotate subcommand with grace-period support (d7 fe-3)
- Project close-out — update all docs for multi-key auth
- Close out d6/d7; add d8, mcp-wiring, and multi-instance planning artifacts
- Add adr-09 mcp http mount spike; check off k-1
- Add mcpconfig dataclass and [mcp] toml section
- Mount mcp http app at /mcp in create_app() lifespan
- Pass config.namespaces to apikeymiddleware in create_mcp_http_app()
- Implement be-5 authenticated namespace propagation in tool closures
- Add namespace gate to search/search_with_context; add be-7 integration tests
- Add mcpstatusdetail to get /status response
- Add mcp status field to get /health response
- Finalize [mcp] section in archon-search.toml.example
- Close out d9 mcp http wiring — sync docs to shipped feature
- Kick off k1 — agree contracts c1/c2/c3 and scenarios s1–s16; harden plan
- Add hash_doc_ids field to telemetryconfig (d8 be-1)
- Implement hmac-sha256 doc_id hasher and wire salt into lifespan (d8 be-2)
- Add doc_ids_hashed field and doc_id_hasher factory param to telemetryentry
- Wire doc_id hasher into rest search and mcp search tools (d8 be-4)
- Add telemetrystatusdetail to get /status (d8 be-5)
- Add get /status http path to display hash_doc_ids_enabled
- Close out project — update all documentation for hashed doc_id telemetry mode
- Align k1 contracts and task specs after team review; check off k1
- Add 09_multi_instance_setup.md — be-1 core manual sections
- Update .env.example with real registry path, key isolation guidance; add lint tests
- Fix part 2 step 1 .env instructions to match current .env.example state
- Add api key isolation, http client config, and mcp client config sections (be-3)
- Verify credential isolation and fix doc accuracy issues found in t-3 manual walkthrough
- Add fastembed cache section, doc index entry, and cross-links (be-4)
- Close out multi-instance setup project — t-4 acceptance checks
- Add clickable links to all brief and plan documents
- Add e0 ux limitations audit and five sub-briefs
- Fix stale adr link, clarify d8 typespec, drop mis backlog files
- Add e0a file-type completeness team plan and typespec contract
- Ratify k1 contract seam; harden tsp accuracy and add s7 scenario
- Expand office format support to 8 new extensions
- Add be-2 observations on markitdown extras and format support
- Add supported-extension table to ingestion guide
- Update 110 catalog entry; close out e0a t-2
- Add team plan, typespec contracts, and resolve brief open questions
- Align k1 contracts; add s4b, s11b; fix stale message strings and seam accuracy
- Raise hyde and rag fusion timeout defaults from 5.0 → 10.0
- Add rag_fusion_warning to searchpipelineresult (e0b be-2)
- Add expansion_used and expansion_warning to search responses (be-3)
- Add failed_expired terminal job state (be-4)
- Transition failed jobs to failed_expired when aged-out or retry-exhausted
- Add be-5 transition() vs update() observation
- Add ingestresult.warnings and acl sidecar warning propagation (be-6)
- Propagate acl warnings through ingest job result (be-7)
- Add hyde and rag fusion key availability to get /status (e0b be-8)
- Add truncated_count to get /telemetry/stats
- Add be-9 observations on bool|none identity check and openapi regen
- Surface failed_expired_ingest_count in get /status (e0b be-10)
- Add t-3 observations on pre-seeding jobstore and bool|none e2e scope
- Add secrets loading via environmentfile (linux) and wrapper script (macos)
- Create .secrets.env for ai expansion on wizard install (be-12)
- Add --timeout option to maintenance run --wait; exit 0 on timeout, exit 2 on failed
- Add --timeout to export/backup --wait; exit 2 on failed/failed_expired
- Surface hyde/rag fusion key warnings and failed_expired count (e0b fe-3)
- Surface ingestresult warnings on stderr in cli (e0b fe-4)
- Add fe-4 observations on branch-convergence loops and clirunner kwarg removal
- T-close — acceptance fact-check, docs, learnings, plan checkoff
- Add k1 kickoff contracts and team plan for e0c api surface fixes
- Shuffle sample_chunk_texts; raise max_sample_chunks 20→100
- Add top_k_max operator ceiling to searchconfig (e0c be-2)
- Wire max_fanout and top_k_max from config into route handlers
- Add e0c be-3 observations on 422 shape change and boundary assertions
- Expose search config limits in get /status response (e0c be-4)
- Add e0c t-1 observations on docstring accuracy and mutual-exclusion guards
- Add cursor-based pagination to list_documents (e0c be-5)
- Add get /collections/{name}/documents endpoint with cursor pagination; update mcp list_documents to accept cursor
- Add e0c be-6 observations (asyncio.run, query pattern, scenario coverage)
- T-3 close-out — check off all docs and flip t-3 plan checkbox
- K1 kickoff — align contracts c1–c3, resolve q1, fix brief
- Add k1 session learnings to learnings.md
- Add ingesterror, _file_exceeds_limit helper, and ingestresult.code field (e0d be-1)
- Add ingestconfig dataclass and [ingest] toml section (e0d be-2)
- Add e0d be-2 observations (bool/int subtype, path_home ratchet)
- Add configurable size guard in ingest_file; wire max_file_mb
- Add sync 413 pre-check to post /ingest for oversized single files
- Add code field to ingestresultschema; propagate file_too_large from ingest_file
- Restore over-fetch headroom and rag fusion glob filter; close out e0d
- Establish e0e k1 contracts and scenarios; check off k1
- Add `applied_filters` to `searchresponse`; drop language single-collection caveat
- Add filters param to search_many; thread to all 4 hybrid_search_with_trace call sites; glob overfetch + per-leg post-filter
- Lift v1 filter+collections restriction; echo applied_filters; add be-3 tests
- Lift multi-collection language restriction; forward all filters to search_many()
- Update competitor analysis a-e; add phase g roadmap with 19 items
- Close-out t-3; document applied_filters and multi-collection filter support
- Fix 16 iterative-review findings across roadmap and analysis
- Move f3 graphrag and g3 entity graph to phase e
- Add missing completed items; fix e0 links; drop backlog originals
- Mark e0 complete; renumber phase e–g items; split effort matrix; remove roadmap.md
- Complete k1 kickoff; resolve q1-q11; fix typespec contracts
- Add graphconfig dataclass and [graph] optional extras (e1a/be-1)
- Add entities module for graphrag be-2 (e1a)
- Add graphstore with lancedb node/edge tables
- Implement graphextractor with spacy ner, c3 path, and co-occurrence edges
- Wire graphextractor + graphstore into ingest_file (be-5)
- Add graph sub-object to get /status (e1a fe-1)
- Implement graphexpander for graph_mode=naive query expansion (be-6)
- Add naive graph expansion to search and search_many
- Add graph_mode=naive to post /search; expose graph_expansion_applied in response
- Add graph_mode to mcp search tool; guard search_with_context
- Add graph_mrr report-only metric; wire stubgraphexpander (be-9)
- T-6 close-out — fix snake_case method names, dedup breaking entry, complete plan
- Iterative-review close-out — production-ready team plan
- K1 kickoff — verify e1a artefacts; correct plan spec gaps
- Add community entity and leiden graphconfig fields (e1b be-1)
- Add communities table and chunk accessor methods (e1b be-2)
- Add communitybuilder with leiden clustering and size splitting
- Implement mmr representative selection and llm summary stub (e1b be-3b)
- Add `graph build-communities` cli subcommand (e1b be-4)
- Add global community search mode; expose normalize_ingested_by
- Extend graph_mode to local|global; catch graphcommunitiesnotbuilterror
- Add graph_mode=local single-collection search path (e1b be-7a)
- Add be-7a observations (private symbol import, review cycle coverage)
- Add search_many() local-mode fanout (be-7b)
- Add community_count and last_built_at to statuscollectionentry (e1b be-8)
- Extend mcp search tool to accept graph_mode local and global (e1b be-9)
- Restore fastmcp stub after module-level install; update e1b docs (t-5)
- Add e1c contracts + brief + team plan; close k1 kickoff
- Add graph-provenance entity types and pydantic models (be-1)
- Extend /explain schema for graph provenance (be-2)
- Add graph_mode parameter to pipeline.explain() — null pass-through (be-3)
- Wire graph_mode through explain_endpoint; enforce hyde_applied=false invariant
- Verify openapi snapshot; fix hyde vector leak; add schema assertions (be-9)
- Add graph error guards to post /explain and mcp explain (be-5)
- Add graph_mode to mcp explain tool (e1c be-6)
- Implement be-7 naive graph traversal wiring in pipeline.explain()
- Wire community-mode (local/global) traversal in pipeline.explain(); extract _explain_merge_and_rank
- Close-out project — verify acceptance criteria, update arch docs, add changelog; move brief + plan to completed/
- Author c1–c5 typespec contracts and team plan for k1 kickoff; check off k1
- Add expires_at/scopes columns, e2a migrations, query_expiring_chunks; bump store_schema_version 0→1
- Add e2a ttl/scope fields to chunkrecord and collectionmeta
- E2a be-3 — ttl computation + scopes assignment in ingest
- E2a be-4 — rest ttl + expiring chunks endpoint
- Add be-4 tdd observations (page_count semantics, patch exclude_unset, ttl validator parity)
- Add chunk_ttl_seconds and chunk_scopes to ingest_file and ingest_directory tools
- Add e2a t-1 asyncio event-loop safety pattern for testclient tests
- Add prune_expired_chunks + count_expired_chunks; add maintenanceconfig.prune_expired_chunks flag
- Add _run_expired_chunk_pruning policy to maintenanceloop
- Add expired_chunk_count and last_expired_pruned_at to maintenancestatusdetail (e2a be-8)
- Add e2a t-2 observations on lancedb close() and fresh searchstore pattern
- Add scope predicate to build_where and hybrid_search_with_trace
- Add scope_filter to search, search_many, search_with_context, explain (be-10)
- Add scope_filter to searchrequest and explainrequest (be-11)
- Add scope_filter to search, search_with_context, explain tools; add scopes to documentinfoschema
- Close-out t-5 — doc accuracy fixes, runbook additions, openapi regen
- Deduplicate scope_filter validation into shared _validators.py
- Add e2b graph inspection brief; add e2c to roadmap
- Complete k1 — align contracts and scenarios
- Be-1 add graphmention dataclass and mentions field to graphextractionresult
- Add e2b graph inspection config fields (be-2)
- Add mentions table to graphstore (e2b be-3)
- Accumulate entity mentions in graphextractor
- Add mentions write hook to pipeline.ingest_file
- Implement graph_inspector use case for inspection api
- Implement get /graph endpoint for graph inspection (be-7)
- Implement graphml export for graph inspection
- Mark t-1 manual graphml smoke test as completed
- Implement get /graph/cross-collection cross-collection graph inspection
- Add mcp get_graph and get_graph_cross_collection tools
- Close-out documentation updates and openapi fix
- Add e2b t-2 close-out observation on fastapi response_model
- Add get_entity_presence_across_collections for tf-idf idf denominator
- Add tf-idf salience scoring to inspect_collection
- Add ?salience= param to get /graph/{collection}; fix namespace scoping
- Add tf-idf scoring to inspect_cross_collection; extract _apply_tfidf helper
- Add ?salience= to get /graph/cross-collection; fix namespace scoping
- T-3 close-out — update breaking.md, claude.md, architecture docs, and contract snapshot
- Add e2c/e2d briefs, seam contracts, api contracts; record learnings
- Mark k1 complete — contracts c1, c2, c3 agreed
- Namespace-scope graphstore table names and add charset validation (e2d be-1)
- Add e2d be-1 observations
- Check off be-2 and be-3 — work landed in be-1 commit
- Record be-1 no-bundle violation and prevention
- Add startup warning for legacy pre-e2d graph tables (be-1b)
- Wire graph mention cleanup into delete_document (e2d be-4)
- Add graphstore gc methods and gcpassresult entity
- Add graph garbage-collection configuration fields
- Add be-7 graph garbage collection policy with async community rebuild
- Fix _run_graph_gc return type and stale count aggregation
- Cap xdist at 4, one suite at a time; compact learnings.md
- Add graph gc status fields to status response (be-8)
- Display graph gc status (stale_mention_count and last_graph_gc_at) in status output
- Reduce duplication and clarify test logic
- Document e2d graph lifecycle hygiene feature in claude.md; close out t-5
- Commit k-1 plan artifacts; correct leidenalg seeding api finding
- Add 4 graph recall metrics to evalmetrics and floors
- Harden be-1 — add missing docstrings; tighten field-set test
- Wire 4 graph recall floors into runner constants and toml parser
- Compute graph_naive_recall_at_5 from multi-hop traces (be-4)
- Add seed parameter for deterministic leiden partitioning
- Implement be-6 — realcommunityevalbackend, dispatchingcommunitystore, realgraphexpander
- Add 2wikimultihopqa corpus with local/global graph mode queries
- Add error handling and fix build_communities_for_eval fixture
- Restore deleted alt-model queries/labels; bump ceiling to 55
- Address da follow-up findings; drop dead assert, fix whitespace
- Add lancedb_root param and local/global recall metrics
- Replace tautological unit test; harden integration guard
- Update learnings.md with be-11 eval gate test placement lesson
- Calibrate real graph recall gates with leiden-seeded communities
- Fix t-4 review findings; commit mandatory learnings update
- Finalize e2e graph eval gates close-out; add license-datasets and readme documentation
- Preserve baseline waivers in regenerate.py; refresh stale hash
- Record baseline waiver_ids bug and thresholds_hash sync rule in learnings
- Graduate d8, e1b, e2a–e2e briefs/plans/contracts to completed
- Mark e2b–e2e shipped in competitive matrix; add now column
- Replace anthropic key notes with new feature ideas and analyses
- Condense claude.md to rules-only; fix six stale claims
- Agree e2f contracts and scenarios (k1); fix 16 typespec defects
- Add synonym_of relationshiptype and extraction_method to graphedge; migrate pre-e2f edge tables
- Add name_embedding column and vector_search_nodes to graphstore
- Add graphconfig synonym-detection fields (be-3)
- Add graphstoreprotocol and synonymdetector (e2f be-4)
- Add post-ingest synonym enrichment hook and debounce (e2f be-5)
- Add aliasloader and wire into synonym enrichment orchestrator
- Add synonym health metrics to status api; add relationship_type to graph inspection
- Add synonym_bridge_recall_at_5 gate; synonym-bridge corpus; two gated integration tests (be-8)
- Clarify be-8 gate non-vacuity mechanism (post-review)
- E2f close-out — synonym edges doc updates and acceptance fact-check
- Add e2g code def/ref typed graph team plan and contracts
- Confirm k1 contracts/scenarios/open-questions record
- Verify swift/c# tree-sitter grammars install and parse clean
- Add def/ref relationshiptype members and document extraction_method
- Add defrefextractor for same-file code def/ref edges
- Wire defrefextractor into post-ingest pipeline hook
- Harden be-3 def/ref delete and re-ingest lifecycle
- Add be-4 cross-file inferred def/ref matching
- Add ast/cast chunker wired to shared scopetable
- Soft-degrade missing code parsers, wizard [code]+[graph] bundle
- Add be-7 pagerank scoring for code-symbol graphs
- Record be-7 pagerank learnings (merge_insert scoping, vacuous-async-test trap)
- Rewrite prune_expired_chunks delete predicate off f-string
- Add compute_impact bfs blast-radius traversal
- Record be-8 store.py scope-creep recurrence
- Record be-8 fix-agent scope-creep pattern
- Add graph_impact on rest and mcp for be-9
- Extend defrefextractor to 7 more languages (be-5)
- Add be-10 code-lane eval gate with real ast/defref wiring
- T-4 close-out — documentation update and acceptance fact-check
- Iterative-review of ppr retrieval mode team plan
- K-1 kickoff — confirm open-question resolutions and fix plan consistency
- Be-1 — add ppr_damping, ppr_top_entities, naive_max_expansion_terms to graphconfig
- Be-2 — widen graph_mode literal to include "ppr" and add ppr_entities_matched to all schemas
- Be-3 — add "ppr" dispatch stub to pipeline with hybrid fallback
- Be-4 — add get_mentions_for_entity_ids to graphstore
- Be-5 — implement pprwalker with personalized pagerank walk
- Be-6 — wire pprwalker into pipeline, blend prepend-then-rerank, propagate ppr_entities_matched
- Be-7 — add ppr provenance to /explain; add get_nodes_by_ids to graphstore
- Be-8 — cap naive expansion at naive_max_expansion_terms in graphexpander
- Be-9 — add graph_ppr_bridge/negative_control_recall_at_5 eval metrics
- Doc updates, ac fact-check, full suite
- Add e2h planning artifacts — brief, typespec contracts, ci gap note
- K1 — add e2i planning artifacts and check off kickoff task
- Be-0 — define llmenrichmentclientprotocol and anthropicenrichmentclient
- Be-1 — add entity_type to graphnodeinspection and graphnoderesponse
- Fe-1 — add self-contained graph_viewer.html with vis-network v9.1.9
- Be-2 — get /graph/{collection}/view route handler with ?token= auth
- Fix truncation-banner wording, add fetch-error annotation, check off
- Doc updates, .tsp contract fixes, ac fact-check
- Check off e2j and e2 parent in roadmap; update three doc references
- Add openai shim planning docs; update roadmap and learnings
- Strengthen k1 contracts — add streaming model, s18/s19 scenarios
- Add openaishimconfig for g9 openai-compatible shim (be-1)
- Be-2 — add openai shim pydantic schemas (modelobject, modellist, openaierror, openaierrorresponse)
- Be-3 — get /v1/models handler, openai401middleware, and app wiring
- Add chat completion pydantic schemas (be-4)
- Implement post /v1/chat/completions non-streaming path (be-5)
- Add requestvalidationerror handler for openai shim 422 rewriting (be-6)
- Add streaming sse branch to post /v1/chat/completions (be-7)
- Project close-out — update all documentation for openai shim (t-3)
- Mark g9 complete in roadmap
- K1 kickoff — agree contracts, scenarios, and open questions
- Define queryexpansionprovider protocol; extract anthropicqueryexpansionprovider
- Extend hydeconfig/ragfusionconfig with provider + ollama_base_url; add ollama extra and startup configerror guards
- Implement ollamaqueryexpansionprovider with never-raise contract
- Wire provider factory into generators; add rate-limit skip for ollama
- Add provider field to hyde/rag fusion status detail; provider-aware key_available
- Update install wizard with provider selection for hyde/rag fusion
- Implement openaiqueryexpansionprovider
- Wire openaiqueryexpansionprovider into generator factories
- Project close-out & acceptance fact-check
- Fall back to claude cli when anthropic_api_key is absent or invalid
- Register 'unit' pytest marker in pyproject.toml
- Add no_synthesis bypass and 60s cli timeout
- Use substantial pdf fixture for page-break-marker test
- Remove en_core_web_sm direct url dep from graph extra
- Download en_core_web_sm spacy model after graph extra install
- Iterative-review cycle 1 fixes
- Update world class roadmap


## [next] — E1c Graph-Path Provenance in /explain

**Graph-path provenance on `POST /explain`**
- `ExplainRequest` gains `graph_mode: "naive" | "local" | "global" | null` (default `null` — zero behaviour change for non-graph callers)
- `ExplainResponse` gains `graph_mode_applied: "naive" | "local" | "global" | null`
- `ExplainResult` gains `graph_provenance: { steps: [{ entity, entity_id, relationship?, community_id?, chunk_id? }] } | null`; non-graph results always carry `graph_provenance: null`
- 422 guard: `graph_mode` non-null and `[graph] enabled = false` → plain string detail `"graph_mode requires [graph] enabled=true in server config"` (no `code` field), matching the `/search` pattern
- 422 guard: `graph_mode = "local"` or `"global"` and communities not yet built → `code: "graph_communities_not_built"` (pipeline exception `GraphCommunitiesNotBuiltError` caught at route layer)
- 422 guard: `graph_mode` non-null with multi-collection fanout (`collections` list with > 1 entry) → 422; single-collection only for E1c
- MCP `explain` tool gains `graph_mode` parameter with the same semantics; result dict exposes `graph_mode_applied` and per-result `graph_provenance`
- New Pydantic entities `TraversalStep` and `GraphProvenance`; `TraversalStep` Pydantic validator rejects all-null optional fields (`relationship`, `community_id`, `chunk_id`)
- OpenAPI snapshot updated


## [next] — C2 Multilingual Retrieval

**Language Detection + Filter**
- Per-document language tagging via fasttext `lid.176.ftz` model: ingested documents receive an ISO 639-1/639-3 language code (e.g. `"fr"`, `"de"`) or `"unknown"` on all chunks when `config.multilingual=True`
- `language=<code>` filter on `POST /search` and MCP `search`/`search_with_context` tools: returns only chunks matching the specified language state; `language=unknown` returns below-threshold chunks; legacy chunks tagged `""` are excluded by explicit language filters
- Three-state language contract: `""` (pre-C2 legacy), `"unknown"` (processed, below threshold), `"<code>"` (detected); language filter is single-collection only (multi-collection fan-out rejects with 422)

**Install**
- `pip install archon-search[multilingual]` installs `fasttext-wheel` for language detection
- `archon-search install --multilingual` downloads `lid.176.ftz` (CC-BY-SA 3.0) to `~/.archon-search/models/`; `--accept-fasttext-license` for non-interactive installs
- Server startup with `multilingual=true` and missing package or model file raises a clear `RuntimeError` before accepting requests

**Configuration**
- New `language_detection_confidence_threshold: float = 0.7` config key (range `(0.0, 1.0]`) under `[database]` section
- See `archon-search.toml.example` for the commented example

**Telemetry**
- `FilterFlags.language_filter_used: bool` added to telemetry entries (no raw language value is stored — telemetry no-raw-query invariant preserved)

**Status**
- `GET /status` emits a per-collection warning when `multilingual=true` and the collection contains untagged (`language=""`) chunks; indicates re-ingest is required

**Breaking change**: `SearchResult.language`, `ScoredSearchCandidate.language`, `ExplainResult.language`, and `ExplainNearMiss.language` now return `""` instead of `None` for legacy/untagged chunks. See `BREAKING.md`.

**FTS tokenization (C2 Phase 12)**: language-aware FTS tokenizer support confirmed via spike. LanceDB `FTS(language="French")` API used when collection dominant language is a recognized code. `GROUP BY` SQL is not supported in LanceDB 0.30.2 — dominant language computed via Python-side `Counter` over fetched `language` column values.


## [26.6.710] - 2026-06-02

**fastembed 0.8.0 compatibility + wizard command**

**Installation & Setup**
- Fixed cross-encoder import fallback for fastembed 0.8.0, which moved `TextCrossEncoder` from top-level to `fastembed.rerank.cross_encoder`; pre-warm now succeeds regardless of fastembed version
- Added `archon-search wizard` as the recommended post-install entry point, consolidating shared CLI options into a reusable decorator and improving first-run UX


## [26.6.707] - 2026-06-02

**CI fix for CalVer tag parsing in initial release backfill**

- Fixed `git-cliff` OID parsing error when generating initial release notes — dotted CalVer version tags (e.g. `26.5.333`) are not valid git object IDs. Now uses git range syntax (`..${TAG}`) instead of bare tag names to correctly reference the tag as a range endpoint, allowing proper commit backfill for first releases.


## [26.6.705] - 2026-06-02

**Evaluation baseline recalibration and test suite fixes**

**Evaluation & Metrics Calibration**

- Fixed baseline calibration in `regenerate.py` to match pytest stub environment, eliminating inconsistency between local measurement and gated CI runs. The script now installs `_search_stubs` before calibrating, ensuring routing centroids are computed under the same chunking strategy (`_FakeRecursiveChunker` word-count splitter) as CI tests. Retrieval metrics improved to `recall_at_3: 0.9630`, `recall_at_5: 1.0`, `ndcg_at_5/10: 0.9879`; routing metrics floor values updated to reflect new measured baselines (`routing_accuracy: 0.9394`, `routing_mrr_centroid: 0.6667`).

**Test Suite & CI**

- Updated evaluation test files to reflect C1 pipeline/meta field renames: `pipeline._embedder` → `pipeline._global_embedder` and `CollectionMeta(embedding_model=...)` → `active_embedding_model=...`. Fixes `AttributeError`/`TypeError` failures in the `live_eval` suite.
- Excluded `live_eval` marker from default CI test suite. Live evaluation tests require real model weights (fastembed + cross-encoder) and are now gated to explicit `uv run pytest -m live_eval` invocation, preventing artifact download failures in standard CI runs.


## [26.6.701] - 2026-06-01

**OpenAPI schema alignment + CI release automation**

**OpenAPI & validation**
- OpenAPI snapshot regenerated on Python 3.12 to ensure HTTP error responses (e.g. `422 Unprocessable Entity` vs `Content`) match CI environment and published schema

**Release workflow**
- `make_latest` flag now automatically set to `true` for new releases and `false` for backfill patches, streamlining version promotion and preventing accidental latest-tag overwrites on patch deployments


## [26.6.698] - 2026-06-01

**Per-collection embedding models + reindex lifecycle + MCP tooling**

**Per-Collection Embedding Models**

- `POST /collections/` now accepts optional `embedding_model` field; when provided, validates it and stores as `active_embedding_model` in CollectionMeta. Omitting it falls back to the global config model, enabling mixed-model deployments where different collections use different fastembed models.
- `GET /collections/{name}` returns `active_embedding_model`, `pending_embedding_model`, `needs_reindex`, and `reindex_job_id` in CollectionDetail, replacing the old `embedding_model` field (breaking change documented in BREAKING.md).
- `GET /collections/` list endpoint now includes `active_embedding_model` and `needs_reindex` in each CollectionSummary.
- `PATCH /collections/{name}` endpoint implements a full 5-case state machine: validates the new model, checks dimension compatibility against stored vectors via O(1) `count_chunks()` and `get_stored_vector_dimension()`, guards against in-flight reindex jobs (409 on PENDING/RUNNING with auto-clear of stale IDs), and fast-paths empty collections without reindexing. Dimension mismatch returns 422.
- All search routes (`POST /search`, `GET /explain`) now dispatch via per-collection embedder: single-collection queries fetch `active_embedding_model` from CollectionMeta and resolve the embedder from EmbedderCache; multi-collection queries use the global config model. `SearchResponse` gains `embedding_model: str` field.
- Router automatically excludes results from collections whose `active_embedding_model` differs from the chosen collection's model, preventing cross-space vector contamination.

**Reindex Job Lifecycle & CLI**

- `POST /collections/{name}/reindex` creates a ReindexJob with `target_embedding_model` captured from the request, enforces the 409 guard for active jobs, and spawns `_reindex_task` instead of the default ingest task. On successful reindex, `pending_embedding_model` is promoted to `active_embedding_model`, and `needs_reindex` and `reindex_job_id` are cleared.
- CLI `archon-search sync` now resolves the per-collection embedder from `active_embedding_model` before ingesting files, falling back to the global embedder for legacy collections without CollectionMeta. Spurious reindex triggers are eliminated by reading the authoritative `active_embedding_model` from the database rather than comparing against config.
- `needs_reindex` field in `/status` response indicates which collections require reindexing after a model change, without affecting readiness counters (`collections_failed`, `collections_indexing`).

**EmbedderCache & Configuration**

- New `EmbedderCache` class provides async LRU caching of Embedder instances with concurrent-load deduplication: multiple concurrent requests for the same model are serialized via `asyncio.Event` so only one thread calls `make_embedder`, while others await the result. Failed loads clean up the event so waiters retry rather than deadlock.
- `embedder_cache_size` (default 3, min 1) and `eager_load_embedders` (default false) configuration keys added to SearchConfig. When `eager_load_embedders=true`, the cache preloads all distinct `active_embedding_model` values from the database at startup, improving first-request latency for multi-model deployments.
- EmbedderCache is wired into app.state during lifespan startup and passed to MCP factories, enabling all ingest/search/explain paths to resolve embedders on-demand.

**MCP Tooling**

- New `update_collection(collection_name, embedding_model)` MCP tool (#11) mirrors the PATCH endpoint state machine: validates the model, checks dimensions, enforces the 409 guard, promotes pending models on success, and handles stale job clearance. Uses namespace isolation via `ctx.meta.get("namespace")`.
- `search`, `search_with_context`, `explain`, `ingest_file`, and `ingest_directory` MCP tools updated to dispatch via per-collection embedder, falling back to `pipeline._global_embedder` when the cache is absent or the model is empty.
- Single-collection `explain` nullifies the pre-computed query vector when the chosen collection's embedder differs from the global one, preventing cross-space vector scoring.

**Testing & Documentation**

- Mixed-model eval fixtures added: alt-model collection with 2 corpus files and 2 documents, configurable `EvalEmbedderBackend.model_name` parameter, and new test `test_eval_exercises_per_collection_dispatch` verifying that distinct embedder models produce distinct `active_embedding_model` values in CollectionMeta.
- ADR-08 documents the EmbedderCache design decision, including OrderedDict LRU eviction, `asyncio.Lock` serialization, `asyncio.Event` concurrent-load deduplication, `asyncio.to_thread` for ONNX init, and eager-load opt-in.
- README config section corrected for key grouping (`[database]`, `[search]`, `[routing]`, `[collections]`); embedder_cache_size and eager_load_embedders added to `[database]`; MCP tool count updated to 11 and `update_collection` listed.
- UserManual section 04 adds per-collection model configuration and reindex lifecycle guide.
- Baseline eval metrics refreshed for 57 docs / 33 queries reflecting C1 corpus expansion.


## [26.5.654] - 2026-05-31

- Dynamic git-cliff download url — static url lacks version number


## [26.5.652] - 2026-05-31

**Flat release notes — bold title from first feat, no section headings**

- Rename briefs and plans and move the completed items to completed/ folder
- Flat release notes — bold title from first feat, no section headings
- Backfill all release notes on each release cut


## [26.5.647] - 2026-05-31

### Bug Fixes
- allow fractional seconds in captured_at regex


## [26.5.645] - 2026-05-31

### Bug Fixes
- full-suite regressions surfaced by uv run pytest
- patch DocumentChunker + drop invalid .json() assertions
- comprehensive review-and-fix pass — JSON-envelope myth + test gaps + Review/ docs
- drop "silent empty results" rationale from success_rate alert
- patch DocumentChunker.__init__ in contract test to avoid chonkie gpt2 download
- drop exc_info on TimeoutError log — traceback is asyncio internals noise
- refresh OpenAPI snapshot for explain EndpointKind enum (Task 1.1 follow-up)
- sanitize /explain error details + strengthen route tests (Task 3.1 review)
- don't echo input value in MCP explain validation error (final review)
- address Task 1.1 review findings (tests, comments, plan consistency)
- address review findings (gate comment, test strengthening, multiline guard)
- resolve ingest-503 regressions (pipeline kwarg threading, mock locks, timeout source, test isolation)
- 503 leaves no orphaned state; add per-collection + orphan tests
- harden cancel-path OSError in background ingest task
- correct default reranker model name to Xenova/ms-marco-MiniLM-L-6-v2
- add ge=0 constraints to readiness count fields in JobCounts and ReadinessDetail (B2 Task 4.1 follow-up)
- add is_warm to mock reranker backends; update B3/B5 plan docs
- refresh baseline eval_hash after B2 is_warm backends change
- reconcile hybrid_search retrieval tie-break to (-score, chunk_id) (B3 Task 2.3)
- resolve FastMCP stub pollution across test collection order


### Documentation
- present the package as a standalone repo, drop parent-project framing
- correct MCP tool list and export_enabled behavior
- add CLAUDE.md, contributing guide, and Documentation/ tree
- integrate competitive analysis systems
- add A1-A7 briefs and plans, cross-checked for consistency
- record types.py vs _types.py import-path audit (Task 1.1)
- eval-harness fixture sweep audit (Task 7.2)
- partition-map docstrings on ChunkRecord + persistence pointer (Task 8.1)
- BREAKING.md entry + roadmap amendment (Task 8.2)
- documentation audit + acceptance verification close-out (Task 8.3)
- bundle A1's deferred 503 sync-surface fix as A5c
- demote 503-surface line from acceptance checkbox to A5c-pointer note
- A4 -> A3 cross-reference fixes; drop Review/ tree
- final verification and documentation update
- add observability + stage-level latency feature brief
- incorporate multi-agent review fixes into observability brief
- second-round review fixes (ingest seam, route scope, fts, MCP id)
- scope ingest instrumentation to ingest_file (leaf convergence)
- align AC#7 with the instrument-in-leaf / emit-at-entry-point seam
- feature brief for deeper health and readiness (item 22)
- add server-side multi-collection search primitive brief
- add stronger-collection-routing feature brief
- address review findings on routing brief
- clarify eval-gate floor mechanics
- update docs for /search 500/504 behavior
- fix 503 table row — remove /route, add A3 pipeline 500/504 rows
- fix review-round-2 issues in 140, 530, and runbook
- fix stale line ref in collection-not-found 404 row
- mark A3 complete in backlog, fix 990 runbook description
- sweep all remaining stale CON-5/silent-failure refs
- update Review/UserManual_07 for post-A3 behavior
- correct stale JSON-envelope claims in brief and plan
- document /explain endpoint, explain MCP tool, and internals (Task 5.1)
- propagate explain tool/endpoint to all stale tool-count and route docs
- mark Task 1.1 complete
- BREAKING.md entry for path-safety behaviour change (Task 1.6)
- mark Task 1.7 complete — Phase 1 wiring sanity verified
- mark Phase 2 tasks complete (Tasks 2.0-2.4)
- mark Tasks 2c.1 and 2c.2 complete
- record MCP store_busy surface (Task 2c.3)
- mark Task 2c.3 complete — Phase 2c done
- security-doc + roadmap consolidation (Tasks 3.1, 3.2)
- documentation sweep for ingest hardening (Task 3.3)
- mark Task 3.3 complete — plan A5 fully shipped
- document durability contract + OSError->500 mapping (Phase 4)
- mark all plan tasks complete
- fix ARCHON_SEARCH_KEY_FILE line citation (key_manager.py:14 -> :16)
- add tiered install profiles brief, plan, and AGENTS.md; ignore .claude/.serena
- move completed A-series items (A1–A7) from Backlog/ to Completed/
- remove Documentation/Review/ working copies
- mark Task 3.3 complete in B1 plan
- update architecture docs and toml.example for B1 observability surface (Task 6.1)
- move completed B1 observability plan and brief to Completed/
- add B2–B5 backlog plans and update B5 incremental-centroid brief
- final verification and documentation update (B2 Task 7.1)
- move completed B2 and B3 plans/briefs from Backlog to Completed
- apply iterative-review fixes to plan and brief
- final documentation update for B4 hybrid routing (Task 7.1)
- move completed B4 plan to Completed/ and update B5 spec notes
- documentation update for incremental centroid maintenance
- add live eval lane documentation across README and architecture docs
- document format and backup_count in archon-search.toml.example
- update docs for structured logging and log rotation
- update all docs for tiered install profiles feature
- add git-cliff prerequisite to CLAUDE.md and contributing.md
- final verification and documentation update for C1
- move B6 to completed, add B7 and github-releases backlog items


### Features
- pin custom_score nullability explicitly + round-trip tests (Task 2.1)
- add IngestedBy Literal + INGESTED_BY_VALUES constant (Task 3.1)
- extend DocumentChunker.chunk() signature (Task 3.2)
- wire ingested_by through call sites + X-Ingested-By normalizer (Task 3.3)
- remove silent legacy ingested_by write coercion (Task 3.4)
- grow SearchResult with 5 new metadata fields (Task 4.1)
- mirror SearchResult fields on SearchResultSchema + fix acl drift (Task 4.2)
- populate metadata fields on hybrid_search results (Task 4.3)
- strip vector from MCP search_with_context neighbors (Task 5.1)
- per-collection ingest lock + StoreBusyError + 30s timeout (Task 6.1)
- SearchStore.reindex_metadata + ReindexResult (Task 6.2)
- archon-search collection reindex-metadata CLI (Task 6.3)
- add language field to SearchResult dataclass
- populate language in hybrid_search row projection
- surface language in SearchResultSchema + MCP; add include_metadata suppression
- SearchFilters Pydantic model with validation and date coercion
- normalize_iso_utc helper in _types.py
- predicate builder, escape helpers, fetch helper in store_filters.py
- hybrid_search accepts SearchFilters; .where() on both branches
- fnmatch glob post-RRF filter + legacy indexed_at warning
- pipeline filter forwarding + filter+ACL attrition WARNING
- REST SearchRequest embeds SearchFilters
- MCP search/search_with_context accept filter kwargs
- typed FilterFlags submodel for privacy-safe telemetry
- reindex-metadata --normalize-timestamps backfill flag
- propagate /search pipeline exceptions as HTTP 500
- add 504 timeout guard to /search handler
- telemetry explain endpoint kind + from_explain_result factory (Task 1.1)
- ScoredSearchCandidate.acl + SearchStore.hybrid_search_with_trace delegate (Task 2.1)
- thread A1 metadata fields onto trace candidates (Task 2.2.5)
- router _score_collections refactor + rank_with_scores (Task 2.2)
- SearchPipeline.explain diagnostic method (Task 2.3)
- public /explain wire schemas (Task 1.2)
- POST /explain route handler (Task 3.1)
- explain MCP tool (Task 4.1)
- validate_ingest_path + PathUnsafeError (Task 1.1)
- wire validate_ingest_path into POST /collections (Task 1.2)
- wire validate_ingest_path into POST /ingest (Task 1.3)
- wire validate_ingest_path into MCP ingest_file (Task 1.4)
- wire validate_ingest_path into MCP ingest_directory (Task 1.5)
- add _sql_quote_str SQL literal-quoting helper (A5b dependency)
- add _where_eq/_where_in SQL fragment helpers (Task 2.2)
- CI guard preventing f-string SQL in store.py (Task 2.4)
- ingest_chunks skips lock when _locked_by_caller (Task 2c.1)
- pre-acquire per-collection lock -> 503 on ingest endpoints (Task 2c.2)
- MCP ingest tools surface StoreBusyError as code=store_busy (Task 2c.3)
- durable-write helper, ADR-06, CI gates (Phase 1)
- migrate 5 durable-write sites + crash-injection tests (Phase 2a)
- route OSError->500 handling + durable-write lint gate (Phase 2b)
- telemetry persistent-fd with rotate-only fsync (Phase 3, Tasks 3.1-3.2)
- add StageRecorder, record_stage, bind_stage_recorder, correlation_id (B1 Task 1.1)
- add ObservabilityConfig and [observability] TOML section (B1 Task 1.2)
- add pure-ASGI RequestContextMiddleware for X-Request-ID propagation (B1 Task 2.1)
- register RequestContextMiddleware on FastAPI and MCP apps (B1 Task 2.2)
- instrument Embedder.embed with record_stage("embed") (B1 Task 3.1)
- instrument Reranker.rerank and _rerank_with_trace with record_stage("rerank") (B1 Task 3.2)
- instrument hybrid_search and _hybrid_search_with_trace with record_stage (B1 Task 3.3)
- instrument _score_collections with record_stage("route") (B1 Task 3.4)
- instrument search_with_context and ingest_file with record_stage (B1 Task 3.5)
- add correlation_id field to TelemetryEntry and all four factories (B1 Task 4.1)
- thread correlation_id into all telemetry enqueue sites (B1 Task 4.2)
- add stage_timings_ms to ExplainResponse and wire bind_stage_recorder (B1 Task 4.3)
- add stage_timings structured-log emission in /search and /route handlers (B1 Task 5.1)
- emit stage_timings log records in MCP ingest tools and CLI ingest paths (B1 Task 5.2)
- emit stage_timings log records in MCP search and search_with_context tools (B1 Task 5.3)
- add PING_TIMEOUT_SECONDS and PING_TTL_SECONDS constants (B2 Task 1.1)
- add SearchStore.ping() with TTL cache (B2 Task 1.2)
- add is_warm property to EmbedderBackend, ModelEmbedder, Embedder (B2 Task 2.1)
- add is_warm property to RerankerBackend, ModelReranker, Reranker (B2 Task 2.2)
- add reranker_is_warm and embedder_is_warm to SearchPipeline (B2 Task 2.3)
- add JobStore.count_by_status() with zero-filled dict (B2 Task 3.1)
- add readiness schemas — CheckStatus, ReadinessResponse, ReadinessDetail, JobCounts, WatcherReport (B2 Task 4.1)
- add /ready to _EXEMPT_PATHS in middleware_auth (B2 Task 5.1)
- add GET /ready endpoint and app.state.watcher_manager slot (B2 Task 5.2)
- add readiness sub-object to GET /status (B2 Task 6.1)
- add collection provenance field and excluded_collections envelope (B3 Task 1.1)
- add fan-out execution config keys and wire into SearchPipeline (B3 Task 3.1)
- add SearchPipeline.search_many multi-collection fan-out (B3 Task 3.2)
- accept collections list on POST /search for multi-collection fan-out (B3 Task 4.1)
- add collections parameter to MCP search tool for multi-collection fan-out (B3 Task 5.1)
- extend explain for multi-collection fan-out with provenance (B3 Task 6.1)
- record fanout count for multi-collection search (B3 Task 7.1)
- add ranked_collections field to QueryEvalTrace (B4 Task 1.1)
- add routing MRR and P@1 metrics to EvalMetrics (B4 Task 1.2)
- expand routing fixtures with faq collection (B4 Task 1.3)
- wire ranked_collections into runner and compute centroid routing MRR (B4 Task 1.4)
- regenerate baseline with centroid routing MRR and add thresholds floor (B4 Task 1.5)
- add description_embedding field to CollectionMeta (B4 Task 2.1)
- add description_embedding_json column and persistence (B4 Task 2.2)
- wire migrate_description_embedding into startup sequence (B4 Task 2.3)
- embed description at ingest and store on CollectionMeta (B4 Task 3.1)
- embed description in recompute_collection_meta (B4 Task 3.2)
- add description_embedding to _ROUTING_FIELDS and fetch_metadata opt-in (B4 Task 4.1)
- implement strategy+description_weight hybrid blending (B4 Task 4.2)
- add routing_strategy and routing_description_weight config knobs (B4 Task 5.1)
- wire routing_strategy/description_weight into router and strip description_embedding from MCP (B4 Task 5.2)
- wire hybrid router pass into run_eval_suite (B4 Task 6.1)
- regenerate baseline with hybrid routing metrics and set floors (B4 Task 6.2)
- add centroid_sum, mutations_since_recompute, needs_recompute to CollectionMeta
- add centroid_sum_json, mutations_since_recompute, needs_recompute to _meta_schema and round-trip
- add migrate_centroid_sum() with per-column resumable guards and startup wiring
- make update_collection_meta lock-acquiring
- add _do_read_meta_unlocked and _do_write_meta_unlocked helpers
- add _do_fetch_doc_vectors_unlocked helper
- add CI guard for _do_*_unlocked call safety
- add elementwise_sum pure helper to store.py
- add _do_update_meta_on_add helper and centroid config
- wire _do_update_meta_on_add into ingest_chunks
- surface needs_recompute via ChunkIngestResult return type
- add _do_subtract_meta_on_delete helper
- vector-aware delete_document with lock and StoreBusyError handling
- add store.update_description partial-write method
- refactor ingest_directory with update_description and needs_recompute wiring
- flip centroid_incremental_enabled default to True
- extend recompute_collection_meta with centroid_sum, force, and short-circuit
- remove recompute_collection_meta from watcher-sync hot path
- live_eval marker + directory scaffolding
- parameterize _build_pipeline_with_eval_backends + run_eval_suite backend
- live/conftest.py — autouse shadow + session fixtures
- live eval suite smoke test (live_eval marker)
- extend EvalBaseline with 6 optional model-version fields
- load_live_thresholds() in archon_search/eval/live_report.py
- MetricVerdict + LiveEvalReport + build_live_report()
- write_live_report_json() + write_live_report_markdown()
- extend live smoke test to write JSON + MD report artifacts
- live-backend acceptance tests 1, 3, 8, 9, 10
- add archon-search-eval-live.yml CI workflow
- extend SearchConfig and load_config() for [logging] keys
- add CorrelationIdFilter to logging_setup.py
- implement configure_logging() with file handler and JSON support
- wire configure_logging() as first call in run_server()
- normalise all getLogger() calls to __name__ + add CI guard
- add InstallProfile registry with English and multilingual profiles
- add profile and multilingual fields to SearchConfig
- add _write_profile_config, _profile_toml; fix configure_providers durable write
- make reranker optional in SearchPipeline for no-reranker profiles
- add advisory install lock with PID-based stale detection
- add _check_disk_space() pre-flight check and InstallError
- add _prewarm_models() with threading.Timer timeout and lazy download
- add reinstall guard with NeedsForceDeleteError
- add _execute_force_reinstall with 5-step rollback sequence
- add _render_profile_table and _render_summary display helpers
- add Jina CC-BY-NC-4.0 license gate
- add _select_profile with interactive prompt and validation
- rewrite SearchInstaller.run() with full profile-aware install flow
- consolidate install_cmd.py into thin Click shim
- add cliff.toml with CalVer pattern and TDD tests
- add CHANGELOG.md stub, .gitattributes, and awk extraction tests
- add git-cliff >= 2.4 pre-flight check to release.sh
- provisional tag computation (count+1) with count verification
- CHANGELOG.md shell-prepend, commit, and push in release.sh
- updated --dry-run output with tag, cliff notes, and curl preview
- add github-release CI job to archon-search-release.yml


### Refactoring
- Task 1.2 review polish (import order, test assertions)
- Task 1.4 review polish (hoist message map, sharpen test)
- hoist mid-file test imports to module top (Task 1.5 review)
- replace 5 f-string SQL sites with _where_eq/_where_in (Task 2.3)
- promote _rerank_with_trace to public rerank_candidates (B3 Task 2.1)
- migrate explain and eval trace to rerank_candidates (B3 Task 2.2)

All notable changes to archon-search are recorded here.
Prior release history is available via `git log`.
