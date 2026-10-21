# Brief: Make a sync cancellable — today `DELETE /jobs/{id}` always 409s on every non-terminal sync

**ID:** 2026-08-20-010 · **Type:** Enhancement · **Status:** Open — deferred, no implementation yet
**Found during:** fix-brief-D documentation sweep, following up on [[2026-08-19-000-oom-crash-incident-report.md]] (the operator stopped the runaway startup sync by `launchctl bootout`-ing the whole service — there is still no in-band way to do that)

## Problem

An operator watching RSS climb during a collection sync — the automatic startup sync or a manual `POST /sync` — has no way to stop just that sync short of stopping the whole process (`launchctl bootout` / `kill`). `DELETE /jobs/{id}` on any non-terminal `SyncJob` always answers `409` with `{"detail": "a sync cannot be cancelled"}` (`routes_jobs.delete_job`; it answered `"the startup sync cannot be cancelled"` for the startup sync only until the next release extended the 409 to every non-terminal `SyncJob`, manual `POST /sync` ones and legacy `QUEUED`/`PENDING` rows included), regardless of how the sync is behaving. That 409 is deliberate, not an oversight — this brief is about whether and how to remove it, not a bug repro.

## Why it 409s today (verified against source)

`delete_job` (`archon_search/server/routes_jobs.py`) answers `404` for a missing or other-namespace job and an idempotent `200` for a terminal one; then, for any job where `isinstance(job, SyncJob)` holds, it returns `409` with `_SYNC_JOB_NOT_CANCELLABLE_DETAIL` before it would otherwise transition an active job to `CANCELLING`. The reason is recorded in the comment above `_SYNC_JOB_NOT_CANCELLABLE_DETAIL`:

- Neither sync task polls or observes its own job's status. The lifespan's `_run_startup_sync` (`archon_search/server/app.py`) and the manual path's `_sync_task` (`archon_search/server/routes_sync.py`) drive the job row forward themselves and have no code path that reacts to an external `CANCELLING` write. Moving the row to `CANCELLING` via `DELETE` would therefore not actually stop anything; the sync keeps running.
- `CANCELLING` is one of `_CRASH_STATUSES` (`archon_search/jobs/store.py`, alongside `RUNNING`), and sync jobs are ingest-family (`_INGEST_FAMILY_JOB_TYPES`). If the process were then killed (or crashed) with the job stuck at `CANCELLING`, the next boot's `JobStore._load()` would rewrite it to `FAILED / "process_restart"` — exactly the marker `crashed_ingest_on_load` keys on — arming the crash-loop guard for a cancel request that was never a crash. Since suppression is sticky (fix-brief-C item 1 / fix-brief-D docs), that false marker would suppress every subsequent boot's startup sync until an operator ran a clean `POST /sync`, which is a strictly worse outcome than the current 409.

## What would need to change

1. **Actually stop the sync.** The startup sync's task handle exists at `app.state._startup_sync_task` (assigned in the lifespan right after the task is created; defaults to `None` in `create_app`'s `app.state` init). A manual sync's task is only tracked anonymously in `app.state._background_tasks` (`routes_sync._spawn_sync_task`), so it would need a job-id → task mapping. `DELETE /jobs/{id}` would then call `.cancel()` on the task instead of writing `CANCELLING` to the job row.
2. **Record the right terminal status — done for the job row.** Both sync paths already catch `asyncio.CancelledError` and record `JobStatus.CANCELLED` (`_run_startup_sync` via `_finish_startup_sync_job(..., status=JobStatus.CANCELLED)`; `_sync_task` via `_persist_job_status`), so a clean shutdown mid-sync no longer looks like a failure in `GET /jobs`. Still open: `_run_startup_sync`'s `CancelledError` branch sets `app_state.sync_result = StartupSyncResult.FAILED`, which is moot during shutdown but would make an operator-initiated cancel read as `checks.sync: "fail"` / `sync_result: "failed"` (see the readiness question below).

## Open questions

- **Shutdown ordering.** An operator-initiated cancel and the lifespan's own shutdown-time cancellation (which cancels every task in `app.state._background_tasks`) would call `.cancel()` on the same task through different code paths. `asyncio.Task.cancel()` is idempotent, but the interaction with the job-store write racing a concurrent shutdown write has not been worked out — needs a design pass, not just a call-site change.
- **`sync_lock` release.** `_run_startup_sync` holds `app_state.sync_lock` via `async with app_state.sync_lock:` around the `collection_sync.sync()` call only; `_sync_task` releases it in its `finally`. A cancellation should release the lock via those normal exit paths, but an operator-initiated cancel has not been exercised under test — verify before shipping.
- **Mid-sync consistency.** Does cancelling `collection_sync.sync()` partway through a collection leave `IndexingStateStore` / LanceDB tables in a state a subsequent `POST /sync` can cleanly resume from, or does it need its own cleanup? Unverified — `sync.py`'s per-collection write path was not audited for cancellation-safety as part of this brief.
- **Readiness during cancellation.** `_startup_sync_pending()` (`routes_ready.py`) gates on `not task.done()`. A cancelled task becomes `done()` once the `CancelledError` has propagated and been handled, so `/ready` should recover promptly — but this interacts with the `sync_result` question in item 2 (does `checks.sync` read `"fail"` for an operator-requested cancel, same as a real failure, or does it need its own `CheckStatus`?).
- **Wire contract.** Does a successful cancel return `202` like every other `DELETE /jobs/{id}`, or does a sync deserve a distinct response? `GET /openapi.json` is authoritative — replacing the `409` is a breaking-contract change requiring a `BREAKING.md` entry.

## Non-goals

This brief is scope-defining only. It covers both the startup sync and a manual `POST /sync`, since both now answer the same `409`; it does not propose making them cancellable in different ways, nor changing how a server shutdown already cancels them (recording `CANCELLED`).

## References

- [[archon_search/server/routes_jobs.py]] — `delete_job`'s `isinstance(job, SyncJob)` 409 branch and `_SYNC_JOB_NOT_CANCELLABLE_DETAIL` (with its rationale comment)
- [[archon_search/server/app.py]] — `_run_startup_sync` and its `CancelledError` → `CANCELLED` branch, the lifespan's shutdown task cancellation, the `app.state._startup_sync_task` init comment in `create_app`
- [[archon_search/server/routes_sync.py]] — `_sync_task`'s `CancelledError` → `CANCELLED` handling, `_spawn_sync_task`
- [[archon_search/jobs/store.py]] — `_CRASH_STATUSES`, `_INGEST_FAMILY_JOB_TYPES`
- [[2026-08-19-000-oom-crash-incident-report.md]] — the manual `launchctl bootout` this would replace
