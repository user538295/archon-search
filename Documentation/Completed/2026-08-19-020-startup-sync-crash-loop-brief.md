# Bug Brief: Startup sync auto-re-ingests every configured collection on every boot — after an OOM crash the server re-enters the same death spiral unattended

**ID:** 2026-08-19-020 · **Severity:** P0 · **Status:** Done (2026-08-20) — crash-loop guard shipped as decided 2026-08-19 (owner): Fix item 1 only, suppress on the first `process_restart` marker; the `sync_on_startup` config opt-out (former item 2) was considered and **rejected**. `JobStore.crashed_ingest_on_load` (`jobs/store.py`) gates the lifespan's startup-sync task (`server/app.py`); the state is `StartupSyncResult.SUPPRESSED` on `GET /status` and `checks.sync: "warn"` (still 200) on `GET /ready`, cleared to `"done"` / `"ok"` by a clean `POST /sync` (`server/routes_sync.py`). The startup sync is itself job-backed (`_start_startup_sync_job` opens a `SyncJob`, driven to `DONE`/`FAILED` on every exit path), so a crash *during the unattended startup sync* — the sequence in Production evidence below, which writes no job of its own — also leaves a `process_restart` marker: the guard arms itself instead of only breaking the first hop. Both Verification bullets covered by `tests/test_c1_bug_repros.py`; contract change recorded in `BREAKING.md`.
**Found during:** [[2026-08-19-000-oom-crash-incident-report.md]]

## Problem

`app.py`'s lifespan spawns a background startup-sync task that re-syncs **all** configured
collections on every server start (`app.py:701-756`). Combined with the launchd service
(auto-start at login, KeepAlive), this means: an ingest that crashes the machine →
reboot → launchd restarts the server → startup sync immediately restarts the exact same ingest →
machine crashes again. There is no crash-loop detection, no operator confirmation, and no way to
boot the server without it re-entering the workload — even though the job store *already knows*
the previous run died mid-ingest (it marks RUNNING jobs `FAILED / "process_restart"` on load,
`jobs/store.py:326-335`).

Observed live during the incident: 4 minutes after the post-crash reboot the server was already
re-running RapidOCR over the same corpus with no user action, and RSS was climbing again.

## Failing repro

1. Configure ≥1 collection in `[collections]`, `watch = true`.
2. Start the server; while ingest is running, `kill -9` it (simulates the OOM death).
3. Start the server again.

**Observed:** the startup sync begins re-ingesting the same corpus immediately (log:
RapidOCR/model loads within ~60 s of boot; `_run_startup_sync` task created unconditionally at
`app.py:752-756` whenever `all_cols` is non-empty). The jobs file simultaneously shows the prior
ingest marked `FAILED / "process_restart"` — the signal exists and is ignored.

## Root cause

`app.py:705-756`: `_run_startup_sync(all_cols)` is created unconditionally when any collection is
configured. Its only guards are a wall-clock timeout (`_STARTUP_SYNC_TIMEOUT_SECONDS`) and the
`sync_lock` against a concurrent POST /sync. Nothing consults:
- the JobStore's `process_restart` markers (unclean previous exit, `jobs/store.py:326-335`),
- any resource budget,
- any operator opt-out (`[collections] watch` gates only the filesystem watcher, not the sync).

## Production evidence

Session 2 (post-crash boot 12:07:48): server start 12:11:57, `Filesystem watcher started for 2
collection(s)` 12:12:30, RapidOCR engines loading 12:13:01 with **no user-initiated job** (jobs
file untouched); live capture at 12:16 showed 2.9 GB RSS + 1.2 GB GPU, 42% CPU, and the user
independently reported "It is already started to eat the memory". Service had to be
`launchctl bootout`-ed to stop the loop.

## Fix

1. **Crash-loop guard (core fix):** in the lifespan, before creating `_run_startup_sync`, check
   whether the JobStore load just marked any ingest-family job `process_restart` (have
   `JobStore.load()` return / expose that fact — it already computes it). If yes: **skip the
   automatic sync**, log a prominent WARNING ("previous run died mid-ingest; automatic startup
   sync suppressed — run POST /sync or `archon-search sync` to resume"), and surface the state in
   `GET /status` / `/ready` (as a degraded-but-ready flag, not a 503 — keep the lifespan-never-
   blocks invariant).
2. ~~Config opt-out (`sync_on_startup`)~~ — **rejected by owner decision 2026-08-19**: the guard
   alone covers the incident pattern; no new config surface.
3. **Resolved:** suppress on the FIRST `process_restart` marker (no two-strike rule) — the
   failure class is machine-killing and the resume path is one command.

## Verification

- Test: seed a jobs file containing a RUNNING ingest job → construct the app → assert the
  startup-sync task is **not** created and the WARNING is logged; with a clean jobs file the task
  is created (existing behavior). Note `tests/CLAUDE.md` lifespan-task rules
  (`app.state` defaults, poll `.done()`).
- Test: the suppressed state is visible in `GET /status` (degraded flag) and clears after a
  successful manual sync.

## References

- [[archon_search/server/app.py]] — `_run_startup_sync` creation (:701-756), watcher block (:653-699)
- [[archon_search/jobs/store.py]] — `process_restart` marking on load (:326-335)
- [[2026-08-19-000-oom-crash-incident-report.md]] — session-2 live observation
- [[2026-08-19-010-image-ocr-unbounded-memory-brief.md]] — the workload this re-triggers
