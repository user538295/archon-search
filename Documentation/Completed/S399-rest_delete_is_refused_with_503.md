## Bug: `collection remove` during an active ingest is refused with a 503

**ID**: S399-rest_delete_is_refused_with_503
**Scenario**: S399
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: DELETE /collections/archon_s399_docs returned 200 while its ingest job was still writing; UserManual/50_ingestion_and_collections.md:124 documents 503 for an active write in progress
assert 200 == 503

### What should happen
- Step 2 reaches `RUNNING`, establishing `:124`'s "active write in progress" precondition
  (`100_jobs_and_async_operations.md:45`).
- Step 3 returns HTTP **`503`** — `:124`, "Active write in progress → server **503**". `:117`
  makes `DELETE /collections/{name}` the surface the CLI proxies, so this is the same refusal the
  CLI must surface.
- Step 5 exits **non-zero** — `:124` documents a refusal, and a refused removal cannot report
  success. The exact code is not documented, so only "not `0`" is asserted.
- Step 5 prints a non-empty message referring the caller back to the running job — `:124`, "CLI
  prints a **retry-after-job message**". The exact wording is not documented (contrast `:123`,
  which quotes the pinned-only string verbatim), so the assertion is that the output is non-empty
  and does not claim the collection was removed.

### Steps to reproduce
1. Seed `/tmp/archon-s399-docs/` with 12 documents and run `archon-search collection add /tmp/archon-s399-docs`.
2. Poll `archon-search jobs status <job_id>` until it reports `RUNNING`.
3. `curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" http://127.0.0.1:8765/collections/<name>`
4. Wait for `<job_id>` to reach a terminal state; remove the collection; repeat steps 1–2.
5. `archon-search collection remove <name>` while the second job reports `RUNNING`.
6. Wait for the second job to reach a terminal state; remove the collection.

### Evidence
```
E   AssertionError: DELETE /collections/archon_s399_docs returned 200 while its ingest job was still writing; UserManual/50_ingestion_and_collections.md:124 documents 503 for an active write in progress
E   assert 200 == 503
```
