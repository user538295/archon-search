# Review: Architecture/600_api_reference_or_public_interface.md

## Summary

The document is largely accurate and traces well to the source code. The REST/MCP/CLI tables match the route modules, the MCP tool list (9 tools) is correct, the auth model description matches `middleware_auth.py`, and the BREAKING.md notes about `top_k` and `search` response shape match the code. Several smaller inaccuracies and omissions exist, mostly around CLI flag/argument signatures (e.g. `collection remove <path>` is documented as `<path>` and is in fact `<path>`, not `<name>`; `collection info`/`reindex` take `<collection_name>` which the doc correctly names `<name>` — but the underlying behaviour for `remove` is path-based, not name-based, and the doc table calls it `<path>` for `remove`, which is correct), and around a few response/error/status details (notably the `/route` 400 conditions, `IngestRequest` validators, and the `IndexingStateResponse` "empty body" claim). No critical/load-bearing errors found.

## Inaccuracies (numbered)

1. **Line 39 — "returns empty body when no state file exists"**: Inaccurate. `routes_state.py` returns `IndexingStateResponse(collections={}, last_updated=None, trigger=None)`, i.e. a populated JSON object with empty `collections` and null fields, not an "empty body".

2. **Line 51 — `/search` `top_k` description**: The doc says "**`top_k` is ignored** (see BREAKING.md); pipeline uses `config.top_k_return`." This is correct in spirit but the wording "see BREAKING.md" is fine — however the doc fails to mention that `top_k` is still validated in `SearchRequest` (`ge=1, le=100`), so submitting `top_k=0` or `top_k=101` returns a 422 even though the value is otherwise ignored. Minor but worth noting in an authoritative reference.

3. **Line 51 — `SearchResponse` schema**: Doc says `{results: [SearchResultSchema], acl_filtered: bool}`. Correct, but `SearchResultSchema` fields are not listed (`doc_id, chunk_id, text, score, source_path`); other entries in the table similarly cite a schema name without listing fields. Consistency issue, not a factual error.

4. **Line 53 — "503 when meta lookup fails"**: Accurate (`routes_search.py` returns `503` on meta lookup exception). Pre-A3 this review noted that pipeline `search()` exceptions were swallowed and returned `200` with `{results: [], acl_filtered: false}` — that behavior was fixed in A3 (CON-5). Post-A3 the doc's status-code table (which lists 500 and 504 for pipeline failures) correctly documents the current behavior; this inaccuracy is resolved.

5. **Line 59 — `RouteRequest` fields `{query, slots?}`**: Correct. `slots: int | None = None` confirmed.

6. **Line 61 — "`400` empty query / `slots < 1`; `504` on 30 s routing timeout"**: Accurate. Confirmed in `routes_route.py` (line 75–79 for 400, line 100 timeout=30.0, line 135 for 504).

7. **Line 70 — `POST /collections/` "Register a path as a collection and enqueue first ingest (returns `202`)"**: Accurate, but the doc fails to mention the 409 cases: (a) duplicate resolved path, (b) name collision across namespaces. These materially affect integrators and are worth surfacing.

8. **Line 72 — "DELETE `/collections/{name}` ... `409` if pinned-only"**: Accurate. Also returns `404` on cross-namespace or unknown name (not mentioned but obvious from the namespace-gating note on line 65).

9. **Line 79 — `IngestRequest` schema `{collection, path?, documents?, ingested_by}`**: Mostly accurate. Two nuances:
   - `ingested_by` in the request body is always overwritten server-side from the `X-Ingested-By` header (defaulting to `"archon-search-cli"`), so the request-body field is effectively ignored (`routes_jobs.py` lines 98–99). The doc lists it as part of the schema, which is misleading.
   - `collection` validator rejects empty string but not whitespace-only (`if not v` — only catches empty string), unlike `SearchRequest.collection` which uses `.strip()`. Minor inconsistency the doc doesn't surface.

10. **Line 81 — "Terminal jobs return `200` (idempotent); active jobs transition to `CANCELLING` and return `202`"**: Accurate, but incomplete. Jobs already in `CANCELLING` also return `202` (line 149–151 of `routes_jobs.py`). The doc's "active jobs" wording could be read to exclude `CANCELLING`, but the code paths are: terminal→200, active(`RUNNING`/`PENDING`)→`CANCELLING`+202, already-`CANCELLING`→202, unknown→500.

11. **Line 89 — `/telemetry/stats` query params `since, until`**: Accurate. Doc does not mention `400` on invalid date ordering (raised from `reader.resolve_dates`), which both telemetry endpoints can return.

12. **Line 98 — MCP `search` arguments**: Accurate (`query: str`, `collection: str | None`).

13. **Line 99 — `search_with_context` default `context_window: int = 1`**: Accurate.

14. **Line 101 — `ingest_directory` `glob_pattern = "**/*"`**: Accurate.

15. **Line 102 — `list_collections` "centroid omitted"**: Accurate (`d.pop("centroid", None)` in `mcp.py` line 171). Doc says returns `list[CollectionMeta-without-centroid]`; the actual return type is `list[dict[str, Any]]` derived via `asdict(CollectionMeta)` minus `centroid` — close enough but not strictly typed as a `CollectionMeta`.

16. **Line 105 — `list_documents` "`collection?`, `limit: int = 100`"**: Accurate.

17. **Line 106 — `delete_document` "`doc_id: str`, `collection?`" returns `{"deleted": int}`**: Accurate.

18. **Line 117 — "Every subcommand accepts `--config <path>` unless noted"**: Inaccurate. `stop` and `status` do **not** accept `--config` (confirmed in `cli/stop.py` and `cli/status.py` — neither has the option). The doc table notes "—" for `stop` and `status` flags, so this is internally inconsistent: the prose says "unless noted" but the table only notes by leaving Key flags empty without a `--no-config` disclaimer. Either the prose or the table should be tightened.

19. **Line 126 — `ingest` defaults to `~/.archon-search/history/sessions`**: Accurate.

20. **Line 129 — "`add <path>`"**: Accurate.

21. **Line 130 — "`remove <path>`"**: Accurate (Click `@click.argument("path")` — takes a path, not a name).

22. **Line 130 — "rejects pinned-only"**: Accurate.

23. **Line 130 — `--dry-run`, `--force` for `collection remove`**: Both flags exist, **but** `--force` is checked only as mutually exclusive with `--dry-run` — it does not actually bypass any check beyond that. The flag's docstring says "Proceed even if service is running" but no service-running check exists in `cli/collection.py::remove`. The doc just listing the flag is technically accurate but obscures that `--force` is effectively a no-op besides the mutex with `--dry-run`.

24. **Line 131 — "`info <name>`"**: Accurate.

25. **Line 132 — "`reindex <name>` — Clear state, drop table, re-ingest from source path"**: Accurate.

26. **Lines 133–135 — `config show`/`get`/`set`**: Accurate. Doc says `set` supports "bool/int/float coercion" — confirmed (lines 116–127 of `config_cmd.py`). The doc does not mention that `get` requires exactly `section.field` (two-part dotted key) and errors otherwise; not load-bearing.

27. **Line 124 — `install` "register and start service, poll `/health` until ready"**: Accurate. Doc omits that `install` aborts (exit 1) if health check fails within `_HEALTH_TIMEOUT = 60` seconds — significant for operators.

28. **Line 125 — `uninstall` "`--delete-db`"**: Accurate.

29. **Line 11 — "Both REST and MCP run through the same `APIKeyMiddleware`"**: Accurate — `mcp.py::create_mcp_http_app` wraps the streamable HTTP app with `APIKeyMiddleware(api_key=api_key, namespaces={})`. **However**, note that the MCP app is constructed with an **empty `namespaces={}`** dict (line 251 of `mcp.py`), unlike the REST app which passes `config.namespaces`. This means namespace-scoped keys configured in `[namespaces]` do not authenticate on the MCP transport — only the default bootstrap key works. The doc's "same auth model everywhere" framing glosses over this.

30. **Line 14 — "cross-namespace access yields `404`, never `403`"**: Accurate at the route level (confirmed in `routes_collections.py`, `routes_jobs.py`). MCP tools, however, do not appear to perform namespace gating at all (they use `pipeline.search(...)` without a `namespace=` argument in most places — e.g. `mcp.py` line 46). This contradicts the doc's claim that namespace enforcement is consistent across surfaces. Worth flagging.

31. **Line 21 — "Only `/health` actually appears in the OpenAPI schema; the others are defensive (FastAPI never includes them)"**: Accurate; matches the `_configure_openapi` comment in `app.py`.

32. **Line 139 — "schema is built in `archon_search/server/app.py::_configure_openapi`"**: Accurate.

## Verified claims

- All eight REST route module names (`routes_health.py`, `routes_state.py`, `routes_status.py`, `routes_search.py`, `routes_route.py`, `routes_collections.py`, `routes_jobs.py`, `routes_telemetry.py`) exist and contain the documented endpoints.
- `GET /health` returns `HealthResponse{status, version}` and is unauthenticated.
- `GET /indexing-state` is namespace-filtered.
- `GET /status` returns PID, version, per-collection progress with ETA, namespace-filtered.
- `POST /search` request schema `{collection, query, top_k}` matches `SearchRequest`.
- `POST /search` response is `{results: [...], acl_filtered: bool}` — matches `SearchResponse`.
- `POST /route` accepts `{query, slots?}`, returns `{pre_context, pinned_names, routable_names, decomposer_invoked}`, 30s timeout → 504, validation errors → 400.
- 9 MCP tools registered in `mcp.py` with exactly the names listed: `search`, `search_with_context`, `ingest_file`, `ingest_directory`, `list_collections`, `get_collections_meta`, `get_collection_meta`, `list_documents`, `delete_document`.
- MCP `search` returns `{"results": [...], "acl_filtered": bool}` (not bare list).
- BREAKING.md confirms both breaking-change notes (`top_k` ignored; `search` shape changed).
- CLI entry point `archon-search` is a Click group with subcommands: `start`, `stop`, `status`, `install`, `uninstall`, `ingest`, `sync`, `collection` (with `list/add/remove/info/reindex`), `config` (with `show/get/set`).
- `APIKeyMiddleware` uses `secrets.compare_digest` and iterates all namespace entries without early exit.
- `_EXEMPT_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}`.
- 401 responses include `WWW-Authenticate: Bearer`.
- DELETE `/jobs/{job_id}` semantics: terminal→200, active→CANCELLING+202.
- DELETE `/collections/{name}` returns 409 for pinned-only.
- Telemetry endpoints return `DisabledResponse{enabled: false}` when telemetry is off.
- BearerAuth security scheme is injected by `_configure_openapi` for all non-exempt paths.

## Unverifiable / ambiguous

- **Line 5 — "Last reviewed: 2026-05-20"**: Cannot verify whether a review actually occurred on that date; the doc's `Status: Draft` line is consistent with an unreviewed draft.
- **Line 12 — "CalVer segments do not encode compatibility"**: Project convention claim; not a code-verifiable assertion but consistent with `010_engineering_principles_and_constraints.md` per the CLAUDE.md doc map.
- **Line 18 — "All endpoints except `GET /health` require ...Bearer..."**: Verified for paths that exist in the FastAPI app; cannot exhaustively verify across third-party mounts (none observed beyond the MCP transport).
- **Line 113 — "REST and MCP intentionally not 1:1"**: True observation, but a stronger statement than the code "proves" — the asymmetry could equally be unintentional drift. The doc's framing is editorial.
- **Line 137 — "If this document diverges from `/openapi.json`, the schema wins — and a follow-up doc fix is required"**: Policy claim, not code-verifiable.
- The CLI table column heading "Key flags" is implicitly excluding the `<arg>`/`<name>`/`<path>` positional arguments. Whether the omission of `--non-interactive`, `--dry-run` semantics in `install` matters is editorial — the flags themselves are listed.
