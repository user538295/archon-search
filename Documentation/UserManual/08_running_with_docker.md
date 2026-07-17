**Purpose**: Run `archon-search` from the published Docker image — `docker run`, `docker compose`, and the build-arg matrix for CPU/GPU images.
**Audience**: End users / operators who want a portable, reproducible deployment unit.
**Status**: Stable
**Last reviewed**: 2026-06-12 / **Next review**: 2027-06-12

# Running with Docker

The Docker image is the only deployment unit that does not require a host-level service install. The container runs `archon-search serve` (see [`03_running_the_server.md`](03_running_the_server.md)) in the foreground, binds to `0.0.0.0:8765` inside the container, persists all runtime state under `/data`, and writes structured logs to stderr so `docker logs` works.

## Principles

1. **One container, one volume.** A single mounted volume at `/data` holds the LanceDB index, logs, telemetry JSONL, the API key file, the jobs file, the fastembed model cache, and the ingest history. There is no separate state directory to manage.
2. **Env vars first, TOML optional.** Every required deployment knob is reachable via an environment variable — `ARCHON_SEARCH_API_KEY`, `ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_PORT`, `ARCHON_SEARCH_DATA_DIR`. The TOML file is optional and is only read when you explicitly mount one and point `ARCHON_SEARCH_CONFIG` at it.
3. **Stderr is the only log sink by default.** `ARCHON_SEARCH_CONTAINER=1` is baked into the image; `logging_setup.configure_logging()` attaches a `StreamHandler(sys.stderr)` so `docker logs` (and Kubernetes log collectors) capture every line.
4. **Non-root by default.** The image runs as UID 1000 (`appuser`). `/data` is pre-`chown`ed so anonymous-volume runs (`docker run` without `-v`) work. If you bind-mount a host directory, make sure UID 1000 can write to it (`chown 1000:1000 /path/to/data` or `chmod 0777`).
5. **Single writer.** LanceDB is single-writer per `db_path`. Do not mount the same volume into more than one running container.

## Image variants

| Tag | Base image | Use when |
| --- | --- | --- |
| `:latest`, `:<version>` | `python:3.12-slim` | Any host without an NVIDIA GPU. CPU inference only. |
| `:gpu`, `:<version>-gpu` | `nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04` | NVIDIA host with the container toolkit installed. Uses `onnxruntime-gpu` instead of CPU `onnxruntime`. |

Both images embed the source commit as `org.opencontainers.image.revision`. Inspect a running tag with:

```bash
docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
  ghcr.io/user538295/archon-search:gpu
```

A failed GPU build does **not** delete the previous floating `:gpu` tag — operators pulling `:gpu` may receive an older successful build. Read the release notes for the version you expect, and confirm the SHA above matches.

## `docker run` quickstart

Ephemeral — key regenerates every restart, no persistence:

```bash
docker run --rm -p 8765:8765 ghcr.io/user538295/archon-search:latest
```

Production — supply the key via env, persist runtime state to a named volume:

```bash
docker run -d \
  --name archon-search \
  -e ARCHON_SEARCH_API_KEY=$ARCHON_SEARCH_API_KEY \
  -v archon-search-data:/data \
  -p 8765:8765 \
  ghcr.io/user538295/archon-search:latest
```

Smoke-test from the host:

```bash
curl http://127.0.0.1:8765/health    # liveness — always 200 when the process is up
curl http://127.0.0.1:8765/ready     # readiness — 200 once storage is connected, 503 before
curl -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" http://127.0.0.1:8765/status
```

The image declares a `HEALTHCHECK` that polls `/ready` every 15s after a 30s start-period, so `docker ps` shows `(healthy)` once the storage layer is up.

## `docker compose`

The shipped [`docker-compose.yml`](../../docker-compose.yml) declares three sibling services — `archon-dev`, `archon-test`, `archon-prod` — each with its own named volume and host port. The compose file uses variable substitution for the image, so you can override it locally:

```bash
# .env (next to docker-compose.yml; see .env.example)
ARCHON_SEARCH_API_KEY=replace-me-with-a-64-char-hex-string
ARCHON_SEARCH_IMAGE=archon-search:latest   # uncomment for a locally-built image
```

```bash
docker compose up archon-dev
docker compose down archon-dev
```

| Service | Host port | Volume | Restart policy |
| --- | --- | --- | --- |
| `archon-dev` | `18765` | `archon-dev-data` | none |
| `archon-test` | `18766` | `archon-test-data` | none |
| `archon-prod` | `8765` | `archon-prod-data` | `unless-stopped` |

Each service has `stop_grace_period: 30s` so `docker compose down` lets uvicorn drain in-flight HTTP requests before the runtime kills it.

### Sharing the fastembed model cache

By default each service downloads its own fastembed model weights into its own `/data/fastembed-cache`. To share the cache across services and avoid re-downloading several hundred MB on every container recreate, follow the three-step uncomment procedure marked in `docker-compose.yml`:

1. Uncomment `FASTEMBED_CACHE_PATH: /data/fastembed-cache` in the `environment:` block of every service that should share it.
2. Uncomment `- archon-model-cache:/data/fastembed-cache` in the `volumes:` block of those services.
3. Uncomment the `archon-model-cache:` declaration at the bottom of the file under `volumes:`.

All three must be uncommented together — compose rejects the config otherwise.

## Environment variables

The container reads the same env vars as a host-installed server, plus two that are pre-set in the image:

| Variable | Default in container | Effect |
| --- | --- | --- |
| `ARCHON_SEARCH_API_KEY` | unset | Bearer token. If unset and no `/data/.search.env` exists, the server auto-generates one on startup. **Without a persistent volume this regenerates every restart.** |
| `ARCHON_SEARCH_HOST` | `0.0.0.0` (set by `serve`) | Bind address. The `serve` subcommand defaults host to `0.0.0.0`; explicit env or TOML still wins. |
| `ARCHON_SEARCH_PORT` | `8765` | Bind port. 1–65535. |
| `ARCHON_SEARCH_DATA_DIR` | `/data` (baked into image) | Root of every runtime path: `db_path` (`/data/search`), `log_file` (`/data/logs/archon-search.log`), `telemetry.log_dir` (`/data/search-logs`), key file (`/data/.search.env`), managed key store (`/data/keys.json`), jobs file (`/data/archon-search-jobs.json`), fasttext models (`/data/models`), ingest history (`/data/history/sessions`). Do not change unless you also remount the volume. |
| `ARCHON_SEARCH_CONTAINER` | `1` (baked into image) | Adds `StreamHandler(sys.stderr)` to the `archon_search` logger so `docker logs` captures output. |
| `FASTEMBED_CACHE_PATH` | `/data/fastembed-cache` (baked into image) | Persists fastembed-downloaded model weights on the mounted volume instead of the ephemeral container layer. |
| `ARCHON_SEARCH_KEY_FILE` | unset | Overrides the key file path. Takes precedence over `ARCHON_SEARCH_DATA_DIR` for the key file only. |
| `ARCHON_SEARCH_CONFIG` | unset | Points at a TOML config file. Required if you want `archon-search collection add` to work inside the container — that command sends the path to the server which writes it to TOML, and without `ARCHON_SEARCH_CONFIG=/data/archon-search.toml` the server will try to write outside the mounted volume. `collection remove` now proxies to the server (no direct TOML write on the CLI side). The `serve` subcommand logs a warning at startup when `ARCHON_SEARCH_DATA_DIR` is set but `ARCHON_SEARCH_CONFIG` is not. |

## Persistence layout

When `/data` is mounted, the volume looks like this after a few requests:

```
/data
├── .search.env                  # API key, mode 0600
├── keys.json                    # Managed key store (D7), mode 0600; created on first key create/rotate
├── archon-search-jobs.json      # JobStore, atomic-rename writes
├── search/                      # LanceDB index
├── logs/
│   └── archon-search.log        # only when [logging].log_file is non-empty
├── search-logs/
│   └── YYYY-MM-DD.jsonl         # telemetry, when [telemetry].enabled=true
├── models/
│   └── lid.176.ftz              # fasttext language detector (multilingual only)
├── fastembed-cache/             # fastembed model weights
└── history/
    └── sessions/                # history sessions directory
```

## TLS termination

The container speaks plaintext HTTP only. Put a reverse proxy (nginx, Caddy, Traefik) in front of it for any non-loopback exposure. See [`OperatorGuide/01_deployment_topologies.md`](../OperatorGuide/01_deployment_topologies.md) for the reverse-proxy patterns.

## Known limitations

- **`GET /ready` does not gate readiness on model availability** — `ready` reflects only whether the LanceDB storage layer is connected. D6 added a `checks.models` field that reports model-validation state (`pending`/`ok`/`warn`/`fail`) but does not affect the HTTP status or the `ready` flag. The first `/search` after a cold start may still pay a multi-second model-load tax.
- **In-flight ingest jobs are not awaited on SIGTERM** — the container exits cleanly, but a job in progress is marked `FAILED` on the next start. Tune `stop_grace_period` to your workload.
- **`archon-search collection add` writes to the TOML file via the server** — see `ARCHON_SEARCH_CONFIG` above. `collection remove` also proxies through the server. Operators who need dynamic collection management inside the container must mount a config file under `/data` and point `ARCHON_SEARCH_CONFIG` at it.
- **No Apple Silicon / Metal GPU image.** Apple GPUs are not supported in v1.

## See also

- [`03_running_the_server.md`](03_running_the_server.md) — the `serve` subcommand and the rest of the CLI lifecycle.
- [`01_installation.md`](01_installation.md) — host-level install via the wizard, for non-container deployments.
- [`09_multi_instance_setup.md`](09_multi_instance_setup.md) — running a native-service prod instance and a Docker dev-UAT instance side by side on the same machine.
- [`OperatorGuide/01_deployment_topologies.md`](../OperatorGuide/01_deployment_topologies.md) — reverse-proxy patterns and topology comparison.
- [`Architecture/160_operational_readiness_monitoring_and_reliability.md`](../Architecture/160_operational_readiness_monitoring_and_reliability.md) — health/ready surface, runbooks.
