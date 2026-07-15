# Feature Brief: Clear Error When `maintenance run` Cannot Reach the Server

## Problem
Running `archon-search maintenance run` when the server is stopped prints a raw system error (`[Errno 61] Connection refused`) that gives no indication of what went wrong or what to do next.

## Goal
When the server is not running, the command prints a plain, actionable message and exits cleanly — no raw exception text, no confusing exit code.

## Users & Context
Operators running maintenance from a cron job, a script, or the terminal after restarting a machine. They know archon-search but may not immediately recognise an errno 61 as "server is down."

## Core Flow
1. User runs `archon-search maintenance run`.
2. Server is not running.
3. Command prints: `"The server is not running. Start it first with: archon-search serve"`
4. Command exits with code 0 (no server = nothing to trigger, not a bug).

## In Scope
- Replace the raw `httpx.HTTPError` echo in `maintenance_cmd.py:321` with a plain, actionable message.
- Exit with code 0 when the server is unreachable (intent was to trigger a pass — if there's no server, the intent is satisfied vacuously).

## Out of Scope
- Running maintenance in-process without a server (requires constructing the full `SearchStore` + `JobStore` stack; a separate, larger feature).
- Detecting whether the server is running before attempting the call (a pre-flight check adds complexity with no benefit over catching the same error).

## Key Decisions
- **Exit code 0, not 1:** A missing server is not a program error — it is an operator state. Exiting 1 causes cron jobs and monitoring scripts to alert unnecessarily.
- **Single actionable line:** No multi-paragraph explanation. One sentence, the right command to run.

## Edge Cases & Constraints
- **`--wait` flag with no server:** The `--wait` path also calls `httpx.get` for status polling — the same `httpx.HTTPError` catch covers it; no second change needed.
- **Server running but auth wrong (401):** Not affected — that path reaches the `resp.status_code != 202` branch, which is handled separately and correctly.

## Open Questions
- Should the message include `archon-search start` (launchd/systemd install) or `archon-search serve` (foreground)? Both are valid. Suggest checking whether the platform service is installed and printing the appropriate command — or always printing `archon-search serve` as the safe default.

## Future Iterations
- In-process maintenance mode (`--in-process` flag) that runs `MaintenanceLoop._run_one_pass()` directly without a server — deferred because it requires constructing the full store stack and adds concurrent-write risk while Issue 8 (CLI/server coordination) is unresolved.

## References
- [[archon_search/cli/maintenance_cmd.py]] `[code-agent]` — line 319–322, the exact catch block to change

## Recommendation
One-line fix in one file. The current behaviour is actively misleading — errno 61 reads as a network or configuration problem to anyone who hasn't memorised POSIX error codes. Fix this before the next release.
