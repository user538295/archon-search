# Bug Brief: Crash-recovery job marking keeps the pre-crash `updated_at` — `process_restart` failures carry timestamps from before the crash

**ID:** 2026-08-19-080 · **Severity:** P1 (functional data loss — `_evict_old` silently drops crash-recovered jobs older than 7 days) · **Status:** Fixed 2026-08-30 — the recovery `dataclasses.replace` now stamps `updated_at=_now_iso()` (`jobs/store.py:361-367`). The `crashed_at` preservation idea under "Tradeoff" was not implemented and remains an optional follow-up.
**Found during:** [[2026-08-19-000-oom-crash-incident-report.md]]

## Problem

When `JobStore` loads its persisted file and finds jobs in a crash status (RUNNING or CANCELLING), it marks
them `FAILED / error="process_restart"` via
`dataclasses.replace(job, status=JobStatus.FAILED, error="process_restart")`
(`jobs/store.py:362-367`) — **without** touching `updated_at`. Every other transition goes through
`JobStore.update`, which stamps `updated_at=_now_iso()` (`jobs/store.py:136`).

Result: a job that died in a crash reports an `updated_at` from *before* the crash — the moment it
last legitimately transitioned (typically its own RUNNING transition, milliseconds after
creation). Anyone (human or tool) reading the jobs API/file concludes the job "failed 4 ms after
creation", when it actually ran for half an hour and died with the process. This actively misled
the 2026-08-19 incident investigation until the code was read.

**This is not just a forensics issue.** Because `_evict_old` uses `updated_at` to decide whether to
drop terminal jobs (cutoff: 7 days), a crash-recovered job whose pre-crash `updated_at` is older
than 7 days will be silently evicted on the very next load — permanent loss of the job record.

## Failing repro

1. Create an ingest job; let it reach RUNNING or CANCELLING; `kill -9` the server.
2. Start the server: `uv run archon-search serve`; read the job (API or `~/.archon-search/archon-search-jobs.json`).

**Observed (production, job `a33c4ab7…`):** `created_at 09:36:59.505409Z`,
`updated_at 09:36:59.509392Z`, `status FAILED`, `error "process_restart"` — although the job ran
from 11:36:59 until the ~12:06 system freeze and the marking itself happened at ~12:11 on the
next boot. Expected: `updated_at` ≈ the recovery time (12:11), i.e. strictly after the restart.
Note: the discrepancy between `created_at`/`updated_at` at 09:36:59Z and "job ran from 11:36:59"
is likely a UTC vs local-time display artifact, not a data error.

## Root cause

`jobs/store.py:361-367`: the load-time recovery path is the only status transition that bypasses
the `updated_at` stamping done by `update()` (:136).

## Fix

One line: include the stamp in the recovery replace —
`dataclasses.replace(job, status=JobStatus.FAILED, error="process_restart",
updated_at=_now_iso())`. (`updated_at` is a plain field on the job dataclasses; the same
`_now_iso()` helper is already used at :136.) Keep `created_at` untouched. Note the file is
persisted only when `modified` is returned — that already happens on this path.

**Tradeoff:** the fix overwrites the only timestamp that recorded when the job was last alive
before the crash. The pre-crash `updated_at` becomes inaccessible. To preserve it, a separate
enhancement could add a `crashed_at` field populated with the old `updated_at` value before
overwriting. This is not required for this fix.

## Verification

Shipped regression tests in `tests/test_job_store.py`, all parametrized and all seeding via the
shared `_seed_job_file` helper:

- `test_crash_recovery_stamps_updated_at` (`RUNNING`/`CANCELLING`) — asserts FAILED /
  `process_restart`, `updated_at >= load_start_time` (parsed as datetimes, not string-compared),
  `created_at` unchanged, and that the refreshed `updated_at` is present in the re-read on-disk JSON.
- `test_crash_recovery_survives_eviction_cutoff` (`RUNNING`/`CANCELLING`) — a crash row whose
  pre-crash `updated_at` is 30 days old still exists after load; this is the data-loss regression.
- `test_load_leaves_non_crash_jobs_untouched` (`DONE`/`FAILED`/`CANCELLED`) — negative case: a
  terminal job keeps its status, `error`, `created_at` and `updated_at`.

Not covered: parametrization over non-ingest job types (`ExportJob`, `SyncJob`). The recovery
branch is type-agnostic (`dataclasses.replace` on the common base fields), so this is an optional
hardening, not a gap in the fix.

## References

- [[archon_search/jobs/store.py]] — recovery replace (:362-367), crash-status check (:361-367), stamping transition (:136)
- [[2026-08-19-000-oom-crash-incident-report.md]] — how the stale stamp misdirected the timeline
