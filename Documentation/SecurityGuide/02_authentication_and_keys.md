**Purpose**: Document how `archon-search` authenticates clients, where keys live, and what the current model does not yet do.
**Audience**: Security engineers, IT admins operating the service.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2027-05-20

# Authentication and Keys

`archon-search` authenticates every non-`/health` request with a static bearer token. There is a single default key plus an optional map of additional keys, one per namespace. There is no rotation, expiry, or revocation primitive in v1.

For context on where this fits, see [`01_threat_model.md`](./01_threat_model.md) and [`../Architecture/150_security_and_privacy_architecture.md`](../Architecture/150_security_and_privacy_architecture.md).

## Principles

1. **One default key, optional per-namespace map.** The minimum useful model; nothing more.
2. **Key file is owner-only.** Mode `0600` on creation, and re-tightened on every read.
3. **Constant-time comparison and no early exit.** Timing-side-channel exposure is bounded.
4. **No silent key minting on disk.** Auto-generation happens once; subsequent starts reuse the file.

## Authentication model

The middleware lives at `archon_search/server/middleware_auth.py`. Two key sources are checked, in order:

1. **Per-namespace map.** Every entry in `config.namespaces` (`<key_hex> = <namespace>`) is compared against the incoming token. The loop iterates all entries without an early `break` (`middleware_auth.py:40–43`) — deliberate, to avoid leaking which key prefix matched via timing.
2. **Default key fallback.** If no namespace key matched, the token is compared against the single default key returned by `load_or_generate_key()`. A match here stamps the namespace as `DEFAULT_NAMESPACE` (`archon_search/constants.py`).

A failure on both paths returns `401` with `WWW-Authenticate: Bearer` and an empty body. A success stamps `request.state.namespace`, which the rest of the app uses to scope reads, writes, and ACL filtering.

Exempt paths (no auth): `/health`, `/docs`, `/openapi.json`, `/redoc` (`middleware_auth.py:16`). Only `/health` exposes runtime data; the other three are schema only and are not even included in the OpenAPI `paths` table at runtime (`archon_search/server/app.py:67–69`). #Unverified — the runtime-data vs schema-only distinction was not cross-checked against `routes_health.py`.

Token comparison uses `secrets.compare_digest` end-to-end (`middleware_auth.py:42`, `46`).

## Key bootstrap

Resolution order in `archon_search/key_manager.py::load_or_generate_key`:

1. **`ARCHON_SEARCH_API_KEY` env var.** Must be a non-empty lowercase hex string (`^[0-9a-f]+$`). Invalid values are logged as a warning and ignored; the loader falls through.
2. **Key file.** Default `~/.archon-search/.search.env`. The path can be overridden by setting `ARCHON_SEARCH_KEY_FILE` **before `archon_search.key_manager` is imported** — the variable is read once at module import time (`key_manager.py:14`) and frozen into the module-global `KEY_FILE`. Setting it after import has no effect. The file must contain a line `ARCHON_SEARCH_API_KEY=<hex>`.
3. **Auto-generation.** `secrets.token_hex(32)` produces 64 hex characters. The key is written via `os.open(..., O_WRONLY | O_CREAT | O_EXCL, 0o600)` to a temp file in the same directory, then `os.replace`d into place, and finally re-chmodded to `0o600` via `_chmod_600(KEY_FILE)` at `key_manager.py:131` to guarantee the final mode after the rename. Concurrent first-start writers are handled by retrying via `_load_from_file()` (`key_manager.py:87–114`).

## File permissions

| Aspect | Behavior | Source |
| --- | --- | --- |
| Creation mode | `0600` requested via the explicit mode argument to `os.open(..., O_WRONLY \| O_CREAT \| O_EXCL, 0o600)` (subject to umask); `O_EXCL` guarantees exclusivity, not mode. The final mode is re-applied via `_chmod_600(KEY_FILE)` after `os.replace` (`key_manager.py:131`). | `key_manager.py:89`, `key_manager.py:131` |
| Re-tighten on read | If `stat & 0o777 != 0o600`, the loader chmods back to `0o600`. `OSError` from `stat`/`chmod` is silently swallowed (`except OSError: pass`); no log is emitted on failure. | `key_manager.py:54–59` |
| Windows | `_chmod_600` skips chmod on `win32` and logs "permission check skipped". | `key_manager.py:135–138` |

The directory `~/.archon-search/` is created with `os.makedirs(..., exist_ok=True)` and inherits the user's umask; the operator should ensure the parent directory is not group/world-readable on shared hosts.

## Environment overrides

| Variable | Effect | Validation |
| --- | --- | --- |
| `ARCHON_SEARCH_API_KEY` | When **valid**, used as the default key for the lifetime of the process and the file is not consulted. When **invalid**, the loader logs a warning and falls through to the file, then to auto-generation. | Must match `^[0-9a-f]+$`. Invalid → warning and fall-through. |
| `ARCHON_SEARCH_KEY_FILE` | Read/write the key file at this path instead of the default. **Resolved at module import time only** (`key_manager.py:14`); setting it after import has no effect. | No content validation here; the file content is validated by the normal loader. |

There is no env override for the per-namespace map — those are loaded from `[namespaces]` in `archon-search.toml` (`archon_search/config.py:225–233`).

## Per-namespace keys

The `[namespaces]` block maps a hex key to a namespace identifier:

```toml
[namespaces]
"deadbeef…" = "team-a"
"cafef00d…" = "team-b"
```

Each entry adds another bearer token that, when presented, scopes the request to its namespace. Keys are validated only for type at config load (string-to-string); the format check (`^[0-9a-f]+$`) is not currently re-applied at this layer — invalid hex will simply never match a token, with no early diagnostic. Namespace values are validated by `_validate_namespace` at request time (`middleware_auth.py:56`); a malformed value returns `500` to the client and logs an error.

## Current limitations

The v1 model deliberately omits several features that production deployments typically need. They are tracked, not hidden:

- **No rotation.** There is no in-process API to swap the key. Restart with a new file or env var is the only path. Tracked as `SEC-1` in [`../Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md) and as item **D7** in [`../Backlog/03_world_class_roadmap.md`](../Backlog/03_world_class_roadmap.md). #Unverified — these references were not cross-checked against the linked files.
- **No expiry.** Keys live forever once issued.
- **No revocation list.** Removing a leaked per-namespace key requires editing the config file and restarting.
- **No audit log of authentication failures beyond the application log.** Failures log at `debug` (`middleware_auth.py:62` is only on success); 401s are not separately recorded.
- **No CIDR / origin restrictions inside the middleware.** Bind address and reverse proxy are the only network controls — see `05_network_exposure_and_tls.md`.

## Manual rotation procedure

Rotation is a stop-edit-start sequence:

1. **Stop the server.** `archon-search stop` (or stop the OS service).
2. **Rotate the default key.** Edit `~/.archon-search/.search.env` and replace the `ARCHON_SEARCH_API_KEY=<hex>` line with a new lowercase hex string. To generate a fresh value: `python -c "import secrets; print(secrets.token_hex(32))"`.
3. **Rotate per-namespace keys** by editing the `[namespaces]` block in `~/.archon-search/archon-search.toml`. Remove the old entry and add the new one — both per-namespace keys remain valid only if both entries remain in the file. Note: the default key from `~/.archon-search/.search.env` is a single value, so old and new *default* keys cannot be valid concurrently — the file holds only one default at a time.
4. **Verify permissions.** `ls -l ~/.archon-search/.search.env` should show `-rw-------`. If not, `chmod 600` it before restart; the loader will also fix it on next read.
5. **Restart the server.** `archon-search start`. The startup log line `API key authentication enabled (source: …)` confirms which source was used (`app.py:123`).
6. **Update clients.** Any client still presenting the old key now receives `401`.

The rotation is atomic at the *file* level (writes to the file replace it) but not atomic across clients — there is a window during step 5 in which clients with the old key will fail. There is no overlap mode in v1.

## Verifying authentication is working

```bash
# Should return 401 with WWW-Authenticate: Bearer
curl -i http://127.0.0.1:8765/state

# Should return 200 with JSON state
curl -i -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" http://127.0.0.1:8765/state

# /health is intentionally open
curl -i http://127.0.0.1:8765/health
```

If `/state` returns `200` without a header, the middleware is misconfigured — check `app.py` is calling `app.add_middleware(APIKeyMiddleware, …)` and that no later middleware short-circuits the chain.

## Related documents

- [`01_threat_model.md`](./01_threat_model.md) — scope and trust boundaries.
- [`03_authorization_and_acl.md`](./03_authorization_and_acl.md) — what the namespace is used for once auth succeeds.
- [`05_network_exposure_and_tls.md`](./05_network_exposure_and_tls.md) — where bearer auth stops being sufficient.
- [`../Architecture/150_security_and_privacy_architecture.md`](../Architecture/150_security_and_privacy_architecture.md) — broader architecture context.
- [`../Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md) — `SEC-1`.
- [`../Backlog/03_world_class_roadmap.md`](../Backlog/03_world_class_roadmap.md) — item D7.
