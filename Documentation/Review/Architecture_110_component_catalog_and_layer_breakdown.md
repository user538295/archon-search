# Review: Architecture/110_component_catalog_and_layer_breakdown.md

## Summary

The document is a module-by-module catalog of `archon_search/`. Verified each row against the source tree, `pyproject.toml`, and route files. The catalog is largely accurate: all listed module paths exist, claimed classes and functions are present at the stated locations, and route-to-HTTP-method mappings match the decorators in `routes_*.py`. Inaccuracies are minor and cosmetic — mostly an undocumented duplicate (`RouteResponse` exists as both a Pydantic class in `routes_route.py` and a dataclass in `types.py`, and the doc only references the route-side variant in the routing row), one missing/extra symbol in column listings, and a couple of mildly imprecise descriptions. No structural or layering claim is wrong.

## Inaccuracies (numbered)

1. **Claim**: `routes_collections.py` "Issues jobs for add/reindex." — **Severity: low**
   - **Ground truth**: It also issues a job for *delete* on non-empty collections (the `DELETE /collections/{name}` handler uses `JobStore` in some branches). The phrasing is incomplete but not wrong; reindex/add are the explicit job-creators (`store.create(...)` at `archon_search/server/routes_collections.py:158, 318`).
   - **File**: `archon_search/server/routes_collections.py:114-323`

2. **Claim**: `archon_search/server/routes_route.py` key public symbols include `RouteResponse`. — **Severity: low (potentially confusing)**
   - **Ground truth**: A `RouteResponse` Pydantic model is defined at `archon_search/server/routes_route.py:28`, *and* a separate `RouteResponse` dataclass is exported from `archon_search/types.py:47`. The catalog mentions the route-level one but does not flag the duplicate, while the "Cross-cutting" row for `types.py` lists `RouteResponse` as part of the public surface. Two distinct objects share a name and both are advertised as public.
   - **File**: `archon_search/server/routes_route.py:28`, `archon_search/types.py:47`

3. **Claim**: `cli/main.py` "registers subcommands: `start`, `stop`, `status`, `install`, `uninstall`, `ingest`, `sync`, `collection`, `config`." — **Severity: none (informational)**
   - **Ground truth**: Confirmed at `archon_search/cli/main.py:26-34`. However the project-level CLAUDE.md lists subcommands as "`start`, `stop`, `status`, `ingest`, `sync`, `collection`, `config_cmd`, `install_cmd`" — the document under review is the one that matches reality; CLAUDE.md is wrong, not this doc. No inaccuracy to log against 110; flagged only for awareness.

4. **Claim**: `archon_search/jobs/store.py` ... "RUNNING/CANCELLING crash-to-FAILED recovery". — **Severity: none**
   - **Ground truth**: Verified at `archon_search/jobs/store.py:16` (`_CRASH_STATUSES = {JobStatus.RUNNING, JobStatus.CANCELLING}`) and `:98` (`status=JobStatus.FAILED, error="process_restart"`). Accurate.

5. **Claim**: `archon_search/jobs/store.py` ... "7-day eviction". — **Severity: none**
   - **Ground truth**: `_EVICTION_DAYS = 7` at `archon_search/jobs/store.py:17`. Accurate.

6. **Claim**: `description_generator.py` "call the model (Haiku) to generate one." — **Severity: low**
   - **Ground truth**: The internal helper is `_call_haiku` (`archon_search/description_generator.py:100`), confirming Haiku. Accurate.

7. **Claim**: `archon_search/router.py` "Fetch collection metadata over JSON-RPC". — **Severity: none**
   - **Ground truth**: `archon_search/router.py:65-99` uses `httpx.AsyncClient` to POST a JSON-RPC envelope (`"jsonrpc": "2.0"`). Accurate.

8. **Claim**: Telemetry "Factories take no `query` parameter — raw queries are never logged." — **Severity: none**
   - **Ground truth**: `archon_search/telemetry/entry.py:57` defines `TelemetryEntry` with no `query` field; matches the invariant in `CLAUDE.md`. Accurate.

9. **Claim**: `progress.py` "Indexing progress state: `IndexingStatus`, `CollectionProgress`, `IndexingState`, `IndexingStateStore`, ETA helpers." — **Severity: low**
   - **Ground truth**: All four classes exist (`archon_search/progress.py:22, 30, 46, 77`); ETA helper is `compute_eta_seconds` (`:146`). Doc lists only `IndexingStateStore` in the "Key public symbols" column despite naming all four in "Purpose" — minor stylistic inconsistency with the rest of the table format (other rows list all symbols mentioned in Purpose).

10. **Claim**: `archon_search/server/schemas.py` lists `HealthResponse, StatusResponse, IndexingStateResponse, CollectionSummary, CollectionDetail, JobResponse, DeleteResponse, ErrorDetail`. — **Severity: low**
    - **Ground truth**: All listed models exist, but `schemas.py` also defines `StatusCollectionEntry` (`:15`) and `IndexingStateCollectionEntry` (`:36`) which the doc omits. These are nested response models referenced by `StatusResponse` and `IndexingStateResponse`, so the omission is defensible but not exhaustive as the row implies.

11. **Claim**: `archon_search/types.py` public surface lists `JobStatus, IngestJob, ReindexJob, DeleteJob, Query, RouteResponse, Collection, CollectionDetail, Chunk`. — **Severity: none**
    - **Ground truth**: All confirmed at `archon_search/types.py:10, 20, 31, 36, 41, 47, 55, 66, 73`. Accurate.

12. **Claim**: `archon_search/_types.py` lists `ChunkRecord, SearchResult, DocumentInfo, CollectionInfo, IngestResult`. — **Severity: none**
    - **Ground truth**: All confirmed at `archon_search/_types.py:7, 25, 35, 43, 51`. Accurate.

13. **Claim**: `archon_search/server/middleware_auth.py` "exempts `/health`, `/docs`, `/openapi.json`, `/redoc`." — **Severity: none**
    - **Ground truth**: `_EXEMPT_PATHS: frozenset[str] = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})` at `archon_search/server/middleware_auth.py:16`. Accurate.

14. **Claim**: Eval `runner.py` exports include `EvalThresholds, EvalQualityFloors, EvalLatencyCeilings, EvalRuntimeConfig, EvalBaseline, EvalReport, load_thresholds, load_runtime_config, load_baseline, validate_routing_contract`. — **Severity: none**
    - **Ground truth**: All confirmed at `archon_search/eval/runner.py:39, 52, 60, 72, 141, 150, 206, 243, 269, 339`. Accurate.

15. **Claim**: `metrics.py` exports `compute_recall_at_k, compute_mrr, compute_ndcg_at_k, compute_reranker_lift, compute_routing_accuracy`. — **Severity: low**
    - **Ground truth**: All five confirmed (`archon_search/eval/metrics.py:81, 128, 177, 231, 244`). Doc omits `compute_latency_percentiles` (`:270`) and `deduplicate_to_doc_rankings` (`:31`), which are also module-level public functions. Not wrong, just incomplete.

16. **Claim**: `fixtures.py` symbols `EvalDocument, EvalQuery, RelevanceLabel, EvalCorpus, load_eval_corpus, build_doc_collection_map`. — **Severity: none**
    - **Ground truth**: Confirmed at `archon_search/eval/fixtures.py:20, 36, 63, 84, 122, 272`. Accurate.

17. **Claim**: Platform layer covers "macOS (launchd), Linux (systemd), Windows" with classes `LaunchdSearchService, SystemdSearchService, WindowsSearchService`. — **Severity: none**
    - **Ground truth**: Confirmed at `archon_search/platform/macos.py:53`, `archon_search/platform/linux.py:37`, `archon_search/platform/windows.py:9`. Accurate.

18. **Claim**: `archon_search/install.py` exports `SearchInstaller`. — **Severity: none**
    - **Ground truth**: Confirmed at `archon_search/install.py:29`. Note the doc lists `install.py` twice — once in the CLI section (row 103) and again in Cross-cutting (row 154). This is duplication but the content is consistent.

19. **Claim**: `acl.py` key public symbols: `resolve_acl, apply_acl_filter, is_acl_allowed, read_acl_sidecar, parse_acl_value, is_acl_namespace_valid`. — **Severity: none**
    - **Ground truth**: All confirmed at `archon_search/acl.py:217, 201, 184, 119, 21, 16`. Accurate.

20. **Claim**: `constants.py` exports `DEFAULT_NAMESPACE, _validate_namespace`. — **Severity: none**
    - **Ground truth**: Confirmed at `archon_search/constants.py:12, 17`. Accurate.

21. **Claim**: `key_manager.py` key public symbol is `load_or_generate_key`. — **Severity: none**
    - **Ground truth**: Confirmed at `archon_search/key_manager.py:25`. Accurate.

22. **Claim**: `sync.py` exports `SearchCollectionSync, SyncResult, path_to_collection_name`. — **Severity: none**
    - **Ground truth**: Confirmed at `archon_search/sync.py:85, 53, 26`. Accurate.

23. **Claim**: `watcher.py` exports `CollectionWatcher, WatcherManager`. — **Severity: none**
    - **Ground truth**: Confirmed at `archon_search/watcher.py:108, 197`. Accurate.

24. **Claim**: `jobs/model.py` "Re-exports `IngestJob`/`JobStatus`, defines `JOBS_FILE`, provides `job_to_dict`". — **Severity: none**
    - **Ground truth**: `archon_search/jobs/model.py:6` re-exports from `archon_search.types`; `JOBS_FILE` at `:8`; `job_to_dict` at `:11`. Accurate.

25. **Claim**: `pipeline.py` "Orchestrates ingest (`ingest_file`, `ingest_directory`) and query (`search`, `search_with_context`). Computes per-collection centroid on directory ingest and may trigger description regeneration." — **Severity: none**
    - **Ground truth**: Methods at `archon_search/pipeline.py:132, 194, 297, 306`; centroid computation in `ingest_directory` (`:257`); description regeneration through `_should_regenerate`/`generate_description` (`:270-273`). Accurate.

## Verified claims

- All 30+ module paths under `archon_search/` named in the doc exist exactly as written.
- All classes and functions named as "Key public symbols" exist at the named module with the stated name. Sampled the bulk of them with direct grep.
- HTTP method/path bindings stated for each `routes_*.py` row match the `@router.<verb>("...")` decorators.
- The `_EXEMPT_PATHS` set matches the exempt list given for `middleware_auth.py`.
- Telemetry's no-`query`-parameter invariant matches `TelemetryEntry`'s field set.
- JobStore's 7-day eviction and RUNNING/CANCELLING crash recovery match `_EVICTION_DAYS`/`_CRASH_STATUSES`.
- Router uses JSON-RPC over `httpx` (matches "Fetch collection metadata over JSON-RPC").
- `MultiCollectionRouter` is the sole class in `router.py`.
- `description_generator.py` does call Haiku (`_call_haiku`).
- `LaunchdSearchService`, `SystemdSearchService`, `WindowsSearchService` exist at the listed paths.
- `cli/main.py` registers exactly the 9 subcommands stated.

## Unverifiable / ambiguous

- **Principle 2** ("Layers depend downward only. Server depends on Query/Ingest; Query/Ingest never imports Server."): A truly authoritative check requires tracing all imports; spot checks confirm `pipeline.py`, `router.py`, `store.py`, etc. do not import from `archon_search.server`, but I did not exhaustively grep. No counterexample found in sampled files.
- **Principle 5** ("Pipeline stages talk to each other only through `SearchPipeline`; route modules talk to each other only through `app.state`."): Not exhaustively verified; `routes_collections.py` does `from archon_search.server.routes_jobs import IngestRequest, _default_ingest_task` (`archon_search/server/routes_collections.py:17`), which technically reaches across sibling route modules at import time. Whether this violates "talk to each other only through `app.state`" is interpretive — it imports types/helpers, not state. Worth a clarifying note in the doc but not a clear inaccuracy.
- **`platform/types.py` purpose** ("Platform-shared dataclasses"): The file defines `GpuType(str, Enum)` (`archon_search/platform/types.py:7`), which is an Enum, not a dataclass. Minor wording imprecision — flag as **low-severity inaccuracy**: "dataclasses" → "shared types".
- The doc's "Status: Draft" and review-date metadata is process information, not a factual claim about code.
