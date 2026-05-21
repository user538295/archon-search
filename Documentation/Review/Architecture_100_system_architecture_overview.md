# Review: Architecture/100_system_architecture_overview.md

## Summary

The doc is largely accurate at the conceptual layer (pipeline ordering, two-stage retrieval, router tiers, single auth boundary, telemetry storage). However, several concrete claims diverge from source:

- The biggest factual error is the runtime topology claim that the MCP HTTP app is "mounted via Starlette" into the FastAPI app. In source, `archon_search.server.app.create_app` does not mount any MCP app; `create_mcp_http_app` in `server/mcp.py` exists but is never invoked anywhere in the package, and the entry point `python -m archon_search.server` only runs the FastAPI app via `run_server` -> `uvicorn.run(app, ...)`. As a result the C4 L2 arrow `MCPAPP --> AUTH` and the router-to-MCP JSON-RPC arrow describe a code path that is reachable in tests but not in the shipped server.
- The default LanceDB path in the C4 L2 diagram (`~/.archon-search/db`) is wrong; the configured default is `~/.archon-search/search`.
- `/health`, `/docs`, `/openapi.json`, `/redoc` are listed in `_EXEMPT_PATHS`, but the doc's principles section claims "nothing else is exempt except `/health`". These two statements in the doc contradict each other; the middleware exempts all four.
- The `JobStore` description "JSON file with atomic rename writes and crash-recovery (RUNNING/CANCELLING jobs become FAILED on load)" is consistent with source. Good.
- ACL filtering placement description ("between the two stages") matches `SearchPipeline.search` source.

## Inaccuracies (numbered)

1. Claim: "`uvicorn` serves the FastAPI app; the MCP HTTP app is mounted via Starlette." (line 150)
   - Ground truth: The FastAPI app produced by `create_app` does not mount any MCP sub-app; there is no `app.mount(...)` and no call to `create_mcp_http_app` anywhere outside tests. `run_server` only runs the FastAPI app with `uvicorn.run(app, host=..., port=...)`.
   - Evidence: `archon_search/server/app.py:120-156` (no `mount`, only `add_middleware` + `include_router`), `archon_search/server/__main__.py:1-6`, grep for `create_mcp_http_app` finds only the definition at `archon_search/server/mcp.py:237`.
   - Severity: High (load-bearing architectural claim; misleads readers about how MCP is served).

2. Claim: C4 L2 diagram shows `MCPAPP[FastMCP app<br/>server.mcp.create_app]` running inside the `archon-search process` with `APP --> MCPAPP` not drawn but `MCPAPP --> AUTH` and `ROUTER -->|JSON-RPC| MCPAPP` shown. (lines 50, 70, 76)
   - Ground truth: In the running server, only FastAPI is started. `MultiCollectionRouter` calls `search_url` over JSON-RPC, but the server it talks to is the FastMCP HTTP app that is never wired up in `create_app` or `run_server`. The "router calls back into MCP" topology depicted is not how the shipped binary runs.
   - Evidence: `archon_search/router.py:64-95` (HTTP POST to `self._search_url`), `archon_search/server/app.py:120-156`, `archon_search/server/__main__.py:1-6`.
   - Severity: High.

3. Claim: C4 L2 diagram `LANCE[(LanceDB<br/>~/.archon-search/db)]`. (line 61)
   - Ground truth: Default `db_path` is `~/.archon-search/search`.
   - Evidence: `archon_search/config.py:33` (`db_path: str = "~/.archon-search/search"`).
   - Severity: Medium (factually wrong path; could mislead operators).

4. Claim: "REST and MCP share `APIKeyMiddleware`; nothing else is exempt except `/health`." (line 15)
   - Ground truth: `_EXEMPT_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}`. The middleware exempts four paths, not one. The doc later (line 146) correctly lists all four, contradicting the principle statement.
   - Evidence: `archon_search/server/middleware_auth.py:16`.
   - Severity: Medium (internal contradiction; security-adjacent claim).

5. Claim: "Background tasks owned by the FastAPI lifespan: `TelemetryWriter`, `Pruner`." (line 150)
   - Ground truth: True only when `config.telemetry.enabled` is set. By default `telemetry.enabled = false` so neither background task is started. The doc presents them as unconditional.
   - Evidence: `archon_search/server/app.py:95-105` (`if config.telemetry.enabled:` gate), `archon_search/config.py` defaults (telemetry disabled by default per CLAUDE.md and config default).
   - Severity: Low (qualifier missing; the underlying mechanism is correct).

6. Claim: C4 L2 diagram lists `JOBSFILE[(~/.archon-search/<br/>archon-search-jobs.json)]`. (line 63)
   - Ground truth: Path matches `JOBS_FILE: Path = Path.home() / ".archon-search" / "archon-search-jobs.json"`.
   - Evidence: `archon_search/jobs/model.py:8`.
   - Severity: None (verified correct — listed here only for completeness, not an inaccuracy).

7. Claim: "The API key is a 32-byte hex string in `~/.archon-search/.search.env`, chmod 600." (line 142)
   - Ground truth: `secrets.token_hex(32)` produces 64 hex characters representing 32 bytes of entropy. The phrase "32-byte hex string" is ambiguous — it could be read as a 32-character hex string (which would only be 16 bytes). Source actually emits 64 hex characters / 32 bytes.
   - Evidence: `archon_search/key_manager.py:85` (`key = secrets.token_hex(32)  # 64 hex chars`).
   - Severity: Low (ambiguous wording; the comment in source itself says "64 hex chars" to disambiguate, so the doc should match).

8. Claim: "The watcher debounces filesystem events (default 5 s) so a burst of writes triggers exactly one reindex." (line 158)
   - Ground truth: `debounce_seconds: float = 5.0` is the default in `_DebounceHandler`, `CollectionWatcher`, and `WatcherManager`.
   - Evidence: `archon_search/watcher.py:47, 117, 204`.
   - Severity: None (verified correct).

9. Claim: "`MultiCollectionRouter.rank` scores each collection's stored centroid against the query embedding (cosine), applies a confidence gate, and shortlists." (line 132)
   - Ground truth: Mostly true, but `rank` has an additional behavior the doc omits: collections with `embedding_model != self._embedding_model` (or `centroid is None`) are pushed to an "unscored" list and appended after scored ones, and if there are no scored collections at all the confidence gate is bypassed and unscored are returned up to `shortlist_size`.
   - Evidence: `archon_search/router.py:127-158`.
   - Severity: Low (simplification, not strictly wrong, but the embedding-model mismatch path is non-obvious and worth a sentence).

10. Claim: "The router calls `get_collections_meta` over JSON-RPC against the MCP endpoint — it does not import the pipeline." (line 138)
    - Ground truth: `MultiCollectionRouter.fetch_metadata` does post a JSON-RPC `tools/call` with `name: get_collections_meta` to `self._search_url`. The "does not import the pipeline" claim is true (the file imports `CollectionMeta` only).
    - Evidence: `archon_search/router.py:72-95` (JSON-RPC payload), top of `router.py` imports.
    - Severity: None (verified). Note however that — per inaccuracy #1 and #2 — in the shipped server the MCP endpoint the router would talk to is not actually being served, so this code path is currently dead at runtime.

11. Claim: "`SearchPipeline.ingest_file` runs `parser -> chunker -> embedder -> store`, assigning sequential chunk IDs and propagating ACLs." (line 118)
    - Ground truth: Matches source. Sequential chunk IDs are formatted `f"{doc_id}-{idx:06d}"`, ACL is resolved via `resolve_acl(path, _acl)` and propagated onto each record.
    - Evidence: `archon_search/pipeline.py:170-171, 161`.
    - Severity: None.

12. Claim: "`SearchPipeline.search` runs `embedder -> store.hybrid_search -> acl filter -> reranker`." (line 118)
    - Ground truth: Matches source exactly.
    - Evidence: `archon_search/pipeline.py:300-303`.
    - Severity: None.

13. Claim: "`ingest_directory` additionally computes a centroid over all batch vectors and updates `CollectionMeta`, optionally regenerating the auto description." (line 118)
    - Ground truth: Matches source; `_compute_centroid(all_vectors)` is invoked and `update_collection_meta` is called.
    - Evidence: `archon_search/pipeline.py:258-289`.
    - Severity: None.

14. Claim: "Stages are independent classes wired by the orchestrator. The ordering `parser -> chunker -> embedder -> store -> reranker` is enforced by `SearchPipeline`; no other module should bypass it. Backends (`EmbedderBackend`, `RerankerBackend`) are Protocols..." (line 124)
    - Ground truth: `EmbedderBackend` and `RerankerBackend` are `Protocol`s.
    - Evidence: `archon_search/embedder.py:10` (`class EmbedderBackend(Protocol):`), `archon_search/reranker.py:14` (`class RerankerBackend(Protocol):`).
    - Severity: None.

15. Claim: "Three tiers in `get_pre_context`: `n_routable <= 3` / `4 <= n_routable <= shortlist_size` / `n_routable > shortlist_size`." (lines 134-136)
    - Ground truth: Source matches. Tier-3 also returns `None` when the confidence gate fails (shortlist empty), which the doc does not mention but is implicit.
    - Evidence: `archon_search/router.py:194-213`.
    - Severity: None.

16. Claim: "`SearchStore.connect()` runs `migrate_namespace` and `migrate_acl` before the app starts serving (see `server/app.py` lifespan)." (line 157)
    - Ground truth: The lifespan calls `connect()` then `migrate_namespace()` then `migrate_acl()` as separate awaits — they are not run from inside `connect()`. The parenthetical "(see `server/app.py` lifespan)" is accurate, but the leading sentence says `connect()` runs the migrations, which is wrong.
    - Evidence: `archon_search/server/app.py:90-92` (three separate `await` calls), `archon_search/store.py:86-91` (`connect()` does not invoke the migrations).
    - Severity: Medium (misattributes the call site).

17. Claim: "Reciprocal Rank Fusion, returns `top_k_retrieve` candidates. The cross-encoder reranker then narrows to `top_k_return`." (line 128)
    - Ground truth: `SearchPipeline.search` passes `top_k=self._top_k_retrieve` to `hybrid_search`, and `top_k=self._top_k_return` to `reranker.rerank`.
    - Evidence: `archon_search/pipeline.py:301, 303`.
    - Severity: None.

18. Claim: C4 L2 component list includes `KEY[key_manager.py]` and `AUTH -->|loads key| KEY`. (lines 58, 84)
    - Ground truth: `APIKeyMiddleware` itself does not load the key; the key is loaded by `load_or_generate_key()` in `create_app` and passed into the middleware constructor as `api_key`. The arrow direction (middleware loads from key_manager) is misleading.
    - Evidence: `archon_search/server/app.py:85, 121` (key loaded outside middleware), `archon_search/server/middleware_auth.py:20-23` (middleware receives `api_key` as constructor arg).
    - Severity: Low (diagram simplification, but technically wrong dependency direction).

## Verified claims

- Layered pipeline ordering `parser -> chunker -> embedder -> store -> reranker` matches `pipeline.py`.
- Two-stage retrieval: hybrid (vector + FTS, RRF) -> cross-encoder rerank, with ACL filtering between stages.
- `SearchPipeline.search` execution order: embedder -> hybrid_search -> apply_acl_filter -> reranker.
- 9 MCP tools registered in `server/mcp.py`.
- `JobStore` uses atomic-rename writes and converts `RUNNING`/`CANCELLING` jobs to `FAILED` on load (`_CRASH_STATUSES`, `_load`).
- Jobs file path: `~/.archon-search/archon-search-jobs.json`.
- Telemetry log dir default: `~/.archon-search/search-logs`.
- API key file: `~/.archon-search/.search.env`, mode 600.
- Watcher debounce default: 5.0 s.
- `EmbedderBackend` and `RerankerBackend` are `typing.Protocol` types.
- `MultiCollectionRouter.fetch_metadata` issues a JSON-RPC `tools/call` for `get_collections_meta`.
- Three-tier routing logic in `get_pre_context` (n_routable <= 3 / <= shortlist_size / > shortlist_size).
- `_EXEMPT_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}`.
- `ingest_directory` computes centroid via `_compute_centroid` and updates `CollectionMeta`.
- `SearchPipeline` constructor takes `top_k_retrieve` and `top_k_return` from config.
- Watcher `Observer` is a watchdog thread per `CollectionWatcher`.

## Unverifiable / ambiguous

- "Long-running ingest/reindex work is offloaded to the async `JobStore`" — the `JobStore` itself is synchronous (no `async def`) and stores job records; the actual offloading is done by `BackgroundTasks` in `routes_jobs.py`/`routes_collections.py`. The phrase conflates "the job records live in JobStore" with "JobStore is what runs the work async". Borderline accurate; depends on how strict you read "offloaded to". Evidence: `archon_search/jobs/store.py` (no async methods), `archon_search/server/routes_jobs.py` (uses FastAPI `BackgroundTasks`).
- The phrase "`MCPAPP --> AUTH`" in the C4 L2 diagram is correct for `create_mcp_http_app` (which does add `APIKeyMiddleware`) but, as noted in inaccuracy #1, that app is not actually started in the shipped server. Whether to keep the arrow depends on whether the doc intends to describe the shipped runtime or the code-as-designed.
- "modules above only depend on the pipeline" (principle 1) — informal architectural claim, not directly verifiable from a single file.
