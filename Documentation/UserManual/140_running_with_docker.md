**Purpose**: Run `archon-search` from the published Docker image — `docker run`, `docker compose`, and the build-arg matrix for CPU/GPU images.
**Audience**: End users / operators who want a portable, reproducible deployment unit.
**Status**: Stable
**Last reviewed**: 2026-07-29 / **Next review**: 2027-07-29

# Running with Docker

The Docker image is the only deployment unit that does not require a host-level service install. The container runs `archon-search serve` (see [`40_running_the_server.md`](40_running_the_server.md)) in the foreground, binds to `0.0.0.0:8765` inside the container, persists all runtime state under `/data`, and writes structured logs to stderr so `docker logs` works.

## Principles

1. **One container, one volume.** A single mounted volume at `/data` holds the LanceDB index, logs, telemetry JSONL, the API key file, the jobs file, the fastembed model cache, and the ingest history. There is no separate state directory to manage.
2. **Env vars first, TOML optional.** Every required deployment knob is reachable via an environment variable — `ARCHON_SEARCH_API_KEY`, `ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_PORT`, `ARCHON_SEARCH_DATA_DIR`. The TOML file is optional and is only read when you explicitly mount one and point `ARCHON_SEARCH_CONFIG` at it.
3. **Stderr is the only log sink by default.** `ARCHON_SEARCH_CONTAINER=1` is baked into the image; `logging_setup.configure_logging()` attaches a `StreamHandler(sys.stderr)` so `docker logs` (and Kubernetes log collectors) capture every line.
4. **Non-root by default.** The image runs as UID 1000 (`appuser`). `/data` is pre-`chown`ed so anonymous-volume runs (`docker run` without `-v`) work. If you bind-mount a host directory, make sure UID 1000 can write to it (`chown 1000:1000 /path/to/data` or `chmod 0777`).
5. **Single writer.** LanceDB is single-writer per `db_path`. Do not mount the same volume into more than one running container.
6. **Optional extras are installed at runtime, not bake-time.** The base image ships only the core package. Optional feature sets (`graph`, `code`, `multilingual`) are pip-installed by the entrypoint script on first start into a `/pip-packages` named volume, so the image stays lean and extras are cached across restarts.

## Entrypoint and runtime extras installation

The container entrypoint is `scripts/docker-entrypoint.sh` (copied to `/entrypoint.sh` in the image) and runs under `tini` as PID 1. Before handing off to `archon-search serve`, it:

1. Reads `ARCHON_EXTRAS` (default `graph,code,multilingual`; empty string = core-only, no install).
2. Checks a stamp file at `/pip-packages/.extras-installed`. If the stamp is absent or the extras list has changed, runs `python3 -m pip install --no-cache-dir --target /pip-packages ".[${ARCHON_EXTRAS}]"`.
3. If `graph` is in `ARCHON_EXTRAS`, downloads the spaCy model `en_core_web_sm` into `/pip-packages` if not already present (required by `graph.enabled = true`).
4. Prepends `/pip-packages` to `PYTHONPATH` and execs the CMD (`archon-search serve`).

**First start** triggers a pip install whose duration is network-bound. The image bakes `PIP_NO_CACHE_DIR=1`, so every first start on a fresh `/pip-packages` volume re-downloads the full dependency set — typically a few minutes on a fast idle uplink, and longer on a slow or contended one. The `HEALTHCHECK` allows up to 10 minutes (600s start-period) before it counts failures. Subsequent starts are instant (stamp matches). Mount `/pip-packages` as a named volume to persist the install across container recreates:

```bash
docker volume create archon-search-packages
docker run -d \
  --name archon-search \
  -e ARCHON_SEARCH_API_KEY=$ARCHON_SEARCH_API_KEY \
  -v archon-search-data:/data \
  -v archon-search-packages:/pip-packages \
  -p 8765:8765 \
  ghcr.io/user538295/archon-search:latest
```

To skip extras entirely (lean core-only deployment), pass an empty value:

```bash
docker run ... -e ARCHON_EXTRAS="" ...
```

When `ARCHON_EXTRAS` is set to an empty string, the entrypoint skips the pip install block and proceeds directly to exec. Note: if `ARCHON_EXTRAS` is **unset** (not passed at all), the default `graph,code,multilingual` applies.

## Image variants

The CPU / GPU variant is chosen at **build time** via the `BASE_IMAGE` build-arg (see the `Dockerfile` header); the release workflow publishes both variants to GHCR under these tags:

| Tag | Base image | Use when |
| --- | --- | --- |
| `:latest`, `:<version>` | `python:3.12-slim` | Any host without an NVIDIA GPU. CPU inference only. |
| `:gpu`, `:<version>-gpu` | `nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04` | NVIDIA host with the container toolkit installed. Uses `onnxruntime-gpu` instead of CPU `onnxruntime` (fastembed 0.8.0 ships no `[gpu]` extra, so the GPU image swaps it manually). |

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

The image declares a `HEALTHCHECK` that polls `/ready` every 15s after a 600s start-period. The generous start-period accommodates the network-bound first-start extras install (see "Entrypoint and runtime extras installation" above); subsequent starts are instant (stamp matches) but the start-period is fixed at image level. `docker ps` shows `(healthy)` once the storage layer is up.

## `docker compose`

The shipped [`docker-compose.yml`](../../docker-compose.yml) declares three sibling services — `archon-dev`, `archon-test`, `archon-prod` — each with its own named volume and host port. The compose file uses variable substitution for the image (`ARCHON_SEARCH_IMAGE`), so you can override it locally:

```bash
# .env (next to docker-compose.yml; see .env.example)
ARCHON_SEARCH_IMAGE=ghcr.io/user538295/archon-search:25.6.1   # pin a release tag
# ARCHON_SEARCH_IMAGE=archon-search:latest                    # or a locally-built image
ARCHON_SEARCH_API_KEY=replace-me-with-a-64-char-hex-string
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

> **Multi-instance warning:** `docker-compose.yml` interpolates `ARCHON_SEARCH_API_KEY` into *every* service. Setting it in `.env` silently gives all three the same key. Leave it unset for multi-instance runs and let each instance auto-generate its own key from its own volume — see [`150_multi_instance_setup.md`](150_multi_instance_setup.md).

### Sharing the fastembed model cache

By default each service downloads its own fastembed model weights into its own `/data/fastembed-cache`. To share the cache across services and avoid re-downloading several hundred MB on every container recreate, follow the three-step uncomment procedure marked in `docker-compose.yml`:

1. Uncomment `FASTEMBED_CACHE_PATH: /data/fastembed-cache` in the `environment:` block of every service that should share it.
2. Uncomment `- archon-model-cache:/data/fastembed-cache` in the `volumes:` block of those services.
3. Uncomment the `archon-model-cache:` declaration at the bottom of the file under `volumes:`.

All three must be uncommented together — compose rejects the config otherwise (`undefined volume`).

## Environment variables

The container reads the same env vars as a host-installed server, plus several that are pre-set in the image:

| Variable | Default in container | Effect |
| --- | --- | --- |
| `ARCHON_SEARCH_API_KEY` | unset | Bearer token. If unset and no `/data/.search.env` exists, the server auto-generates one on startup. **Without a persistent volume this regenerates every restart.** |
| `ARCHON_SEARCH_HOST` | `0.0.0.0` (set by `serve`) | Bind address. The `serve` subcommand defaults host to `0.0.0.0`; explicit env or TOML still wins. |
| `ARCHON_SEARCH_PORT` | `8765` | Bind port. 1–65535. |
| `ARCHON_SEARCH_DATA_DIR` | `/data` (baked into image) | Root of every runtime path: `db_path` (`/data/search`), `log_file` (`/data/logs/archon-search.log`), `telemetry.log_dir` (`/data/search-logs`), key file (`/data/.search.env`), managed key store (`/data/keys.json`), jobs file (`/data/archon-search-jobs.json`), fasttext models (`/data/models`), ingest history (`/data/history/sessions`). One env var relocates the entire runtime tree — do not change it unless you also remount the volume. |
| `ARCHON_SEARCH_CONTAINER` | `1` (baked into image) | Adds `StreamHandler(sys.stderr)` to the `archon_search` logger so `docker logs` captures output. |
| `FASTEMBED_CACHE_PATH` | `/data/fastembed-cache` (baked into image) | Persists fastembed-downloaded model weights on the mounted volume instead of the ephemeral container layer. |
| `ARCHON_SEARCH_KEY_FILE` | unset | Overrides the key file path. Takes precedence over `ARCHON_SEARCH_DATA_DIR` for the key file only. |
| `ARCHON_SEARCH_CONFIG` | unset | Points at a TOML config file. Required if you want `archon-search collection add` to work inside the container — that command sends the path to the server which writes it to TOML, and without `ARCHON_SEARCH_CONFIG=/data/archon-search.toml` the server will try to write outside the mounted volume. `collection remove` also proxies through the server. |
| `ARCHON_EXTRAS` | `graph,code,multilingual` | Comma-separated list of optional package extras installed by the entrypoint on first start into `/pip-packages`. Set to `""` to skip extras and run core-only. Change the value and the entrypoint re-installs on the next start (stamp-based detection). |
| `HOME` | `/data` (baked into image) | Required so pip operations inside the container write to the persistent volume rather than the ephemeral layer. Do not override unless you also relocate `/data`. |

## Persistence layout

When `/data` is mounted, the volume looks like this after a few requests:

```
/data
├── .search.env                  # API key, mode 0600
├── keys.json                    # Managed key store, mode 0600; created on first key create/rotate
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

When `/pip-packages` is mounted (recommended), it holds the optional-extras install:

```
/pip-packages
├── .extras-installed            # stamp file: contents = last installed ARCHON_EXTRAS value
├── en_core_web_sm/              # spaCy model (present when graph extra is installed)
└── <wheel contents …>           # graph, code, multilingual extras and their transitive deps
```

## Development workflow (claude_cli + full extras)

`docker-compose.override.yml` in the repo root defines an `archon-dev` service that wires up a full-featured dev container: multilingual embeddings, graph extraction, HyDE + RAG Fusion via the `claude_cli` provider, and a fastembed model cache bind-mounted from the Mac host.

### Prerequisites

- `~/.cache/fastembed` populated (run `uv run archon-search serve` once on the host, or copy from another machine).
- Claude Code installed on the Mac host and logged in.

### Steps

1. **Start the host-side claude proxy** in a separate terminal:

   ```bash
   python3 scripts/claude-proxy.py
   # claude proxy listening on 127.0.0.1:18766
   ```

   This HTTP server accepts `POST /` from inside the container, runs the real `claude` binary on the Mac host, and streams stdout back. The container mounts `scripts/claude-container-wrapper` at `/usr/local/bin/claude` — a minimal Python shim that POSTs to `http://host.docker.internal:18766`.

2. **Start the dev container:**

   ```bash
   docker compose up archon-dev
   ```

   The override file mounts:
   - `archon-dev-data:/data` — persistent LanceDB index and key file
   - `archon-dev-packages:/pip-packages` — cached optional extras
   - `./archon-search.docker-dev.toml:/config/archon-search.toml:ro` — dev config (full profile, graph enabled, claude_cli HyDE/RAG Fusion)
   - `~/.cache/fastembed:/data/fastembed-cache` — host fastembed model cache (avoids re-downloading ~500 MB)
   - `~/.archon-search/models:/data/models` — host fasttext model cache

3. **First start only:** the entrypoint installs graph + code + multilingual extras and downloads `en_core_web_sm` — a network-bound download that takes a few minutes (longer on a slow uplink; the image allows up to 10 minutes before the healthcheck counts failures). Watch progress with `docker compose logs -f archon-dev`.

4. **Smoke-test:**

   ```bash
   KEY=$(docker compose exec archon-dev cat /data/.search.env | grep -o '[^=]*$')
   curl -H "Authorization: Bearer $KEY" http://127.0.0.1:8765/status
   ```

The dev config (`archon-search.docker-dev.toml`) enables:
- `[database] profile = "max"` with `multilingual-e5-large` embeddings and `jina-reranker-v2-base-multilingual`
- `[graph] enabled = true`
- `[hyde] provider = "claude_cli" model = "haiku"`
- `[rag_fusion] provider = "claude_cli" model = "haiku"`

## CLI behavior in Docker

When `ARCHON_SEARCH_CONTAINER=1` is set (baked into the image), the CLI adapts its behavior to the container context.

### What works

All commands that contact the running server via HTTP work identically inside the container:

| Command | Notes |
|---|---|
| `archon-search --help`, `--version` | Offline; always exit 0 |
| `archon-search config show` | Reads local TOML; no server required |
| `archon-search status --api-url <url> --api-key <key>` | HTTP fallback — shows telemetry from `GET /status` |
| `archon-search key list --api-url <url> --api-key <key>` | HTTP |
| `archon-search collection list --api-url <url> --api-key <key>` | Proxies `GET /collections/` |
| `archon-search collection info <name> --api-url <url> --api-key <key>` | HTTP |
| `archon-search collection add <path> --wait --api-url <url> --api-key <key>` | HTTP; use `--wait` to confirm job completion |
| `archon-search ingest --path <file> --collection <name> --wait --api-url <url> --api-key <key>` | HTTP; use `--wait` to confirm job completion |
| `archon-search jobs status <id> --api-url <url> --api-key <key>` | HTTP |
| `archon-search maintenance run --api-url <url> --api-key <key>` | HTTP |

### Service management commands (clean error, not a traceback)

`start`, `stop`, `install`, and `uninstall` manage the host-level service (systemd/launchd) and are meaningless in a container. In container mode they emit a single actionable message and exit 1 — no Python traceback:

```
Service management is not available in container mode. Use 'archon-search serve' to run the server.
```

### `status` HTTP fallback

`status` has two information sources: the platform service layer (systemctl) and the HTTP `/status` endpoint. In container mode, systemctl is absent and the service-section line (`stopped`) is suppressed. The `_fetch_server_status()` call runs unconditionally — when the server is reachable, the full HTTP telemetry is shown; when unreachable, the command exits 0 silently (no traceback).

Example (server running inside the container):

```bash
archon-search status --api-url http://127.0.0.1:8765 --api-key $ARCHON_SEARCH_API_KEY
# Telemetry: enabled
#   hash_doc_ids_enabled: False
# Collections: 2
#   smoke (1 document)
# ...
```

The exact fields rendered depend on the server's telemetry config and the ingested corpus. When no collections are present, the `Collections:` line is omitted entirely.

## TLS termination

The container speaks plaintext HTTP only. Put a reverse proxy (nginx, Caddy, Traefik) in front of it for any non-loopback exposure. See [`../OperatorGuide/10_deployment_topologies.md`](../OperatorGuide/10_deployment_topologies.md) for the reverse-proxy patterns.

## Known limitations

- **`GET /ready` does not gate readiness on model availability** — `ready` reflects only whether the LanceDB storage layer is connected. A `checks.models` field reports model-validation state (`pending`/`ok`/`warn`/`fail`) but does not affect the HTTP status or the `ready` flag. The first `/search` after a cold start may still pay a multi-second model-load tax.
- **In-flight ingest jobs are not awaited on SIGTERM** — the container exits cleanly, but a job in progress is marked `FAILED` on the next start. Tune `stop_grace_period` to your workload.
- **`archon-search collection add` writes to the TOML file via the server** — see `ARCHON_SEARCH_CONFIG` above. `collection remove` also proxies through the server. Operators who need dynamic collection management inside the container must mount a config file under `/data` and point `ARCHON_SEARCH_CONFIG` at it.
- **No Apple Silicon / Metal GPU image.** Apple GPUs are not supported in v1.

## Development and testing with Docker

Separate from the production image above, `docker-compose.override.yml` defines two containers — built from `Dockerfile.test`, not the production `Dockerfile` — for running the test suite in a clean Linux environment. They mount your source live and run as a non-root user (uid 1000).

- **`archon-test-runner`** — one-shot. Runs the full suite plus the smoke tests, then exits:

  ```bash
  docker compose build archon-test-runner        # one-time
  docker compose run --rm archon-test-runner      # full suite + smoke
  ```

- **`archon-dev-shell`** — persistent. Start it, shell in to work interactively, stop it when done:

  ```bash
  docker compose up -d archon-dev-shell
  docker compose exec archon-dev-shell bash       # run pytest / serve inside
  docker compose stop archon-dev-shell
  ```

Both share a named venv volume (`archon-docker-venv`), so the one-time install (core + `graph` extra + spaCy model) is paid once and reused across both services and across restarts. Model weights are bind-mounted from your host `~/.cache/fastembed`.

For the full explanation — volume architecture, the two-phase test split, and why the graph extra matters — see [`../docker-test-runner.md`](../docker-test-runner.md).

## Related documents

- [`00_index.md`](00_index.md) — UserManual table of contents and reading order.
- [`40_running_the_server.md`](40_running_the_server.md) — the `serve` subcommand and the rest of the CLI lifecycle.
- [`10_installation.md`](10_installation.md) — host-level install via the wizard, for non-container deployments.
- [`150_multi_instance_setup.md`](150_multi_instance_setup.md) — running a native-service prod instance and a Docker dev-UAT instance side by side on the same machine.
- [`../OperatorGuide/10_deployment_topologies.md`](../OperatorGuide/10_deployment_topologies.md) — reverse-proxy patterns and topology comparison.
- [`../Architecture/160_operational_readiness_monitoring_and_reliability.md`](../Architecture/160_operational_readiness_monitoring_and_reliability.md) — health/ready surface, runbooks.
</content>
</invoke>
