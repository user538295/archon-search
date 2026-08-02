## Bug: `jobs show <id>` prints full detail for a completed job

**ID**: S97-collection_matches_ingest_target
**Scenario**: S97
**Severity**: medium
**Version**: archon-search, version 26.8.1751

### What happened
AssertionError: collection is '', expected 's097-col'
job_id       e8266377-82b9-4af0-a719-c7347f8f59b7
job_type     ingest
status       DONE
collection   
source       user
source_path  
created_at   2026-08-01T19:23:42.039668+00:00
updated_at   2026-08-01T19:23:42.665783+00:00
result       {'warnings': []}

assert '' == 's097-col'

- s097-col

### What should happen
- Exits 0 (job is `DONE`; the doc states `show` exits 1 only for `FAILED`, `FAILED_EXPIRED`, or `CANCELLED`).
- Output contains all of: `job_id`, `job_type`, `status`, `collection`, `source`, `source_path` fields.
- `status` value is `DONE`.
- `job_type` value is `ingest`.
- `collection` value is `s097-col` (the `--collection` target of the ingest that created the job).

### Steps to reproduce
1. `archon-search jobs show "$JOB_ID"`

### Evidence
```
E   AssertionError: collection is '', expected 's097-col'
E     job_id       e8266377-82b9-4af0-a719-c7347f8f59b7
E     job_type     ingest
E     status       DONE
E     collection   
E     source       user
E     source_path  
E     created_at   2026-08-01T19:23:42.039668+00:00
E     updated_at   2026-08-01T19:23:42.665783+00:00
E     result       {'warnings': []}
E     
E   assert '' == 's097-col'
E     
E     - s097-col
```
