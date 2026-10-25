**Purpose**: Enumerate the supported deployment topologies for `archon-search` on a single host, with binding, supervision, and reverse-proxy guidance.
**Audience**: SREs and sysadmins running `archon-search` in production on a single machine.
**Status**: Draft
**Last reviewed**: 2026-10-25
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
| Windows | n/a | n/a | n/a | **Not supported as a service** — `archon_search/platform/windows.py` raises `NotImplementedError` for install/start/stop/uninstall; `status()` returns a non-running `ServiceStatus` instead of raising. The server code guards its POSIX-only calls (`fchmod`, `geteuid`, directory `fsync`, `O_NOFOLLOW`), but nothing has been run on Windows. |

## Foreground

`archon-search serve` (`archon_search/cli/serve.py`) is the canonical foreground entry point: it calls `load_config(path, serve=True)` (host defaults to `127.0.0.1`, or to `0.0.0.0` inside the container image, where `ARCHON_SEARCH_CONTAINER=1`; `[server].host` in TOML or `ARCHON_SEARCH_HOST` in the env wins over both) and then `run_server(config, config_path)` in the calling shell (`config_path` is `--config`, or `None` for the default path). It never invokes `launchctl` or `systemctl`. `Ctrl-C` / `SIGTERM` are the only stop signals; there is no restart-on-crash.

```bash
archon-search serve                       # default config, host 127.0.0.1:8765 (0.0.0.0 in the container image)
ARCHON_SEARCH_HOST=127.0.0.1 \
ARCHON_SEARCH_PORT=9000 archon-search serve   # explicit env override
archon-search serve --config ./local.toml     # alternative TOML
```

`archon-search start` (`archon_search/cli/start.py`) is a different subcommand: it validates the config and then delegates to `launchctl start` (macOS) or `systemctl --user start` (Linux) via `_get_service().start()`; it returns immediately and does not run uvicorn in the calling shell. Use `start` when you want OS supervision; use `serve` when you do not. See [`../UserManual/40_running_the_server.md`](../UserManual/40_running_the_server.md) for the `serve` vs. `start` contrast.

The module runner still works and is functionally equivalent to `serve` minus the `serve=True` host default:

```bash
uv run python -m archon_search.server
```

This binds to whatever `[server].host` says (default `127.0.0.1`), the same as `serve` outside a container.

## launchd (macOS)

`archon-search install` writes `~/Library/LaunchAgents/com.archon.search.plist` via `archon_search/platform/macos.py`. Key facts an operator needs:

- `KeepAlive=true`, `RunAtLoad=true`, `ThrottleInterval=60`: launchd respawns the agent on crash, with a minimum 60s spawn interval between attempts (`ThrottleInterval` is a throttle, not a literal "restart after 60s" timer).
- The agent runs a generated wrapper script (`<data-dir>/run-server.sh`, where `<data-dir>` is `~/.archon-search/` unless `ARCHON_SEARCH_DATA_DIR` relocates it) under `/usr/sbin/taskpolicy -b` (background QoS). The wrapper sources `<data-dir>/.secrets.env` when present (HyDE / RAG Fusion keys).
- `WorkingDirectory=<data-dir>` and the `EnvironmentVariables` `ARCHON_SEARCH_CONFIG=<config path>` and `ARCHON_SEARCH_DATA_DIR=<data-dir>`. The plist is generated with Python's `plistlib`, so every value is XML-escaped and `&`, `<` or `>` in a path is safe. The wrapper passes the secrets-file and interpreter paths through `shlex.quote`, so spaces, quotes, `$`, backticks and `\` in them stay literal.
- Paths the plist or wrapper cannot carry are refused up front, each with its own reason:
  - A data-dir or config path that would not read back unchanged from the plist: one containing a C0 control character other than tab and newline (`\r` included: `plistlib` rewrites it as a newline when writing; DEL and C1 controls are accepted), one containing U+FFFE or U+FFFF, or one that is not valid UTF-8. The plist's wrapper and log paths live under `<data-dir>`, so the data-dir check covers them.
  - A data-dir or config path with leading or trailing whitespace, which the server strips from `ARCHON_SEARCH_DATA_DIR` / `ARCHON_SEARCH_CONFIG` and so would run on a different path (`ARCHON_SEARCH_DATA_DIR='/srv/data /'` yields a data dir ending in a space).
  - An interpreter path that is empty (Python could not determine it).
  - An interpreter or secrets-file path that contains NUL or is not valid UTF-8.
- `wizard` (including `--dry-run`) runs these checks before any download or config write, and `install` before stopping the running service, so a refused install leaves the current service running.
- On a refusal, `wizard` and `install` write no plist or wrapper and exit 1 with a single line: `Error during install:`, the reason, then the offending value.
- An existing plist is rewritten only when `archon-search install` or `archon-search wizard` registers the service again.
- Logs: stdout and stderr both go to `<data-dir>/logs/archon-search.log`.
- Survival across reboot: `launchd` user agents start on user login. There is no equivalent of `loginctl enable-linger`; if the host reboots and no one logs in, the service does not run.

Manual operations:

```bash
launchctl list com.archon.search
launchctl unload  ~/Library/LaunchAgents/com.archon.search.plist
launchctl load    ~/Library/LaunchAgents/com.archon.search.plist
```

## systemd (Linux)

`archon-search install` writes `~/.config/systemd/user/archon-search.service` via `archon_search/platform/linux.py`. The install path also runs `loginctl enable-linger $USER` so the agent survives logout. Defaults:

- `ExecStart="{python}" -m archon_search.server`, `Restart=always`, `RestartSec=5`.
- `WorkingDirectory=<data-dir>`, `Environment="ARCHON_SEARCH_CONFIG=<config path>"`, `Environment="ARCHON_SEARCH_DATA_DIR=<data-dir>"`. Each value is escaped for its directive, so spaces and `%` in paths are safe: `%` is doubled to `%%` everywhere; `ExecStart=` and `Environment=` values are double-quoted, and in the `Environment=` values `\` and `"` are also backslash-escaped; `WorkingDirectory=` is written unquoted and, when it contains `..`, resolved like the `EnvironmentFile=` path below.
- Values the service cannot run with are refused up front, each with its own reason. The first two checks are shared with macOS and run before the systemd-specific ones, so outer whitespace such as a trailing tab or newline in a data-dir or config path gets the server-strip reason:
  - An interpreter path that is empty (Python could not determine it).
  - A data-dir or config path with leading or trailing whitespace (anything Python's `str.strip()` removes, including the control characters `\x1c`–`\x1f` and `\x85` and the no-break space U+00A0, which therefore get this reason), which the server strips from `ARCHON_SEARCH_DATA_DIR` / `ARCHON_SEARCH_CONFIG` and so would run on a different path (`ARCHON_SEARCH_DATA_DIR='/srv/data /'` yields a data dir ending in a space).
  - An interpreter path containing `"`, `'`, `\`, `$`, `*`, `?` or `[` (systemd will not run it from `ExecStart=`; `*?[` since systemd v261).
  - Any interpreter, data-dir, config or secrets-file path containing a control character such as a newline (it would inject extra unit lines) or bytes that are not valid UTF-8.
  - A `WorkingDirectory=` or `EnvironmentFile=` path ending in an odd number of `\` (systemd would join the next line onto it), starting or ending in whitespace (systemd would strip it), or still containing `..` after resolution (systemd ignores a non-normalized path).
- The checks run on the values as written, after any `..` resolution. `wizard` (including `--dry-run`) runs them before any download or config write, and `install` before stopping the running service, so a refused install leaves the current service running.
- On a refusal, `wizard` and `install` write no unit and exit 1 with a single line: `Error during install:`, the reason, then the offending path.
- An existing unit is rewritten only when `archon-search install` or `archon-search wizard` registers the service again.
- `Nice=10`, `CPUQuota=50%` — the unit deliberately yields CPU to the rest of the host.
- `EnvironmentFile=-<data-dir>/.secrets.env` loads the HyDE / RAG Fusion provider keys; the `-` prefix makes the file optional. The path is rendered with any `%` doubled to `%%` (systemd expands `%` specifiers there) and, when it contains `..`, resolved to a normalized absolute path (systemd ignores a non-normalized one); a symlinked data dir is kept as the link unless the path contains `..`, which is resolved (following symlinks). Units written by older releases point at `~/.archon-search/.secrets.env` regardless of `ARCHON_SEARCH_DATA_DIR` — with a custom data dir, move any keys from `~/.archon-search/.secrets.env` into `<data-dir>/.secrets.env`, then re-run `archon-search install` with the same `ARCHON_SEARCH_DATA_DIR`, passing `--config <path-to-your-archon-search.toml>` unless the config lives at `<data-dir>/archon-search.toml`, to regenerate the unit.
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
- **Remote access required**: bind `127.0.0.1` and front it with a reverse proxy that terminates TLS and forwards to `127.0.0.1:8765`. Do **not** bind `0.0.0.0` directly outside a container: no TLS, no rate limiter, no IP filter. The Docker container is the exception — it binds `0.0.0.0` inside the container but the publish (`-p`) flag controls host-side reachability, and the shipped `docker-compose.yml` already publishes `127.0.0.1:8765:8765` (prod), `127.0.0.1:18765:8765` (dev) and `127.0.0.1:18766:8765` (test), so nothing is reachable from another host until you add a `ports:` entry for another interface (compose appends it; it does not replace) or put a proxy in front. With `docker run`, keep `-p 127.0.0.1:8765:8765`.

`install` (`archon_search/cli/install_cmd.py`) performs **no port-in-use pre-check**: it registers the service, starts it, then polls `GET /health` (`_wait_for_service` in `install.py`) up to a profile-derived timeout. If another process already owns the configured port, the service start command still succeeds (launchd/systemd accept it) and the child uvicorn then fails to bind; the health poll times out and the CLI exits with a warning. For port changes either set `[server].port` in `~/.archon-search/archon-search.toml` (`archon-search config set server.port N` — a fresh wizard install does not write the key) and restart, or set `ARCHON_SEARCH_PORT` in the environment (overrides TOML).

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

- The auth middleware exempts two paths from the `Bearer` requirement: `/health` and `/ready` (`_EXEMPT_PATHS` in `archon_search/server/middleware_auth.py`). Use `GET /ready` (or `GET /health` for pure liveness) as the proxy's upstream-health check. `/health` returns only `{"status":"running"}`. `/docs`, `/redoc` and `/openapi.json` are not served unless `[server].docs_enabled = true`, in which case they are served **without** a token: block them at the proxy if you enable it.
- CORS is off: the server sends no `Access-Control-*` header unless `[server].cors_origins` lists an origin (explicit `scheme://host[:port]`, never `*`). Configure the origins in `archon-search.toml`, not at the proxy. The server also refuses request bodies above `[server].max_request_body_mb` (default 32) with `413`, so keep any proxy body limit at or above it.

## Process supervision options

`launchd` and `systemd --user` are first-class for host installs. `docker` is first-class for containerised deployments — the shipped image runs `archon-search serve` under `tini` as PID 1; the orchestrator owns restart-on-crash (`docker-compose.yml` ships `restart: unless-stopped` for the production service). For everything else (`runit`, `s6`, `supervisord`, or your own supervisor) the contract is minimal:

- One process. The uvicorn server runs inside `run_server` (`archon_search/server/app.py`), invoked either by `archon-search serve` (foreground; host defaults to `127.0.0.1`, `0.0.0.0` in the container image), `python -m archon_search.server` (foreground; host honours `[server].host`), or `archon-search start` (which shells out to `launchctl`/`systemctl`, whose `ExecStart` is `"{python}" -m archon_search.server`). Both foreground entry points and the service units read the same `[server].host` default, so point your supervisor's `ExecStart` at either.
- Recognised environment overrides: `ARCHON_SEARCH_CONFIG` (config path), `ARCHON_SEARCH_API_KEY` (API key), `ARCHON_SEARCH_KEY_FILE` (key file path), plus `ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_PORT`, `ARCHON_SEARCH_DATA_DIR`, and `HF_HUB_OFFLINE` (honoured by the model downloader: a model not already in `<data dir>/models` then fails instead of downloading). The model weights always live under `<data dir>/models`, so a supervisor must give the server the same `ARCHON_SEARCH_DATA_DIR` the wizard ran with — otherwise the service looks for the weights the pre-warm downloaded in the wrong directory and downloads them again on first load. The registered plist/unit bake in `ARCHON_SEARCH_CONFIG=<config path chosen at install time>` — the `--config PATH` passed to `archon-search wizard` or `archon-search install` is honoured; when the flag is omitted it falls back to the same default config path the wizard writes to: `ARCHON_SEARCH_CONFIG` as set at install time, else `<data dir>/archon-search.toml` — plus `ARCHON_SEARCH_DATA_DIR=<resolved data dir>`. The Docker image bakes in `ARCHON_SEARCH_DATA_DIR=/data` and `ARCHON_SEARCH_CONTAINER=1`.
- Mounts: persistent volume on `~/.archon-search/` for host installs, or `/data` for the Docker image. One mounted volume covers `db_path`, `log_file`, `[telemetry].log_dir`, the key file, the jobs file, the model cache (`<data dir>/models`), and the ingest history.
- Signals and exit codes: `SIGTERM` (what Docker, systemd and launchd send) makes uvicorn stop accepting connections, give open requests up to `[server].graceful_shutdown_seconds` (default 25, below Docker's 30 s `stop_grace_period`) and then cancel them, and run the FastAPI lifespan shutdown: stop the filesystem watcher, drain the telemetry writer (`telemetry_writer.drain_and_stop()`), cancel background tasks (startup sync, model warm-up, model validation, running ingest jobs, which end as `CANCELLED`, not `FAILED`), then disconnect the stores. There is **no job-drain**: an in-flight ingest is cancelled, not awaited. Measured on Apple silicon (2026-10-25, real models): an idle server exits 0.14-0.27 s after `SIGTERM`, one under search/ingest load 0.2-0.8 s, and `docker stop` took 0.40 s; in-flight requests completed and no 5xx was seen. The process ends **by the signal**, so the exit status is **143** in a container shell or `docker inspect` (128 + 15), or `-15` to a parent that reads the raw wait status; do not treat it as a crash. `SIGINT` (Ctrl-C) exits 0 in about 0.8-1.1 s. `SIGINT` **during model warm-up** takes about 1.8-3.4 s (about 3 s with `bge-large` + `bge-reranker-base`) because the in-flight model load runs in a non-daemon thread that is joined at exit and cannot be interrupted; `SIGTERM` during warm-up is not delayed (0.2 s). Nothing is corrupted either way. The shipped `docker-compose.yml` sets `stop_grace_period: 30s`; tune your supervisor's stop timeout to your workload, and keep `graceful_shutdown_seconds` below it.


## Container image

The facts below were measured on 2026-10-25 with the `linux/arm64` image built on an Apple M4 Pro Docker VM. x86_64 and NVIDIA CUDA images were not run here.

- **Size.** `archon-search:local` is about **4.2 GB** (`docker images`). The build trims the COPY layer (125 MB to 5.4 MB through `.dockerignore`) and the pip layer (2.47 GB to 2.22 GB by pruning installed `tests/` directories), and adds about 530 MB of pre-fetched docling models (below); `claude_agent_sdk` (279 MB) and about 370 MB of `.pyc` files are kept. The size moves with the dependency versions, because the image installs unlocked latest dependencies.
- **CPU-only torch.** An `/etc/pip.conf` points pip at PyTorch's CPU wheel index, so the image carries `torch 2.14.1+cpu` with no `nvidia`/`cuda` packages; `torch.cuda.is_available()` is `False` and `[database].device = "auto"` resolves to `cpu`. There is no `mps` in a Linux container.
- **Non-root and read-only.** It runs as `appuser` (uid 1000), whose home directory is `/data`, so caches land on the volume. Under `--read-only --tmpfs /tmp -v data:/data -v pip:/pip-packages` the default image starts, ingests, searches and OCRs a PNG through docling; the **writable set is `/data`, `/tmp`, and `/pip-packages` whenever `ARCHON_EXTRAS` is non-empty** (the default is `graph,code,multilingual`). With `ARCHON_EXTRAS=` (empty) `/pip-packages` is not needed. Everything under `/data` is created `0700`/`0600`.
- **Docling models are baked in.** `scripts/prefetch-parse-models.py` runs the real parser on a generated scanned PDF and PNG at build time into `/opt/hf-cache` (`HF_HUB_CACHE`, owned by `appuser`), and the build fails if parsing fails. Without it the first PDF or image parse downloaded 506 MB inside `[ingest].parse_timeout_seconds` (a scanned-PDF ingest took about 71 s; it now takes about 6 s, and a parse with `--network none --read-only` took 4.5 s). RapidOCR's checkpoints ship inside its wheel. Embedder, reranker and graph weights still use `<data dir>/models` on the volume. The image also installs `libxcb1`, `libx11-6`, `libxext6`, `libglib2.0-0` and a minimal `libgl1` so `import cv2` works (a build-time guard checks it).
- **Health check.** `HEALTHCHECK --interval=15s --timeout=5s --start-period=600s --retries=3` requests `/ready` on `localhost:${ARCHON_SEARCH_PORT:-8765}`. The port comes from the **environment variable only**: a port set only in the TOML is not seen by the probe. A failed warm-up that left the default embedder or reranker cold turns the container `unhealthy`; the background retry clears it (see [20_monitoring_and_alerts.md](20_monitoring_and_alerts.md)).
- **Ports and extras.** Compose publishes `127.0.0.1:<port>` only (see [Binding and firewalling](#binding-and-firewalling)) and keeps `/pip-packages` on a named volume per service, so the first-start extras install (which needs network access and retries up to 3 times) is not repeated after `down`/`up`. The volume is re-filled when the image, the Python version or `ARCHON_EXTRAS` changes. The install records the exact artifacts its own resolve chose but does **not** verify hashes: a hash-pinned install needs a hash catalog generated at build time for every extras combination, and `--index-url`-only is not possible because PyPI has no Linux `torch+cpu`. Neither is implemented.
- **Parity tests inside the image are CPU-only by design.** The runtime-parity lane collects 145 test ids on the host and 73 in the image; the 72 missing ones are all `…-mps]` parametrizations, and a Linux container has no `mps`. The image run (torch 2.14.1+cpu) passes 69 and skips 4, against 139 passed and 6 skipped on the host (CPU plus `mps`); no case is dropped.
- **`claude-proxy` (dev override only).** `scripts/claude-proxy.py` refuses to start without `CLAUDE_PROXY_TOKEN`, requires the matching `X-Claude-Proxy-Token` header, accepts only loopback or `host.docker.internal` `Host` headers and rejects any request carrying an `Origin` header. `docker-compose.override.yml` and `scripts/claude-container-wrapper` pass the token through; export the same value in the shell that runs `docker compose`.
- **Shutdown.** `tini` forwards `SIGTERM`; `docker stop` took 0.40 s and the container exited 143 with no traceback (see [Process supervision options](#process-supervision-options)).

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
