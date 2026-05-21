**Purpose**: Enumerate the supported deployment topologies for `archon-search` on a single host, with binding, supervision, and reverse-proxy guidance.
**Audience**: SREs and sysadmins running `archon-search` in production on a single machine.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2027-05-20

# Deployment Topologies

`archon-search` is a single-process Python server. There is no clustering, no leader election, and no native TLS. Production-grade deployment means: pin the bind address, run it under a real supervisor, and put a TLS terminator in front if anything outside the host needs to reach it. See `Architecture/160_operational_readiness_monitoring_and_reliability.md` for the operational surface this builds on.

## Principles

1. **One process per host.** LanceDB is opened by a single writer (`store.py`). Running two `archon-search` instances against the same `db_path` will corrupt state. See ADR `ADRs/01_lancedb_as_local_vector_store.md`.
2. **Bind locally, terminate TLS upstream.** The server has no built-in TLS, no rate limiter, and no IP allowlist. Any non-loopback exposure must go through a reverse proxy. See `Architecture/150_security_and_privacy_architecture.md`.
3. **Let the OS supervise.** `launchd` (macOS) and `systemd --user` (Linux) are the supported lifecycle managers — restart-on-crash is their job, not the server's.
4. **No host/port environment override.** `host` and `port` are TOML-only (`archon_search/config.py`, fields on `SearchConfig`). Tracked as `ARCH-2` in `Architecture/530_technical_debt_refactoring_roadmap.md`. #Unverified (tech-debt tag)

## Topology summary

| Topology | Lifecycle owner | Restart | TLS | When to use |
| --- | --- | --- | --- | --- |
| Foreground (`uv run python -m archon_search.server`) | Operator shell | Manual | None | Local dev, smoke tests. |
| `launchd` user agent (macOS) | `launchctl` | `KeepAlive=true`, `ThrottleInterval=60` | None | Workstation production on macOS. |
| `systemd --user` (Linux) | `systemd` | `Restart=always`, `RestartSec=5` | None | Server-class Linux host. |
| Reverse-proxied (any of the above + nginx/Caddy) | Same as base | Same as base | Terminator | Any non-loopback exposure. |
| Windows | n/a | n/a | n/a | **Not supported** — `archon_search/platform/windows.py` raises `NotImplementedError` for install/start/stop/uninstall; `status()` returns a non-running `ServiceStatus` instead of raising. Tracked as `PLT-1`. #Unverified |

## Foreground

There is no `archon-search start --foreground` subcommand. `archon-search start` (`archon_search/cli/start.py`) only validates the config and then delegates to `launchctl start` (macOS) or `systemctl --user start` (Linux) via `_get_service().start()`; it returns immediately and does not run uvicorn in the calling shell. Running `uv run archon-search` with no subcommand prints Click help.

The genuine foreground entry point is the module runner, which calls `run_server` (`archon_search/server/app.py:152`) directly:

```bash
uv run python -m archon_search.server
```

The process logs to stderr; no log file is written unless you redirect. Suitable only for debugging — for this foreground command, `Ctrl-C` is the only stop signal, and there is no restart-on-crash.

## launchd (macOS)

`archon-search install` writes `~/Library/LaunchAgents/com.archon.search.plist` via `archon_search/platform/macos.py::LaunchdSearchService`. Key facts an operator needs:

- `KeepAlive=true`, `ThrottleInterval=60`: launchd respawns the agent on crash, with a minimum 60s spawn interval between attempts (`ThrottleInterval` is a throttle, not a literal "restart after 60s" timer).
- Wrapped in `/usr/sbin/taskpolicy -b` (background QoS). This is intended to keep ingest from contending with foreground UI work; there is no benchmark or test in the repo that enforces this. #Unverified
- Logs: stdout and stderr both go to `~/.archon-search/logs/archon-search.log`.
- Survival across reboot: `launchd` user agents start on user login. There is no equivalent of `loginctl enable-linger`; if the host reboots and no one logs in, the service does not run.

Manual operations:

```bash
launchctl list com.archon.search
launchctl unload  ~/Library/LaunchAgents/com.archon.search.plist
launchctl load    ~/Library/LaunchAgents/com.archon.search.plist
```

## systemd (Linux)

`archon-search install` writes `~/.config/systemd/user/archon-search.service` via `archon_search/platform/linux.py::SystemdSearchService`. The install path also runs `loginctl enable-linger $USER` so the agent survives logout. Defaults:

- `Restart=always`, `RestartSec=5`.
- `Nice=10`, `CPUQuota=50%` — the unit deliberately yields CPU to the rest of the host.
- Logs go to `journalctl --user -u archon-search` (no rotation policy in v1, see `Architecture/160_operational_readiness_monitoring_and_reliability.md`).

Manual operations:

```bash
systemctl --user status  archon-search
systemctl --user restart archon-search
journalctl --user -u archon-search -e
```

## Binding and firewalling

Default bind is `127.0.0.1:8765` (`config.py` defaults). Recommended postures:

- **Workstation, only the local user**: leave `host = "127.0.0.1"`. No firewall rule needed.
- **Single-host service for other processes on the same box**: leave it on loopback; the OS-level user boundary is your only isolation. The auth design is not multi-tenant — see `Architecture/150_security_and_privacy_architecture.md`.
- **Remote access required**: bind `127.0.0.1` and front it with a reverse proxy that terminates TLS and forwards to `127.0.0.1:8765`. Do **not** bind `0.0.0.0` directly: no TLS, no rate limiter, no IP filter.

`install` (`archon_search/cli/install_cmd.py`) performs **no port-in-use pre-check**: it calls `_get_service().register()`, then `.start()`, then polls `/health` for up to 60s via `_wait_for_health`. If another process already owns the configured port, `service.start()` will still succeed (launchd/systemd accept the start command) and the agent's child uvicorn will then fail to bind; the health poll will time out and the CLI exits with a warning. For port changes edit `[server].port` in `~/.archon-search/archon-search.toml` and restart — no env override exists.

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
        proxy_read_timeout 120s;            # reranker + ingest can be slow on cold start #Unverified
    }
}
```

Notes:

- The auth middleware exempts four paths from the `Bearer` requirement: `/health`, `/docs`, `/openapi.json`, `/redoc` (`_EXEMPT_PATHS` in `archon_search/server/middleware_auth.py:16`). Use `GET /health` as the proxy's upstream-health check; treat `/docs`, `/openapi.json`, and `/redoc` as unauthenticated documentation surface and block them at the proxy if you don't want them publicly reachable.
- CORS is wide-open in v1: `CORSMiddleware(allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])` (`archon_search/server/app.py:122`). If you front the service with a browser-facing origin, rely on the proxy for CORS hardening; do not assume the server restricts it.
- The server does not parse `X-Forwarded-For` or correlate request IDs — see `ARCH-3` in `Architecture/530_technical_debt_refactoring_roadmap.md`. #Unverified (tech-debt tag)

## Process supervision options

`launchd` and `systemd --user` are first-class. If you need something else (e.g. containers), the contract is minimal:

- One process. The uvicorn server runs inside `run_server` (`archon_search/server/app.py:152`), invoked by `python -m archon_search.server` (`archon_search/server/__main__.py`). The `archon-search start` CLI does **not** run uvicorn in-process — it shells out to `launchctl`/`systemctl`, which then spawn a child whose `ExecStart` is `{python} -m archon_search.server`. If you write your own supervisor, point its `ExecStart` (or equivalent) at `python -m archon_search.server`.
- Working directory: irrelevant.
- Recognised environment overrides: `ARCHON_SEARCH_CONFIG` (config path), `ARCHON_SEARCH_API_KEY` (API key), `ARCHON_SEARCH_KEY_FILE` (key file path). None are strictly required for the foreground path, but the registered plist/unit bake in `ARCHON_SEARCH_CONFIG=~/.archon-search/archon-search.toml` (`macos.py:79`, `linux.py:93`) — if you wrap the process yourself, you almost certainly want to set `ARCHON_SEARCH_CONFIG` to point at your config.
- Mounts: persistent volume on `~/.archon-search/` (or wherever `db_path`, `log_file`, `[telemetry].log_dir` point). All three keys exist in `SearchConfig` / `TelemetryConfig` (`archon_search/config.py`).
- Signals: `SIGTERM` triggers FastAPI lifespan shutdown. On the shutdown side the lifespan in `app.py` (lines 109–118) disconnects the search store and, if telemetry is enabled, calls `telemetry_writer.drain_and_stop()` before cancelling background tasks. There is **no explicit job-drain** on shutdown — in-flight jobs in `JobStore` are not awaited. No grace-period constant is defined in the server, plist, or unit; tune your supervisor's stop timeout to your workload. #Unverified (specific grace value)

`docker-compose`, `runit`, `s6`, `supervisord` are all viable wrappers — the supported guarantees stop at "the OS restarts the process on crash and points logs somewhere persistent". CI exercises only the Linux/macOS branches (`PLT-2`). #Unverified (tech-debt tag)

## Related documents

- `Architecture/160_operational_readiness_monitoring_and_reliability.md` — observability, install lifecycle.
- `Architecture/150_security_and_privacy_architecture.md` — threat model, ACL semantics.
- `OperatorGuide/02_monitoring_and_alerts.md` — what to probe once you have a topology running.
- `OperatorGuide/05_incident_runbook.md` — failures specific to each topology.
