# Brief: Make the startup sync cancellable — today `DELETE /jobs/{id}` always 409s on it

**ID:** 2026-08-20-010 · **Type:** Enhancement · **Status:** Open — deferred, no implementation yet
**Found during:** fix-brief-D documentation sweep, following up on
[[2026-08-19-000-oom-crash-incident-report.md]] (the operator stopped the runaway startup sync by
`launchctl bootout`-ing the whole service — there is still no in-band way to do that)

## Problem

An operator watching RSS climb during the automatic startup collection sync has no way to stop
just that sync short of killing the whole process (`launchctl bootout` / `kill`). `DELETE
/jobs/{id}` on the `SyncJob` that backs the startup sync always answers `409` with
`{"detail": "the startup sync cannot be cancelled"}` (`_STARTUP_SYNC_JOB_NOT_CANCELLABLE_DETAIL`,
`archon_search/server/routes_jobs.py:43`), regardless of how the sync is behaving. That 409 is
deliberate, not an oversight — this brief is about whether and how to remove it, not a bug repro.

## Why it 409s today (verified against source)

`delete_job` (`routes_jobs.py:721-757`) special-cases the job whose id matches
`app.state._startup_sync_job_id` (set once, right after `_start_startup_sync_job` succeeds inside
`_run_startup_sync`, `archon_search/server/app.py:213-214`) and refuses before it would otherwise
transition an active job to `CANCELLING`. The reason is recorded in the `app.state` init comment
(`app.py:1049-1057`):

- The lifespan's `_run_startup_sync` task does not poll or observe its own job's status — it drives
  the job row forward itself and has no code path that reacts to an external `CANCELLING` write.
  Moving the row to `CANCELLING` via `DELETE` would therefore not actually stop anything; the sync
  keeps running.
- `CANCELLING` is one of `_CRASH_STATUSES` (`archon_search/jobs/store.py:32`, alongside `RUNNING`).
  If the process were then killed (or crashed) with the job stuck at `CANCELLING`, the next boot's
  `JobStore._load()` would rewrite it to `FAILED / "process_restart"` — exactly the marker
  `crashed_ingest_on_load` keys on — arming the crash-loop guard for a cancel request that was
  never a crash. Since suppression is now sticky (fix-brief-C item 1 / fix-brief-D docs), that
  false marker would suppress every subsequent boot's startup sync until an operator ran a clean
  `POST /sync`, which is a strictly worse outcome than the current 409.

## What would need to change

1. **Actually stop the sync.** The task handle already exists at `app.state._startup_sync_task`
   (assigned at `app.py:947` right after the task is created; defaults to `None` and is documented
   at `app.py:1046-1048`). `DELETE /jobs/{id}` would need to call `.cancel()` on it instead of (or
   in addition to) writing `CANCELLING` to the job row.
2. **Record the right terminal status.** `_run_startup_sync`'s `except asyncio.CancelledError:`
   branch (`app.py:257-266`) currently sets `app_state.sync_result = StartupSyncResult.FAILED` and
   finishes the job `status=JobStatus.FAILED, error=_STARTUP_SYNC_ERROR_CANCELLED` — i.e. today, a
   cancellation (including an ordinary clean shutdown mid-sync, not just a hypothetical future
   operator-initiated one) is indistinguishable from a real failure. Contrast with
   `routes_sync.py`'s `_sync_task` (the manual `POST /sync` path), which was fixed in this same
   round to record `JobStatus.CANCELLED` on `asyncio.CancelledError` instead of leaving the job
   `RUNNING`. `_run_startup_sync` needs the equivalent fix — record `CANCELLED`, not `FAILED` — both
   as a prerequisite for a clean operator-cancel feature and as a latent inconsistency in its own
   right (a plain shutdown mid-startup-sync currently looks like a failure in `GET /status` /
   `sync_result`, not a cancellation).

## Open questions

- **Shutdown ordering.** An operator-initiated cancel and the lifespan's own shutdown-time
  cancellation would call `.cancel()` on the same task through different code paths. `asyncio.Task
  .cancel()` is idempotent, but the interaction with `job_store.update()` racing a concurrent
  shutdown write has not been worked out — needs a design pass, not just a call-site change.
- **`sync_lock` release.** `_run_startup_sync` holds `app_state.sync_lock` via `async with
  app_state.sync_lock:` around the `collection_sync.sync()` call only (`app.py:221-222`); a
  cancellation propagating through that `async with` should release the lock via the normal
  context-manager exit path, but this has not been exercised under test — verify before shipping.
- **Mid-sync consistency.** Does cancelling `collection_sync.sync()` partway through a collection
  leave `IndexingStateStore` / LanceDB tables in a state a subsequent `POST /sync` can cleanly
  resume from, or does it need its own cleanup? Unverified — `sync.py`'s per-collection write path
  was not audited for cancellation-safety as part of this brief.
- **Readiness during cancellation.** `_startup_sync_pending()` (`routes_ready.py`) gates on `not
  task.done()`. A cancelled task becomes `done()` once the `CancelledError` has propagated and been
  handled, so `/ready` should recover promptly — but this interacts with the terminal-status
  question above (does `checks.sync` read `"fail"` for an operator-requested cancel, same as a real
  failure, or does it need its own `CheckStatus`?).
- **Wire contract.** Does a successful cancel return `202` like every other `DELETE /jobs/{id}`, or
  does the special-cased job deserve a distinct response? `GET /openapi.json` is authoritative —
  any change here is a breaking-contract change requiring a `BREAKING.md` entry.

## Non-goals

This brief is scope-defining only. It does not propose cancelling a **manual** `POST /sync`
(`_sync_task` in `routes_sync.py`) differently than today — that path already supports
`DELETE /jobs/{id}` normally, since it is not the startup-sync special case.

## References

- [[archon_search/server/routes_jobs.py]] — `delete_job` startup-sync special case (:721-757),
  `_STARTUP_SYNC_JOB_NOT_CANCELLABLE_DETAIL` (:43)
- [[archon_search/server/app.py]] — `_run_startup_sync` (:186-290), `CancelledError` handling
  (:257-266), `app.state._startup_sync_task` / `_startup_sync_job_id` init comments (:1046-1057)
- [[archon_search/server/routes_sync.py]] — `_sync_task`'s `CancelledError` → `CANCELLED` handling
  (the pattern `_run_startup_sync` would need to mirror)
- [[archon_search/jobs/store.py]] — `_CRASH_STATUSES` (:32)
- [[2026-08-19-000-oom-crash-incident-report.md]] — the manual `launchctl bootout` this would replace
