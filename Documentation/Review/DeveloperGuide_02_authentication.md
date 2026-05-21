# Review: DeveloperGuide/02_authentication.md

Sources verified against:
- `archon_search/key_manager.py`
- `archon_search/server/middleware_auth.py`
- `archon_search/config.py`
- `archon_search/constants.py`

## Summary

The document is largely accurate. The auth scheme, header format, token format, file path, env var names, permission mode, namespace mapping, and 401 response shape all match the source. A handful of small wording inaccuracies remain — primarily around method-scoping of the exempt path, the framing of the chmod-on-read behavior, the visibility of `/openapi.json` and `/docs` in FastAPI, and one minor mischaracterization of how the validator is applied.

## Inaccuracies (numbered)

1. **Line 8 — "The only exempt path is `GET /health`"** is wrong on two counts.
   - Exemption in `middleware_auth.py` is by path, not method: `if request.url.path in _EXEMPT_PATHS`. *Any* HTTP method (POST, OPTIONS, etc.) to `/health` skips auth.
   - `_EXEMPT_PATHS` is `{"/health", "/docs", "/openapi.json", "/redoc"}` — `/docs`, `/openapi.json`, and `/redoc` are also exempt, not just defensive. FastAPI does serve `/openapi.json` and `/docs` by default (these are real, reachable endpoints when docs are enabled), so the parenthetical "FastAPI never includes them in the OpenAPI schema" is misleading — they are reachable URLs whose handlers FastAPI mounts; they simply are not themselves listed as paths in `paths` of the OpenAPI document.

2. **Line 13 — "The validator is `^[0-9a-f]+$`"** — the regex literal is correct (`_HEX_RE = re.compile(r"^[0-9a-f]+$")`), but it is applied via `fullmatch`, not `match`/`search`. The behavioral outcome is the same here because the pattern is already anchored, so this is a minor stylistic note rather than a functional inaccuracy.

3. **Line 14 — "Tokens are compared … against every entry in the namespace map (no early exit) plus the default key"** — accurate, but the citation `middleware_auth.py:42` points to the `compare_digest` call *inside* the loop. The "no early exit" property is implemented by the absence of a `break` on line 43 (`resolved_namespace = ns  # no break`). The fallback to the default key on line 46 *does* short-circuit (uses `and`), so the constant-time claim is only fully held across the namespace map; a hit in the map plus a miss on the default key, vs. two misses, are not strictly indistinguishable. The doc's framing is close enough but slightly stronger than what the code guarantees.

4. **Line 32 — "the file is forced to mode `0o600` every time it is read"** — the code only calls `_chmod_600` when the current mode differs from `0o600` (`if mode != 0o600: _chmod_600(KEY_FILE)`). A correctly-permissioned file is not re-chmodded on each read.

5. **Line 60 — "The file is not consulted in that case (`key_manager.py::_load_from_env`)"** — accurate as a statement about resolution order (`load_or_generate_key` returns early on env-var hit), but the cited symbol `_load_from_env` does not itself implement the "don't consult the file" behavior; that ordering lives in `load_or_generate_key`. Minor: cite `load_or_generate_key` instead.

6. **Line 96 — "every entry in this map (no break, to keep comparison time independent of which token matched), then falls back to the default key"** — same caveat as item 3: the default-key check is short-circuited by `and`, so timing across the full check is not strictly uniform between "matched in map" and "matched default". The "no break" property only holds within the namespace loop.

7. **Line 102 — "an invalid configured namespace produces a `500` from the middleware"** — accurate. Note that `_validate_namespace` *also* rejects the reserved name `"deny-all"` (raises `ValueError`); the doc does not mention this. Not strictly an inaccuracy, but a gap.

## Verified claims

- Single auth scheme: `Authorization: Bearer <token>`, used for both REST and MCP via the same middleware (`APIKeyMiddleware` in `middleware_auth.py`). ✓
- Token validator regex: `^[0-9a-f]+$` lowercase hex (`_HEX_RE` in `key_manager.py`). ✓
- Resolution order: env var → key file → auto-generate (`load_or_generate_key`). ✓
- Env var name: `ARCHON_SEARCH_API_KEY` (`ENV_VAR` in `key_manager.py`). ✓
- Override env var for key file path: `ARCHON_SEARCH_KEY_FILE`, with `expanduser()` applied. ✓
- Default key file path: `~/.archon-search/.search.env`. ✓
- Auto-generated key: `secrets.token_hex(32)` → 64 hex chars; written via tmp file + `os.replace` with `0o600`. ✓
- Key file line format: `ARCHON_SEARCH_API_KEY=<hex>` (parsed by `line.startswith(f"{ENV_VAR}=")`). ✓
- Comparison primitive: `secrets.compare_digest`. ✓
- 401 response shape: `Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})`, no body. ✓
- "No `detail` field on 401 responses": correct — the middleware returns an empty body, not the FastAPI default JSON envelope. ✓
- Successful auth writes `request.state.namespace`. ✓
- `DEFAULT_NAMESPACE = "default"` and is at `constants.py:12`. ✓
- `_validate_namespace` defined at `constants.py:17`. ✓
- Invalid configured namespace → 500. ✓
- Bearer scheme check is case-sensitive: `parts[0] != "Bearer"` ("bearer" is rejected). ✓
- Header parser uses `split(" ", 1)` and requires exactly two parts — so empty header, missing token, or extra parts return 401. ✓
- `[namespaces]` is loaded as `dict[str, str]` in `config.py::load_config` (non-string keys/values → `ConfigError`). ✓
- `_chmod_600` is a no-op on Windows (`sys.platform == "win32"`); the doc's "on non-Windows platforms" qualifier is correct. ✓

## Unverifiable / ambiguous

- **MCP endpoint at `/mcp` sharing the same middleware** — not verified directly in this review (only `middleware_auth.py`, `key_manager.py`, `config.py`, `constants.py` were inspected). The CLAUDE.md project context asserts this is so, and the middleware is unconditional on path (except `_EXEMPT_PATHS`), so any router mounted on the app — including MCP — will pass through it. To verify, inspect `archon_search/server/app.py` and `archon_search/server/mcp.py`.
- **"Collections and jobs are namespace-scoped server-side. … Cross-namespace access returns `404`"** (line 101) — out of scope for the files inspected; would need to check the route handlers (`routes_collections.py`, `routes_jobs.py`, `routes_search.py`) and the ACL module.
- **"There is no cookie, no query-string fallback, no per-tool token"** (line 12) — verified for the middleware (it only reads the `Authorization` header), but a global statement about the whole app would require checking that no route handler reads an alternative credential.
- **Smoke-test example** (line 110+) assumes default host/port `127.0.0.1:8765`. These match `SearchConfig` defaults in `config.py`, but a user with a customized `[server]` block would need different values. Not an inaccuracy, just an unstated assumption.
- **Line 84 reference to `06_error_handling.md`** — existence and contents of that sibling doc were not verified.
