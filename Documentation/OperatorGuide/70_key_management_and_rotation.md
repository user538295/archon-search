**Purpose**: Operational runbook for issuing, listing, revoking, and rotating API keys against a running server — no restart required.
**Audience**: Operators running `archon-search` in production.
**Status**: Draft
**Last reviewed**: 2026-10-25
**Next review**: 2027-07-29

# Key Management and Rotation

D7 added a durable multi-key store (`KeyStore` in `key_manager.py`, backed by
`~/.archon-search/keys.json`) so you can **issue, revoke, and rotate keys while
the server keeps running** — no restart, no dropped connections. This page is
the task-oriented runbook.

For the full auth model — key resolution order, the legacy env-var / TOML
`[namespaces]` path, threat considerations, and the exempt endpoints — read the
authoritative reference: [`../SecurityGuide/02_authentication_and_keys.md`](../SecurityGuide/02_authentication_and_keys.md).
This page does not repeat it.

## Things to know first

- **Raw tokens are never stored.** `keys.json` persists only the SHA-256 digest
  (`token_hash`) of each key. The raw bearer token is printed **once** at
  creation/rotation and is unrecoverable afterwards — capture it immediately.
  (`KeyRecord` in `key_manager.py`.)
- **Every write is live.** `KeyStore` checks the `(mtime, size, inode)` of `keys.json` on every auth check and re-parses it only when that changed, so `create` / `revoke` / `rotate` (even one written by another process) take effect on the next request. No `archon-search stop` / `start` cycle is needed.
- **Rotation and server-wide operations need an admin key; key formats are enforced.** `key rotate` / `POST /keys/rotate` / MCP `rotate_key` need the default key or a managed key created with `admin` (a namespace key gets `403`). A non-admin key can create, list and revoke only non-admin keys of its own namespace. The default key (`ARCHON_SEARCH_API_KEY` or `.search.env`) must be lowercase hex of at least 32 characters and a TOML `[namespaces]` token at least 32 printable ASCII characters, or the server refuses to start. See [`../SecurityGuide/02_authentication_and_keys.md`](../SecurityGuide/02_authentication_and_keys.md#admin-scope).

## Surfaces

You can drive key management three ways; all reach the same `KeyStore`.

| CLI (`archon-search key …`) | REST | MCP tool |
|---|---|---|
| `key create` | `POST /keys` → `201` | `create_key` |
| `key list` | `GET /keys` | `list_keys` |
| `key revoke <ID>` | `DELETE /keys/{key_id}` | `revoke_key` |
| `key rotate` | `POST /keys/rotate` | `rotate_key` |

The CLI commands are thin HTTP proxies to a running server. Each accepts
`--api-url` (default `http://localhost:8765`) and `--api-key` (falls back to
`ARCHON_SEARCH_API_KEY` or the key file). The MCP tools are registered **only
when a key store is configured** — with no key store, `/mcp` exposes none of
the four key tools (`server/mcp.py`).

## Issuing a key

Managed keys are scoped to exactly one namespace. Provide an optional label and
an optional expiry.

```bash
# No expiry:
archon-search key create --namespace team-a --label "ci-runner"

# Relative expiry (30d / 12h / 3600s) or an ISO-8601 datetime with timezone:
archon-search key create --namespace team-a --expires 30d
archon-search key create --namespace team-a --expires 2026-12-31T23:59:59Z
```

Naive datetimes (no timezone offset) are rejected. The **raw token is printed to
stdout exactly once**; the warning banner and metadata (`id`, `namespace`,
`created_at`, `expires_at`) go to stderr — so `TOKEN=$(archon-search key create
--namespace team-a)` captures a clean token.

REST equivalent:

```bash
curl -sS -X POST http://localhost:8765/keys \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"namespace":"team-a","label":"ci-runner","expires_at":"2026-12-31T23:59:59Z"}'
```

An invalid namespace (containing `__`, or leading/trailing `_`) returns `422`.
See [`../UserManual/150_multi_instance_setup.md`](../UserManual/150_multi_instance_setup.md)
for how namespaces isolate collections.

## Listing keys

```bash
archon-search key list                      # active keys only (default)
archon-search key list --namespace team-a   # scope to one namespace
archon-search key list --status all         # include revoked
archon-search key list --status revoked
```

Active is the default view; when revoked keys are hidden the CLI prints a hint
count. TOML synthetic keys (from `[namespaces]`) appear with `id: null` — they
are always active and can only be removed by editing `archon-search.toml` and
restarting.

## Revoking a key

```bash
archon-search key revoke <KEY_ID>        # prompts: "Revoke key …? This cannot be undone."
archon-search key revoke <KEY_ID> --yes  # skip the prompt in scripts (-y)
```

Revocation is immediate and idempotent (revoking an already-revoked key still
returns success). Unknown IDs return `404`. You **cannot** revoke a TOML
synthetic key via the API — passing the literal `null` returns `404` with a hint
to edit the config file instead. In non-interactive contexts (a pipe or CI) the
confirmation prompt aborts with a non-zero exit rather than silently revoking, so
always pass `--yes` in automation.

## Rotating the default key

Rotation mints a **new** default key, writes its raw token to the key file (`.search.env` under the data dir), records it in `keys.json`, retires the old key, and only then makes the new key the server's current default key (`app.state.api_key`). REST and the mounted `/mcp` endpoint share that one current key: `POST /keys/rotate`, `archon-search key rotate` and MCP `rotate_key` all retire it, and `/mcp`, like REST, accepts the new key immediately and rejects the retired one as soon as it is revoked or its grace ends — no restart. Rotations run one at a time under `KeyStore.rotation_lock`, which the REST routes and the mounted MCP app share, so two concurrent rotations cannot both retire the same key and leave the first one's new key active; the lock covers one server process only (`key_manager.rotate_and_persist_default_key`).

```bash
archon-search key rotate            # old key revoked immediately
archon-search key rotate --grace 1h # old key stays valid for 1 hour, then expires
```

- **`--grace` is the drain window.** With a grace period the old key gets
  `expires_at = now + grace` and stays `active` until then, so in-flight requests
  authenticated with the old key keep working. Without it (or `--grace 0s`), the
  old key is revoked the instant rotation completes.
- **Default grace comes from config.** `[auth].rotate_grace_seconds` (default
  `0`, in `config.py`) is the fallback when you don't pass `--grace`. A per-call
  `grace_seconds` in the request body / `--grace` flag overrides it. Both are
  capped at 3153600000 seconds (100 years) so the expiry stays a representable
  date; a larger request body value is a `422`.
- **Env-var lock-out.** If `ARCHON_SEARCH_API_KEY` is set in the server's
  environment it always overrides the key file, so rotation would be a silent
  no-op — the server returns `409`. Unset the env var and restart before using
  managed rotation.

The new raw token is printed once (stdout); rotation metadata (`new_key_id`,
`old_key_id`, `old_key_status`, `old_key_expires_at`) goes to stderr.

### Worked rotation runbook

Zero-downtime rotation of the default key with a grace window:

1. **Rotate with a grace window** long enough for clients to update:
   ```bash
   archon-search key rotate --grace 1h
   ```
   Capture the printed token. The server now accepts **both** the new key and
   the old key (old key `active`, `expires_at = now + 1h`).
2. **Distribute the new key** to every client, CI secret, and connector. Nothing
   breaks yet — in-flight and not-yet-updated clients still authenticate with the
   old key during the window.
3. **Verify** the new key works and confirm you see it in the store:
   ```bash
   archon-search key list
   ```
4. **Grace expires.** After the window the old key stops being accepted
   automatically (a one-time INFO line is logged). If you finish updating clients
   early, you can retire it immediately with `archon-search key revoke <OLD_ID>`.

Pick `--grace 0s` (or plain `rotate`) only when you can update every client
atomically — e.g. a single container reading `.search.env`.

### Recovering from an incomplete rotation

Rotation writes the new token to `.search.env` first and updates `keys.json` second. If the second step fails, `POST /keys/rotate` (and so `archon-search key rotate`) answers `500` and MCP `rotate_key` answers `internal_error`, both with `Rotation incomplete: .search.env holds the new key but keys.json was not updated; see the server log for details`. The server log names the cause: `rotate: keys.json update failed after <key file> was written`, with its traceback (the same line for REST and MCP).

Causes (`key_manager.rotate_and_persist_default_key`):

- **`keys.json` could not be written**: its directory is not writable (only possible when `ARCHON_SEARCH_KEY_FILE` puts `.search.env` in another directory, since otherwise the `.search.env` write fails first and nothing changes), or the disk filled up between the two writes (`ENOSPC`).
- **A leftover `keys.json.tmp`** from a process killed during a `keys.json` write. The write refuses to overwrite it (`FileExistsError`). A server killed this way cannot start again until the file is removed (see [Leftover temp files](#leftover-temp-files)), so a running server meets one only when another process writing the same `keys.json` (a second server on the same data directory) was killed mid-write.
- **`keys.json` became unreadable after the pre-write check.** Rotation checks that `keys.json` is readable before it writes `.search.env`; a `keys.json` that is already unreadable is refused with nothing written (see [When `keys.json` cannot be read](#when-keysjson-cannot-be-read)), so this cause needs the file to break during the rotation itself.
- **A recorded token that differs from the one written to `.search.env`** — a defensive check for a bug. Its log line is `rotate: keys.json recorded an unexpected token after <key file> was written — BUG`, without a traceback.

A directory fsync that fails after `keys.json` or `.search.env` was renamed into place is **not** a cause: the file is written, so rotation logs a WARNING (`... written but its rename durability is unconfirmed`) and completes normally. Nor is a cancelled rotation request; see [A rotation cancelled mid-way](#a-rotation-cancelled-mid-way).

What that leaves behind:

- **`keys.json` is unchanged.** The old default key's record, if it had one, is still `active` with no grace expiry.
- **`.search.env` holds a token no `keys.json` record matches**, and the running server does not accept it on REST or `/mcp`: its current default key (`app.state.api_key`) is still the old one, because the new key is published only after both writes succeed. Clients that send the old key keep working. The local CLI does not: without `--api-key` it reads the orphaned token from `.search.env` and gets `401`.
- **Do not restart the server before you recover.** On restart the server takes the orphaned token from `.search.env` as its default key, and the old key keeps working only if it has an active `keys.json` record. A default key that was never rotated before (auto-generated or written by the installer) has none, so every client still holding it gets `401` from then on.

To recover, fix the cause first (free disk space, make the `keys.json` directory writable, remove a leftover `keys.json.tmp`), then:

1. **Server not restarted since the failure:** rotate again, authenticating with the old key. Without `--api-key` the CLI reads the orphaned token from `.search.env` and gets `401`.
   ```bash
   archon-search key rotate --api-key <OLD_KEY> --grace 1h
   ```
   The server's own copy of `<OLD_KEY>` is gone from `.search.env`, and no endpoint returns the in-memory one, so take it from wherever it was handed out: a client or CI configuration, an MCP connector's settings, or your secret store. Alternatively, call MCP `rotate_key` from a client still configured with the old key; on the mounted `/mcp` endpoint it retires the same current key as `POST /keys/rotate`. Either way the old key is retired as in a normal rotation and `.search.env` is overwritten with a fresh token; the orphaned token is never recorded.
2. **Server restarted since the failure:** the orphaned token is now the current default key, so `archon-search key rotate` (or MCP `rotate_key`) works, but it retires the orphaned token, not the old key, whose record (if it had one) stays `active`. Find the old key with `archon-search key list` and run `archon-search key revoke <OLD_ID>` once clients have moved to the new key.

### A rotation cancelled mid-way

If the task handling a rotation request is cancelled mid-rotation, that is **not** an incomplete rotation by itself. Once the rotation has passed its pre-write checks (grace value, current key, readable `keys.json`), its writes run to completion before the cancellation goes on, so cancelling the request alone never leaves `.search.env` ahead of `keys.json`. The caller gets no response, and the server log says how the rotation ended:

- WARNING `rotate: cancelled mid-rotation, but it completed: <key file> holds the new key (new_key_id=<id>); the caller did not receive its token` — the rotation took effect: the old key is retired (or in its grace window) and the new key is current on REST and `/mcp`. The caller never got the token, but `.search.env` on the server host holds it: read it there (the local CLI already uses it when run without `--api-key`) and distribute it as after a normal rotation.
- ERROR `rotate: cancelled mid-rotation, and the rotation failed`, with the traceback — the rotation failed as described on this page: an [incomplete rotation](#recovering-from-an-incomplete-rotation) when the traceback is a `RotationIncompleteError`, otherwise nothing changed.
- ERROR `rotate: cancelled mid-rotation and the rotation task was cancelled too; check <key file> and keys.json` — the rotation's own task was cancelled too, so its outcome is unknown; compare `.search.env` with `keys.json` and, if `.search.env` holds a token `keys.json` does not record, treat it as an [incomplete rotation](#recovering-from-an-incomplete-rotation).

### When `keys.json` cannot be read

If `keys.json` exists but cannot be read or parsed (unreadable, empty, not valid UTF-8 JSON — an integer over Python's 4300-digit limit or nesting past its recursion limit included — not a JSON array, or holding an invalid record), the server logs an ERROR naming the problem and:

- **refuses every key change** — `POST /keys`, `DELETE /keys/{key_id}` and `POST /keys/rotate` answer `500` and MCP `create_key`, `revoke_key` and `rotate_key` answer `internal_error`, all with `keys.json could not be read; no key changes were made; see the server log for details`. Nothing is written, so the managed keys in the file are not overwritten with only the new record. A rotation refused this way has not touched `.search.env`.
- **authenticates as if `keys.json` were empty**: managed keys stop working, while the default key and the TOML `[namespaces]` tokens still authenticate. `GET /keys` lists no keys at all, since the TOML records are stored in `keys.json` too.
- **at startup** logs `keys.json is unreadable — TOML namespace keys not persisted; keys.json left unchanged` and starts anyway.

Restore `keys.json` from a backup or repair it by hand, then retry; no restart is needed, since the next request notices the changed file. One exception: if `keys.json` was already unreadable at startup, the TOML `[namespaces]` records were not written to it, so they stay out of `GET /keys` / `list_keys` until the next restart (the server writes them only at startup, `create_app` lifespan in `server/app.py`). Those tokens still authenticate in the meantime.

### Leftover temp files

Both key files are written through a temp file next to them (`.search.env.tmp`, `keys.json.tmp`), created exclusively and removed on any error. Only a crash or kill in the middle of a write leaves one behind, and the next write to that file then fails instead of overwriting it:

- **`.search.env.tmp`**: every rotation fails with `Failed to write .search.env — rotation aborted; keys.json unchanged.` and changes nothing. Rotation never removes the temp file, and a restart removes it only when `.search.env` holds no valid key.
- **`keys.json.tmp`**: every key change fails — a rotation as an [incomplete rotation](#recovering-from-an-incomplete-rotation), `POST /keys` and a revoking `DELETE /keys/{key_id}` as a plain-text `500`, MCP `create_key` / `revoke_key` as `internal_error` — and the server cannot start, because its startup write of the TOML key records fails too.

Delete the temp file (it can hold a token that was never recorded, so do not keep a copy), then retry. Default locations: `~/.archon-search/.search.env.tmp` (`<key file>.tmp` when `ARCHON_SEARCH_KEY_FILE` is set) and `~/.archon-search/keys.json.tmp` (under `ARCHON_SEARCH_DATA_DIR` when set).

## Related documents

- [`../SecurityGuide/02_authentication_and_keys.md`](../SecurityGuide/02_authentication_and_keys.md) — authoritative auth & key model (read this first)
- [`00_index.md`](00_index.md) — OperatorGuide index / reading order
- [`90_incident_runbook.md`](90_incident_runbook.md) — what to do on a suspected key compromise
- [`../UserManual/40_running_the_server.md`](../UserManual/40_running_the_server.md) — starting/stopping the server and the CLI proxy model
- [`../UserManual/150_multi_instance_setup.md`](../UserManual/150_multi_instance_setup.md) — namespaces and multi-tenant isolation
- [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) — full REST/MCP/CLI reference (`GET /openapi.json` is authoritative)
