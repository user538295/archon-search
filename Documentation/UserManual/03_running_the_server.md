**Purpose**: Start, stop, and check the `archon-search` server.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-05-20 / **Next review**: 2027-05-20

# Running the server

## Principles

1. **One server, one bind address.** Defaults to `127.0.0.1:8765`. Change `host`/`port` under `[server]` to expose differently — never expose `0.0.0.0` without firewalling the port. #Unverified (normative guidance, not a code-enforced check)
2. **`GET /health` is unauthenticated; everything else requires a Bearer token.** This is enforced by `archon_search/server/middleware_auth.py`.
3. **The same process serves REST and MCP.** `archon_search/server/app.py` mounts the FastAPI router; `archon_search/server/mcp.py` mounts the MCP `/mcp` endpoint using the same auth middleware.
4. **Service identity is fixed.** The plist label / systemd unit name is hard-coded; `archon-search stop` does not need `--config`.

## Starting and stopping

`archon-search start` is **not** a foreground launcher. It always delegates to the platform service adapter (launchctl on macOS, systemctl --user on Linux). There is no `--foreground` flag on `start`. For a foreground/blocking invocation — Docker, CI, `nohup`, or any context where you do not want platform service management — use `archon-search serve` (see below). The CLI surface is uniform:

```bash
archon-search start            # register-and-start via launchd / systemd
archon-search stop
archon-search status
archon-search serve            # foreground; blocks until SIGTERM / Ctrl-C
```

Flags:

- `start --config PATH` — validate an alternative config file before delegating to the service (`archon_search/cli/start.py`). `stop` and `status` take no flags.
- `serve --config PATH` — same `--config` semantics as `start`, but runs uvicorn in the foreground via `run_server(config)`. The host default is `0.0.0.0` (overridable by `[server].host` in TOML or `ARCHON_SEARCH_HOST` in the env), so a containerised invocation is publicly reachable on the mapped port. `serve` never calls launchd/systemd and never registers a service — see `archon_search/cli/serve.py`. For the Docker image and the docker-compose stack see [Documentation/UserManual/08_running_with_docker.md](08_running_with_docker.md).

What `start` actually does (`archon_search/cli/start.py`):

1. Calls `load_config(config_path)`; if a `ConfigError` is raised internally it is caught, printed to stderr as `Error: <msg>`, and the process exits with code 1 (the exception does not propagate to the caller).
2. Calls `_get_service().start()`, which on macOS runs `launchctl load` + `launchctl start` (`archon_search/platform/macos.py`) and on Linux runs `systemctl --user start` (`archon_search/platform/linux.py`).
3. Prints the literal string `archon-search started` on success. **It does not print the bind address.** The default bind comes from `[server]` in `archon-search.toml` and is `127.0.0.1:8765` (`archon_search/config.py`).

Prerequisite: the service must have been registered with `archon-search install` first — that command writes the plist/unit file. Without it, `start`'s `launchctl load` / `systemctl` step will fail. `register()` (install) and `start()` are distinct lifecycle steps (`archon_search/platform/service.py`).

`status` prints one of (`archon_search/cli/status.py`):

- `running (PID <n>, uptime <s>s)` when both PID and uptime are known (uptime formatted as integer seconds),
- `running (PID <n>)` when the PID is known but uptime is not,
- `running` (no parentheses, no PID) when the service reports running but PID is unknown,
- `stopped` otherwise.

## Endpoints exposed

| Endpoint | Auth | Source |
| --- | --- | --- |
| `GET /health` | none | `routes_health.py` — returns `{"status":"running","version":"<pkg-version>"}` |
| `GET /docs` | none | FastAPI Swagger UI (exempt in `_EXEMPT_PATHS`, `server/middleware_auth.py`) |
| `GET /openapi.json` | none | Authoritative API contract (exempt) |
| `GET /redoc` | none | ReDoc UI (exempt) |
| `GET /status` | bearer | Rich operator status, per-collection progress (`routes_status.py`) |
| `GET /indexing-state` | bearer | Machine-readable indexing state (`routes_state.py`) |
| `POST /search` | bearer | Hybrid search (`routes_search.py`) |
| `POST /route` | bearer | Centroid-based collection routing (`routes_route.py`) |
| `GET/POST/DELETE /collections/...`, `POST /collections/{name}/reindex` | bearer | Collection management and reindex (`routes_collections.py`) |
| `POST /ingest`, `GET/DELETE /jobs/{job_id}` | bearer | Async ingest jobs (`routes_jobs.py`) |
| `GET /telemetry/stats`, `GET /telemetry/entries` | bearer | Telemetry read-back (`routes_telemetry.py`) |
| `POST /mcp` (and `GET` for streamable HTTP transport) | bearer | MCP transport mounted via FastMCP `streamable_http_app()` (`mcp.py`) |
| `GET /v1/models` | bearer | OpenAI-compatible model list (G9 shim — only when `[openai_shim] enabled = true`) |
| `POST /v1/chat/completions` | bearer | OpenAI-compatible chat completions backed by Archon retrieval (G9 shim — only when `[openai_shim] enabled = true`) |

Both `/v1` endpoints co-mount on the existing REST port — no second server process is required. When `[openai_shim] enabled = false` (the default), no `/v1` routes are registered.

The full schema is `GET /openapi.json` — treat that as authoritative over any documentation snippet.

## Bearer-token examples

The key is in `~/.archon-search/.search.env` after first start. Read it into the shell:

```bash
source ~/.archon-search/.search.env
# $ARCHON_SEARCH_API_KEY is now a shell variable (not exported to subprocesses);
# the curl commands below work because the shell expands $ARCHON_SEARCH_API_KEY
# before exec'ing curl. To export it as an environment variable use:
#   set -a; source ~/.archon-search/.search.env; set +a

curl -s http://127.0.0.1:8765/health
# {"status":"running","version":"…"}

curl -s http://127.0.0.1:8765/status \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY"

curl -s -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection":"docs","query":"how does the router work?"}'
```

If you prefer a static key without touching the file, export `ARCHON_SEARCH_API_KEY` before starting the server — see [`02_configuration.md`](./02_configuration.md).

## Applying config changes

The server reads configuration only at startup, so changes to `~/.archon-search/archon-search.toml` (host, port, chunk size, telemetry, etc.) require a restart:

```bash
archon-search stop && archon-search start
```

On-disk corpus changes are a separate matter — the file watcher (`archon_search/watcher.py`) and sync layer detect them at runtime without a restart.

If you changed `[database].chunk_size` and left `[database].auto_reindex_on_chunk_size_change = true` (the default; `archon_search/config.py`), affected collections will be reindexed on the next start.

## Related documents

- [`02_configuration.md`](./02_configuration.md) — `[server]`, auth, key files.
- [`04_ingestion_and_collections.md`](./04_ingestion_and_collections.md) — populating the index.
- [`05_searching.md`](./05_searching.md) — search and routing endpoints.
- [`07_troubleshooting.md`](./07_troubleshooting.md) — common failure modes.
- [`09_multi_instance_setup.md`](./09_multi_instance_setup.md) — running prod (native service) and dev-UAT (Docker) side by side on the same machine.
