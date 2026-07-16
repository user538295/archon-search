# Feature Brief: Status Command Shows No Active Job Information

## Problem
When a user runs `archon-search status` after submitting a long ingest, backup, or reindex job, the output shows nothing about that job — no count, no progress, no indication that any work is happening. The terminal appears to say "everything is fine" while the system is silently busy.

## Goal
`archon-search status` includes a job queue summary line when work is in progress: how many jobs are running and how many are waiting. When nothing is running, the line is absent (no noise in the common idle case).

## Users & Context
Any operator who submitted work through the CLI or REST API and wants to know whether it has completed. Most common after `collection add`, `collection reindex`, or `backup --now` — all of which return a job ID and leave the work running in the background.

## Core Flow
1. User runs `archon-search status`.
2. Command calls `GET /jobs` filtered to `status=RUNNING,PENDING` (or a new `active_job_count` field on `GET /status`).
3. If any active jobs exist: print `"Jobs: N running, M queued — run \`archon-search jobs list\` for details"` (once bug-021 lands).
4. If zero active jobs: omit the line entirely.
5. Existing status output (auth warnings, failed_expired, GC, telemetry) is unchanged.

## In Scope
- Adding active job count to `archon-search status` output
- Calling `GET /jobs?status=RUNNING,PENDING` (or reading from `GET /status` if the field is added there)
- Pointing users to `archon-search jobs list` once bug-021 is implemented

## Out of Scope
- Per-job progress details in `status` output — that belongs in `archon-search jobs list` (bug-021)
- Adding job queue info to `GET /status` REST response — only if the endpoint doesn't already expose it; check before adding schema changes
- Streaming or live-updating status output

## Key Decisions
- **Omit when idle**: zero active jobs produces no output change — avoids adding clutter to the common case
- **Summary only, not details**: `status` stays a quick-glance command; deep job inspection is bug-021's job
- **Depends on bug-021 for the pointer line**: the "run `jobs list`" hint only makes sense once that command exists; without it, just print the counts

## Edge Cases & Constraints
- **Server not running**: existing connection-refused handling already covers this (bug-012); no new handling needed
- **`GET /jobs` not filtering by status**: verify the endpoint accepts `?status=` query params before implementing; if not, filter client-side on the response
- **Large job counts**: cap the summary at a reasonable ceiling (e.g. "50+ jobs queued") rather than fetching all pages

## Open Questions
- Does `GET /status` already return any job queue field? If so, use it and avoid the second HTTP call. Check `archon_search/server/routes_status.py` and `StatusResponse` in `schemas.py`.
- Does `GET /jobs` support `?status=RUNNING,PENDING` filtering, or only single-status filters?

## Future Iterations
- Live-refreshing status (watch mode): `archon-search status --watch` that refreshes every N seconds
- Per-collection job breakdown in status output

## References
- [[archon_search/cli/status.py]] `[code-agent]` — current status command, no job section
- [[archon_search/server/routes_jobs.py]] `[code-agent]` — `GET /jobs` endpoint
- [[archon_search/server/schemas.py]] `[code-agent]` — `StatusResponse`, `JobResponse` models
- [[Documentation/Backlog/bug-021-jobs-cli-command-brief.md]] `[user]` — prerequisite for the pointer line

## Recommendation
This is a low-effort, high-visibility fix: one extra HTTP call and three lines of output. The pain point is real — submitting a job and having no way to check it from the CLI is a broken workflow. Implement after bug-021 (jobs CLI) so the pointer line is immediately useful, but the count-only version is worth shipping even without it.
