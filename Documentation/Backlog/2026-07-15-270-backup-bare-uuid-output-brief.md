# Feature Brief: Backup Output Shows Collection Names, Not Bare Job IDs

## Problem
When a user runs `archon-search backup --now`, the output is a plain list of UUIDs — one per collection being backed up. There is no indication of which UUID belongs to which collection, so the user cannot tell what is being backed up or which job to track if something fails.

Current output:
```
Queued jobs:
a1b2c3d4-...
e5f6g7h8-...
```

## Goal
`backup --now` prints one line per queued job showing the collection name alongside the job ID. If `--wait` is passed, completion status is also shown per collection by name.

Target output:
```
Queued jobs:
  my_docs       → a1b2c3d4-...
  project_code  → e5f6g7h8-...
```

## Users & Context
Operators running scheduled or manual backups who need to know which collections were queued and how to track each job individually if a failure occurs.

## Core Flow
1. User runs `archon-search backup --now` (optionally with `--wait`).
2. Server queues one backup job per collection and responds.
3. CLI prints each collection name and its job ID on one line.
4. If `--wait`: as each job completes, CLI prints `collection_name: DONE` or `collection_name: FAILED — <error>`.

## In Scope
- `backup --now` output: print `{collection}: {job_id}` pairs
- `backup --now --wait` progress: show per-collection status as jobs resolve

## Out of Scope
- Changing how `backup status` displays — that command is separately formatted and not broken
- Changing backup scheduling or trigger logic

## Key Decisions
- **Change `BackupTriggerResponse.queued` on the server from `list[str]` to `list[{collection, job_id}]`**: this is the clean fix — the CLI gets what it needs without extra HTTP calls, and `queued` becomes consistent with the `skipped` list which already returns `{collection, reason}` pairs. The CLI-only workaround (poll each job ID immediately) would cost N extra HTTP calls on every backup run. This is a breaking schema change; add to `BREAKING.md` and regenerate the OpenAPI snapshot. The `_wait_for_jobs` helper must be updated to accept a `{job_id: collection}` map so it can label progress lines by collection name rather than job ID.
- **`--wait` output**: use collection name (not job ID) as the progress label — the job ID is already printed at queue time; the user doesn't need to see it again during polling.

## Edge Cases & Constraints
- `BackupTriggerResponse.queued` is `list[str]` today (`schemas.py:509`) — changing it to `list[QueuedBackupJob]` is a breaking schema change; add to `BREAKING.md`.
- `_wait_for_jobs` currently receives `list[str]` job IDs. If the schema changes, it needs `list[{collection, job_id}]` threaded through, or a `{job_id: collection}` lookup dict.
- The `skipped` list already shows `{collection, reason}` pairs — the fix makes `queued` consistent with `skipped`.

## Open Questions
- Does `_wait_for_jobs` need to accept a full `job_id → collection` map upfront, or is a lazy `GET /jobs/{id}` re-fetch on failure sufficient? The map approach labels every progress line by collection name; the lazy approach labels only failures. Both work — decide based on how much per-cycle progress output `--wait` should show.

## Future Iterations
- `backup status` could also show per-collection backup size and duration — deferred, not part of this fix.

## References
- [[archon_search/cli/backup_cmd.py:112–115]] `[code-agent]` — bare `click.echo(job_id)` loop
- [[archon_search/server/schemas.py:506–510]] `[code-agent]` — `BackupTriggerResponse` with `queued: list[str]`
- [[archon_search/server/schemas.py:455–476]] `[code-agent]` — `JobResponse` has `collection: str = ""` — available via `GET /jobs/{id}`

## Recommendation
Fix the server response first (`BackupTriggerResponse.queued` → list of objects), then update the CLI to print the collection name. The CLI-only workaround (poll each job immediately) ships faster but adds N HTTP calls and is unnecessary once the server sends the right data. The skipped list already does this correctly — `queued` should match it.
