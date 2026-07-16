**Purpose**: Document how `archon-search` authenticates clients, where keys live, and what the current model does not yet do.
**Audience**: Security engineers, IT admins operating the service.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2027-05-20

# Authentication and Keys

`archon-search` authenticates every request with a bearer token, except a small set of exempt paths: `/health`, `/ready`, `/docs`, `/openapi.json`, `/redoc`. D7 added a durable multi-key store (`KeyStore` in `key_manager.py`) backed by `~/.archon-search/keys.json`. Operators can now issue, revoke, and rotate API keys **without restarting the server**. The legacy single-key path (env var + TOML `[namespaces]`) is unchanged and continues to work with zero config changes.

For context on where this fits, see [`01_threat_model.md`](./01_threat_model.md) and [`../Architecture/150_security_and_privacy_architecture.md`](../Architecture/150_security_and_privacy_architecture.md).

## Principles

1. **One default key, optional per-namespace map.** The minimum useful model; nothing more. **D7** adds a durable multi-key store as an additive layer.
2. **Key file is owner-only.** Mode `0600` on creation, and re-tightened on every read. `keys.json` is also written with mode `0600`.
3. **Constant-time comparison.** Timing-side-channel exposure is bounded. Managed-key loop exits early on match (constant-time per-comparison, not per-key-count); TOML namespace loop retains its no-early-exit design.
4. **No silent key minting on disk.** Auto-generation happens once; subsequent starts reuse the file.
5. **Raw tokens never stored.** Managed keys store only the SHA-256 hex digest (`token_hash`). The raw token is printed once at creation time and is never retrievable.

## Authentication model

The middleware lives at `archon_search/server/middleware_auth.py`. Three key sources are checked, in order (D7 dispatch order):

1. **Managed keys (`KeyStore.active_keys()`).** `active_keys()` re-reads `keys.json` from disk on every call (no in-memory cache; ~<1 ms I/O for the typical small file). The token hash is computed once per request (`sha256(token).hexdigest()`), then compared with `hmac.compare_digest` against each managed key's `token_hash`. The loop exits on first match. Expired keys (`expires_at <= now`) and revoked keys (`status="revoked"`) are excluded by `active_keys()`.
2. **TOML `[namespaces]` map.** Every entry in `config.namespaces` (`<key_hex> = <namespace>`) is compared against the incoming token. The loop iterates all entries without an early `break` — deliberate, to preserve the timing-safe design from before D7.
3. **Default key fallback.** The token is compared against the single default key returned by `load_or_generate_key()`. **Rotation-revocation guard**: before accepting via this path, the middleware checks whether the token hash matches a revoked or expired managed key record in `keys.json`; if found, `401` is returned even if the raw token matches `_api_key`. A match on all other paths stamps the namespace as `DEFAULT_NAMESPACE` (`archon_search/constants.py`).

A failure on all paths returns `401` with `WWW-Authenticate: Bearer` and an empty body. A success stamps `request.state.namespace`, which the rest of the app uses to scope reads, writes, and ACL filtering.

Exempt paths (no auth): `/health`, `/ready`, `/docs`, `/openapi.json`, `/redoc` (`middleware_auth.py`). Only `/health` and `/ready` expose runtime data; the other three are schema only.

## Key bootstrap

Resolution order in `archon_search/key_manager.py::load_or_generate_key`:

1. **`ARCHON_SEARCH_API_KEY` env var.** Must be a non-empty lowercase hex string (`^[0-9a-f]+$`). Invalid values are logged as a warning and ignored; the loader falls through.
2. **Key file.** Default `~/.archon-search/.search.env`. The path is resolved lazily on every call via `key_manager.get_key_file()`. Two env vars affect it: `ARCHON_SEARCH_KEY_FILE` (highest priority — a specific file path) and `ARCHON_SEARCH_DATA_DIR` (re-roots the default to `$DATA_DIR/.search.env`, used by the container image). Both are read fresh on every call, so setting them after the package is imported (as the container entrypoint does) works as expected. `ARCHON_SEARCH_KEY_FILE` must be an absolute path; empty or whitespace-only values fall through to the `DATA_DIR`-derived default. The file must contain a line `ARCHON_SEARCH_API_KEY=<hex>`.
3. **Auto-generation.** `secrets.token_hex(32)` produces 64 hex characters. The key is written through the durable helper `_durable_io.atomic_write_bytes(key_file, payload, mode=0o600)`, which creates the temp file via `os.open(..., O_WRONLY | O_CREAT | O_EXCL, 0o600)` so the mode is set at creation time (no chmod-after-rename window), fsyncs the file, `os.replace`s it into place, and fsyncs the parent directory. There is no separate post-rename `_chmod_600` call on the write path. Concurrent first-start writers raise `FileExistsError` from `O_EXCL` and are recovered by retrying via `_load_from_file()`. See the [durability contract](../Architecture/130_data_architecture_and_persistence.md#durability-contract) and [ADR-06](../ADRs/06_durable_state_writes_via_fsync.md).

## File permissions

| Aspect | Behavior | Source |
| --- | --- | --- |
| Creation mode | `0600` set at creation via the explicit mode argument to `os.open(..., O_WRONLY \| O_CREAT \| O_EXCL, 0o600)` inside `_durable_io.atomic_write_bytes` (subject to umask); `O_EXCL` guarantees exclusivity. There is no post-`os.replace` re-chmod on the write path — mode-at-creation closes the world-readable window a `write` + `chmod` sequence would open. | `key_manager.py::_generate_and_write`, `_durable_io.py` |
| Re-tighten on read | If `stat & 0o777 != 0o600`, the loader chmods back to `0o600`. `OSError` from `stat`/`chmod` is silently swallowed (`except OSError: pass`); no log is emitted on failure. | `key_manager.py::_load_from_file` |
| Windows | `_chmod_600` (read path only) skips chmod on `win32` and logs "permission check skipped". | `key_manager.py::_chmod_600` |

The directory `~/.archon-search/` is created with `os.makedirs(..., exist_ok=True)` and inherits the user's umask; the operator should ensure the parent directory is not group/world-readable on shared hosts.

## Environment overrides

| Variable | Effect | Validation |
| --- | --- | --- |
| `ARCHON_SEARCH_API_KEY` | When **valid**, used as the default key for the lifetime of the process and the file is not consulted. When **invalid**, the loader logs a warning and falls through to the file, then to auto-generation. | Must match `^[0-9a-f]+$`. Invalid → warning and fall-through. |
| `ARCHON_SEARCH_KEY_FILE` | Read/write the key file at this path instead of the default. Resolved lazily on every call via `key_manager.get_key_file()` (no module import-time capture), so the container entrypoint or test harness can set it after Python imports `archon_search`. | Must be an absolute path; empty/whitespace falls through to the `DATA_DIR`-derived default; `~` is expanded; a tilde with HOME unset raises `ValueError`. |
| `ARCHON_SEARCH_DATA_DIR` | Re-roots the default key file to `$DATA_DIR/.search.env` when `ARCHON_SEARCH_KEY_FILE` is not set. Set this in the container image (`ENV ARCHON_SEARCH_DATA_DIR=/data`) to land the key on the mounted volume. | Validated by `archon_search.paths.get_data_dir()` — must be absolute, non-empty. |

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

Several features remain out of scope:

- **No audit log of authentication failures beyond the application log.** 401s are not separately recorded beyond the application log.
- **No CIDR / origin restrictions inside the middleware.** Bind address and reverse proxy are the only network controls — see `05_network_exposure_and_tls.md`.
- **No per-key ACL scoping.** All managed keys have equal power (no admin vs. client role distinction). Deferred to v2.
- **TOML `[namespaces]` changes require restart.** Managed keys hot-load from disk; TOML namespace tokens are read once at startup.
- **MCP `api_key` not hot-reloaded on rotation.** `POST /keys/rotate` updates `.search.env` and `keys.json`; the MCP server's `APIKeyMiddleware.api_key` is not reloaded until restart (documented limitation S24).
- **`POST /keys/rotate` blocked when `ARCHON_SEARCH_API_KEY` env var is set** — returns `409` (the env var always wins).

Previously tracked as `SEC-1` ("no rotation, expiry, or revocation primitive") in [`../Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md): **resolved by D7**.

## Managed key rotation procedure (D7 — no restart required)

```bash
# Issue a key for a specific namespace
archon-search key create --namespace my-team

# List active keys
archon-search key list

# Revoke a key (prompts for confirmation, showing the key's label if known)
archon-search key revoke <id>

# Revoke without the prompt (scripts / CI)
archon-search key revoke <id> --yes

# Rotate the default key (old key immediately revoked)
archon-search key rotate

# Rotate with a 60-second grace window for in-flight requests to drain
archon-search key rotate --grace 60s
```

The raw token is printed to **stdout only** once at creation time — store it immediately. The same operations are available via `POST /keys`, `GET /keys`, `DELETE /keys/{id}`, and `POST /keys/rotate` REST endpoints, and via the `create_key`, `list_keys`, `revoke_key`, `rotate_key` MCP tools.

## Legacy rotation procedure (requires restart)

For deployments using only the env var / file path (no managed keys):

1. **Stop the server.** `archon-search stop` (or stop the OS service).
2. **Replace the default key.** Edit `~/.archon-search/.search.env` and replace the `ARCHON_SEARCH_API_KEY=<hex>` line with a new lowercase hex string. To generate a fresh value: `python -c "import secrets; print(secrets.token_hex(32))"`.
3. **Rotate per-namespace keys** by editing the `[namespaces]` block in `~/.archon-search/archon-search.toml`. Remove the old entry and add the new one.
4. **Verify permissions.** `ls -l ~/.archon-search/.search.env` should show `-rw-------`. If not, `chmod 600` it before restart.
5. **Restart the server.** `archon-search start`.
6. **Update clients.** Any client still presenting the old key now receives `401`.

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
