## Bug: `[maintenance] retry_max_age_hours = 0` disables retry: FAILED ingest jobs go to `FAILED_EXPIRED`

**ID**: S499-failed_job_is_expired_and_never_retried
**Scenario**: S499
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: with `retry_max_age_hours = 0` the maintenance pass re-enqueued the FAILED ingest job as 1 new job(s) with source='maintenance', i.e. it took 50_maintenance_and_jobs.md:59's *eligible* branch. :61 states that `0` 'effectively disables retry (no job is ever young enough)' and :57 that an aged-out job is 'never retried again'. (The instance's own startup warning also contradicts :61 — it announces immediate retry eligibility, matching 30_configuration.md:182's 'immediate churn' instead. The two doc pages conflict.)
job after the pass: {"job_id": "5bce577f-5a82-4b09-a0e8-b2f4ebc632da", "status": "FAILED", "created_at": "2026-08-06T21:59:21.630584+00:00", "updated_at": "2026-08-06T21:59:21.632808+00:00", "result": null, "error": "path does not exist or is not a file/directory: /private/tmp/archon_s499_absent_dir", "namespace": "default", "progress": null, "source": "user", "source_path": "/private/tmp/archon_s499_absent_dir", "collection": "s499_docs", "retry_count": 0, "output_path": null, "archive_path": null, "kind": null, "migrations_applied": null, "backup_confirmed": null, "job_type": "ingest"}
GET /jobs?status=FAILED_EXPIRED: {"items": [], "next_cursor": null, "total": 0}
GET /jobs (all): {"items": [{"job_id": "45b6c163-19a2-4927-ba14-60809dc3a162", "status": "PENDING", "created_at": "2026-08-06T21:59:23.664925+00:00", "updated_at": "2026-08-06T21:59:23.664925+00:00", "result": null, "error": null, "namespace": "default", "progress": null, "source": "maintenance", "source_path": "/private/tmp/archon_s499_absent_dir", "collection": "s499_docs", "retry_count": 0, "output_path": null, "archive_path": null, "kind": null, "migrations_applied": null, "backup_confirmed": null, "job_type": "ingest"}, {"job_id": "5bce577f-5a82-4b09-a0e8-b2f4ebc632da", "status": "FAILED", "created_at": "2026-08-06T21:59:21.630584+00:00", "updated_at": "2026-08-06T21:59:21.632808+00:00", "result": null, "error": "path does not exist or is not a file/directory: /private/tmp/archon_s499_absent_dir", "namespace": "default", "progress": null, "source": "user", "source_path": "/private/tmp/archon_s499_absent_dir", "collection": "s499_docs", "retry_count": 0, "output_path": null, "archive_path": null, "kind": null, "migrations_applied": null, "backup_confirmed": null, "job_type": "ingest"}], "next_cursor": null, "total": 2}
/status failed_expired_ingest_count: 0
startup warning(s): ['[maintenance].retry_max_age_hours = 0: all failed ingest jobs will be immediately eligible for retry regardless of age; this may cause excessive retry churn']
assert not [{'archive_path': None, 'backup_confirmed': None, 'collection': 's499_docs', 'created_at': '2026-08-06T21:59:23.664925+00:00', ...}]

### What should happen
- Step 7: the instance starts healthy and logs a warning naming
  `[maintenance].retry_max_age_hours = 0` — `30_configuration.md:182`, "`0` warns". This proves
  the key was read by this build, so any outcome below cannot be explained by an ignored setting.
- Step 2: the seeded job reaches `FAILED` — the precondition of the retry policy, which
  `50_maintenance_and_jobs.md:55` scopes to "every FAILED ingest job".
- Step 3: the trigger returns `202` with `{"status": "triggered"}`
  (`50_maintenance_and_jobs.md:72`).
- Step 4: after the pass the job's status is **`FAILED_EXPIRED`** — line 57's aged-out branch
  ("created before `now - retry_max_age_hours`"), which at `0` is every existing job.
- Step 5: that job appears in `GET /jobs?status=FAILED_EXPIRED` (line 61;
  `UserManual/100_jobs_and_async_operations.md:53`; `UserManual/160_troubleshooting.md:176`).
- Step 6: **no** job with `source: "maintenance"` exists for that `source_path` — line 61,
  "Setting `retry_max_age_hours = 0` effectively disables retry", and line 57, "never retried
  again". A maintenance-sourced re-enqueue is line 59's *eligible* branch, which `0` excludes.

### Steps to reproduce
1. Start an isolated instance whose config carries:
   ```toml
   [maintenance]
   retry_max_age_hours = 0
   ```
2. Seed one FAILED ingest job:
   `curl -s -X POST "$BASE/ingest" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{"path":"/tmp/archon_s499_absent_dir","collection":"s499_docs"}'`
   then poll `GET /jobs/{job_id}` until it is terminal.
3. `curl -s -X POST "$BASE/maintenance/trigger" -H "Authorization: Bearer $KEY"` and poll
   `GET /status` until `maintenance.last_run_at` is non-null (the pass completed).
4. `curl -s "$BASE/jobs/{job_id}" -H "Authorization: Bearer $KEY"`
5. `curl -s "$BASE/jobs?status=FAILED_EXPIRED" -H "Authorization: Bearer $KEY"`
6. `curl -s "$BASE/jobs" -H "Authorization: Bearer $KEY"` — look for any job with
   `source: "maintenance"` for the same `source_path`.
7. Read the instance's `serve.log` for the startup warning.

### Evidence
```
E   AssertionError: with `retry_max_age_hours = 0` the maintenance pass re-enqueued the FAILED ingest job as 1 new job(s) with source='maintenance', i.e. it took 50_maintenance_and_jobs.md:59's *eligible* branch. :61 states that `0` 'effectively disables retry (no job is ever young enough)' and :57 that an aged-out job is 'never retried again'. (The instance's own startup warning also contradicts :61 — it announces immediate retry eligibility, matching 30_configuration.md:182's 'immediate churn' instead. The two doc pages conflict.)
E     job after the pass: {"job_id": "5bce577f-5a82-4b09-a0e8-b2f4ebc632da", "status": "FAILED", "created_at": "2026-08-06T21:59:21.630584+00:00", "updated_at": "2026-08-06T21:59:21.632808+00:00", "result": null, "error": "path does not exist or is not a file/directory: /private/tmp/archon_s499_absent_dir", "namespace": "default", "progress": null, "source": "user", "source_path": "/private/tmp/archon_s499_absent_dir", "collection": "s499_docs", "retry_count": 0, "output_path": null, "archive_path": null, "kind": null, "migrations_applied": null, "backup_confirmed": null, "job_type": "ingest"}
E     GET /jobs?status=FAILED_EXPIRED: {"items": [], "next_cursor": null, "total": 0}
E     GET /jobs (all): {"items": [{"job_id": "45b6c163-19a2-4927-ba14-60809dc3a162", "status": "PENDING", "created_at": "2026-08-06T21:59:23.664925+00:00", "updated_at": "2026-08-06T21:59:23.664925+00:00", "result": null, "error": null, "namespace": "default", "progress": null, "source": "maintenance", "source_path": "/private/tmp/archon_s499_absent_dir", "collection": "s499_docs", "retry_count": 0, "output_path": null, "archive_path": null, "kind": null, "migrations_applied": null, "backup_confirmed": null, "job_type": "ingest"}, {"job_id": "5bce577f-5a82-4b09-a0e8-b2f4ebc632da", "status": "FAILED", "created_at": "2026-08-06T21:59:21.630584+00:00", "updated_at": "2026-08-06T21:59:21.632808+00:00", "result": null, "error": "path does not exist or is not a file/directory: /private/tmp/archon_s499_absent_dir", "namespace": "default", "progress": null, "source": "user", "source_path": "/private/tmp/archon_s499_absent_dir", "collection": "s499_docs", "retry_count": 0, "output_path": null, "archive_path": null, "kind": null, "migrations_applied": null, "backup_confirmed": null, "job_type": "ingest"}], "next_cursor": null, "total": 2}
E     /status failed_expired_ingest_count: 0
E     startup warning(s): ['[maintenance].retry_max_age_hours = 0: all failed ingest jobs will be immediately eligible for retry regardless of age; this may cause excessive retry churn']
E   assert not [{'archive_path': None, 'backup_confirmed': None, 'collection': 's499_docs', 'created_at': '2026-08-06T21:59:23.664925+00:00', ...}]
```
