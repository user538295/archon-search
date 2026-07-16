# Feature Brief: Per-Collection Community Rebuild via REST API

## Problem
Running `archon-search graph build-communities` while the server is active writes directly to the same database files the server is managing — with no coordination between the two processes. This can corrupt the graph community tables, and the current state is invisible to the server's job tracker, `/status`, and `/jobs` endpoints.

## Goal
Community rebuilds are triggered through the server's REST API (new `POST /graph/{collection}/rebuild-communities` endpoint), so the CLI proxies to it, the server serialises concurrent writes, and the job is visible in `/jobs`.

## Users & Context
Developers and operators who have ingested a large code or document corpus with the graph feature enabled and want to explicitly trigger a Leiden community rebuild for a specific collection — for example, after bulk re-ingest or after changing the graph topology significantly.

## Core Flow
1. User runs `archon-search graph build-communities <collection>`.
2. CLI sends `POST /graph/<collection>/rebuild-communities` to the running server.
3. Server enqueues an async job (same pattern as `POST /collections/<name>/migrate`) and returns `202 + job_id`.
4. CLI prints the job ID and optionally polls with `--wait` until `DONE` or `FAILED`.
5. User checks progress at any time with `archon-search jobs status <job_id>`.

## In Scope
- New REST endpoint `POST /graph/{collection}/rebuild-communities` that enqueues a community rebuild job
- CLI `graph build-communities` updated to call that endpoint instead of running in-process
- `--wait` flag (reuse the polling pattern from `cli/collection.py`'s `_poll_migration_job`)
- Clear error when server is not running: `"Server is not running. Start it first with: archon-search start"`

## Out of Scope
- Bulk rebuild across all collections at once (use `archon-search maintenance run` for that)
- Changing the Leiden algorithm parameters at call time (those come from `archon-search.toml`)
- In-process fallback mode (rejected — keeps the concurrent-write race alive)

## Key Decisions
- **Add the REST endpoint rather than deprecating the CLI command:** `maintenance run` rebuilds all collections; users need per-collection control, so a dedicated endpoint is justified.
- **Server-side serialisation via `asyncio.Lock` per collection:** the same lock already used for ingest protects against concurrent server-side rebuilds. The CLI-in-process path bypasses this entirely — proxying to the server is the only safe fix.
- **Dependent on bug-008:** the broader CLI-proxies-to-server architecture; this brief is one instance of that pattern applied to the graph subsystem.

## Edge Cases & Constraints
- **Server not running:** print the standard "server not running" message and exit non-zero. No in-process fallback.
- **Graph not enabled:** server returns 404 or 422; CLI surfaces the message clearly (`"Graph feature is not enabled. Set graph.enabled = true in archon-search.toml"`).
- **Collection does not exist:** server returns 404; CLI echoes the error.
- **Rebuild already in progress:** server should return 409 with `"community rebuild already in progress for this collection"` rather than enqueueing a duplicate job.
- **`leidenalg` not installed on the server:** server raises `ConfigError` at startup when `graph.enabled = true` and extras are missing — rebuild request never reaches the handler.

## Open Questions
- Does the existing `MaintenanceLoop._spawn_rebuild_task` path return a `job_id` that can be tracked, or does it fire-and-forget? If fire-and-forget, the new endpoint needs its own `JobStore` entry.
- Should the new endpoint accept optional `namespace` parameter (multi-tenant deployments) or derive it from the Bearer token's namespace?
- What is the correct HTTP method — `POST /graph/{collection}/rebuild-communities` or `POST /graph/{collection}/communities/rebuild`? Check existing graph route conventions in `routes_graph.py`.

## Future Iterations
- A `POST /graph/rebuild-communities` (no collection param) as a convenience for rebuilding all collections, replacing the current `maintenance run` catch-all for this specific operation.
- Progress streaming (SSE) for long-running Leiden partitioning on large graphs.

## References
- **Team plan:** [2026-07-15-110-graph-build-communities-bypass-team-plan.md](./2026-07-15-110-graph-build-communities-bypass-team-plan.md)
- [[archon_search/cli/graph_cmd.py:64–87]] `[code-agent]` — in-process implementation being replaced
- [[archon_search/cli/collection.py]] `[code-agent]` — REST-proxy + `--wait` pattern to follow (`migrate_cmd.py` does not exist; the migrate CLI and its `_poll_migration_job` polling helper live in `cli/collection.py`)
- [[archon_search/jobs/]] `[code-agent]` — async job infrastructure
- [[archon_search/server/routes_graph.py]] `[code-agent]` — existing graph routes; new endpoint goes here
- [[Documentation/Backlog/2026-07-15-120-cli-server-proxy-brief.md]] `[user]` — parent architectural brief this depends on (formerly referenced as "bug-008")

## Recommendation
Build this as part of the bug-008 CLI-server-proxy rollout — it is one of the eight commands that need porting. The only thing that makes it slightly more work than the others is the missing REST endpoint: every other bypassing command (add, remove, reindex, ingest, sync) already has a server-side route to call. The new endpoint is small (delegates to `CommunityBuilder.build()` wrapped in a job), but it must be built before the CLI change can land. Do not compromise on the serialisation constraint — concurrent in-process writes to the community tables are the most data-corrupting failure mode in this batch.
