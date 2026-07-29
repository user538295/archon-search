**Purpose**: Explain how `archon-search` resolves API keys, how to obtain or override the default key, and how per-namespace keys work for client integrations.
**Audience**: Engineers wiring up an HTTP or MCP client to a running `archon-search` instance.
**Status**: Draft
**Last reviewed**: 2026-05-20 / **Next review**: 2027-05-20

# Authentication

`archon-search` uses a single auth scheme: `Authorization: Bearer <token>`. The same middleware (`archon_search/server/middleware_auth.py`) protects both the REST routes and the MCP endpoint at `/mcp`. #Unverified — MCP middleware wiring not directly confirmed in this review; would need inspection of `archon_search/server/app.py` and `archon_search/server/mcp.py`. Exemption is by path, not method: `_EXEMPT_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}`, and *any* HTTP method to these paths bypasses auth. `/docs`, `/openapi.json`, and `/redoc` are real, reachable FastAPI-mounted endpoints when docs are enabled (they are not listed in the `paths` of the OpenAPI document itself, but their handlers are served).

## Principles

1. **One header, every request.** Both REST and MCP read the same `Authorization: Bearer <token>` header. There is no cookie, no query-string fallback, no per-tool token.
2. **Hex tokens only.** The key file and the env var must contain a lowercase hex string. The validator is `^[0-9a-f]+$` applied via `fullmatch` (`key_manager.py::_HEX_RE`, used by `_validate_key`). Anything else is logged and rejected.
3. **Constant-time comparison within the namespace map.** Tokens are compared with `secrets.compare_digest` against every entry in the namespace map without an early exit (`middleware_auth.py`, the loop intentionally omits `break` — see the `# no break` comment on the `resolved_namespace = ns` assignment). The fallback to the default key is then short-circuited via `and`, so the timing guarantee is uniform *across the namespace map* but is not strictly indistinguishable between "matched in map" and "fell through to default-key check".
4. **The token resolves a namespace.** On success, the middleware writes the namespace name to `request.state.namespace`. Every handler filters by that value; cross-namespace access surfaces as `404`. #Unverified — the per-handler filtering and 404 behavior were not confirmed in this review (would require inspecting `routes_collections.py`, `routes_jobs.py`, `routes_search.py`, and the ACL module).
5. **Failures are uniform.** `401` with `WWW-Authenticate: Bearer` and no body — same response for missing header, wrong scheme, or unknown token.

## Where the default key lives

Resolution order in `archon_search/key_manager.py::load_or_generate_key`:

1. `ARCHON_SEARCH_API_KEY` environment variable, if set and hex-valid.
2. `~/.archon-search/.search.env` (or the path in `ARCHON_SEARCH_KEY_FILE`), parsing the `ARCHON_SEARCH_API_KEY=...` line.
3. Auto-generate a 64-char hex token (`secrets.token_hex(32)`), write it durably to the key file with mode `0o600` set at creation via `_durable_io.atomic_write_bytes` (fsync → `os.replace` → fsync parent dir; no chmod-after-rename window), and return it.

The key file is created lazily on first server start. Its layout is intentionally `.env`-shaped so you can `source` it in a shell:

```
ARCHON_SEARCH_API_KEY=ab12...cd34
```

On non-Windows platforms, when the key file is read the loader checks its mode and only calls `_chmod_600` if the current mode differs from `0o600` (`key_manager.py::_load_from_file`); a correctly-permissioned file is not re-chmodded on each read.

### Reading the key from a client

The simplest pattern is to read the env file once at startup.

Python (no dependency):

```python
from pathlib import Path

def load_archon_key() -> str:
    p = Path.home() / ".archon-search" / ".search.env"
    for line in p.read_text().splitlines():
        if line.startswith("ARCHON_SEARCH_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("archon-search key not found")
```

Shell:

```bash
set -a; source ~/.archon-search/.search.env; set +a
curl -H "Authorization: Bearer ${ARCHON_SEARCH_API_KEY}" http://127.0.0.1:8765/health
```

### Overriding the default key

- Set `ARCHON_SEARCH_API_KEY=<hex>` before starting the server. The file is not consulted in that case — the resolution order is implemented by `key_manager.py::load_or_generate_key`, which returns early on an env-var hit.
- Or set `ARCHON_SEARCH_KEY_FILE=/path/to/other.env` to redirect both reads and the bootstrap write. The path is `~`-expanded.

Generate a fresh key yourself with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

## The Bearer header

Every call except `GET /health` must include:

```
Authorization: Bearer <hex token>
```

A missing header, a non-`Bearer` scheme, or a token that fails `compare_digest` against every configured key returns:

```
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer
```

There is no body and no `detail` field on `401` responses — that envelope is reserved for application-level errors (see `06_error_handling.md`).

## Namespaces and per-namespace keys

`archon-search` supports multi-tenant key segregation through a `[namespaces]` map in `archon-search.toml`:

```toml
[namespaces]
"7e3a9c..." = "team-alpha"
"4f1b2d..." = "team-beta"
```

Each entry maps a token (hex string) to a namespace name. The middleware iterates every entry in this map without an early `break`, keeping comparison time independent of which token matched *within the map*. It then falls back to the default key (this fallback is short-circuited via `and`, so timing is not strictly uniform between a map hit and a default-key check), which resolves to `DEFAULT_NAMESPACE = "default"` (`archon_search/constants.py:12`).

Consequences for clients:

- **A client picks a namespace by picking a key.** There is no `?namespace=` parameter. The token *is* the tenant identity.
- **Collections and jobs are namespace-scoped server-side.** `GET /collections/`, `GET /jobs/{id}`, and `POST /search` only see resources that belong to the caller's namespace. Cross-namespace access returns `404`.
- **Namespace names are validated.** `_validate_namespace` (`constants.py:17`) restricts them to `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$` and additionally rejects the reserved name `"deny-all"`; an invalid configured namespace produces a `500` from the middleware. Treat namespace names as opaque from the client side.

If you need to support multiple teams with one process, give each team a distinct token from the `[namespaces]` map. Do not share the default `~/.archon-search/.search.env` key between tenants — that key always resolves to `default`.

## Verifying auth from a client

A copy-paste smoke test:

```bash
# Should return 200 without auth.
curl -sf http://127.0.0.1:8765/health

# Should return 401.
curl -si http://127.0.0.1:8765/collections/ | head -1

# Should return 200 with JSON body.
curl -sf -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  http://127.0.0.1:8765/collections/
```

If the third call returns `401`, your token does not match. Re-read the file (it may have been regenerated) and confirm the running server's PID against `archon-search status`.

## Related documents

- [`../Architecture/150_security_and_privacy_architecture.md`](../Architecture/150_security_and_privacy_architecture.md) — full auth/ACL/privacy model.
- [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) — endpoint reference.
- [`06_error_handling.md`](./06_error_handling.md) — status codes and error envelopes.
- [`../UserManual/30_configuration.md`](../UserManual/30_configuration.md) — `[namespaces]` config block.
