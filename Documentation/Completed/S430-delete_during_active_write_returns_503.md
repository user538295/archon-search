## Bug: `DELETE /collections/{name}` during an active write returns **503**

**ID**: S430-delete_during_active_write_returns_503
**Scenario**: S430
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: DELETE /collections/s430_bulk returned 200 while the ingest job for that collection was RUNNING; UserManual/50_ingestion_and_collections.md:124 documents a 503 for an active write in progress on this route (:117, :166).
job status: RUNNING before, FAILED after
response body: {'name': 's430_bulk', 'deleted': True}
assert 200 == 503

### What should happen
- The ingest job is `RUNNING` immediately before step 5, and has not reached `DONE` immediately after it — the documented "active write in progress" precondition holds across the `DELETE` (`100_jobs_and_async_operations.md`; the job is the only black-box signal of an in-flight write).
- Step 5 returns HTTP **`503`** (`50_ingestion_and_collections.md:124`, applied to the route named at :117 and :166).
- The `DELETE` is **not** honoured while the write is in flight: the response is not a `200` success body reporting `"deleted": true` — the documented outcome refuses the removal and directs the caller to retry after the job (:124).

### Steps to reproduce
1. Start a private instance: `ARCHON_SEARCH_DATA_DIR=$TMP ARCHON_SEARCH_CONFIG=$TMP/archon-search.toml ARCHON_SEARCH_PORT=$PORT archon-search serve &`
2. Create 400 markdown files under `$TMP/s430_bulk/`.
3. `curl -sS -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d "{\"path\":\"$TMP/s430_bulk\"}" http://127.0.0.1:$PORT/collections/` → note `job_id`.
4. Poll `curl -sS -H "Authorization: Bearer $KEY" http://127.0.0.1:$PORT/jobs/<job_id>` until `"status":"RUNNING"`, then wait 10 more seconds.
5. `curl -sS -o /dev/null -w '%{http_code}\n' -X DELETE -H "Authorization: Bearer $KEY" http://127.0.0.1:$PORT/collections/s430_bulk`
6. Re-read `GET /jobs/<job_id>` to confirm the job had not already reached `DONE` when step 5 ran.

### Evidence
```
E   AssertionError: DELETE /collections/s430_bulk returned 200 while the ingest job for that collection was RUNNING; UserManual/50_ingestion_and_collections.md:124 documents a 503 for an active write in progress on this route (:117, :166).
E     job status: RUNNING before, FAILED after
E     response body: {'name': 's430_bulk', 'deleted': True}
E   assert 200 == 503
```
