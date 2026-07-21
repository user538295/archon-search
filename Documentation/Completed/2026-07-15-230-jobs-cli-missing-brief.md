# Feature Brief: Jobs CLI Commands

## Problem
Users who submit long-running operations (ingest, migrate, export) receive a job ID but have no CLI command to check its status — they must use `curl` against the REST API directly or wait blindly.

## Goal
`archon-search jobs list` and `archon-search jobs show <job_id>` exist and return human-readable job status from the server, so users never need to leave the terminal to track what the server is doing.

## Users & Context
Any operator who has just run `archon-search collection add` (bug-005), `collection migrate`, or `export` and received a job ID back. They want to know: is it done? did it fail? what went wrong?

## Core Flow

**List jobs:**
1. User runs `archon-search jobs list`
2. CLI calls `GET /jobs` on the running server
3. Output shows a table: job ID (truncated), type, collection, status, started, elapsed
4. User can filter with `--status running|done|failed|pending`

**Show one job:**
1. User runs `archon-search jobs show <job_id>`
2. CLI calls `GET /jobs/{job_id}`
3. Output shows full detail: ID, type, collection, status, started, finished, error message if failed
4. If job is still running, user can add `--wait` to poll until completion (reuses the polling pattern from `export_cmd.py:101–150`)

## In Scope
- `archon-search jobs list` — tabular output, optional `--status` filter
- `archon-search jobs show <job_id>` — full detail output, optional `--wait` flag
- Human-readable elapsed time (e.g. "2m 14s") and status labels
- Helpful error message when server is not running (consistent with bug-012 fix)

## Out of Scope
- `jobs cancel <job_id>` — no cancellation endpoint exists in the REST API today
- Streaming live log output — the REST API does not expose job logs
- `--namespace` flag — `GET /jobs` already scopes to the authenticated namespace (`routes_jobs.py:512–515`); multi-namespace listing is a follow-on

## Key Decisions
- **REST proxy only, no in-process access:** Consistent with the bug-008 architecture direction; jobs are server-side state, the CLI has no business reading the job store directly.
- **Reuse `export_cmd.py` polling template:** `export_cmd.py:101–150` already implements `--wait` with configurable poll interval and timeout — extract it into a shared `_poll_job(api_url, api_key, job_id)` helper rather than copy-pasting.
- **Truncated IDs in list, full IDs in show:** UUIDs are 36 chars; list output truncates to first 8 chars for readability; `show` prints the full ID.
- **Pass `--status` to the server, no client-side re-filtering:** `GET /jobs` already accepts `?status=` and filters before returning (`routes_jobs.py:505`, `517–520`); the CLI forwards the flag directly.
- **Add `job_type: str` to `JobResponse` and `job_to_dict` before CLI work begins:** `JobResponse` has no type field today (`schemas.py:455–476`); the server already owns the twelve-case type→label mapping in `store.py:307–369` — surface it in the response rather than duplicating it in the CLI. Every future client gets the correct label automatically; the alternative (derive client-side) would duplicate the twelve-case mapping and require a second update site whenever a new job type is added.

## Edge Cases & Constraints
- **Server not running:** Print `"Server is not running. Start it first with: archon-search start"` (consistent with bug-012).
- **Unknown job ID:** Server returns 404; CLI prints `"Job <id> not found."` and exits 1.
- **`--wait` timeout:** Default 10 minutes; configurable via `--timeout <seconds>`; prints elapsed time on exit.
- **Large job list:** `GET /jobs` may return many entries on long-running servers; default to last 50, add `--limit` flag.

## Future Iterations
- `jobs cancel <job_id>` once a cancellation endpoint is added
- `jobs watch <job_id>` — live streaming output if the server ever exposes job logs
- Namespace filter `--namespace` on `jobs list`

## References
- [[archon_search/server/routes_jobs.py]] `[code-agent]` — `GET /jobs` and `GET /jobs/{id}` endpoints
- [[archon_search/cli/export_cmd.py:101–150]] `[code-agent]` — existing `--wait` polling template to extract into shared helper
- [[Documentation/Backlog/bug-005-collection-add-async-brief.md]] `[user]` — prerequisite: `collection add` returns job_id, needs this to be useful
- [[Documentation/Backlog/bug-016-graph-build-communities-bypass-brief.md]] `[user]` — prerequisite: graph build-communities returns job_id, needs this to be useful
- [[Documentation/Backlog/bug-012-connection-refused-ux-brief.md]] `[user]` — shared error message pattern for server-not-running

## Recommendation
Build this before shipping bug-005 and bug-016. Returning a job ID to a user who has no way to check it is worse UX than the current blocking behavior — it creates uncertainty instead of removing it.

Implementation is now fully unblocked: status filtering is server-side (Q1), namespace scoping is automatic (Q3). The one prerequisite decision is Q2: add `job_type: str` to `JobResponse` and `job_to_dict` before starting CLI work — otherwise the list table has no type column to display.

Estimated scope: add `job_type` to the server (30 min), two Click commands (1.5–2 h), extract `_poll_job` from `export_cmd.py:101–150` (30 min). Total: 2.5–3 h.
