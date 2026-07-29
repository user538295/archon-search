**Purpose**: Operational runbook for issuing, listing, revoking, and rotating API keys against a running server — no restart required.
**Audience**: Operators running `archon-search` in production.
**Status**: Draft
**Last reviewed**: 2026-07-29
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

## Two things to know first

- **Raw tokens are never stored.** `keys.json` persists only the SHA-256 digest
  (`token_hash`) of each key. The raw bearer token is printed **once** at
  creation/rotation and is unrecoverable afterwards — capture it immediately.
  (`KeyRecord` in `key_manager.py`.)
- **Every write is live.** `KeyStore` re-reads `keys.json` on every auth check,
  so `create` / `revoke` / `rotate` take effect on the next request. No
  `archon-search stop` / `start` cycle is needed.

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

Rotation mints a **new** default key, writes its raw token to the key file
(`.search.env` under the data dir), updates the live server's in-memory key, and
retires the old key — all under a lock so concurrent rotations can't strand an
orphaned active key.

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
  `grace_seconds` in the request body / `--grace` flag overrides it.
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

## Related documents

- [`../SecurityGuide/02_authentication_and_keys.md`](../SecurityGuide/02_authentication_and_keys.md) — authoritative auth & key model (read this first)
- [`00_index.md`](00_index.md) — OperatorGuide index / reading order
- [`90_incident_runbook.md`](90_incident_runbook.md) — what to do on a suspected key compromise
- [`../UserManual/40_running_the_server.md`](../UserManual/40_running_the_server.md) — starting/stopping the server and the CLI proxy model
- [`../UserManual/150_multi_instance_setup.md`](../UserManual/150_multi_instance_setup.md) — namespaces and multi-tenant isolation
- [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) — full REST/MCP/CLI reference (`GET /openapi.json` is authoritative)
