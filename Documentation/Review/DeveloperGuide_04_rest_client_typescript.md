# Review: DeveloperGuide/04_rest_client_typescript.md

## Summary

The doc is mostly accurate in shape: paths, methods, request/response models, the `Bearer` auth, the `X-Ingested-By` header, and the `top_k`-accepted-but-ignored note all match the source. Two real bugs in the TypeScript code samples would break callers at runtime: (1) the `JobResponse.status` union is lower-case but the wire enum is upper-case, so `waitForJob` would loop forever, and (2) `cancelJob` returns 202 on the non-terminal path and the generic `request()` helper treats only `!res.ok` as error — that part is fine — but the doc never tells the reader that DELETE `/jobs/{id}` can return 202 with a `JobResponse` body. A few smaller schema-shape gaps (missing `POST /collections/{name}/reindex`, missing top-level `acl_filtered` semantics, `IngestRequest.ingested_by` overwritten server-side from header) are listed below.

Verified against: `archon_search/server/schemas.py`, `routes_health.py`, `routes_search.py`, `routes_collections.py`, `routes_jobs.py`, `routes_route.py`, `app.py`, `jobs/model.py`, `types.py`.

## Inaccuracies (numbered)

1. **`JobResponse.status` literal union is wrong-case.** Doc declares `"pending" | "running" | "done" | "failed" | "cancelling" | "cancelled"`. Wire values are upper-case: `archon_search/types.py:10-16` defines `JobStatus` with `PENDING`, `RUNNING`, `DONE`, `FAILED`, `CANCELLED`, `CANCELLING`, and `jobs/model.py:15` serializes `job.status.value` — so the API emits `"PENDING"`/`"RUNNING"`/`"DONE"`/`"FAILED"`/`"CANCELLING"`/`"CANCELLED"`. The `waitForJob` example (`["done", "failed", "cancelled"].includes(job.status)`) would never terminate.

2. **`cancelJob` may return 202, not 200.** `routes_jobs.py:147` and `:150` set `response.status_code = 202` when the job was active or already in `CANCELLING`. The `ArchonClient.request<T>` helper only branches on `!res.ok`, which is correct (202 is ok), but the documented `Promise<JobResponse>` return doesn't note that the response is asynchronous and the cancellation has only been *requested* on a 202. Worth surfacing alongside the `waitForJob` flow.

3. **`POST /collections/` returns 202, not 200.** Documented signature `addCollection(...): Promise<JobResponse>` is shape-correct but the doc never mentions the 202 status (`routes_collections.py:114`). Same for `POST /ingest` (`routes_jobs.py:91`) and `POST /collections/{name}/reindex` (`routes_collections.py:299`). Not a runtime bug for `fetch` (still `res.ok`), but a documentation gap.

4. **Missing endpoint: `POST /collections/{name}/reindex`.** Implemented at `routes_collections.py:299-324`, returns 202 + `JobResponse`, supports `X-Ingested-By`. Not present in the client class or the text. The doc claims to mirror the Python guide and the full endpoint reference; this one is silently omitted.

5. **Missing endpoints not represented in the client (acknowledged or not).** `GET /status` (`routes_status.py`), `GET /state` (`routes_state.py`), `GET /telemetry/*` (`routes_telemetry.py`) are all real authenticated endpoints. The doc presents `ArchonClient` as the integration surface without listing what it deliberately omits. The Python guide it claims to mirror should be cross-checked; either include them or call out the scope.

6. **`IngestRequest.ingested_by` field is effectively ignored when sent in the body.** `routes_jobs.py:97-99` unconditionally overwrites `body.ingested_by` with the `X-Ingested-By` header (defaulting to `"archon-search-cli"`). The TS type marks it optional which is true at the wire level, but the example doesn't note that putting it in the JSON body is a no-op — only the header counts. Same shadowing in `add_collection` (`routes_collections.py:157`) and `reindex` (`:317`).

7. **`AddCollectionRequest` does not accept `ingested_by`.** The server-side `AddCollectionRequest` (`routes_collections.py:31-32`) has only `path`. The doc's `addCollection` method correctly puts `ingestedBy` in the header — fine — but readers might infer symmetry with `IngestRequest`. Worth a one-liner.

8. **`SearchResponse.acl_filtered` semantics undocumented.** The type is correct (`schemas.py` / `routes_search.py:57-59`). The doc never explains what `acl_filtered: true` means (some results were excluded by ACL). Mentioned here only because the doc's stated goal is to mirror the Python guide.

9. **`/search` swallows pipeline errors to an empty 200 — accurate, but the path is `/search` not `/search/`.** The note in "Principles" §3 is right (`routes_search.py:82-84` returns `SearchResponse(results=[], acl_filtered=False)` on exception, and meta lookup failure returns 503 — not all failure modes collapse to 200, only the pipeline stage). Worth a small clarification: a missing collection returns 404 (`:73-74`), and a store-meta lookup failure returns 503 (`:71`), so "always 200 with empty results" is too strong.

10. **`listCollections` path has a trailing slash; `getCollection` / `removeCollection` do not — this is correct, but unstated.** `routes_collections.py:23` declares `prefix="/collections"` and the list/add handlers are mounted at `"/"`, so `GET /collections/` and `POST /collections/` are the canonical paths. `GET/DELETE /collections/{name}` have no trailing slash. The code sample matches; document it explicitly to forestall "I'll just normalize the slashes" bugs.

11. **`health()` returns `Promise<HealthResponse>` but does not validate the response.** Minor: the bypass-the-bearer-header code path uses raw `r.json()` and never checks `r.ok`. A failing `/health` would resolve, not reject. The Python guide presumably does something similar; flag for parity.

12. **`HealthResponse.status` comment ("running") is the only value, but it's not enforced as a literal.** Server returns the literal string `"running"` (`routes_health.py:20`). The TS type uses `string`; tightening to `"running"` would catch regressions. Documentation-quality issue, not a bug.

13. **`RouteRequest.slots` validation.** Server rejects `slots < 1` with HTTP 400 (`routes_route.py:78-79`) and `query` empty/whitespace with 400 (`:75-76`). The TS type allows any `number | null`. Same kind of nit as #12.

14. **`ErrorDetail` mention is unused.** Defined in the types block and exported in the import list of `archonClient.ts` but never referenced — `ArchonError` carries the raw `body: string` instead of parsing `{detail: string}`. Either parse it or drop the type from the example.

15. **Axios paragraph: `error.response.data.detail` shape.** Correct for the 4xx/validation cases (FastAPI/`HTTPException` produces `{"detail": "..."}`). FastAPI's default 422 validation response uses `{"detail": [{...}, ...]}` (a list), not a string. Worth a half-sentence caveat.

## Verified claims

- Bearer auth on all non-public endpoints (`app.py:60-73`, `middleware_auth.py`).
- `/health` is unauthenticated (`_EXEMPT_PATHS` in `app.py:24`).
- `X-Ingested-By` header is read by `POST /ingest`, `POST /collections/`, `POST /collections/{name}/reindex`.
- `HealthResponse { status, version }` matches `schemas.py:10-12` and `routes_health.py:19-20`.
- `CollectionSummary` fields (`name`, `path`, `description`, `doc_count`, `chunk_count`, `namespace`, `status`) match `schemas.py:52-59`.
- `CollectionDetail` adds `embedding_model`, `centroid_present`, `last_indexed`, `acl_protected_count`, `acl_open_count` — matches `schemas.py:62-67`.
- `JobResponse` fields (`job_id`, `status`, `created_at`, `updated_at`, `result`, `error`, `namespace`) match `schemas.py:70-77` and `jobs/model.py:11-21`.
- `DeleteResponse { name, deleted }` matches `schemas.py:80-82`.
- `SearchRequest { collection, query, top_k? }` matches `routes_search.py:17-20`; `top_k` is bounded `[1,100]` server-side (extra constraint the doc omits but client doesn't need to know).
- `SearchResult { doc_id, chunk_id, text, score, source_path }` matches `routes_search.py:39-44`.
- `SearchResponse { results, acl_filtered }` matches `routes_search.py:57-59`.
- `RouteRequest { query, slots? }` matches `routes_route.py:23-25`.
- `RouteResponse { pre_context, pinned_names, routable_names, decomposer_invoked }` matches `routes_route.py:28-32`.
- `IngestRequest { collection, path?, documents?, ingested_by? }` field-name match against `routes_jobs.py:27-31`.
- `AddCollectionRequest { path }` matches `routes_collections.py:31-32`.
- Paths and methods used by the client class are all correct: `GET /health`, `GET /collections/`, `GET /collections/{name}`, `POST /collections/`, `DELETE /collections/{name}`, `POST /search`, `POST /route`, `POST /ingest`, `GET /jobs/{id}`, `DELETE /jobs/{id}`.
- The `top_k`-accepted-but-ignored claim is consistent with the BREAKING.md reference (not re-verified here against BREAKING.md — only against `routes_search.py`, which still declares the parameter; behavior delegated to `pipeline.search` which the doc claims uses `top_k_return` from config — see `app.py:138-139` confirming `top_k_return` is passed to `SearchPipeline`).
- `~/.archon-search/.search.env` key file format and `ARCHON_SEARCH_API_KEY=` line prefix match `key_manager.py` semantics described in CLAUDE.md (not re-read in this review).

## Unverifiable / ambiguous

- "If you need machine-generated types, run `openapi-typescript` against that endpoint." — not verified that the generated types would be correct; depends on `_configure_openapi` output. Plausible.
- "Pipeline errors return HTTP 200 with `results=[]` today (debt `CON-5` / roadmap A4)." — partly verified (`routes_search.py:82-84`); the CON-5 / roadmap A4 identifier is not checked.
- "Cross-reference `BREAKING.md`" — not opened during this review.
- The claim that this page "mirrors `03_rest_client_python.md`" — `03_rest_client_python.md` not opened; mirror-completeness not assessed.
- The 5-minute / 300_000 ms timeout in `waitForJob` is arbitrary user advice, not a contract — neither verified nor disputable.
- Authorization header behavior under CORS preflight (the doc constructs a browser-adjacent client; `app.py:122` sets permissive CORS) — not investigated.
