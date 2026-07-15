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
7. The collection path is still written to `archon-search.toml` by the CLI directly (config update stays local, doesn't need the server).

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
- **Read-only commands** (`list`, `info`) — these can optionally continue using a direct database connection for speed; they don't write and don't need the server. Whether to also proxy them is a separate decision.
- **Server startup from the CLI** — auto-starting the server when it's not running is out of scope for this brief; it is a separate UX feature.

## Key Decisions
- **Require the server for all write operations:** this is the right constraint. The in-process path predates the async job system and was never updated. `migrate` already proved this model works.
- **Template: `migrate_cmd.py`:** every command follows this exact pattern — `httpx` call, handle 202 with job ID, `--wait` flag polls `GET /jobs/{id}`, clean error on connection refused.
- **Config update stays local:** writing the path to `archon-search.toml` is a CLI-only operation (file I/O, no server involvement). Only the ingest itself is proxied.
- **Error message on missing server:** `"archon-search serve is not running. Start it first."` — not a raw `[Errno 61] Connection refused`.

## Edge Cases & Constraints
- **Server not running:** print a human-readable error and exit 1. Do not attempt in-process fallback. Do not swallow the errno.
- **Auth:** CLI reads the API key from `--api-key`, then `ARCHON_SEARCH_API_KEY` env var, then the local key file — same priority order as `migrate_cmd._resolve_api_key()`.
- **`collection remove` while server is running:** the server today has a `--force` flag bypass in the CLI. The proxied version removes the need for the flag — the server handles concurrent-access safely.
- **`sync` command:** the sync path writes `IndexingStateStore` from both the CLI and the server watcher simultaneously today. After this fix, only the server writes to it; the CLI submits a trigger.
- **Large ingest progress with `--wait`:** the polling loop should print a progress line every N seconds (matching `migrate --wait` behaviour), not spin silently.
- **`graph build-communities`:** if a REST endpoint for this does not yet exist on the server, the command should error with _"This operation requires the server to expose a /graph/.../build-communities endpoint (not yet available). Use the server directly."_ rather than silently falling back to in-process.

## Open Questions
- Does a `DELETE /collections/{name}` endpoint exist, or does remove need a new endpoint? Check `routes_collections.py`.
- Does the `sync` command have a REST trigger endpoint, or does it need one added? (`POST /maintenance/trigger` may cover this, or a dedicated `POST /sync/trigger` may be needed.)
- Does `graph build-communities` have a REST endpoint? If not, is adding one in scope for this brief or a follow-up?
- Should `list` and `info` also proxy via REST for consistency, or stay direct for speed? (The `CollectionDetail` schema from `GET /collections/{name}` already filters out raw vectors — see Issue 7.)
- The `ingest` CLI command (`archon_search/cli/ingest.py`) — does it have its own command group or is it an alias for `collection add`? Need to confirm scope before implementation.
- After `collection add` proxies via REST, the server creates and tracks a job. Should the path still be written to `archon-search.toml` before or after the job is confirmed DONE? (Recommendation: write it before submission, so the path is registered even if the job fails and the user retries.)

## Future Iterations
- Auto-detect server and offer to start it if not running (`archon-search serve --background`).
- `--wait` with a live progress bar (rich / click progress) instead of line-by-line polling output.
- `list` and `info` proxied via REST to benefit from the server's already-warm embedder cache (returns `CollectionDetail` shape, removes the duplicate store connection).
- A `--no-server` escape hatch for disaster-recovery scenarios (e.g. server will not start due to a corrupt index) — explicitly labelled as unsafe, writes a lock file, disables concurrent server writes.

## References
- [[archon_search/cli/collection.py]] `[code-agent]` — all in-process command implementations; `migrate_cmd` is the reference template
- [[archon_search/server/routes_collections.py]] `[code-agent]` — REST endpoints that already exist; `POST /collections/` returns `202 + job_id`
- [[archon_search/jobs/]] `[code-agent]` — async job infrastructure bypassed by the CLI today
- [[Documentation/Backlog/bug-006-maintenance-run-ux-brief.md]] `[user]` — related: maintenance run also requires server; same pattern applies

## Recommendation
This is the most important fix in the bug-001–bug-008 batch. Every other CLI performance and UX issue (slow startup, frozen terminal during ingest, invisible jobs, onnxruntime warnings from duplicate model loading) is a downstream symptom of this one architectural gap. The `migrate` command already proves the right design works — this brief is a matter of applying that pattern consistently to the eight commands that were written before the job system existed. The hardest part is confirming which REST endpoints exist for each operation and adding the missing ones before the CLI can call them. Do not compromise on the "server required for writes" constraint — a dual-path fallback will keep the races and invisible-job problems alive indefinitely.
