**Purpose**: Document how `archon-search` authenticates clients, where keys live, and what the current model does not yet do.
**Audience**: Security engineers, IT admins operating the service.
**Status**: Draft
**Last reviewed**: 2026-10-25
**Next review**: 2027-05-20

# Authentication and Keys

`archon-search` authenticates every request with a bearer token, except two exempt paths: `/health` and `/ready`. `/docs`, `/redoc` and `/openapi.json` are off by default (and `/openapi.json` needs a token); with `[server].docs_enabled = true` all three are served without auth. A durable multi-key store (`KeyStore` in `key_manager.py`, D7) backed by `~/.archon-search/keys.json` lets operators issue, revoke, and rotate API keys **without restarting the server**. The legacy single-key path (env var + TOML `[namespaces]`) continues to work, subject to the key-format rules below. A key is either an **admin** key (server-wide operations) or a namespace key (see [Admin scope](#admin-scope)).

For context on where this fits, see [`01_threat_model.md`](./01_threat_model.md) and [`../Architecture/150_security_and_privacy_architecture.md`](../Architecture/150_security_and_privacy_architecture.md).

## Principles

1. **One default key, optional per-namespace map.** The minimum useful model; nothing more. **D7** adds a durable multi-key store as an additive layer. The default key is always admin; TOML `[namespaces]` tokens never are.
2. **Key file is owner-only.** Mode `0600` on creation, and re-tightened on every read. `keys.json` is also written with mode `0600`, and the data directory is `0700`.
3. **Constant-time comparison.** Timing-side-channel exposure is bounded. Tokens are compared as UTF-8 bytes, so a non-ASCII `Authorization` value is a plain `401` rather than an error. Managed-key loop exits early on match (constant-time per-comparison, not per-key-count); TOML namespace loop retains its no-early-exit design.
4. **No silent key minting on disk.** Auto-generation happens once; subsequent starts reuse the file.
5. **Raw tokens never stored.** Managed keys store only the SHA-256 hex digest (`token_hash`). The raw token is printed once at creation time and is never retrievable.
6. **Weak keys are refused, not repaired.** A default key or TOML token below the length and format rules, or an unusable key file, stops the server at startup with a `ConfigError` that names the rule but never the value.

## Authentication model

The middleware lives at `archon_search/server/middleware_auth.py`. Authentication is `authenticate_token`, which checks three key sources in order and returns an identity (`namespace`, `admin`) or `None`:

1. **Managed keys (`KeyStore.active_keys()`).** `KeyStore` caches the parsed records and revalidates them against the `(st_mtime_ns, st_size, st_ino)` of `keys.json` on every call, so a revoke written by another process, or a file replaced underneath, is seen on the next request. The token hash is computed once per request (`sha256(token).hexdigest()`), then compared with `hmac.compare_digest` against each managed key's `token_hash`. The loop exits on first match. Expired keys (`expires_at <= now`) and revoked keys (`status="revoked"`) are excluded by `active_keys()`. The record's `admin` flag becomes the identity's `admin`.
2. **TOML `[namespaces]` map.** Every entry in `config.namespaces` (`<token> = <namespace>`) is compared against the incoming token. The loop iterates all entries without an early `break` — deliberate, to preserve the timing-safe design from before D7. These identities are never admin.
3. **Default key fallback.** The token is compared against the single current default key, read per request from the REST app's `app.state.api_key` (set from `load_or_generate_key()` at startup and replaced by each rotation); the mounted `/mcp` app reads the same attribute (`api_key_state=app.state`). **Rotation-revocation guard**: before accepting via this path, the middleware checks whether the token hash matches a revoked or expired managed key record in `keys.json`; if found, `401` is returned even if the raw token matches the current default key. A match stamps the namespace as `DEFAULT_NAMESPACE` (`archon_search/constants.py`) and `admin = True`.

A failure on all paths (including an empty token or a token that is not ASCII) returns `401` with `WWW-Authenticate: Bearer` and an empty body. A success stamps `request.state.namespace` and `request.state.admin`, which the rest of the app uses to scope reads, writes, ACL filtering and the admin check. A token that matches but whose namespace fails validation returns `500` with an empty body.

Exempt paths (no auth): `/health` and `/ready` (`_EXEMPT_PATHS`). `/health` returns only `{"status":"running"}`; the version and the MCP bind address are in the authenticated `GET /status`. `/ready` returns only the three check states. When `[server].docs_enabled = true`, `/docs`, `/redoc` and `/openapi.json` also skip auth (`DOCS_PATHS`), because Swagger UI and ReDoc fetch the schema without credentials.

### Graph viewer: view tokens, not `?token=`

A browser navigation cannot send an `Authorization` header, and a real API key in a URL leaks into access logs, history and proxies. The graph viewer therefore does **not** accept the key as `?token=` (that parameter was removed). Instead an authenticated client calls `POST /graph/{collection}/view-token` and opens the returned `view_url` (`/graph/{collection}/view?view_token=…`). A view token is an HMAC over the namespace, the collection and an expiry, keyed by a random per-process secret (it dies on restart), valid for 60 seconds. It is accepted only on `GET /graph/{collection}/view` (as `?view_token=`, and only when no `Authorization` header is present) and on `GET /graph/{collection}` (as the Bearer credential of the viewer's own data fetch), only for that collection and namespace, and is never admin. The viewer page embeds a fresh view token, never the real key. As a second layer, `uvicorn.access` log lines are filtered so the values of `view_token`, `token`, `api_key` and `access_token` query parameters are redacted.

### Admin scope

`archon_search/server/authz.py` is the one place the policy lives; REST and MCP both call it. These need an **admin** key and answer `403 {"detail": "this operation requires an admin API key"}` to any other: `POST /keys/rotate`, `POST /backup/trigger`, `POST /maintenance/trigger`, `POST /sync`, `GET /telemetry/*`, `POST /collections/` (it appends a path to the shared config that `/sync` and the startup sync process for every namespace) and the MCP `rotate_key` tool. Who is admin: the default key (env var or key file); a managed key created with `admin: true` (`POST /keys`, MCP `create_key`; the `archon-search key create` CLI has no `--admin` flag); a `keys.json` record that predates the field only if it is a managed key in the `default` namespace. TOML `[namespaces]` tokens are never admin.

Key management rule: an admin may manage any key. A non-admin may create, list and revoke only non-admin keys of its own namespace and can never mint an admin key; naming another namespace gets `403`, and a foreign or admin key id on revoke looks like "not found" (`404`).

## Key bootstrap

Resolution order in `archon_search/key_manager.py::load_or_generate_key`:

1. **`ARCHON_SEARCH_API_KEY` env var.** Must be lowercase hex (`^[0-9a-f]+$`) of at least 32 characters (`MIN_API_KEY_LENGTH`; generated keys are 64). A set but invalid or short value raises `ConfigError` and the server **refuses to start**; it does not fall through to the file. When valid, it is also persisted to the key file (best effort) so local CLI commands can authenticate.
2. **Key file.** Default `~/.archon-search/.search.env`. The path is resolved lazily on every call via `key_manager.get_key_file()`. Two env vars affect it: `ARCHON_SEARCH_KEY_FILE` (highest priority — a specific file path) and `ARCHON_SEARCH_DATA_DIR` (re-roots the default to `$DATA_DIR/.search.env`, used by the container image). Both are read fresh on every call, so setting them after the package is imported (as the container entrypoint does) works as expected. `ARCHON_SEARCH_KEY_FILE` must be an absolute path; empty or whitespace-only values fall through to the `DATA_DIR`-derived default. The file must contain a line `ARCHON_SEARCH_API_KEY=<hex>` obeying the same rules as the env var. A file that holds an invalid or short value, or that exists but cannot be read, raises `ConfigError` at startup and is left untouched (it is never overwritten with a fresh key, because that would lock every client out). A file with no key line is replaced by a generated key.
3. **Auto-generation.** `secrets.token_hex(32)` produces 64 hex characters. The key is written through the durable helper `_durable_io.atomic_write_bytes(key_file, payload, mode=0o600, replace=False)`, which creates the temp file via `os.open(..., O_WRONLY | O_CREAT | O_EXCL, 0o600)` so the mode is set at creation time (no chmod-after-rename window), fsyncs the file, publishes it with `os.link` so a generated key never overwrites an existing file, and fsyncs the parent directory. Concurrent first-start writers cannot overwrite each other: the loser adopts the key already on disk. See the [durability contract](../Architecture/130_data_architecture_and_persistence.md#durability-contract) and [ADR-06](../ADRs/06_durable_state_writes_via_fsync.md).

## File permissions

| Aspect | Behavior | Source |
| --- | --- | --- |
| Creation mode | `0600` set at creation via the explicit mode argument to `os.open(..., O_WRONLY \| O_CREAT \| O_EXCL, 0o600)` inside `_durable_io.atomic_write_bytes` (subject to umask); `O_EXCL` guarantees exclusivity. There is no post-publish re-chmod on the write path — mode-at-creation closes the world-readable window a `write` + `chmod` sequence would open. | `key_manager.py::_generate_and_write`, `_durable_io.py` |
| Re-tighten on read | If `stat & 0o777 != 0o600`, the loader chmods back to `0o600`. A failure of `stat`/`chmod` is logged at WARNING ("Could not check the permissions of …") and the read continues. | `key_manager.py::_parse_key_file` |
| Windows | `_chmod_600` (read path only) skips chmod on `win32` and logs "permission check skipped". The private-mode helpers (`restrict_fd_to_owner`, `ensure_private_dir`) are guarded so a Windows start does not call the POSIX-only functions; nothing was run on Windows. | `key_manager.py::_chmod_600`, `_durable_io.py` |

The data directory and the directories the server creates under it are created `0700` by `_durable_io.ensure_private_dir`, which also tightens an existing directory, but only inside the data directory, so a shared parent such as `/srv` or `/var/log` is never chmodded. Files the server writes (`keys.json`, `jobs.json`, the sync manifest, state files, telemetry, logs, export and backup archives) are `0600`. Directories outside the data directory (a custom `[logging].log_file` location, say) keep the umask of whoever created them; make sure they are not group- or world-readable on shared hosts.

## Environment overrides

| Variable | Effect | Validation |
| --- | --- | --- |
| `ARCHON_SEARCH_API_KEY` | When **valid**, used as the default key for the lifetime of the process and the file is not consulted. When **set but invalid or short**, the server refuses to start (`ConfigError`; the message names the rule and the length, never the value). | Must match `^[0-9a-f]+$` and be at least 32 characters. |
| `ARCHON_SEARCH_KEY_FILE` | Read/write the key file at this path instead of the default. Resolved lazily on every call via `key_manager.get_key_file()` (no module import-time capture), so the container entrypoint or test harness can set it after Python imports `archon_search`. | Must be an absolute path; empty/whitespace falls through to the `DATA_DIR`-derived default; `~` is expanded; a tilde with HOME unset raises `ValueError`. |
| `ARCHON_SEARCH_DATA_DIR` | Re-roots the default key file to `$DATA_DIR/.search.env` when `ARCHON_SEARCH_KEY_FILE` is not set. Set this in the container image (`ENV ARCHON_SEARCH_DATA_DIR=/data`) to land the key on the mounted volume. | Validated by `archon_search.paths.get_data_dir()` — must be absolute, non-empty. |

There is no env override for the per-namespace map — those are loaded from `[namespaces]` in `archon-search.toml`.

## Per-namespace keys

The `[namespaces]` block maps a hex key to a namespace identifier:

```toml
[namespaces]
"deadbeef…" = "team-a"
"cafef00d…" = "team-b"
```

Each entry adds another bearer token that, when presented, scopes the request to its namespace (never admin). Tokens are validated at config load (`namespace_token_problem`): at least 32 characters of printable ASCII without whitespace, otherwise a `ConfigError` that names the rule and the namespace, never the token. A token identical to the default key is also a `ConfigError`, because it would shadow the default key and demote it from admin. Namespace values are validated by `_validate_namespace` at config load (a malformed value raises `ConfigError`) and again at request time, where a malformed value returns `500` to the client and logs an error.

## Current limitations

Several features remain out of scope:

- **No audit log of authentication failures beyond the application log.** 401s are not separately recorded beyond the application log.
- **No CIDR / origin restrictions inside the middleware.** Bind address and reverse proxy are the only network controls — see `05_network_exposure_and_tls.md`.
- **No read-only or per-collection key scope.** A key is admin or namespace-scoped; there is no finer role (no read-only key, no key limited to one collection). Per-collection policy is item E4 on the roadmap.
- **TOML `[namespaces]` changes require restart.** Managed keys hot-load from disk; TOML namespace tokens are read once at startup.
- **Rotation serialization is per process.** `KeyStore.rotation_lock` serializes rotations through REST and the mounted `/mcp` app, which share one current default key (`app.state.api_key`), so a rotation through either transport retires the old default key on both at once. The lock does not span processes.
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

The raw token is printed to **stdout only** once at creation time — store it immediately. The same operations are available via `POST /keys`, `GET /keys`, `DELETE /keys/{id}`, and `POST /keys/rotate` REST endpoints (the last is admin-only), and via the `create_key`, `list_keys`, `revoke_key`, `rotate_key` MCP tools. `POST /keys` and `create_key` take an optional `admin` flag; only an admin key may set it.

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
curl -i http://127.0.0.1:8765/status

# Should return 200 with JSON status
curl -i -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" http://127.0.0.1:8765/status

# /health is intentionally open and returns only {"status":"running"}
curl -i http://127.0.0.1:8765/health

# A TOML namespace token or non-admin managed key gets 403 on an admin-only route
curl -i -X POST -H "Authorization: Bearer $NAMESPACE_KEY" http://127.0.0.1:8765/sync
```

If `/status` returns `200` without a header, the middleware is misconfigured — check `app.py` is calling `app.add_middleware(APIKeyMiddleware, …)` and that no later middleware short-circuits the chain.

## Related documents

- [`01_threat_model.md`](./01_threat_model.md) — scope and trust boundaries.
- [`03_authorization_and_acl.md`](./03_authorization_and_acl.md) — what the namespace is used for once auth succeeds.
- [`05_network_exposure_and_tls.md`](./05_network_exposure_and_tls.md) — where bearer auth stops being sufficient.
- [`../Architecture/150_security_and_privacy_architecture.md`](../Architecture/150_security_and_privacy_architecture.md) — broader architecture context.
- [`../Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md) — `SEC-1`.
- [`../Backlog/03_world_class_roadmap.md`](../Backlog/03_world_class_roadmap.md) — item D7.
