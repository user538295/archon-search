**Purpose**: Start, stop, and check the `archon-search` server, and reach its endpoints.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-07-29 / **Next review**: 2027-07-29

# Running the server

The server is a single process that serves the REST control plane, the MCP endpoint, and (optionally) an OpenAI-compatible shim on one bind address. Runtime state lives under `~/.archon-search/` (or `$ARCHON_SEARCH_DATA_DIR`).

## Principles

1. **One server, one bind address.** The daemon (`start`) defaults to `127.0.0.1:8765` (`[server]` in `archon-search.toml`, `archon_search/config.py`). `serve` flips the host default to `0.0.0.0` for containers — never expose `0.0.0.0` without firewalling the port.
2. **Auth is uniform.** Every endpoint requires a `Bearer` token except `GET /health` and `GET /ready` (and the FastAPI docs paths). Enforced by `_EXEMPT_PATHS` in `archon_search/server/middleware_auth.py`.
3. **The same process serves REST and MCP.** `app.py` builds the FastAPI app; `mcp.py` mounts `/mcp` on the same port with the same auth (when `[mcp].enabled`, the default).
4. **Service identity is fixed.** The launchd plist label / systemd unit name is hard-coded; `stop` needs no `--config`.

## Starting and stopping

`archon-search start` is **not** a foreground launcher. It delegates to the platform service adapter (`launchctl` on macOS, `systemctl --user` on Linux) and daemonizes. For a foreground/blocking invocation — Docker, CI, `nohup`, or any context without platform service management — use `archon-search serve`.

```bash
archon-search start            # register-and-start via launchd / systemd (daemon)
archon-search stop
archon-search status
archon-search serve            # foreground; blocks until SIGTERM / Ctrl-C
```

Flags:

- `start --config PATH` — validate an alternative config before delegating to the service (`cli/start.py`). On a `ConfigError` the message is printed to stderr and the process exits 1. On success it prints the literal `archon-search started` — it does **not** print the bind address.
- `serve --config PATH` — same `--config` semantics, but runs uvicorn in the foreground via `run_server(config)`. The host default is `0.0.0.0` (overridable by `[server].host` or `ARCHON_SEARCH_HOST`), so a containerised invocation is publicly reachable on the mapped port. `serve` never touches launchd/systemd (`cli/serve.py`). For the image and compose stack see [`140_running_with_docker.md`](140_running_with_docker.md).
- `stop` blocks until the platform supervisor confirms the service is actually down (it polls up to ~10 s, because `launchctl unload` / `systemctl --user stop` return before the process has released its listening socket). Once it returns, `archon-search status` reads `stopped` and `GET /health` is unreachable (S04). It prints `archon-search stopped` on confirmation; if the wait times out it instead prints a `Warning: … it may still be running. Check archon-search status.` to stderr and still exits 0 (`cli/stop.py`).
- `status` accepts `--json`, `--api-url`, `--api-key` (`cli/status.py`).

**Prerequisite for `start`:** the service must have been registered with `archon-search install` first (that writes the plist/unit file). Without it, `start`'s `launchctl load` / `systemctl` step fails. There is **no `--port` CLI run flag** — set the port via `[server].port` or `ARCHON_SEARCH_PORT`.

`status` prints one of:

- `running (PID <n>, uptime <s>s)` when both are known,
- `running (PID <n>)` when only the PID is known,
- `running` when the service reports running but the PID is unknown,
- `stopped` otherwise.

When the server is reachable, `status` also prints a **Collections** block — one line per namespace-visible collection with its cached document count and the absolute configured source path (e.g. `mydocs: 142 document(s) — /Users/you/projects/mydocs`); collections with no configured path show `(no configured path)`. Printed even when telemetry is disabled.

## Liveness vs readiness

Two auth-exempt probes are distinct on purpose:

| Probe | Meaning | Behaviour |
| --- | --- | --- |
| `GET /health` | **Liveness** — the process is up and answering | Always `200` `{"status":"running","version":"…","mcp":…}` (`routes_health.py`) |
| `GET /ready` | **Readiness** — storage, model, and startup-sync checks | `200` when storage is OK, no eager warm-up is pending, and no startup collection sync is running, `503` otherwise; body reports per-check status (`routes_ready.py`) |

Use `/health` for a restart-if-dead liveness probe and `/ready` to gate traffic until the store and model backends are usable. For probe wiring and alerting depth see [`../OperatorGuide/20_monitoring_and_alerts.md`](../OperatorGuide/20_monitoring_and_alerts.md).

## Endpoints exposed (overview)

Auth is `Bearer` on all routes except the two probes and the docs paths below. This is a compact map — `GET /openapi.json` is the authoritative contract; see [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) for the exhaustive surface.

| Area | Routes | Auth |
| --- | --- | --- |
| Liveness / readiness | `GET /health`, `GET /ready` | none |
| FastAPI docs | `GET /docs`, `GET /openapi.json`, `GET /redoc` | none |
| Status | `GET /status`, `GET /indexing-state` | bearer |
| Search / route / explain | `POST /search`, `POST /route`, `POST /explain` | bearer |
| Ingest / jobs | `POST /ingest` (202), `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/resume`, `DELETE /jobs/{id}` | bearer |
| Collections | `GET`/`POST` `/collections/`, `GET`/`DELETE`/`PATCH` `/collections/{name}`, `.../documents`, `.../expiring`, `.../reindex`, `.../reindex-metadata`, `.../migrate`, `.../migrations/pending`, `.../export`, `.../import` | bearer |
| Sync / backup / maintenance | `POST /sync`, `POST /backup/trigger`, `POST /maintenance/trigger` | bearer |
| Graph | `POST /graph/{collection}/rebuild-communities` (202), `GET /graph/{collection}`, `GET /graph/{collection}/view` (HTML), `GET /graph/{collection}/impact/{symbol}`, `GET /graph/cross-collection` | bearer |
| Keys | `POST /keys`, `GET /keys`, `DELETE /keys/{key_id}`, `POST /keys/rotate` | bearer |
| Telemetry | `GET /telemetry/stats`, `GET /telemetry/entries` | bearer |
| MCP | `/mcp` sub-app (only when `[mcp].enabled`, default true) | bearer |
| OpenAI shim | `GET /v1/models`, `POST /v1/chat/completions` (only when `[openai_shim].enabled`) | bearer |

When `[openai_shim].enabled = false` (the default) no `/v1` routes are registered; when `[mcp].enabled = false`, `/mcp` is absent and `/status` reports `mcp: null`.

## Bearer-token examples

The key lands in `~/.archon-search/.search.env` after first start. Source it into the shell:

```bash
source ~/.archon-search/.search.env
# $ARCHON_SEARCH_API_KEY is now a shell variable (not exported to subprocesses).
# To export it as an environment variable instead:
#   set -a; source ~/.archon-search/.search.env; set +a

curl -s http://127.0.0.1:8765/health
# {"status":"running","version":"…","mcp":…}

curl -s http://127.0.0.1:8765/status \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY"

curl -s -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection":"docs","query":"how does the router work?"}'
```

To use a static key without touching the file, set `ARCHON_SEARCH_API_KEY` before starting the server (it overrides the key file) — see [`30_configuration.md`](30_configuration.md).

## Managing API keys

The `archon-search key` CLI manages keys against a running server: `key create`, `key list`, `key revoke` (prompts for confirmation), and `key rotate` (`cli/key_cmd.py`). Raw tokens are never persisted — only SHA-256 hashes in `keys.json`. For the full model, rotation grace, and operational procedure see the authoritative guides:

- [`../SecurityGuide/02_authentication_and_keys.md`](../SecurityGuide/02_authentication_and_keys.md) — how auth and keys work.
- [`../OperatorGuide/70_key_management_and_rotation.md`](../OperatorGuide/70_key_management_and_rotation.md) — the rotation runbook.

## CLI write commands require the server

Write operations from the CLI submit jobs to the running server. These commands **require `archon-search serve` (or `start`) to be running**:

- `collection add <path>` — proxies `POST /collections/`
- `collection remove <name>` — proxies `DELETE /collections/{name}`
- `collection reindex <name>` — proxies `POST /collections/{name}/reindex`
- `collection reindex-metadata <name>` — proxies `POST /collections/{name}/reindex-metadata`
- `collection migrate <name>` — proxies `POST /collections/{name}/migrate`
- `ingest --path <path>` — proxies `POST /ingest`
- `sync` — proxies `POST /sync`
- `graph build-communities <collection>` — proxies `POST /graph/{collection}/rebuild-communities`

They accept `--api-url` / `--api-key` and print a friendly message on connection refused; there is no in-process fallback. `--wait` (where supported) polls `GET /jobs/{id}`.

The read-only `collection list` and `collection info` are **also** server proxies now (`GET /collections/` and `GET /collections/{name}`, `cli/collection.py`) — they too require the server. See [`50_ingestion_and_collections.md`](50_ingestion_and_collections.md).

## Applying config changes

The server reads configuration only at startup, so changes to `~/.archon-search/archon-search.toml` (host, port, chunk size, telemetry, etc.) require a restart:

```bash
archon-search stop && archon-search start
```

On-disk corpus changes are separate — the file watcher (`archon_search/watcher.py`) and sync layer pick them up at runtime without a restart.

If you changed `[database].chunk_size` and left `[database].auto_reindex_on_chunk_size_change = true` (the default), affected collections are reindexed on the next start.

## Related documents

- [`00_index.md`](00_index.md) — UserManual table of contents.
- [`10_installation.md`](10_installation.md) — install and register the service.
- [`30_configuration.md`](30_configuration.md) — `[server]`, auth, env, key files.
- [`50_ingestion_and_collections.md`](50_ingestion_and_collections.md) — populating the index.
- [`60_searching.md`](60_searching.md) — search and routing endpoints.
- [`140_running_with_docker.md`](140_running_with_docker.md) — the image and compose stack.
- [`150_multi_instance_setup.md`](150_multi_instance_setup.md) — native service and Docker side by side.
- [`160_troubleshooting.md`](160_troubleshooting.md) — common failure modes.
- [`../OperatorGuide/20_monitoring_and_alerts.md`](../OperatorGuide/20_monitoring_and_alerts.md) — health/ready probes and alerting.
- [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) — full REST + MCP + CLI reference.
