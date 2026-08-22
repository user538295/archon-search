# Bug Brief: Crash-recovery job marking keeps the pre-crash `updated_at` — `process_restart` failures carry timestamps from before the crash

**ID:** 2026-08-19-080 · **Severity:** P2 (data quality / forensics; no functional breakage found) · **Status:** Open
**Found during:** [[2026-08-19-000-oom-crash-incident-report.md]]

## Problem

When `JobStore` loads its persisted file and finds jobs in a crash status (RUNNING etc.), it marks
them `FAILED / error="process_restart"` via
`dataclasses.replace(job, status=JobStatus.FAILED, error="process_restart")`
(`jobs/store.py:331-334`) — **without** touching `updated_at`. Every other transition goes through
`JobStore.update`, which stamps `updated_at=_now_iso()` (`jobs/store.py:106`).

Result: a job that died in a crash reports an `updated_at` from *before* the crash — the moment it
last legitimately transitioned (typically its own RUNNING transition, milliseconds after
creation). Anyone (human or tool) reading the jobs API/file concludes the job "failed 4 ms after
creation", when it actually ran for half an hour and died with the process. This actively misled
the 2026-08-19 incident investigation until the code was read.

## Failing repro

1. Create an ingest job; let it reach RUNNING; `kill -9` the server.
2. Start the server; read the job (API or `~/.archon-search/archon-search-jobs.json`).

**Observed (production, job `a33c4ab7…`):** `created_at 09:36:59.505409Z`,
`updated_at 09:36:59.509392Z`, `status FAILED`, `error "process_restart"` — although the job ran
from 11:36:59 until the ~12:06 system freeze and the marking itself happened at ~12:11 on the
next boot. Expected: `updated_at` ≈ the recovery time (12:11), i.e. strictly after the restart.

## Root cause

`jobs/store.py:326-335`: the load-time recovery path is the only status transition that bypasses
the `updated_at` stamping done by `update()` (:106).

## Fix

One line: include the stamp in the recovery replace —
`dataclasses.replace(job, status=JobStatus.FAILED, error="process_restart",
updated_at=_now_iso())`. (`updated_at` is a plain field on the job dataclasses; the same
`_now_iso()` helper is already used at :106.) Keep `created_at` untouched. Note the file is
persisted only when `modified` is returned — that already happens on this path.

## Verification

- Test: seed a jobs file with a RUNNING job whose `updated_at` is T0; load the store; assert the
  job is FAILED/`process_restart` **and** `updated_at > T0` (parse both as datetimes; don't
  string-compare).

## References

- [[archon_search/jobs/store.py]] — recovery replace (:326-335), stamping transition (:106)
- [[2026-08-19-000-oom-crash-incident-report.md]] — how the stale stamp misdirected the timeline
