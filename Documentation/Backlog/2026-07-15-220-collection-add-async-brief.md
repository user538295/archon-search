# Feature Brief: Non-Blocking Collection Add (bug-005)

> **STATUS: Shipped in CSP120 (`8c36a6f7`).** This brief is now an archival record. The three stale details below have been corrected to match the shipped design. See the [team plan](./2026-07-15-220-collection-add-async-team-plan.md) for the verified implementation.

## Problem
Running `archon-search collection add <path>` freezes the terminal for minutes while a large directory is indexed — with no progress, no way to cancel cleanly, and no visibility into what's happening.

## Goal
`collection add` returns immediately with a job ID after submitting the ingestion to the server. The user can track progress via `--wait` or by checking `GET /jobs/{id}`. The terminal is never frozen.

## Users & Context
A developer or operator adding a new directory to be indexed. They run the command, expect it to kick off in the background, and want to continue using their terminal immediately. On large codebases (thousands of files), waiting minutes for a prompt to return is a hard blocker.

## Core Flow
1. User runs `archon-search collection add /path/to/dir`
2. The server adds the path to `archon-search.toml` server-side as part of handling the request (the CLI writes nothing)
3. CLI sends `POST /collections/` to the running server (with the collection path only — name is server-derived)
4. Server queues the ingestion job and responds `202` with a `job_id`
5. CLI prints: `Ingestion job submitted: <job_id>` and exits — terminal returns immediately
6. User optionally runs with `--wait` to stay and see live progress (same polling loop as `migrate --wait`)
7. User can check `archon-search status` or the server's `/jobs` endpoint to see job state at any time

## In Scope
- `collection add` command rewritten to call `POST /collections/` REST endpoint
- `--wait` flag: poll `GET /jobs/{job_id}` until terminal, print progress (reuse `_poll_job` from `cli/_helpers.py`)
- `--api-url` and `--api-key` options (same defaults as all other server-proxying commands)
- Clear error if the server is not running: `"archon-search serve is not running. Start it first."`
- The server writes the path to `archon-search.toml` server-side (`_maybe_save_config`) — the CLI writes nothing

## Out of Scope
- Making `collection add` work without a running server (no standalone/in-process fallback — this is the direction of Issue 8)
- onnxruntime warnings (`Some nodes were not assigned to the preferred execution providers`) — these are harmless macOS ARM informational messages at first model load; no action needed
- Progress bar for the in-process path — that path is being retired

## Key Decisions
- **Require a running server:** consistent with the broader Issue 8 direction where all write operations go through the server; avoids concurrent writes and makes jobs visible
- **Reuse `_poll_job`:** already tested (supersedes `_poll_migration_job`), handles DONE/FAILED/CANCELLED states and prints progress — no new polling logic needed
- **Server owns the TOML write:** `archon-search.toml` is the source of truth for which paths are managed; `POST /collections/` appends the path and calls `_maybe_save_config` with rollback on failure. The CLI never touches TOML.

## Edge Cases & Constraints
- **Server not running:** print the clear message above and exit 1 — do not fall back to in-process ingestion
- **Collection already registered:** `POST /collections/` returns `409 Conflict` when the path or derived name is already registered; CLI prints the detail and exits `1`. There is no `200` "already up to date" path.
- **`--wait` + Ctrl-C:** cancel the poll loop locally; the server job continues running (same behaviour as `migrate --wait`)
- **API key resolution:** follow `_resolve_api_key()` — env var `ARCHON_SEARCH_API_KEY`, then the key file; same as every other proxying command

## Open Questions

*All resolved — see the [team plan](./2026-07-15-220-collection-add-async-team-plan.md).*

- **`collection_name` override?** No. `AddCollectionRequest` has only `path` + optional `embedding_model`; name is always server-derived via `path_to_collection_name`. Final.
- **TOML write inside the server?** Already done in CSP120 — the server owns it via `_maybe_save_config`.
- **Race if Ctrl-C between TOML write and REST call?** Moot — there is no client-side TOML write; server writes TOML and enqueues the job within the same request.

## Future Iterations
- `collection add --no-server` in-process flag for initial setup before a server exists (only after Issue 8 architectural work is done and concurrent-write safety is addressed)
- Progress display in the `--wait` path: file counts, chunk counts per second
- `archon-search jobs list` command to show all active/recent ingestion jobs

## References
- [[archon_search/cli/collection.py]] `[code-agent]` — current blocking `add` implementation (lines 73–140)
- [[archon_search/cli/collection.py]] `[code-agent]` — `migrate_cmd` and `_poll_migration_job` pattern to follow (lines 305–450)
- [[archon_search/server/routes_collections.py:243]] `[code-agent]` — async job submission endpoint (`POST /collections/`)
- **Team plan:** [2026-07-15-220-collection-add-async-team-plan.md](./2026-07-15-220-collection-add-async-team-plan.md) — verification/gap-closing plan (feature already shipped in CSP120 `8c36a6f7`; this brief is stale — see the plan's Contradictions section)

## Recommendation
This is a high-impact fix with very low risk — the pattern is already proven by `migrate`. The entire implementation is replacing ~30 lines of in-process pipeline code with ~20 lines of `httpx.post` + optional polling. The hardest part is confirming the `POST /collections/` request body shape and whether the server accepts a pre-derived `collection_name`. Do that check before writing a single line of implementation.
