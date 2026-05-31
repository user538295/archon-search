# Changelog


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
