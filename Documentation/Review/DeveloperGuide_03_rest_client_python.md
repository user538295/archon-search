# Review: DeveloperGuide/03_rest_client_python.md

## Summary

The document is largely accurate against `archon_search/server/routes_*.py` and `schemas.py`. Endpoint paths, payload schemas, status codes, validation rules, and dedup/namespacing semantics all match the source. One factual inaccuracy on job status casing (uppercase, not lowercase) and one minor casing/format issue. The Python snippets are syntactically valid `httpx` usage. Method-chaining `r.raise_for_status().json()` works because httpx's `raise_for_status()` returns `self`.

## Inaccuracies (numbered)

1. **Line 114: job statuses listed in lowercase.** The doc says statuses are `pending`, `running`, `done`, `failed`, `cancelling`, `cancelled`. The enum in `archon_search/types.py:10-16` defines them as `PENDING`, `RUNNING`, `DONE`, `FAILED`, `CANCELLED`, `CANCELLING`, and `job_to_dict` (`archon_search/jobs/model.py:15`) serializes `job.status.value` directly — so wire responses contain uppercase strings. The example membership check `j["status"] in {"done", "failed", "cancelled"}` (line 123) will therefore never match a real response and silently loop forever (until the deadline). Both the status list and the example must be uppercased.

2. **Line 49 example version `"26.5.123"`.** Version is CalVer `YY.M.<rev-count>` per `CLAUDE.md`; `26.5.123` is plausible but presented as if verified. Minor — it is a placeholder example, but the doc claims "every code block was checked" (line 8), which implies the literal output was observed. Soften to "example" or mark as illustrative.

3. **Line 196 routing error wording.** Doc says "`400` for empty `query` or `slots < 1`". The actual `detail` strings from `routes_route.py:76,79` are `"query must not be empty"` and `"slots must be >= 1"`. Status code is correct; phrasing is fine but readers writing exception handlers may key off detail text.

4. **Line 197 timeout phrasing.** Doc says "`504` after a 30 s routing timeout". Code raises `HTTPException(status_code=504, detail="routing timed out")` (`routes_route.py:135`). Correct status; detail text differs from the doc's freeform description but doc never quoted it as the exact body. Borderline — flagging for completeness.

5. **Line 184 cross-reference accuracy.** "MCP tool `search_with_context` (`mcp.py:77`)". Not verified in this review (path/line could drift). The tool name is correct per the project CLAUDE.md MCP tool list; line number not checked.

6. **Line 154 cross-reference `routes_jobs.py:98`.** Verified accurate (line 98 reads `ingested_by = request.headers.get("X-Ingested-By", "archon-search-cli")`). No inaccuracy — listed here as positively confirmed.

## Verified claims

- Default base URL/port `http://127.0.0.1:8765` — `archon_search/config.py:31` (`port: int = 8765`).
- `/health` exempt from auth and returns `HealthResponse{status, version}` — `routes_health.py:18-20`, `schemas.py:10-12`.
- `GET /collections/` returns `list[CollectionSummary]` with `doc_count=0, chunk_count=0` hardcoded — `routes_collections.py:96-104`.
- `GET /collections/{name}` returns `CollectionDetail` with extra fields `embedding_model`, `centroid_present`, `last_indexed`, `acl_protected_count`, `acl_open_count`, and a real `doc_count` from `count_documents` — `routes_collections.py:268-296`, `schemas.py:62-67`. (Note: `chunk_count` remains `0` in the detail response too; the doc only claimed `doc_count` becomes real, which is accurate.)
- `POST /collections/` body `{"path": str}` → 202 + `JobResponse` — `routes_collections.py:31-32, 114, 168`.
- 409 `"collection already registered"` on duplicate resolved path — `routes_collections.py:130`.
- 409 `"collection name already registered"` on global name collision — `routes_collections.py:136, 148`.
- `X-Ingested-By` header consumed by both `POST /collections/` (`routes_collections.py:157`) and `POST /ingest` (`routes_jobs.py:98-99`), and the latter overwrites the JSON body field — exactly as the doc claims.
- `JobResponse` shape `{job_id, status, created_at, updated_at, result, error, namespace}` — `schemas.py:70-77` and `jobs/model.py:11-21`.
- `GET /jobs/{job_id}` returns same `JobResponse`; 404 also used for cross-namespace — `routes_jobs.py:108-116`.
- `DELETE /jobs/{job_id}` idempotency: terminal → 200, active → 202 (CANCELLING), already-CANCELLING → 202 — `routes_jobs.py:136-151`.
- `POST /ingest` `IngestRequest{collection, path?, documents?, ingested_by}` — `routes_jobs.py:27-31`.
- `POST /search` request `SearchRequest{collection, query, top_k=5 (1..100)}` with non-empty validators — `routes_search.py:17-36`. `top_k` accepted-but-ignored claim matches: the handler passes only `query, collection, namespace` to `pipeline.search` (line 77), no `top_k` propagation.
- 404 `"collection not found"`, 503 `"service unavailable"` on meta failure, and the silent-empty-results fallback on pipeline exception — `routes_search.py:71, 74, 82-84`. Documentation's CON-5/A4 reference is accurate behavior-wise.
- `SearchResponse{results: [{doc_id, chunk_id, text, score, source_path}], acl_filtered: bool}` — `routes_search.py:39-59`.
- `POST /route` `RouteRequest{query, slots?}` → `RouteResponse{pre_context, pinned_names, routable_names, decomposer_invoked}` — `routes_route.py:23-32`.
- `/route` 400 for empty query / `slots < 1`; 504 on 30 s timeout — `routes_route.py:75-79, 94-100, 122-135`.
- `DELETE /collections/{name}` returns `DeleteResponse{name, deleted}`; 404 unknown / cross-namespace; 409 pinned-only — `routes_collections.py:171-205`, `schemas.py:80-82`.
- No REST `/search/with_context` endpoint — confirmed; only `routes_search.py` defines `/search`.
- All non-`/health` endpoints require `Bearer` auth — consistent with `middleware_auth.py` being the shared layer (per CLAUDE.md; not directly inspected here).
- `httpx.Client(...).get(...).raise_for_status().json()` chaining — valid; httpx's `raise_for_status()` returns the Response.

## Unverifiable / ambiguous

- "`mcp.py:77`" line reference (line 184) — not verified in this pass.
- The exact CON-5 / roadmap-item-A4 reference text — not inspected; behavior described matches `routes_search.py:82-84`.
- The `/health` example version string `"26.5.123"` — illustrative only; cannot be verified without a running server.
- The pinned-only 409 detail string is longer in code (`routes_collections.py:201-205`) than the doc summarizes; doc paraphrases rather than quotes, so not strictly inaccurate.
- `X-Ingested-By` "job log shows the calling system" (line 102) — header value flows into `IngestRequest.ingested_by`; whether it surfaces in a user-visible "job log" was not traced beyond the request layer.
