**Purpose**: Enumerate the supported deployment topologies for `archon-search` on a single host, with binding, supervision, and reverse-proxy guidance.
**Audience**: SREs and sysadmins running `archon-search` in production on a single machine.
**Status**: Draft
**Last reviewed**: 2026-07-29
**Next review**: 2027-07-29

# Deployment Topologies

`archon-search` is a single-process Python server. There is no clustering, no leader election, and no native TLS. Production-grade deployment means: pin the bind address, run it under a real supervisor, and put a TLS terminator in front if anything outside the host needs to reach it. See [`../Architecture/160_operational_readiness_monitoring_and_reliability.md`](../Architecture/160_operational_readiness_monitoring_and_reliability.md) for the operational surface this builds on.

## Principles

1. **One process per host (per volume).** LanceDB is opened by a single writer (`store.py`). Running two `archon-search` instances against the same `db_path` will corrupt state. To run several independent instances on one host, give each its own `ARCHON_SEARCH_DATA_DIR` and port — see [`../UserManual/150_multi_instance_setup.md`](../UserManual/150_multi_instance_setup.md).
2. **Bind locally, terminate TLS upstream.** The server has no built-in TLS, no rate limiter, and no IP allowlist. Any non-loopback exposure must go through a reverse proxy. See [`../SecurityGuide/05_network_exposure_and_tls.md`](../SecurityGuide/05_network_exposure_and_tls.md).
3. **Let the OS supervise** — except in containers. `launchd` (macOS) and `systemd --user` (Linux) are the supported lifecycle managers for host installs. For containerised deployments the orchestrator (Docker, Kubernetes, ECS) owns restart-on-crash — see [`../UserManual/140_running_with_docker.md`](../UserManual/140_running_with_docker.md).
4. **Host/port/data-dir env overrides exist.** `ARCHON_SEARCH_HOST` and `ARCHON_SEARCH_PORT` override `[server].host` / `[server].port` in TOML; `ARCHON_SEARCH_DATA_DIR` relocates the entire runtime tree under a single root. These are the primary configuration knobs for the Docker image. There is **no `--port` run flag** — port comes from config or the env var.

## Topology summary

| Topology | Lifecycle owner | Restart | TLS | When to use |
| --- | --- | --- | --- | --- |
| Foreground (`archon-search serve` or `python -m archon_search.server`) | Operator shell | Manual | None | Local dev, smoke tests, `nohup` topologies. |
| `launchd` user agent (macOS) | `launchctl` | `KeepAlive=true`, `ThrottleInterval=60` | None | Workstation production on macOS. |
| `systemd --user` (Linux) | `systemd` | `Restart=always`, `RestartSec=5` | None | Server-class Linux host. |
| Docker container | `docker` / orchestrator | Orchestrator policy (`restart: unless-stopped` in shipped compose) | None (reverse-proxy upstream) | Portable, reproducible deployment unit; CI environments. See [`../UserManual/140_running_with_docker.md`](../UserManual/140_running_with_docker.md). |
| Reverse-proxied (any of the above + nginx/Caddy) | Same as base | Same as base | Terminator | Any non-loopback exposure. |
| Windows | n/a | n/a | n/a | **Not supported** — `archon_search/platform/windows.py` raises `NotImplementedError` for install/start/stop/uninstall; `status()` returns a non-running `ServiceStatus` instead of raising. |

## Foreground

`archon-search serve` (`archon_search/cli/serve.py`) is the canonical foreground entry point: it calls `load_config(path, serve=True)` (host defaults to `0.0.0.0` — overridable by `[server].host` in TOML or `ARCHON_SEARCH_HOST` in the env) and then `run_server(config)` in the calling shell. It never invokes `launchctl` or `systemctl`. `Ctrl-C` / `SIGTERM` are the only stop signals; there is no restart-on-crash.

```bash
archon-search serve                       # default config, host 0.0.0.0:8765
ARCHON_SEARCH_HOST=127.0.0.1 \
ARCHON_SEARCH_PORT=9000 archon-search serve   # explicit env override
archon-search serve --config ./local.toml     # alternative TOML
```

`archon-search start` (`archon_search/cli/start.py`) is a different subcommand: it validates the config and then delegates to `launchctl start` (macOS) or `systemctl --user start` (Linux) via `_get_service().start()`; it returns immediately and does not run uvicorn in the calling shell. Use `start` when you want OS supervision; use `serve` when you do not. See [`../UserManual/40_running_the_server.md`](../UserManual/40_running_the_server.md) for the `serve` vs. `start` contrast.

The module runner still works and is functionally equivalent to `serve` minus the `serve=True` host default:

```bash
uv run python -m archon_search.server
```

This binds to whatever `[server].host` says (default `127.0.0.1`) — no `0.0.0.0` override.

## launchd (macOS)

`archon-search install` writes `~/Library/LaunchAgents/com.archon.search.plist` via `archon_search/platform/macos.py`. Key facts an operator needs:

- `KeepAlive=true`, `RunAtLoad=true`, `ThrottleInterval=60`: launchd respawns the agent on crash, with a minimum 60s spawn interval between attempts (`ThrottleInterval` is a throttle, not a literal "restart after 60s" timer).
- The agent runs a generated wrapper script (`~/.archon-search/run-server.sh`) under `/usr/sbin/taskpolicy -b` (background QoS). The wrapper sources `~/.archon-search/.secrets.env` when present (HyDE / RAG Fusion keys).
- Logs: stdout and stderr both go to `~/.archon-search/logs/archon-search.log`.
- Survival across reboot: `launchd` user agents start on user login. There is no equivalent of `loginctl enable-linger`; if the host reboots and no one logs in, the service does not run.

Manual operations:

```bash
launchctl list com.archon.search
launchctl unload  ~/Library/LaunchAgents/com.archon.search.plist
launchctl load    ~/Library/LaunchAgents/com.archon.search.plist
```

## systemd (Linux)

`archon-search install` writes `~/.config/systemd/user/archon-search.service` via `archon_search/platform/linux.py`. The install path also runs `loginctl enable-linger $USER` so the agent survives logout. Defaults:

- `ExecStart={python} -m archon_search.server`, `Restart=always`, `RestartSec=5`.
- `Nice=10`, `CPUQuota=50%` — the unit deliberately yields CPU to the rest of the host.
- Logs go to `journalctl --user -u archon-search`. See [`30_logging.md`](30_logging.md) for retention and rotation guidance.

Manual operations:

```bash
systemctl --user status  archon-search
systemctl --user restart archon-search
journalctl --user -u archon-search -e
```

## Binding and firewalling

Default bind is `127.0.0.1:8765` (`config.py` defaults). Recommended postures:

- **Workstation, only the local user**: leave `host = "127.0.0.1"`. No firewall rule needed.
- **Single-host service for other processes on the same box**: leave it on loopback; the OS-level user boundary is your only isolation. The auth design is not multi-tenant — see [`../SecurityGuide/01_threat_model.md`](../SecurityGuide/01_threat_model.md).
- **Remote access required**: bind `127.0.0.1` and front it with a reverse proxy that terminates TLS and forwards to `127.0.0.1:8765`. Do **not** bind `0.0.0.0` directly outside a container: no TLS, no rate limiter, no IP filter. The Docker container is the exception — it binds `0.0.0.0` inside the container but the publish (`-p`) flag controls host-side reachability; bind the published port to `127.0.0.1:8765` if you want to keep the upstream-proxy posture (`-p 127.0.0.1:8765:8765`).

`install` (`archon_search/cli/install_cmd.py`) performs **no port-in-use pre-check**: it registers the service, starts it, then polls `GET /health` (`_wait_for_service` in `install.py`) up to a profile-derived timeout. If another process already owns the configured port, the service start command still succeeds (launchd/systemd accept it) and the child uvicorn then fails to bind; the health poll times out and the CLI exits with a warning. For port changes either edit `[server].port` in `~/.archon-search/archon-search.toml` and restart, or set `ARCHON_SEARCH_PORT` in the environment (overrides TOML).

## Reverse-proxy patterns

The server speaks plain HTTP/1.1 with `Bearer` auth. Either of the following terminators is sufficient.

### Caddy (TLS automatic via Let's Encrypt)

```caddy
search.example.internal {
    reverse_proxy 127.0.0.1:8765 {
        header_up Host {host}
        # Bearer token is forwarded as-is by default; do not strip Authorization.
    }
}
```

### nginx (TLS configured locally)

```nginx
server {
    listen 443 ssl http2;
    server_name search.example.internal;

    ssl_certificate     /etc/letsencrypt/live/search.example.internal/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/search.example.internal/privkey.pem;

    # archon-search is a single process; no upstream pooling needed.
    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Authorization     $http_authorization;
        proxy_read_timeout 120s;            # reranker + ingest can be slow on cold start
    }
}
```

Notes:

- The auth middleware exempts five paths from the `Bearer` requirement: `/health`, `/ready`, `/docs`, `/openapi.json`, `/redoc` (`_EXEMPT_PATHS` in `archon_search/server/middleware_auth.py`). Use `GET /health` as the proxy's upstream-health check; treat `/docs`, `/openapi.json`, and `/redoc` as unauthenticated documentation surface and block them at the proxy if you don't want them publicly reachable.
- CORS is wide-open in v1: `CORSMiddleware(allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])` (`archon_search/server/app.py`). If you front the service with a browser-facing origin, rely on the proxy for CORS hardening; do not assume the server restricts it.

## Process supervision options

`launchd` and `systemd --user` are first-class for host installs. `docker` is first-class for containerised deployments — the shipped image runs `archon-search serve` under `tini` as PID 1; the orchestrator owns restart-on-crash (`docker-compose.yml` ships `restart: unless-stopped` for the production service). For everything else (`runit`, `s6`, `supervisord`, or your own supervisor) the contract is minimal:

- One process. The uvicorn server runs inside `run_server` (`archon_search/server/app.py`), invoked either by `archon-search serve` (foreground; host defaults to `0.0.0.0`), `python -m archon_search.server` (foreground; host honours `[server].host`), or `archon-search start` (which shells out to `launchctl`/`systemctl`, whose `ExecStart` is `{python} -m archon_search.server`). Point your supervisor's `ExecStart` at `archon-search serve` for the host-defaulting behaviour, or at `python -m archon_search.server` to inherit `[server].host` from TOML.
- Recognised environment overrides: `ARCHON_SEARCH_CONFIG` (config path), `ARCHON_SEARCH_API_KEY` (API key), `ARCHON_SEARCH_KEY_FILE` (key file path), plus `ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_PORT`, and `ARCHON_SEARCH_DATA_DIR`. The registered plist/unit bake in `ARCHON_SEARCH_CONFIG=<config path chosen at install time>` — the wizard's `--config PATH` is honoured, defaulting to `~/.archon-search/archon-search.toml` when the flag is omitted; the Docker image bakes in `ARCHON_SEARCH_DATA_DIR=/data`, `ARCHON_SEARCH_CONTAINER=1`, and `FASTEMBED_CACHE_PATH=/data/fastembed-cache`.
- Mounts: persistent volume on `~/.archon-search/` for host installs, or `/data` for the Docker image. One mounted volume covers `db_path`, `log_file`, `[telemetry].log_dir`, the key file, the jobs file, the fastembed model cache, and the ingest history.
- Signals: `SIGTERM` triggers FastAPI lifespan shutdown. The lifespan disconnects the search store and, if telemetry is enabled, calls `telemetry_writer.drain_and_stop()` before cancelling background tasks. There is **no explicit job-drain** on shutdown — in-flight jobs in `JobStore` are not awaited. The shipped `docker-compose.yml` sets `stop_grace_period: 30s`; tune your supervisor's stop timeout to your workload.

Once a topology is running, wire up probes and log capture — see [`20_monitoring_and_alerts.md`](20_monitoring_and_alerts.md) for what to poll and [`30_logging.md`](30_logging.md) for where output lands per topology.

## Related documents

- [`00_index.md`](00_index.md) — OperatorGuide table of contents and reading order.
- [`20_monitoring_and_alerts.md`](20_monitoring_and_alerts.md) — what to probe once you have a topology running.
- [`30_logging.md`](30_logging.md) — log destinations and rotation per topology.
- [`90_incident_runbook.md`](90_incident_runbook.md) — failures specific to each topology.
- [`../UserManual/140_running_with_docker.md`](../UserManual/140_running_with_docker.md) — Docker image, `docker run` / `docker compose`, env-var matrix, persistence layout.
- [`../UserManual/150_multi_instance_setup.md`](../UserManual/150_multi_instance_setup.md) — running several isolated instances on one host.
- [`../UserManual/40_running_the_server.md`](../UserManual/40_running_the_server.md) — `serve` vs. `start` subcommands.
- [`../SecurityGuide/05_network_exposure_and_tls.md`](../SecurityGuide/05_network_exposure_and_tls.md) — TLS termination and network exposure posture.
