**Purpose**: Run a native-service production instance and a Docker dev-UAT instance side by side on the same machine.
**Audience**: Developers and operators who need an isolated second environment for e2e live tests or release-candidate validation.
**Status**: Stable
**Last reviewed**: 2026-06-25 / **Next review**: 2027-06-25

# Multi-Instance Setup (Prod + Dev-UAT)

Two `archon-search` instances can run on the same machine today using existing isolation primitives — `ARCHON_SEARCH_DATA_DIR`, `ARCHON_SEARCH_PORT`, and named Docker volumes — but this requires a deliberate split of deployment modes:

- **Prod** runs as a native OS service (launchd on macOS, systemd on Linux). It gets GPU acceleration and OS-managed lifecycle (`archon-search start`/`stop`/`status`).
- **Dev-UAT** runs as the `archon-dev` Docker Compose service (port 18765). It gets a version-pinned, throwaway environment with a separate named volume.

The OS service layer supports **one hardcoded service name per user** (`com.archon.search` on macOS, `archon-search` on Linux). A second native service is not supported. Docker is the correct isolation boundary for dev-UAT.

> **Single-writer constraint.** LanceDB allows only one writer per `db_path`. Two containers (or a container and a native service) sharing the same directory simultaneously will produce undefined index state. Each instance **must** have its own data directory and its own named volume.

---

## Architecture overview

```
Host machine
├── Native prod service (archon-search start/stop)
│   ├── Port: 127.0.0.1:8765
│   ├── Data dir: ~/.archon-search/
│   ├── Config: ~/.archon-search/archon-search.toml
│   └── API key: ~/.archon-search/.search.env
│
└── Docker dev-UAT (docker compose up archon-dev)
    ├── Port: 0.0.0.0:18765 → container 0.0.0.0:8765
    ├── Data dir: /data inside archon-dev-data named volume
    └── API key: /data/.search.env inside the volume
```

Each instance has its own:

| Isolation boundary | Prod (native) | Dev-UAT (Docker) |
|----|----|----|
| Data directory | `~/.archon-search/` | `archon-dev-data` Docker volume (`/data`) |
| API key file | `~/.archon-search/.search.env` | `/data/.search.env` inside volume |
| LanceDB index | `~/.archon-search/search/` | `/data/search/` inside volume |
| Config TOML | `~/.archon-search/archon-search.toml` | `/data/archon-search.toml` (if `ARCHON_SEARCH_CONFIG` set); otherwise the in-container default |
| Fastembed model cache | Host default (`~/.cache/fastembed`) | Container default; mount `archon-model-cache:/data/fastembed-cache` to persist across restarts (see `08_running_with_docker.md`) |
| Host port | `8765` | `18765` |
| MCP endpoint | `127.0.0.1:8765/mcp` | `127.0.0.1:18765/mcp` |

> **Two isolation boundaries are NOT controlled by `ARCHON_SEARCH_DATA_DIR`:** the TOML config path (controlled by `ARCHON_SEARCH_CONFIG`) and the fastembed embedding model cache (controlled by `FASTEMBED_CACHE_PATH`, a fastembed-native env var). These have independent defaults for native and Docker deployments. For fastembed cache sharing across multiple Docker instances, see the [shared cache section in `08_running_with_docker.md`](08_running_with_docker.md#sharing-the-fastembed-model-cache).

---

## Prerequisites

- `archon-search` installed from PyPI or a checkout — see [`01_installation.md`](01_installation.md).
- Docker and Docker Compose installed and the Docker daemon running.
- The `docker-compose.yml` from the repository root (it ships three sibling services: `archon-dev`, `archon-test`, `archon-prod`).
- A `.env` file next to `docker-compose.yml` with the correct image path (instructions below).

---

## Part 1 — Start the native prod instance

If you have not already installed prod as a service:

```bash
archon-search wizard       # interactive setup: choose profile, download models, register and start
```

The wizard handles model download, profile selection, and optional features (telemetry, reranker, HyDE/RAG-fusion, log format, and more). When it completes, the service is registered and running.

Or, to register and start using a config that `wizard` already created:

```bash
archon-search install      # register the plist/unit and start the service; requires wizard to have run first
```

Verify the service is running:

```bash
archon-search status       # should show: running (PID <n>, uptime <s>s)
curl http://127.0.0.1:8765/health
# {"status":"running","version":"...","mcp":{...}}
```

To start or stop after the initial install:

```bash
archon-search start
archon-search stop
```

**macOS — what gets created:**

- `~/Library/LaunchAgents/com.archon.search.plist` — launchd plist with `Label=com.archon.search` and `WorkingDirectory` set to the fully-expanded home path (e.g., `/Users/<username>/.archon-search` — the plist stores the expanded path, not the tilde form).
- Prod data directory: `~/.archon-search/`
- Config TOML: `~/.archon-search/archon-search.toml`

**Linux — what gets created:**

- `~/.config/systemd/user/archon-search.service` — systemd user unit with `ExecStart` pointing at the installed Python and `Environment=ARCHON_SEARCH_CONFIG=<path-to-toml>`.

**Port conflict (native service):**

If port 8765 is already taken, the server process crashes and launchd restarts it in a loop (`KeepAlive = true` in the plist). Detect a cycling process:

```bash
launchctl list | grep com.archon.search   # PID of "-" = process is failing
```

For Linux, use:

```bash
systemctl --user status archon-search   # shows "failed" with a cycling PID
journalctl --user -u archon-search --since "5 minutes ago"   # shows the bind error
```

Check the log at `~/.archon-search/logs/archon-search.log`. To move prod to a different port, edit `~/.archon-search/archon-search.toml`:

```toml
[server]
port = 9765   # pick an unused port
```

Alternatively, set `ARCHON_SEARCH_PORT` as an environment variable (e.g., in the plist `EnvironmentVariables` dict on macOS or the systemd unit `Environment=` line on Linux). The env var takes precedence over the TOML value.

Then restart:

```bash
archon-search stop && archon-search start
```

---

## Part 2 — Start the Docker dev-UAT instance

### Step 1 — Create `.env`

Copy `.env.example` to `.env` next to `docker-compose.yml`, then edit the image tag:

```bash
cp .env.example .env
```

**Set the image tag** — the `.env.example` ships with `ARCHON_SEARCH_IMAGE` already uncommented. Replace the `TAG` placeholder with an actual release tag (e.g. `25.6.1`):

```dotenv
ARCHON_SEARCH_IMAGE=ghcr.io/user538295/archon-search:TAG
```

Replace `TAG` with the version you want to pin (e.g. `25.6.1`). Pinning prevents `docker compose pull` from silently upgrading your test environment. If you leave `TAG` literally, the pull will fail — the literal string `TAG` is not a valid image tag.

> **`ARCHON_SEARCH_API_KEY` is already commented out in `.env.example`** — do not uncomment it. The `docker-compose.yml` passes `${ARCHON_SEARCH_API_KEY:-}` to every service via environment substitution, so a non-empty value in `.env` (or your shell environment) overrides auto-generation for all services simultaneously, defeating per-instance key isolation silently. Leave the key line commented out and let each container auto-generate its own key from its own named volume.

### Step 2 — Start dev-UAT

```bash
docker compose up archon-dev -d
```

> **Always specify the service name `archon-dev`.** Bare `docker compose up` starts all three services including `archon-prod` on port 8765, which **conflicts** with the native prod instance already running on that port.

Poll until healthy:

```bash
until curl -sf http://127.0.0.1:18765/ready > /dev/null; do sleep 1; done
echo "dev-UAT ready"
```

Or watch the logs:

```bash
docker compose logs -f archon-dev
```

Confirm:

```bash
curl http://127.0.0.1:18765/health
# {"status":"running","version":"...","mcp":{...}}
```

> **Network exposure:** Docker binds `18765` on all host interfaces (`0.0.0.0`) by default. If you want loopback-only binding (recommended for a dev machine not behind a firewall), change the ports line in `docker-compose.yml` to `"127.0.0.1:18765:8765"`.

**Port conflict (Docker):**

If port 18765 is already occupied, Docker Compose logs the bind error and the container exits with a non-zero code:

```bash
docker compose logs archon-dev   # look for "address already in use"
lsof -i :18765                   # find the occupying process
```

To move dev-UAT to a different host port, edit the `ports` line in `docker-compose.yml` under `archon-dev` directly:

```yaml
ports:
  - "28765:8765"   # change 18765 to any free port
```

Or simply stop the conflicting process first (`lsof -i :18765` to identify it).

### Step 3 — Verify both instances are running

```bash
curl http://127.0.0.1:8765/health    # prod
curl http://127.0.0.1:18765/health   # dev-UAT
```

Both should return HTTP 200.

---

## Part 3 — Verify data isolation

Data written to prod must not be visible to dev-UAT and vice versa. The key check is that each instance's LanceDB index lives in a separate directory.

**Prod data location:**

```
~/.archon-search/
├── search/        ← LanceDB index
├── .search.env    ← API key
└── ...
```

**Dev-UAT data location (inside `archon-dev-data` Docker volume):**

```bash
docker compose exec archon-dev ls /data
# search/   .search.env   ...
```

To confirm search isolation, ingest a document to prod, then search dev-UAT for it:

```bash
# First, set your keys (see Part 4 for retrieval commands)
PROD_KEY=$(grep -o '[^=]*$' ~/.archon-search/.search.env)
DEV_KEY=$(docker compose exec -T archon-dev cat /data/.search.env | grep -o '[^=]*$' | tr -d '\r')

# Ingest to prod — POST /ingest returns 202 (async job)
JOB_ID=$(curl -s -X POST http://127.0.0.1:8765/ingest \
  -H "Authorization: Bearer $PROD_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection":"test-isolation","path":"/path/to/your/file.md"}' \
  | grep -o '"job_id":"[^"]*"' | cut -d'"' -f4)

# Wait for the ingest job to reach DONE (or fail after 60s)
for i in $(seq 1 60); do
  STATUS=$(curl -s -H "Authorization: Bearer $PROD_KEY" \
    http://127.0.0.1:8765/jobs/$JOB_ID \
    | grep -o '"status":"[^"]*"' | cut -d'"' -f4)
  [ "$STATUS" = "DONE" ] && break
  [ "$STATUS" = "FAILED" ] && echo "Ingest job failed" && exit 1
  sleep 1
done

# Search dev-UAT for the same term — should return empty results (data isolation confirmed)
curl -s -X POST http://127.0.0.1:18765/search \
  -H "Authorization: Bearer $DEV_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection":"test-isolation","query":"isolation check"}'
```

Note: Replace `/path/to/your/file.md` with an actual file path on your machine.

---

## Part 4 — API key isolation

Each instance auto-generates its own key on first start and persists it in its own data directory. The keys are independent and do not cross-authenticate.

### Retrieve the prod key

The key is written in env format (`ARCHON_SEARCH_API_KEY=<64-char-hex-token>`, mode `0o600`). To extract just the raw token:

```bash
grep -o '[^=]*$' ~/.archon-search/.search.env
```

Or source the file to set it as a shell variable:

```bash
source ~/.archon-search/.search.env
echo $ARCHON_SEARCH_API_KEY
```

> **Security note:** `source` exports the key into your shell environment for the session — it will appear in `env` output and child processes. For scripting where exposure matters, prefer the `grep` form (ephemeral subshell assignment) or `ARCHON_SEARCH_API_KEY=$(grep -o '[^=]*$' ~/.archon-search/.search.env)`.

> **Note:** `archon-search key list` also shows active keys, but it requires the server to be running and calls `GET /keys`. For initial setup or scripting, the `grep` form above is simpler.

**`ARCHON_SEARCH_KEY_FILE` override:** if you set this env var, it redirects the key file path independently of `ARCHON_SEARCH_DATA_DIR`. Do not set it in a multi-instance setup unless you explicitly want to share a key file between instances.

### Retrieve the dev-UAT key

```bash
docker compose exec archon-dev cat /data/.search.env | grep -o '[^=]*$'
```

> **Precondition:** this command assumes `ARCHON_SEARCH_API_KEY` is not set in the container environment. If it was explicitly set (e.g., via `.env`), no `/data/.search.env` file is written — the key IS the env var value.

### Verify cross-auth fails

```bash
PROD_KEY=$(grep -o '[^=]*$' ~/.archon-search/.search.env)
DEV_KEY=$(docker compose exec -T archon-dev cat /data/.search.env | grep -o '[^=]*$' | tr -d '\r')

# Prod key on dev-UAT port → should return 401 (000 = server not running)
curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $PROD_KEY" \
  http://127.0.0.1:18765/status
# Expected: 401

# Dev key on prod port → should return 401 (000 = server not running)
curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $DEV_KEY" \
  http://127.0.0.1:8765/status
# Expected: 401
```

> **Without a persistent volume**, the dev-UAT key regenerates on every container restart. Bare `docker run -p 18765:8765 ...` without a `-v` mount will break all issued tokens on restart. Always use `docker compose up archon-dev` which handles volume management automatically via `archon-dev-data`.

---

## Part 5 — HTTP client configuration

With both keys retrieved, you can make authenticated requests to each instance. The examples below use `curl`; the same `Authorization: Bearer <token>` header applies to any HTTP client (httpx, requests, etc.).

### Prod (port 8765)

```bash
PROD_KEY=$(grep -o '[^=]*$' ~/.archon-search/.search.env)

# Check server status
curl -s http://127.0.0.1:8765/status \
  -H "Authorization: Bearer $PROD_KEY"

# Search
curl -s -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $PROD_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection":"docs","query":"how does the router work?"}'

# Ingest a file (returns a job_id; poll /jobs/<job_id> for completion)
curl -s -X POST http://127.0.0.1:8765/ingest \
  -H "Authorization: Bearer $PROD_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection":"docs","path":"/path/to/file.md"}'
```

### Dev-UAT (port 18765)

```bash
DEV_KEY=$(docker compose exec -T archon-dev cat /data/.search.env | grep -o '[^=]*$' | tr -d '\r')

# Check server status
curl -s http://127.0.0.1:18765/status \
  -H "Authorization: Bearer $DEV_KEY"

# Search
curl -s -X POST http://127.0.0.1:18765/search \
  -H "Authorization: Bearer $DEV_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection":"docs","query":"how does the router work?"}'
```

`GET /health` and `GET /ready` are unauthenticated on both instances — no `Authorization` header needed.

---

## Part 6 — MCP client configuration

Each instance exposes an MCP endpoint at `/mcp` on its respective port. The MCP endpoint is enabled by default (`mcp.enabled = true`). To disable it, add the following to the instance's `archon-search.toml` and restart:

```toml
[mcp]
enabled = false
```

See [`Documentation/ADRs/09_mcp_http_mount_and_namespace_propagation.md`](../ADRs/09_mcp_http_mount_and_namespace_propagation.md) for the full MCP HTTP mount design.

Verify both MCP endpoints are reachable (a 401 confirms auth is active on this path; a 404 means nothing is mounted there — either `mcp.enabled = false` in the instance's TOML config, or the mount failed during startup):

```bash
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/mcp
# Expected: 401 (prod: auth middleware is active)

curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:18765/mcp
# Expected: 401 (dev-UAT: auth middleware is active)
```

### Claude Code

Use `claude mcp add` to register each instance. The `--scope user` flag makes the server available in all your projects. Use `--scope local` (the default) to keep it private to you and active only in the current project. Use `--scope project` to write to `.mcp.json` for team-shared configuration.

First, extract the keys:

```bash
# Extract prod key (strips the ARCHON_SEARCH_API_KEY= prefix from the env file)
PROD_KEY=$(grep -o '[^=]*$' ~/.archon-search/.search.env)

# Extract dev-UAT key from the container (tr strips Windows-style CRLF from Docker output)
DEV_KEY=$(docker compose exec -T archon-dev cat /data/.search.env | grep -o '[^=]*$' | tr -d '\r')
```

Then register both MCP servers:

```bash
claude mcp add --scope user --transport http archon-prod http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $PROD_KEY"

claude mcp add --scope user --transport http archon-dev http://127.0.0.1:18765/mcp \
  --header "Authorization: Bearer $DEV_KEY"
```

Verify the servers are connected:

```bash
claude mcp list
# Both servers should appear in the list.
```

Alternatively, add them to a project-level `.mcp.json` at the repository root — Claude Code reads this file automatically when you open the project. This file is per-developer infrastructure config; add it to `.gitignore` rather than committing it, since it binds to your local instance addresses and keys:

```json
{
  "mcpServers": {
    "archon-prod": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": {
        "Authorization": "Bearer ${ARCHON_SEARCH_PROD_KEY}"
      }
    },
    "archon-dev": {
      "type": "http",
      "url": "http://127.0.0.1:18765/mcp",
      "headers": {
        "Authorization": "Bearer ${ARCHON_SEARCH_DEV_KEY}"
      }
    }
  }
}
```

Export the keys in the shell before opening the project so Claude Code can interpolate them:

```bash
export ARCHON_SEARCH_PROD_KEY=$(grep -o '[^=]*$' ~/.archon-search/.search.env)
export ARCHON_SEARCH_DEV_KEY=$(docker compose exec -T archon-dev cat /data/.search.env | grep -o '[^=]*$' | tr -d '\r')
```

> **Choose distinct server names** (`archon-prod`, `archon-dev`) so you can tell them apart in tool-call prefixes. Claude Code exposes each server's tools under its configured name.

### Other MCP clients (Python SDK)

The endpoint uses the MCP Streamable HTTP transport. With the Python SDK:

```python
from mcp.client.streamable_http import streamable_http_client
from mcp.client.session import ClientSession

# Prod
async with streamable_http_client(
    "http://127.0.0.1:8765/mcp",
    headers={"Authorization": f"Bearer {PROD_KEY}"},
) as (read_stream, write_stream):
    async with ClientSession(read_stream, write_stream) as session:
        await session.initialize()
        result = await session.call_tool("search", {
            "query": "centroid pre-ranking",
            "collection": "docs",
        })

# Dev-UAT — same pattern, different URL and key
async with streamable_http_client(
    "http://127.0.0.1:18765/mcp",
    headers={"Authorization": f"Bearer {DEV_KEY}"},
) as (read_stream, write_stream):
    ...
```

For TypeScript, use `@modelcontextprotocol/sdk`'s `StreamableHTTPClientTransport` with the same URL and bearer header. See the [MCP TypeScript SDK documentation](https://modelcontextprotocol.io/docs/concepts/transports) for the full client example.

---

## Stopping the instances

```bash
# Stop dev-UAT
docker compose down archon-dev

# Stop prod (native service)
archon-search stop
```

`docker compose down archon-dev` tears down the container but leaves `archon-dev-data` intact. The key and index persist for the next `docker compose up archon-dev`.

To remove the volume (destroys all dev-UAT data):

```bash
docker compose down archon-dev -v
```

---

## Going further — `archon-test` (port 18766)

The shipped `docker-compose.yml` includes a third service, `archon-test`, on port 18766 with its own `archon-test-data` volume. Its purpose is integration testing against a second isolated Docker instance — for example, proving that a search result from `archon-dev` does not appear in `archon-test`.

```bash
docker compose up archon-dev archon-test -d
curl http://127.0.0.1:18766/health   # archon-test
```

`archon-test` is not covered by this manual but follows the same isolation pattern as `archon-dev`. See `docker-compose.yml` for its port and volume configuration.

---

## See also

- [`01_installation.md`](01_installation.md) — full install flow (wizard, profiles, GPU acceleration).
- [`03_running_the_server.md`](03_running_the_server.md) — `start`/`stop`/`status`/`serve` subcommands.
- [`08_running_with_docker.md`](08_running_with_docker.md) — full Docker reference including `docker run`, environment variables, and persistence layout.
- [`02_configuration.md`](02_configuration.md) — TOML configuration reference.
