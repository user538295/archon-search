# Feature Brief: Fix "Connection Refused" Error Messages Across All CLI Commands

## Problem
When a user runs a CLI command that needs the server (`key rotate`, `key create`, `backup --now`, and others) while the server is stopped, they see a raw Python error — `"Error contacting server: [Errno 61] Connection refused"` — with no explanation of what went wrong or how to fix it.

## Goal
Every CLI command that requires a running server prints a clear, actionable message when the server is not reachable — and exits cleanly — instead of surfacing a raw system error code.

## Users & Context
Any operator running archon-search CLI commands in a terminal. Most likely to hit this when the server has crashed, hasn't started yet, or is being managed by a process supervisor. The raw errno message tells them nothing about archon-search specifically.

## Core Flow
1. User runs a server-dependent CLI command (e.g. `archon-search key create --label my-key`).
2. The CLI attempts to reach the server.
3. If the server is unreachable, the CLI prints: `"Server is not running. Start it first with: archon-search start"` and exits with a non-zero code.
4. If the server is reachable, the command proceeds normally.

## In Scope
- All CLI commands that make HTTP calls and currently surface raw `[Errno 61]` / `httpx.ConnectError` to the user
- Confirmed affected commands: `key create` (`key_cmd.py:455`), `key rotate` (`key_cmd.py:182`), `backup --now` (`backup_cmd.py:99`)
- A shared `_require_server(url)` helper in `archon_search/cli/_helpers.py` (or equivalent shared CLI module) that all affected commands call — avoids repeating the fix per file
- bug-006 (`maintenance run` — `maintenance_cmd.py:321`) is the same fix; this brief covers the remaining commands and can be implemented in the same PR as bug-006

## Out of Scope
- Changing what happens when the server IS running (no behaviour change on the happy path)
- Auto-starting the server when it's not running (separate feature; the right default is "tell the user" not "do something invisible")
- Commands that already show a helpful message — no change needed there

## Key Decisions
- **Shared helper over per-file fixes:** A `_require_server(url)` helper in the shared CLI helpers module catches `httpx.ConnectError` / `httpx.HTTPError` and prints the standard message. Every affected command calls it. Adding a new server-dependent command in future uses the same helper — one place to update the message wording.
- **Bundle with bug-006:** The fix is identical; one PR touching `maintenance_cmd.py`, `key_cmd.py`, `backup_cmd.py`, and the new helper is cleaner than two separate PRs.
- **Exit with non-zero code:** The command should exit with a non-zero code (e.g. `sys.exit(1)`) so scripts and CI can detect the failure — but the *message* should be human-readable, not the raw errno.

## Edge Cases & Constraints
- **Server is running but returns a non-200 on a different error:** The helper should only intercept connection errors, not all HTTP errors. A `400 Bad Request` from the server should still surface as-is.
- **`--api-url` flag:** Some commands accept a custom server URL. The helper must use the resolved URL (after flag/env/config resolution), not a hardcoded default.
- **Windows path separators in the start command:** The suggested command `archon-search start` is the same on all platforms — no OS-specific branching needed.

## Key Decisions (continued)

- **Exit code `1`**: consistent with Unix convention and every other error exit in the CLI. A bespoke code `3` ("server unavailable") would require documenting and maintaining a new exit-code contract with no callers today.
- **`_require_server` belongs in `_helpers.py`**: already imported by every CLI module that needs it; no new file justified for a single function.
- **Implementer must grep at implementation time**: `grep -rn "Error contacting server\|ConnectError\|ConnectionRefused" archon_search/cli/` to confirm all affected call sites beyond the three confirmed in this brief.

## Future Iterations
- A `archon-search doctor` command that checks server reachability, config validity, model availability, and disk space — a single diagnostic command that surfaces all of these at once.

## References
- [[archon_search/cli/key_cmd.py]] `[code-agent]` — `key create` (line 455) and `key rotate` (line 182) affected call sites
- [[archon_search/cli/backup_cmd.py]] `[code-agent]` — `backup --now` affected call site (line 99)
- [[archon_search/cli/maintenance_cmd.py]] `[code-agent]` — bug-006 reference fix at line 321
- [[Documentation/Backlog/bug-006-maintenance-run-ux-brief.md]] `[user]` — sibling brief, same bug class

## Recommendation
This is a one-day fix with zero risk — no logic changes, no new behaviour, just better error text. The shared helper pattern means every future server-dependent command gets the right UX for free. Do it in the same PR as bug-006 to avoid touching the same files twice.
