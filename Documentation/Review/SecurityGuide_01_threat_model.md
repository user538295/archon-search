# Review: SecurityGuide/01_threat_model.md

## Summary

The threat model is largely accurate against `archon_search/` source. File paths, line-number anchors (`middleware_auth.py:25`, `:41–47`, `key_manager.py:54–59`, `config.py:209–217`), default loopback bind, key auto-generation flow, and the structural absence of `query` on `TelemetryEntry` all check out. Two notable inaccuracies center on the MCP surface: (1) MCP is not "the same FastAPI app" as REST — `create_mcp_http_app` builds a separate `Starlette` app from a FastMCP server, and (2) it is constructed with `namespaces={}`, so the `[namespaces]` map only authorizes against REST, not MCP. Several smaller exemption-list / wording gaps round out the list.

## Inaccuracies (numbered)

1. **"MCP is not a privileged surface — it shares the REST auth layer." / "Same FastAPI app, same middleware, same key check (`archon_search/server/mcp.py`)"** (trust-boundaries table, row "REST vs MCP").
   - Source: `archon_search/server/mcp.py:237–252`. `create_mcp_http_app` returns a separate `Starlette` app built from `fastmcp_app.streamable_http_app()`; it only shares the *class* `APIKeyMiddleware`, not the same app instance or the same middleware stack as the FastAPI REST app constructed in `archon_search/server/app.py:120–122`. They are two distinct ASGI apps that happen to use the same key file via independent calls to `load_or_generate_key()`.

2. **MCP honors the `[namespaces]` map.** Implied by the trust-boundaries diagram (`KEYS → REST` and `KEYS → MCP`) and by listing "Per-namespace keys" as an asset that applies to both surfaces.
   - Source: `archon_search/server/mcp.py:251` — `starlette_app.add_middleware(APIKeyMiddleware, api_key=api_key, namespaces={})`. The MCP middleware is constructed with an empty namespaces dict, so namespace bearer tokens authenticate REST only. On MCP, only the default key is accepted; every authenticated request resolves to `DEFAULT_NAMESPACE`.

3. **"Bearer auth on every non-`/health` endpoint"** (In-scope section) and **"All endpoints except `GET /health` require a Bearer token"** (echoed elsewhere).
   - Source: `archon_search/server/middleware_auth.py:16` — `_EXEMPT_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})`. Four paths are exempt, not one. `app.py:65–66` notes that `/docs`, `/openapi.json`, `/redoc` are "defensive" because FastAPI does not include them in the OpenAPI schema, but they *are* exempt at the middleware layer and reachable unauthenticated when FastAPI mounts them.

4. **Threat-model line range `config.py:209–217` for "silently coerced to `false`".**
   - The coercion lives at lines 209–217 as claimed, but the document also calls this "silent" while `config.py:214` emits `_logger.warning("telemetry: export_enabled is reserved for a future release and will be ignored")`. It is logged, not silent. Minor wording inaccuracy.

5. **"`load_or_generate_key()` returns exactly one key"** under "Single-tenant, single-process assumption".
   - Source: `archon_search/key_manager.py:25–36`. It returns a `(key, source)` tuple, not just a key. Editorial nit, but the claim "exactly one key" is true.

## Verified claims

- LanceDB path `~/.archon-search/search/` and schema home (`archon_search/store.py`): consistent with `app.py:128–129` (`SearchStore(config.db_path)`).
- API key file default `~/.archon-search/.search.env`, owner-only `0600`, `ARCHON_SEARCH_KEY_FILE` override: `key_manager.py:14–19, 89, 131, 135–143`.
- `ARCHON_SEARCH_API_KEY` env overrides file: `key_manager.py:27–32, 39–46`.
- Key file permission re-tightening at `key_manager.py:54–59`: matches doc.
- `secrets.compare_digest` + non-short-circuiting loop at `middleware_auth.py:41–47`: matches doc (comment on line 39 explicitly states "no early exit — prevents timing leakage"; loop body sets `resolved_namespace = ns` with `# no break`).
- `APIKeyMiddleware.dispatch` at `middleware_auth.py:25`: matches.
- Default bind `127.0.0.1:8765`: `config.py:30–31`.
- Telemetry log dir default `~/.archon-search/search-logs`: `config.py:24`.
- `[telemetry].export_enabled = true` coerced to `False`: `config.py:209–217`.
- `TelemetryEntry` has no `query` field and no factory accepts a `query` argument: `archon_search/telemetry/entry.py:57–145`. All three factories (`from_search_tool_result`, `from_route_response`, `from_error`) are keyword-only and do not list `query`.
- `apply_acl_filter` exists in `archon_search/acl.py` (line 201) and is the ACL gate.
- Per-chunk ACL stored as nullable list of utf8: consistent with `acl.py` semantics (`list[str] | None`); LanceDB column-level confirmation would require reading `store.py` schema, but the API contract is consistent.
- 9 MCP tools registered in `mcp.py`: grep finds exactly 9 `@app.tool()` decorators (lines 38, 76, 124, 139, 163, 178, 188, 200, 215).
- Single-process LanceDB invariant: consistent with codebase using a single `SearchStore` instance per app (`app.py:129`).

## Unverifiable / ambiguous

- "ACL lives on every chunk row as a nullable `list<utf8>`." — Plausible from `acl.py` semantics; the exact LanceDB column declaration in `store.py` was not opened during this review.
- "Generated by `archon_search/description_generator.py`." — File exists in the package listing; content not verified in this pass.
- `SEC-1`, `SEC-2`, `SEC-3`, `TEL-1` cross-references to the tech-debt register — not opened in this review (per rule, do not trust `Documentation/`).
- "`release.sh` performs PyPI signature checks" — referenced under out-of-scope supply chain; `release.sh` was not opened in this review.
- Claim "Each [namespace] entry is a static bearer token" loaded into `APIKeyMiddleware._namespaces` — the *REST* side wiring matches (`app.py:121` passes `config.namespaces`). However see Inaccuracy 2: this does not apply to MCP.
