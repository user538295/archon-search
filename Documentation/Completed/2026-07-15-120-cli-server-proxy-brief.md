# Feature Brief: CLI Write Operations Must Route Through the Server

## Problem
When you run `archon-search collection add`, `remove`, `reindex`, `ingest`, or `sync` from the terminal, the tool does the work itself in a separate background process — completely bypassing the running server. This means the server never knows the operation happened: it shows up in no job list, no status page, and no indexing tracker. Worse, both the CLI and the server may write to the same database file at the same time with no coordination, which can silently corrupt the index.

## Goal
Every write operation from the CLI reaches the server via its API — the same way `collection migrate` works today. The terminal returns immediately with a job ID. Progress is visible in `/jobs` and `/status`. The server's internal lock prevents concurrent writes. The ML model loads only once (in the server process), not twice.

## Users & Context
Operators and developers who use the CLI to manage collections while the server is running. Currently: they run `archon-search collection add <path>`, the terminal freezes for minutes with no feedback, and the server shows nothing happening. After this fix: the CLI submits a job to the server and prints the job ID immediately; the user can check progress with `--wait` or via `/jobs`.

## Core Flow

1. User runs `archon-search collection add ~/my-docs` (or `remove`, `reindex`, `ingest`, `sync`).
2. The CLI checks that the server is reachable (`GET /health`).
3. If the server is not running: the CLI prints a clear message — _"Server is not running. Start it first with: archon-search serve"_ — and exits with code 1.
4. If the server is running: the CLI sends the request to the appropriate REST endpoint (e.g. `POST /collections/`) and receives a job ID.
5. The CLI prints: `"Job submitted: <job_id>. Track progress with: archon-search jobs status <job_id>"`.
6. With `--wait`: the CLI polls the job until it reaches a terminal state (DONE / FAILED) and prints live progress, just as `migrate --wait` does today.
7. The collection path is written to `archon-search.toml` by the **server** as part of processing `POST /collections/` (via `_maybe_save_config()` at `routes_collections.py:187`) — the CLI no longer writes it locally.

## In Scope
- `collection add` — submit to `POST /collections/`
- `collection remove` — submit to `DELETE /collections/{name}` (or equivalent endpoint)
- `collection reindex` — submit to the reindex endpoint
- `collection reindex-metadata` — submit to the reindex-metadata endpoint
- `ingest` CLI command — submit file/directory ingest via REST
- `sync` command — submit sync trigger via REST
- `graph build-communities` — submit via REST if an endpoint exists; gate on server availability otherwise
- All of the above gain `--api-url` and `--api-key` options (matching `migrate`'s signature)
- All of the above gain a `--wait` flag for blocking until the job completes

## Out of Scope
- **In-process fallback mode** — deliberately excluded. A "try server, fall back to direct" dual path hides the problem and still allows concurrent write races. The fix is a clear requirement: server must be running for write operations.
- **Lock-file coordination** — rejected in favour of this proper fix. A `.cli-ingest.lock` file would prevent data corruption but wouldn't fix job invisibility or duplicate model loading.
- **Read-only commands** (`list`, `info`) — resolved (Q-A): they stay direct (they don't write, don't race, and must work with the server off). They gain `--api-url`/`--api-key` for parity but default to direct.
- **Server startup from the CLI** — auto-starting the server when it's not running is out of scope for this brief; it is a separate UX feature.

## Key Decisions
- **Require the server for all write operations:** this is the right constraint. The in-process path predates the async job system and was never updated. `migrate` already proved this model works.
- **Template: `migrate_cmd.py`:** every command follows this exact pattern — `httpx` call, handle 202 with job ID, `--wait` flag polls `GET /jobs/{id}`, clean error on connection refused.
- **Config update is server-side:** `POST /collections/` already calls `_maybe_save_config()` at `routes_collections.py:187`, writing the path to `archon-search.toml` server-side. The CLI's local TOML write is removed to avoid duplicate entries.
- **Error message on missing server:** `"archon-search serve is not running. Start it first."` — not a raw `[Errno 61] Connection refused`.

## Edge Cases & Constraints
- **Server not running:** print a human-readable error and exit 1. Do not attempt in-process fallback. Do not swallow the errno.
- **Auth:** CLI reads the API key from `--api-key`, then `ARCHON_SEARCH_API_KEY` env var, then the local key file — same priority order as `migrate_cmd._resolve_api_key()`.
- **`collection remove` while server is running:** the server today has a `--force` flag bypass in the CLI. The proxied version removes the need for the flag — the server handles concurrent-access safely.
- **`sync` command:** the sync path writes `IndexingStateStore` from both the CLI and the server watcher simultaneously today. After this fix, only the server writes to it; the CLI submits a trigger.
- **Large ingest progress with `--wait`:** the polling loop should print a progress line every N seconds (matching `migrate --wait` behaviour), not spin silently.
- **`graph build-communities`:** if a REST endpoint for this does not yet exist on the server, the command should error with _"This operation requires the server to expose a /graph/.../build-communities endpoint (not yet available). Use the server directly."_ rather than silently falling back to in-process.

## Open Questions

### Resolved (fact-checked against source 2026-07-16)
- ✅ **`DELETE /collections/{name}` exists** — `routes_collections.py:277`, returns synchronous `200` (`DeleteResponse`). No new endpoint needed; `remove` is a mechanical proxy (no `--wait` polling required).
- ✅ **`sync` needs a NEW endpoint** — no sync trigger endpoint exists in any `routes_*.py`. `POST /maintenance/trigger` (`routes_maintenance.py:22`) does **not** cover it: it fires a full maintenance pass (FTS optimize, orphan cleanup, expired-chunk pruning, graph GC, community rebuild, failed-ingest retry), which is semantically unrelated to a corpus re-scan/reconcile (`SearchCollectionSync.sync()`). A dedicated `POST /sync` (202 + job) must be built.
- ✅ **`graph build-communities` has an endpoint** — `POST /graph/{collection}/rebuild-communities` (`routes_graph.py:130`, 202 + job). The CLI was converted to a proxy in GBC110 (`archon_search/cli/graph_cmd.py`). This is the reference implementation for the remaining commands.
- ✅ **`ingest` is a standalone command, not an alias** — `archon_search/cli/ingest.py:25–115`; server endpoint `POST /ingest` already exists (`routes_jobs.py:424`, 202 + job). Mechanical proxy.
- ℹ️ **Endpoint coverage:** 5/8 commands (`add`, `remove`, `reindex`, `ingest`, `graph build-communities`) hit existing endpoints; 2/8 (`reindex-metadata`, `sync`) require new server endpoints built first.

### Resolved decisions (2026-07-16)
- ✅ **Q-A — `list`/`info` stay direct.** They only read metadata (`list_collections`/`get_collection_meta`, `collection.py:53–64`/`208–235`), never write and never embed, so the concurrent-write corruption risk this feature fixes does not apply to them. Routing them through the server would break offline `list` (the most common quick-check) for no safety gain. Keep the direct DB path; add `--api-url`/`--api-key` flags for parity but **default to direct** (proxy available, not required). Read-only reads against the LanceDB file are safe alongside a running server.
- ✅ **Q-B — `collection add`'s local CLI TOML write is REMOVED.** Fact-check against source (`routes_collections.py:187`) revealed that `POST /collections/` already calls `_maybe_save_config()` server-side during every successful request — before the ingest job is enqueued. The CLI's planned pre-write would therefore create duplicate entries in `archon-search.toml`. Resolution: the CLI's local TOML write is removed entirely; the server handles the config write as part of processing `POST /collections/`. The accepted worst case (a stale "registered but not yet populated" entry on ingest failure) still applies — the server's `_maybe_save_config()` runs before the async ingest task completes — but recovery is via `collection reindex <name>` or maintenance sync, not by re-running `collection add` (which would 409 because the collection is already registered).

## Future Iterations
- Auto-detect server and offer to start it if not running (`archon-search serve --background`).
- `--wait` with a live progress bar (rich / click progress) instead of line-by-line polling output.
- `list` and `info` proxied via REST to benefit from the server's already-warm embedder cache (returns `CollectionDetail` shape, removes the duplicate store connection).
- A `--no-server` escape hatch for disaster-recovery scenarios (e.g. server will not start due to a corrupt index) — explicitly labelled as unsafe, writes a lock file, disables concurrent server writes.

## References
- **Team plan:** [2026-07-15-120-cli-server-proxy-team-plan.md](./2026-07-15-120-cli-server-proxy-team-plan.md)
- [[archon_search/cli/collection.py]] `[code-agent]` — all in-process command implementations; `migrate_cmd` is the reference template
- [[archon_search/server/routes_collections.py]] `[code-agent]` — REST endpoints that already exist; `POST /collections/` returns `202 + job_id`
- [[archon_search/jobs/]] `[code-agent]` — async job infrastructure bypassed by the CLI today
- [[Documentation/Backlog/bug-006-maintenance-run-ux-brief.md]] `[user]` — related: maintenance run also requires server; same pattern applies

## Recommendation
This is the most important fix in the bug-001–bug-008 batch. Every other CLI performance and UX issue (slow startup, frozen terminal during ingest, invisible jobs, onnxruntime warnings from duplicate model loading) is a downstream symptom of this one architectural gap. The `migrate` command already proves the right design works — this brief is a matter of applying that pattern consistently to the eight commands that were written before the job system existed. The hardest part is confirming which REST endpoints exist for each operation and adding the missing ones before the CLI can call them. Do not compromise on the "server required for writes" constraint — a dual-path fallback will keep the races and invisible-job problems alive indefinitely.
