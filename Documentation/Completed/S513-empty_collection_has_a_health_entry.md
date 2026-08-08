## Bug: A maintenance pass over an empty collection skips FTS optimize without erroring

**ID**: S513-empty_collection_has_a_health_entry
**Scenario**: S513
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: the empty collection 'empty_corpus' has no entry in maintenance.collection_health (entries: ['maint_docs']); OperatorGuide/50_maintenance_and_jobs.md:96 says there is one entry per collection, :11/:17 say the pass walks every non-excluded collection and :19 documents the empty-collection FTS skip as a case the loop handles
stdout: Maintenance pass triggered.
Waiting for maintenance pass to complete (current: None)...
Maintenance pass complete. last_run_at=2026-08-06T22:12:59.414580+00:00

assert 'empty_corpus' in ['maint_docs']

### What should happen
- Step 4 exits **0**, not `2` — `50:88`. Exit `2` would mean the pass reported an error in some collection's `last_error`, which `50:19` says an empty collection must not cause ("skipped … **not an error**").
- Step 5: `maintenance.last_run_at` is non-`null` — the pass really ran (`50:80`, `50:94`), so the assertions below are not vacuous.
- Step 5: **every** entry in `maintenance.collection_health[]` has `last_error == null` — `50:19` ("not an error") read together with `50:98` ("non-null on any collection means the last pass failed there").
- Step 5: `maintenance.collection_health[]` contains an entry for the **empty** collection — `50:96` ("one entry per collection") together with `50:11`/`50:17` (the pass walks *every* non-excluded collection, and `exclude` is empty per `50:33`). This is the only black-box evidence that the empty collection was visited and its FTS optimize *skipped*, rather than the collection being ignored by the loop altogether; without an entry, the operator alerting `50:98` prescribes (`last_error` per collection) has nothing to watch for that collection.
- The WARNING itself (`FTSIndexNotFoundError`, `50:19`) is **not** asserted: the docs do not state where it is emitted, and the same exception text is also logged by the *ingest* path (`archon_search.pipeline`, "falling back to rebuild_fts_index"), so a log match could not be attributed to the maintenance pass.

### Steps to reproduce
1. Start an isolated instance; ingest one small document into a collection (control).
2. Create an empty directory and register it: `curl -fsS -X POST -H "Authorization: Bearer <iso-key>" -H 'Content-Type: application/json' -d '{"path": "<empty-dir>"}' http://127.0.0.1:<iso-port>/collections/` — poll `GET /jobs/{id}` to a terminal status.
3. `curl -s -H "Authorization: Bearer <iso-key>" http://127.0.0.1:<iso-port>/collections/` — confirm the registered collection reports `chunk_count == 0`.
4. `archon-search maintenance run --wait --timeout 120 --api-url http://127.0.0.1:<iso-port> --api-key <iso-key>`
5. `curl -s -H "Authorization: Bearer <iso-key>" http://127.0.0.1:<iso-port>/status` — read the `maintenance` object.

### Evidence
```
E   AssertionError: the empty collection 'empty_corpus' has no entry in maintenance.collection_health (entries: ['maint_docs']); OperatorGuide/50_maintenance_and_jobs.md:96 says there is one entry per collection, :11/:17 say the pass walks every non-excluded collection and :19 documents the empty-collection FTS skip as a case the loop handles
E     stdout: Maintenance pass triggered.
E     Waiting for maintenance pass to complete (current: None)...
E     Maintenance pass complete. last_run_at=2026-08-06T22:12:59.414580+00:00
E     
E   assert 'empty_corpus' in ['maint_docs']
```
