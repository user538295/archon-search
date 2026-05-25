# Review: UserManual/05_searching.md

## Summary

The document is largely accurate against `routes_search.py`, `routes_route.py`, `mcp.py`, and `pipeline.py`. Verified: hybrid pipeline, `SearchRequest` shape, `SearchResponse` shape, status codes (200/404/503/500/504), `/route` request/response, 400 validation and 504 timeout (30s), MCP tool count (9) and signatures, `default_collection` fallback, `/health` auth exemption, port 8765, streamable-HTTP transport at `/mcp`. One minor inconsistency: `BREAKING.md` (lines 21–22) labels the setting `[search] top_k_return`, but the implementation (config.py:163, archon-search.toml.example line 20) places it under `[database]`. The doc agrees with the implementation (`[database].top_k_return`), so the doc is correct and `BREAKING.md` is the file that drifted — not an inaccuracy in this doc. Note: an earlier version of this review cited "degraded-empty-200" as a valid status-code path; that behavior was removed in A3 (CON-5) — pipeline exceptions now return 500, timeouts return 504.

## Inaccuracies (numbered)

None found that are attributable to this doc. All concrete claims verify against source.

(Adjacent issue, outside this doc: `BREAKING.md` lines 21–22 reference `[search] top_k_return`; the implementation uses `[database] top_k_return`. The user-manual page is the one that's correct.)

## Verified claims

1. "Every `/search` query runs dense vector + FTS retrieval, then a cross-encoder reranker" — `pipeline.py:297-303` calls `store.hybrid_search(...)` then `self._reranker.rerank(...)`.
2. "`POST /search` requires a `collection` field — the server does not auto-pick" — `routes_search.py:18,22-28` make `collection` required and non-empty after strip.
3. "Per-request `top_k` on `/search` is accepted but ignored at the route level; the pipeline uses `[database].top_k_return`" — `SearchRequest.top_k` (line 20) is declared but never passed to `pipeline.search` (line 77). `pipeline.py:297-303` uses `self._top_k_return` (initialized from `cfg.top_k_return`, line 439). Config key is in `[database]` (archon-search.toml.example line 20, config.py:163-167).
4. "REST and MCP are equivalent surfaces. The MCP tools wrap the same pipeline calls" — `mcp.py:46` calls `pipeline.search`, same instance used by REST.
5. `SearchRequest` fields, defaults, `top_k` `1..100` default `5`, validators — match `routes_search.py:17-36`.
6. `SearchResponse` fields (`results`, `acl_filtered`) and `SearchResultSchema` fields (`doc_id`, `chunk_id`, `text`, `score`, `source_path`) — match `routes_search.py:39-59`.
7. Status codes: `200` success; `404` "collection not found"; `503` "service unavailable" on meta lookup failure; `500` on pipeline stage exception (embedder, store, reranker — A3/CON-5); `504` on pipeline timeout (~30 s — A3/CON-5) — `routes_search.py`. Note: the pre-A3 "pipeline exception degrades to empty results with 200" claim verified against the old `routes_search.py:82-84` block; that path was removed in A3.
8. `/route` request shape `{query, slots?}`, `slots` optional and `>= 1` when set, overrides `routing_shortlist_size` — `routes_route.py:23-25, 84-87`.
9. `/route` response `pre_context, pinned_names, routable_names, decomposer_invoked` — `routes_route.py:28-32, 102-107`.
10. `/route` 400 validation messages "query must not be empty" / "slots must be >= 1"; 504 with 30s hard timeout — `routes_route.py:79-82, 96` (`timeout=30.0`), `121` (`raise HTTPException(status_code=504, ...)`).
11. MCP tool count of 9 and all signatures in the table — `mcp.py:38-228` (`search`, `search_with_context`, `ingest_file`, `ingest_directory`, `list_collections`, `get_collections_meta`, `get_collection_meta`, `list_documents`, `delete_document`).
12. `search` MCP returns `{"results": [...], "acl_filtered": bool}` — `mcp.py:59`.
13. `search_with_context` default `context_window=1`, returns `[{result, context_before, context_after}, ...]` — `mcp.py:80, 100-107`.
14. `ingest_directory` default `glob_pattern="**/*"`, reports MCP progress via `ctx.report_progress` — `mcp.py:142, 148-150`.
15. `list_collections` omits centroid; `get_collections_meta` includes centroid — `mcp.py:170-173` (`d.pop("centroid", None)`) vs `mcp.py:181-183`.
16. `get_collection_meta` returns `not_found` error dict when missing — `mcp.py:193-194`.
17. `list_documents` `limit=100` default — `mcp.py:203`.
18. `delete_document` returns `{"deleted": <count>}` — `mcp.py:225`.
19. `collection` omitted → `default_collection` fallback — every tool uses `collection or default_collection`.
20. `/health` exempt from auth, `POST /mcp` streamable-HTTP transport via `create_mcp_http_app` — `mcp.py:230-252`.
21. Port 8765 in example `curl` — matches default in config.py:31 and toml example line 18.

## Unverifiable / ambiguous

1. The doc's "(see `BREAKING.md` — was previously a bare list)" for the MCP `search` tool — `BREAKING.md` was not exhaustively reviewed for that specific historical entry; only the `top_k` section was inspected here. The current shape `{"results": [...], "acl_filtered": ...}` is verified; the historical claim is a `BREAKING.md` cross-reference, not a behavior claim about current code.
2. "MCP client pseudocode" block is explicitly labeled pseudocode and not asserting a real API; nothing to verify.
3. Pinned name resolution behavior (`path_to_collection_name`, namespace filtering) is mentioned only via `routable_names` / `pinned_names` in the response example; the doc does not make specific claims about how these are computed, so there is nothing to falsify there.
