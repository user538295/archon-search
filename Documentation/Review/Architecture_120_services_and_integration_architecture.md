# Review: Architecture/120_services_and_integration_architecture.md

## Summary

The document gives a broadly correct picture of the two transports, shared auth, watcher, and job store, but contains several concrete inaccuracies, the most important of which are:

1. The FastMCP app is NOT mounted alongside the FastAPI app in the running server. `archon_search.server.app.run_server()` only starts the FastAPI app under uvicorn; `create_mcp_http_app()` is defined but only exercised by tests.
2. The watcher-triggered reindex flow does NOT create or touch a `JobStore` job. `SearchCollectionSync.sync_collection` writes to the indexing-state store only.
3. Job statuses are `PENDING / RUNNING / DONE / FAILED / CANCELLING / CANCELLED` — there is no `SUCCEEDED`.
4. `IndexingStatus` values are `pending / in_progress / done / failed` — there is no `INDEXING` or `READY`.
5. `_EXEMPT_PATHS` is set-membership against `request.url.path`; `/docs`, `/openapi.json`, `/redoc` are listed but FastAPI never serves them (no `docs_url`/`redoc_url` configured — the FastAPI default URLs exist by default, but the openapi customizer's own comment calls them "defensive").
6. The MCP HTTP wrapper passes an empty `namespaces={}` dict to `APIKeyMiddleware`, so MCP cannot resolve per-namespace keys at all — only the default key works. The doc's "same `APIKeyMiddleware` instance" wording is misleading: a separate instance is used and it is configured without namespaces.

## Inaccuracies (numbered: quoted claim, ground truth, file:line, severity)

1. Claim: "FastAPI HTTP and FastMCP HTTP ... Both transports share a single authentication boundary".
   Ground truth: Two separate `APIKeyMiddleware` instances are created (one per app). The FastAPI instance receives `config.namespaces`; the MCP instance receives `namespaces={}`. They are not a single instance and they do not share namespace configuration.
   Source: `archon_search/server/app.py:121`, `archon_search/server/mcp.py:251`.
   Severity: medium (semantically misleading; affects mental model of multi-tenant auth).

2. Claim: "MCP endpoint ... mounted alongside the FastAPI app and protected by the same `APIKeyMiddleware` instance."
   Ground truth: Nothing mounts the FastMCP Starlette app onto the FastAPI app. `run_server()` only runs `create_app(...)` (FastAPI) under uvicorn. `create_mcp_http_app` is referenced only from tests.
   Source: `archon_search/server/app.py:152-156`; only reference outside `mcp.py` is `tests/server/test_mcp_auth.py:38`.
   Severity: high (the document presents a deployment topology that does not exist in the running server).

3. Claim: 'Only `/health`, `/docs`, `/openapi.json`, `/redoc` are exempt.'
   Ground truth: Literal contents of `_EXEMPT_PATHS` match. However, `app.py`'s own comment notes that "only /health is a real schema exemption ... /docs, /openapi.json, /redoc are defensive — FastAPI never includes them in the schema." Worth noting; not strictly wrong but easy to misread.
   Source: `archon_search/server/middleware_auth.py:16`, `archon_search/server/app.py:65-66`.
   Severity: low.

4. Claim (table row): "Status — `server/routes_status.py` — `GET /status`".
   Ground truth: Verified. Status endpoint exists at `/status`.
   Source: `archon_search/server/routes_status.py:22`.
   Severity: none (accurate).

5. Claim (table row): "Collections — `POST /collections/{name}/reindex` (202)".
   Ground truth: Verified.
   Source: `archon_search/server/routes_collections.py:299`.
   Severity: none.

6. Claim: "Jobs / Ingest ... `DELETE /jobs/{job_id}`".
   Ground truth: Verified; status returned is 200 for terminal, 202 for cancellation initiated.
   Source: `archon_search/server/routes_jobs.py:119-157`.
   Severity: none.

7. Claim: "Error envelopes use `schemas.ErrorDetail`. 401 returns `WWW-Authenticate: Bearer` from the middleware."
   Ground truth: Verified.
   Source: `archon_search/server/middleware_auth.py:32-35,50-53`.
   Severity: none.

8. Claim: "Tools registered in `server/mcp.py` (verified against source)" — list of 9 tools.
   Ground truth: Verified, all 9 names and pipeline methods match.
   Source: `archon_search/server/mcp.py:38-228`.
   Severity: none.

9. Claim: "get_collections_meta ... Used by `MultiCollectionRouter.fetch_metadata`."
   Ground truth: Out of scope of file 120 but easily checkable; the pipeline method `get_all_collections_meta` is the source — the doc note is plausible but unverified here. Not central to this file's correctness.
   Severity: low / informational.

10. Claim: "stream telemetry through the same `TelemetryWriter`."
    Ground truth: The MCP `create_app` accepts a `writer: TelemetryWriter | None`. Whether it is "the same" writer depends on plumbing that does not exist in the running path (no production caller wires it). In the tests/auth wrapper `create_mcp_http_app(...)`, writer defaults to `None`. So in the running server (FastAPI only), MCP is not running and its writer is moot; in test/local wiring there is no shared instance unless the caller passes it.
    Source: `archon_search/server/mcp.py:30-34, 237-252`.
    Severity: medium.

11. Claim: '`APIKeyMiddleware` reads `Authorization: Bearer <token>`, then resolves a namespace: Iterates `config.namespaces` with `secrets.compare_digest` (no early exit, to avoid timing leakage). Falls back to the single default key from `key_manager.load_or_generate_key()`. Validates the resolved namespace and attaches it to `request.state.namespace`.'
    Ground truth: Verified.
    Source: `archon_search/server/middleware_auth.py:25-63`.
    Severity: none.

12. Claim: "Both the FastAPI app and the FastMCP HTTP app wrap themselves in this middleware."
    Ground truth: True only of the two app factories in isolation. In production (`run_server`), only FastAPI runs. Misleading without that qualifier.
    Source: `archon_search/server/app.py:121`, `archon_search/server/mcp.py:251`.
    Severity: medium.

13. Claim: '`_DebounceHandler` coalesces a burst of filesystem events into a single coroutine call after `debounce_seconds` (default 5 s).'
    Ground truth: Verified — `debounce_seconds: float = 5.0`.
    Source: `archon_search/watcher.py:47, 117`.
    Severity: none.

14. Claim: "The coroutine target is supplied by the server during startup — in practice, it triggers `SearchCollectionSync.sync_collection`, which calls back into `SearchPipeline.ingest_directory`."
    Ground truth: Two issues. (a) Search server startup (`create_app`) does NOT construct or start a `WatcherManager`; the only production wiring of `WatcherManager` is in `archon_search/install.py` (the `install_cmd` path), not the server lifespan. (b) `sync_collection` does not call `ingest_directory`; it computes new/changed/deleted files and calls `_apply_collection_changes`, which uses pipeline ingest-file / delete primitives, not `ingest_directory`. Independent of (a)/(b), watcher integration with the running HTTP server cannot be assumed from the code in `archon_search/server/`.
    Source: `archon_search/server/app.py` (no Watcher import), `archon_search/install.py:205-211`, `archon_search/sync.py:260-296`.
    Severity: high.

15. Claim: "Failure modes (see source: `archon_search/watcher.py`): Observer thread can't be scheduled or started — logged at WARNING, watcher silently inactive."
    Ground truth: Verified for `OSError` in both `schedule()` and `start()`.
    Source: `archon_search/watcher.py:136-154`.
    Severity: none.

16. Claim: "Stopping observer fails — best-effort join with 5 s timeout; thread-leak logged."
    Ground truth: Verified.
    Source: `archon_search/watcher.py:159-185`.
    Severity: none.

17. Claim: "`WatcherManager._shutting_down` short-circuits new callbacks; in-flight syncs get 10 s to drain before cancellation."
    Ground truth: Verified.
    Source: `archon_search/watcher.py:214, 243-252`.
    Severity: none.

18. Claim: 'Jobs are dataclasses (`types.IngestJob` + `ReindexJob`, `DeleteJob`)'.
    Ground truth: The dataclasses exist in `archon_search/types.py`, but the persistent `JobStore` is hard-coded to `IngestJob` only — `store.py` always constructs `IngestJob(**item)` on load. The `ReindexJob` / `DeleteJob` subclasses exist as types but are not used anywhere in the running code paths exercised by the REST API. The reindex route (`POST /collections/{name}/reindex`) creates an `IngestJob`, not a `ReindexJob`.
    Source: `archon_search/jobs/store.py:38-49, 95`; `archon_search/server/routes_collections.py:318`; `archon_search/types.py:31-37`.
    Severity: medium.

19. Claim: 'serialised as JSON to `~/.archon-search/archon-search-jobs.json` with atomic-rename writes.'
    Ground truth: Verified.
    Source: `archon_search/jobs/model.py:8`; `archon_search/jobs/store.py:112-121`.
    Severity: none.

20. Claim: 'Crash recovery: any `RUNNING` or `CANCELLING` job loaded from disk is rewritten to `FAILED` with `error="process_restart"`.'
    Ground truth: Verified.
    Source: `archon_search/jobs/store.py:16, 96-100`.
    Severity: none.

21. Claim: 'Jobs older than 7 days are evicted on every write.'
    Ground truth: Verified — `_EVICTION_DAYS = 7`, `_evict_old` is called from `_write_atomic` and also from `_load`.
    Source: `archon_search/jobs/store.py:17, 113, 123-131`.
    Severity: none.

22. Claim: 'State transitions go through `JobStore.transition(job_id, from_statuses, to_status)` so concurrent updates can\'t race a status backwards.'
    Ground truth: `transition()` exists and enforces guard. However, the lifecycle wrapper `_default_ingest_task` uses plain `store.update(...)` to move PENDING→RUNNING and RUNNING→DONE (and FAILED/CANCELLED), not `transition()`. Only `DELETE /jobs/{job_id}` uses `transition()`. So the "all transitions" framing is inaccurate.
    Source: `archon_search/jobs/store.py:63-77`; `archon_search/server/routes_jobs.py:64-86, 140`.
    Severity: medium.

23. Claim: "Routes that issue jobs: `POST /ingest`, `POST /collections/`, `POST /collections/{name}/reindex`."
    Ground truth: Verified — all three return `JobResponse` with status 202.
    Source: `archon_search/server/routes_jobs.py:91`; `archon_search/server/routes_collections.py:114, 299`.
    Severity: none.

24. Claim (sequence diagram step 10): "SY->>JS: create reindex job (PENDING -> RUNNING)".
    Ground truth: `sync_collection` does NOT touch the `JobStore` at all. There is no job created on watcher-triggered sync.
    Source: `archon_search/sync.py:260-296` (no `JobStore`/`store.create` references); `JobStore` is not imported in `sync.py`.
    Severity: high.

25. Claim (sequence diagram step 11): "SY->>ST: update CollectionProgress (INDEXING)" and step 18: "update CollectionProgress (READY)".
    Ground truth: `IndexingStatus` values are `pending`, `in_progress`, `done`, `failed`. There is no `INDEXING` or `READY`.
    Source: `archon_search/progress.py:22-26`.
    Severity: medium.

26. Claim (sequence diagram step 12): 'SY->>PL: `ingest_directory(path, collection, on_file_complete=...)`'.
    Ground truth: `sync_collection` does not call `ingest_directory`; it routes through `_apply_collection_changes` which uses per-file operations. Even if it did, `SearchPipeline.ingest_directory` takes `progress_cb`, not `on_file_complete` (per the MCP tool wrapper signature and MCP source).
    Source: `archon_search/sync.py:260-296`; `archon_search/server/mcp.py:148-157`.
    Severity: medium/high.

27. Claim (sequence diagram step 15): "PL->>LD: rebuild_fts_index (once at end)".
    Ground truth: Not verified within the files I examined. This is a behavioral claim about `SearchPipeline` / `SearchStore` internals not exercised by `sync.py`'s `sync_collection`. Likely ambiguous.
    Severity: low (unverifiable from the scope reviewed).

28. Claim (sequence diagram step 19): "SY->>JS: transition RUNNING -> SUCCEEDED".
    Ground truth: Two errors — (a) `sync_collection` does not touch `JobStore`; (b) `JobStatus` enum has no `SUCCEEDED` member (terminal success is `DONE`).
    Source: `archon_search/types.py:10-16`; `archon_search/sync.py:260-296`.
    Severity: high.

29. Claim: 'on process crash mid-run, job is rewritten to FAILED ("process_restart") at next load'.
    Ground truth: Verified (see item 20).
    Source: `archon_search/jobs/store.py:96-100`.
    Severity: none.

30. Claim (table row): "Jobs / Ingest — `POST /ingest` (202)".
    Ground truth: Verified.
    Source: `archon_search/server/routes_jobs.py:91`.
    Severity: none.

## Verified claims

- Lifespan calls `migrate_namespace` + `migrate_acl` before traffic flows. (`app.py:90-92`)
- BearerAuth is applied to every non-exempt path in OpenAPI. (`app.py:46-76`)
- 202 status on collection-add, reindex, and ingest. (`routes_collections.py:114,299`; `routes_jobs.py:91`)
- 9 MCP tools registered, names and pipeline methods as listed. (`mcp.py:38-228`)
- Auth resolution iterates without early exit using `secrets.compare_digest`, falls back to default key, attaches `request.state.namespace`. (`middleware_auth.py:25-63`)
- Watcher debounce default 5 s; 10 s drain timeout on shutdown. (`watcher.py:47,117,243-252`)
- Watcher observer scheduling/start `OSError` paths log WARNING and leave the watcher inactive. (`watcher.py:136-154`)
- Jobs file path and atomic-rename writes. (`jobs/model.py:8`; `jobs/store.py:112-121`)
- 7-day job eviction enforced on writes (and on load). (`jobs/store.py:17,113,123-131`)
- `JobStore.transition` enforces from-status guard. (`jobs/store.py:63-77`)
- Telemetry-entry-factories take no `query` parameter (asserted in principle 5; consistent with `archon_search/telemetry/entry.py` per CLAUDE.md invariant — not re-read here).

## Unverifiable / ambiguous

- "The coroutine target is supplied by the server during startup" — the server (`create_app`) does NOT construct a `WatcherManager`. Watcher wiring is performed in `archon_search/install.py` via the `install_cmd` flow, not the HTTP server lifespan. If the document means "the install-time wiring," it should say so; if it means "at server startup," it is wrong. (See item 14.)
- "rebuild_fts_index (once at end)" in the sequence diagram — not verified against `pipeline.py` / `store.py` in the files reviewed for this audit. (Item 27.)
- "MCP tools share the REST auth layer but their names do not mirror REST 1:1" (in CLAUDE.md) — orthogonal to this file but referenced by the "Discrepancy with CLAUDE.md" callout. The callout itself is accurate: `server/mcp.py` does not register `search_status`, `search_start`, etc.
- Whether MCP ever shared a `TelemetryWriter` instance with the FastAPI app in any deployment path — there is no production code wiring `create_mcp_http_app` at all, so the claim is moot. (Item 10.)
