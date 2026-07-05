# Manual Test: T-4 CPU Priority Degradation

## Setup
- Ensure you have a non-root user account that lacks CAP_SYS_NICE capability
- Verify: `getcap /path/to/archon-search` or run `archon-search` as the restricted user

## Test Steps
- [ ] Start `archon-search serve` as a user without CAP_SYS_NICE
- [ ] Trigger GC via `curl -X POST http://localhost:8000/maintenance/trigger -H "Authorization: Bearer <key>"`
- [ ] Monitor logs (tail the log file at ~/.archon-search/archon-search.log)
- [ ] Verify: WARNING logged when setting CPU priority (os.setpriority fails gracefully)
- [ ] Verify: rebuild task completes normally (no crashes or hangs)
- [ ] Verify: GET /status shows communities_invalidated=False after rebuild completes

## Acceptance
All steps passed without crashes or hangs.
