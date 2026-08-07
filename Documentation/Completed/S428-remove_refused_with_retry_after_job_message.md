## Bug: `archon-search collection remove` during an active write returns **503** and a retry-after-job message

**ID**: S428-remove_refused_with_retry_after_job_message
**Scenario**: S428
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: `collection remove` during an active write was not refused; UserManual/50_ingestion_and_collections.md:124 documents a server 503 for an in-progress write, and a refused HTTP-proxy call exits 1 (:16, :112).
CLI exit=0
stdout: "Removed collection 's428_bulk'.
stderr: ''
assert 0 == 1

### What should happen
- The ingest job is `RUNNING` immediately before **and** immediately after step 5 — the documented "active write in progress" precondition holds for the whole `remove` call (`100_jobs_and_async_operations.md`; the job is the only black-box signal of an in-flight write).
- Step 5 exits **`1`** — the CLI is an HTTP proxy and exits `1` when the server refuses the operation (`50_ingestion_and_collections.md:16`, :112).
- Step 5 prints a **retry-after-job message** — output that tells the caller to retry once the in-flight job finishes (:124). Asserted as: the output must not be the success line `Removed collection '<name>'.`, and must name the retry/job condition.
- The underlying `DELETE /collections/{name}` is refused with **503** (:117 + :124). The HTTP status is asserted separately by S430; here the CLI-level contract is the subject.

### Steps to reproduce
1. Start a private instance: `ARCHON_SEARCH_DATA_DIR=$TMP ARCHON_SEARCH_CONFIG=$TMP/archon-search.toml ARCHON_SEARCH_PORT=$PORT archon-search serve &`
2. Create 400 markdown files under `$TMP/s428_bulk/`.
3. `curl -sS -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d "{\"path\":\"$TMP/s428_bulk\"}" http://127.0.0.1:$PORT/collections/` → note `job_id`.
4. Poll `curl -sS -H "Authorization: Bearer $KEY" http://127.0.0.1:$PORT/jobs/<job_id>` until `"status":"RUNNING"`, then wait 10 more seconds and confirm it is still `RUNNING`.
5. `archon-search collection remove s428_bulk --api-url http://127.0.0.1:$PORT --api-key $KEY`
6. Re-read `GET /jobs/<job_id>` to confirm the job was still `RUNNING` across step 5.

### Evidence
```
E   AssertionError: `collection remove` during an active write was not refused; UserManual/50_ingestion_and_collections.md:124 documents a server 503 for an in-progress write, and a refused HTTP-proxy call exits 1 (:16, :112).
E     CLI exit=0
E     stdout: "Removed collection 's428_bulk'.
"
E     stderr: ''
E   assert 0 == 1
```
