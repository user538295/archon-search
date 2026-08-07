## Bug: `collection remove` during an active ingest is refused with a 503

**ID**: S399-cli_prints_a_retry_after_job_message
**Scenario**: S399
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: `collection remove` reported the collection removed while its ingest job was still writing; :124 documents a 503 refusal and a retry-after-job message
output: Removed collection 'archon_s399_docs'.

assert 'removed collection' not in "removed col...399_docs'.

'removed collection' is contained here:
removed collection 'archon_s399_docs'.

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
E   AssertionError: `collection remove` reported the collection removed while its ingest job was still writing; :124 documents a 503 refusal and a retry-after-job message
E     output: Removed collection 'archon_s399_docs'.
E     
E   assert 'removed collection' not in "removed col...399_docs'.
"
E     
E     'removed collection' is contained here:
E       removed collection 'archon_s399_docs'.
```
