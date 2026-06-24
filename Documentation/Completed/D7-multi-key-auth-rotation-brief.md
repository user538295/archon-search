# Feature Brief: D7 — Hardening: Multi-Key Auth with Rotation (SEC-1)

## Problem

When an API key is compromised or a long-running deployment needs periodic credential hygiene, there is no way to revoke or rotate a key without restarting the server. The `namespaces` map in `archon-search.toml` supports multiple static bearer tokens, but there is no expiry field, no revocation flag, and no CLI command to perform a rotation without touching TOML directly and restarting.

## Goal

An operator can issue, revoke, or rotate API keys — including the default key and any per-namespace keys — without restarting the server. Active requests in flight are never rejected mid-flight. Expired or revoked keys are refused immediately on the next request. The key store is durable across restarts and follows the existing `atomic_write_bytes` contract.

## Users & Context

Operators running archon-search as a persistent service (daemon, container, or multi-tenant deployment) who need to onboard or offboard clients, respond to a suspected key compromise, or rotate credentials on a schedule. They interact via the CLI or the REST/MCP API using an already-valid admin key.

## Core Flow

1. **Issue** — Operator runs `archon-search key create --namespace team-a [--expires 90d] [--label ci-runner]` → server generates a new `secrets.token_hex(32)` key, writes it to the durable key store, and prints the bearer token once (plaintext, to stdout only).
2. **List** — Operator runs `archon-search key list` → server prints all key records: `id`, `label`, `namespace`, `created_at`, `expires_at` (or `never`), `status` (`active` / `revoked`). The bearer token itself is never printed again after creation.
3. **Revoke** — Operator runs `archon-search key revoke <id>` → server marks the key as `revoked` in the store and writes it durably. The next request bearing that token receives HTTP 401 immediately.
4. **Rotate default key** — Operator runs `archon-search key rotate` → server generates a new default key, writes it to the existing `.search.env` file (same contract as today), and prints the new token once. The old key is revoked automatically after a configurable grace period.
5. **Auth request** — Client sends `Authorization: Bearer <token>` → middleware loads key records from the in-memory cache (refreshed from disk on any write) → checks token against all active, non-expired records using `secrets.compare_digest` without early exit → resolves namespace → stamps `request.state.namespace` as today.

## In Scope

- **Durable key store** at `get_data_dir() / "keys.json"`: JSON array of key records. Each record: `id` (UUID4 string), `token_hash` (SHA-256 hex of the bearer token — the raw token is never stored), `namespace` (string), `label` (optional string), `created_at` (ISO-8601 UTC), `expires_at` (ISO-8601 UTC or `null`), `status` (`"active"` | `"revoked"`). Written via `atomic_write_bytes` on every mutation (fsync-backed). Mode `0600`. Loaded into memory on server startup and kept in an in-memory cache; cache refreshed from disk whenever a write occurs (single-process; no inotify needed).
- **Token hashing**: SHA-256 of the raw bearer token (hex digest). The raw token is printed once at creation and never stored. Comparison in middleware: `hmac.compare_digest(sha256(token).hexdigest(), record["token_hash"])` — constant-time over the hex strings, maintaining the existing timing-safe contract.
- **`key_manager.py` extension**: new `KeyStore` class with methods `create(namespace, label, expires_at) -> (id, raw_token)`, `revoke(id) -> None`, `list_keys() -> list[KeyRecord]`, `load() -> list[KeyRecord]`. Backed by `atomic_write_bytes`. `KeyStore` is the single writer; `APIKeyMiddleware` reads only from the cached list passed at construction (refreshed on every write).
- **`middleware_auth.py` update**: `APIKeyMiddleware.__init__` gains a `key_store: KeyStore | None = None` parameter alongside the existing `api_key` and `namespaces`. When `key_store` is provided, the middleware reads from `key_store.active_keys()` (all records with `status="active"` and `expires_at > now` or `expires_at=null`) in addition to the legacy `namespaces` map. The legacy `api_key` + `namespaces` path remains intact so existing single-key deployments are unaffected.
- **Expiry enforcement**: checked on each request (wall-clock `datetime.now(UTC)` vs `expires_at`). Expired keys are rejected with HTTP 401 — they are not auto-revoked in the store (operator must explicitly revoke to clean up, or a future GC pass can purge them). An INFO-level log is emitted on first rejection of an expired key.
- **Grace period for `key rotate`**: configurable via `[auth] rotate_grace_seconds` (default `0` — immediate revocation). When `> 0`, the old default key is marked `expires_at = now + grace_seconds` rather than `status = revoked` immediately. This allows in-flight long-poll or streaming clients to complete without a mid-stream 401. Grace period applies only to `key rotate` (the old default key); `key revoke` is always immediate.
- **CLI commands** under `archon-search key`:
  - `archon-search key create --namespace <ns> [--expires <duration>] [--label <str>]` — duration format: `30d`, `12h`, `3600s`, or an ISO-8601 datetime.
  - `archon-search key list [--namespace <ns>] [--status active|revoked|all]`
  - `archon-search key revoke <id>`
  - `archon-search key rotate [--grace <duration>]` — rotates the default key only.
- **REST endpoints** under `/keys` (requires Bearer auth; any active key grants access):
  - `POST /keys` — create a key; body: `{namespace, label?, expires_at?}`; response: `{id, token, namespace, label, created_at, expires_at}` — token field appears **once** in the response.
  - `GET /keys` — list keys (no token field in response); query params `namespace`, `status`.
  - `DELETE /keys/{id}` — revoke a key.
  - `POST /keys/rotate` — rotate default key; body: `{grace_seconds?}`.
- **MCP tools**: `create_key`, `list_keys`, `revoke_key`, `rotate_key` — added to `mcp.py`, sharing the REST auth layer. Token is returned in `create_key` and `rotate_key` responses only.
- **Backward-compatible TOML `[namespaces]` map**: the existing static map continues to work unchanged. Keys declared in `[namespaces]` are loaded as synthetic `KeyRecord` objects (no `id`, no `expires_at`, `status="active"`) and treated the same way in the middleware loop. Migration is optional — operators can move to the key store gradually or never.
- **`[auth]` config section**: `rotate_grace_seconds` (int, default `0`). Added to `archon-search.toml.example`.
- **`app.py` wiring**: `KeyStore` instantiated in `create_app()`; passed to `APIKeyMiddleware` alongside the existing `api_key` and `namespaces`. `app.state.key_store` exposed so route handlers for `/keys` endpoints can call `key_store` methods.
- **`routes_keys.py`**: new route file for the `/keys` resource, following the existing per-resource file convention.
- **`schemas.py` additions**: `KeyCreateRequest`, `KeyResponse` (no `token` field), `KeyCreateResponse` (with `token` field), `KeyListResponse`, `KeyRevokeResponse`, `KeyRotateRequest`, `KeyRotateResponse`.
- **OpenAPI snapshot regenerated** and `BREAKING.md` updated for the new `/keys` endpoints.
- Documentation updated: `CLAUDE.md` (key_manager section), `Documentation/Architecture/150_security_and_privacy_architecture.md`, `Documentation/SecurityGuide/02_authentication_and_keys.md`, API reference (`600_api_reference_or_public_interface.md`), `archon-search.toml.example`.
- Tests: TDD — unit tests for `KeyStore` (create, revoke, expiry, hash storage), middleware tests (expired key → 401, revoked key → 401, grace period behavior, legacy `namespaces` map still works), integration tests for `/keys` REST endpoints and CLI commands, regression test confirming raw token is never written to `keys.json`.

## Out of Scope

- Per-key ACL scoping (a key granting access only to specific collections within its namespace) — namespace-level isolation is sufficient for v1; finer-grained collection ACL already exists via the chunk-level ACL layer.
- Key store migration CLI (`key import-from-toml`) — operators can manually copy tokens or just issue new ones; automated migration adds risk with low reward.
- Key expiry auto-renewal or automatic rotation on a schedule — operator-initiated rotation is the v1 posture; cron-driven rotation can be scripted externally.
- Audit log (who called which endpoint with which key) — telemetry already records search calls; a dedicated auth audit trail is a separate SEC item.
- Multi-process or clustered deployments — `keys.json` is a single-file store; no distributed lock or Postgres backend in v1.
- Key scoping to HTTP methods or specific routes — not needed for the local-user / cooperating-agents threat model.
- JWKS / OAuth / OIDC integration — out of scope for a local retrieval server.

## Key Decisions

- **Store token hashes, never plaintext**: SHA-256 of the raw token is stored. The raw token is printed once at creation (stdout). This matches the threat model — if `keys.json` leaks, tokens are not directly usable. SHA-256 is sufficient here; bcrypt is unnecessary overhead since tokens are already high-entropy random hex (256-bit), not user-chosen passwords.
- **Extend `APIKeyMiddleware` rather than replace it**: the existing `api_key` + `namespaces` constructor parameters stay. `key_store` is an additive optional parameter. This preserves backward compatibility for single-key deployments and existing tests without requiring a flag day.
- **In-memory cache, refreshed on write**: no inotify, no polling. `KeyStore` notifies the middleware (or the middleware re-reads from `KeyStore.active_keys()`) on every mutation. Single-process model means cache coherence is trivial — no cross-process concern in v1.
- **`keys.json` lives in `get_data_dir()`**: consistent with all other runtime state (`archon-search-jobs.json`, `.backup-state.json`). Relocatable via `ARCHON_SEARCH_DATA_DIR`. Mode `0600`, same as `.search.env`.
- **`key rotate` writes to `.search.env` AND creates a `keys.json` record**: the default key continues to live in `.search.env` for compatibility with `ARCHON_SEARCH_API_KEY` env-var users. The rotated-out key is entered into `keys.json` as `revoked` (or expiring if grace > 0) so the middleware rejects it cleanly. The new default key is written to `.search.env` via `atomic_write_bytes`.
- **Grace period via `expires_at`, not a separate `grace` field**: reuses the existing expiry machinery; no new field in the key record. The middleware already checks `expires_at`; a graced-out key is just an expiring key.
- **`/keys` endpoints visible to any authenticated key**: there is no admin-vs-user key distinction in v1. All keys have equal power. If namespace-scoped key management is needed, it is deferred to a future iteration.
- **TOML `[namespaces]` map stays as a first-class citizen**: synthetic `KeyRecord` objects are constructed at load time from the TOML map, but they have no `id` and cannot be revoked via the CLI/API. This is intentional — operators who use TOML exclusively are not broken; operators who want runtime revocation must issue keys via the CLI/API.

## Edge Cases & Constraints

- **Two keys with identical tokens**: impossible — `secrets.token_hex(32)` produces 256-bit entropy; the probability of collision is negligible. No uniqueness constraint needed in the file format.
- **`keys.json` corrupted or unparseable on startup**: log ERROR, proceed with an empty key store (no managed keys active). The default key from `.search.env` / `ARCHON_SEARCH_API_KEY` still works. This degrades gracefully rather than crashing the server.
- **`keys.json` written by a future version with unknown fields**: use `model.model_validate` with `extra='ignore'` (Pydantic) to forward-tolerate unknown fields.
- **Revoking the only active key while the server is running**: allowed. The server does not validate "at least one key must remain active." The next request will fail with 401. Operators must keep track — this is their responsibility.
- **`key rotate` while a client holds the old default key mid-request**: the old key remains valid for `grace_seconds` after rotation. After grace expires, any new request with the old token receives 401. In-flight requests that already passed the middleware check are unaffected (namespace is already stamped on `request.state`).
- **`ARCHON_SEARCH_API_KEY` env var set alongside `keys.json` records**: env var wins for the default key (existing behavior). Managed keys from `keys.json` are checked in addition. If the env-var key is also present as a `keys.json` record with `status=revoked`, the env-var always wins — the env var bypass is intentional for container bootstrap and is documented in the security guide.
- **`expires_at` in the past when server starts**: treated as an expired (but not revoked) key. It produces a 401 on every request. The key remains visible in `key list --status all` for operator reference.
- **Timing-safe comparison with hashed tokens**: `hmac.compare_digest(sha256(token).hexdigest(), record["token_hash"])` — both sides are fixed-length hex strings; `compare_digest` guarantees constant time over equal-length strings. The full loop (no early exit) is preserved from the existing middleware design.
- **TOML `[namespaces]` token appears in both TOML and `keys.json`**: the middleware will match it twice but resolves to the same namespace. Both checks are constant-time; the second match just overwrites `resolved_namespace` with the same value. No error is raised; a DEBUG log can note the duplicate.
- **`key create` called with a namespace not in `_validate_namespace`**: namespace validation is applied at create time (`_validate_namespace(namespace)` raises `ValueError` → HTTP 422). The middleware already validates namespace at request time as a defense-in-depth check.
- **`keys.json` mode drifts from `0600`**: `KeyStore.load()` tightens permissions on read (same pattern as `key_manager._load_from_file`).
- **Windows**: `_chmod_600` already has a Windows skip path in `key_manager.py`. `KeyStore` follows the same pattern.

## Resolved Decisions

- **Key permissions**: Explicit `role: admin | client` field on `KeyRecord`. Only `admin` keys can call `/keys` endpoints (create, revoke, list, rotate). `client` keys can only call search/ingest endpoints. The first key created (the default key) is `admin`. This is deferred to v2 — v1 ships with equal power (Option A) with a clear doc note, and the role field is added in the first follow-up iteration.
- **`key list` default**: Shows active keys only by default. A hint line at the bottom reports the count of hidden revoked keys (e.g., `3 revoked keys hidden — use --status all to show`). `--status all` or `--status revoked` reveals the full history. `--status active` suppresses the hint line for scripting.
- **`--expires` format**: CLI accepts both human-friendly shorthand (`30d`, `12h`, `3600s`) and absolute date (`2026-12-31` or `2026-12-31T00:00:00Z`). ISO-8601 duration strings (`P30D`) are not accepted. REST API accepts ISO-8601 datetime only (no duration string in JSON body).
- **TOML `[namespaces]` migration**: No auto-migration. Config-file keys keep working unchanged after upgrade. Operators issue new managed keys via `key create`; old and new systems coexist indefinitely. The migration path is operator-controlled: issue a new key, update the client, remove the TOML entry when ready.
- **Token display format**: Warning banner printed to `stderr`; raw token printed to `stdout` only. Scripts that capture output via `$()` or pipe get the clean token. Humans watching the terminal see the full warning. Example stderr output:
  ```
  ========================================
  IMPORTANT: Save this token now.
  It will NEVER be shown again.
  ========================================
  ```
  Example stdout output: `Token: abc123...`

## Future Iterations

- Admin-vs-client key roles: only admin keys can create/revoke/list keys; client keys can only call search/ingest endpoints.
- Per-key collection ACL: a key grants access only to a named subset of collections within its namespace.
- Key expiry auto-rotation: a background task that generates a replacement before a key expires and notifies via a configured webhook.
- Audit log: append-only JSONL recording `key_id`, `namespace`, endpoint, and timestamp for every authenticated request. No raw tokens or query strings — same no-raw-query invariant as telemetry.
- `key import-from-toml` migration helper: reads `[namespaces]` from TOML, creates equivalent `keys.json` records, and prints instructions to remove the TOML section.

## Recommendation

Build this now. SEC-1 is rated medium severity but the trigger condition — a suspected key compromise in a long-running deployment — is exactly when restart-to-rotate becomes operationally painful or impossible (e.g., a container in production with active clients). The design stays narrow: extend `key_manager.py` with a `KeyStore`, extend `middleware_auth.py` with an additive parameter, and add a new `routes_keys.py`. The hardest part is the token-hash design (SHA-256, printed once, never stored plaintext) — get that right first, because it is irreversible. The TOML `[namespaces]` backward-compatibility guarantee must not be broken — existing single-key deployments must require zero config changes.
