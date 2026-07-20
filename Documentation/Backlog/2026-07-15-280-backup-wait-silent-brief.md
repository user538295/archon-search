# Feature Brief: Backup --wait Progress Output

## Problem
When a user runs `archon-search backup --now --wait`, the terminal goes silent for the entire backup duration — which can be minutes for large collections. There is no indication that anything is happening, making it indistinguishable from a hung process.

## Goal
The terminal shows a progress line on each poll cycle so the user knows the backup is still running and how far along it is.

## Users & Context
Operators and developers who trigger a manual backup and want to confirm it completes before proceeding — for example, before a server upgrade or a data migration. They're sitting at the terminal watching it, expecting feedback.

## Core Flow
1. User runs `archon-search backup --now --wait`.
2. Server queues one backup job per collection and returns job IDs.
3. CLI enters the polling loop — **on each iteration**, prints one line: `Backing up... (N/total complete)` overwriting the previous line or appending a new one.
4. On success: `Backup completed for all collections` (existing behavior, unchanged).
5. On failure or timeout: existing error messages (unchanged).

## In Scope
- Add a progress line to the polling loop in `backup_cmd.py:189–191` — printed on each iteration before `time.sleep(_POLL_INTERVAL_SECONDS)`
- Show: jobs completed vs total, and optionally elapsed time

## Out of Scope
- Spinner animation or rich progress bars — plain `click.echo` is sufficient
- Per-collection progress within a single backup job (job-level granularity is what the server exposes)
- Changing `--wait` timeout behavior

## Key Decisions
- **Plain newline output over `\r` overwrite:** `\r` overwrite breaks piped output and log capture; newlines are always safe. One line per poll cycle.
- **Count completed + failed together as "done":** the final message already distinguishes failures; the progress line just needs to show movement.

## Edge Cases & Constraints
- Single-collection install: `(1/1 complete)` on the final poll, then the existing success message — acceptable duplication.
- `_POLL_INTERVAL_SECONDS` controls how often the line appears; no change needed to the interval itself.
- If the server drops mid-wait, the existing `httpx.HTTPError` handler fires (line 171) — no change needed there.

## Key Decisions (continued)

- **No elapsed time**: `_POLL_INTERVAL_SECONDS = 2` (constant, `backup_cmd.py:30`) — a line every 2 seconds is already a sufficient heartbeat. Elapsed time adds no value at this cadence.
- **No `--quiet` flag**: output is one line per 2-second poll — minimal enough that suppression is not warranted. Other `--wait` commands (`collection migrate`, `export`) print progress without a quiet flag; adding one here would be an inconsistent UX surface. If suppression is ever needed, a global `--quiet` on the root command is the right scope.

## Future Iterations
- Rich spinner / live-updating progress bar (requires `rich` or similar — out of scope for this fix)
- Per-collection status breakdown during polling

## References
- [[archon_search/cli/backup_cmd.py:148–194]] `[code-agent]` — polling loop, currently no output between start and finish
- [[Documentation/Backlog/bug-005-collection-add-async-brief.md]] `[user]` — same "terminal appears frozen" class of bug

## Recommendation
One-line fix: add `click.echo(f"Backing up... ({done}/{total} complete)")` inside the polling loop at `backup_cmd.py:189`. This is the smallest change that makes the feature usable — a user can tell it's running and roughly how close it is to done. Do not add a spinner or rich output; keep the same click.echo style as every other command in this file.
