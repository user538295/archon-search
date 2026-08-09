## Bug: `GET /collections/{name}/migrate?dry_run=true` — route and query parameter are undocumented

**ID**: S381-dry_run_changes_nothing
**Scenario**: S381
**Severity**: medium
**Version**: archon-search, version 26.8.1931

### What happened
AssertionError: /status reported collections_schema_behind=1 after the dry-run calls; 100_upgrading.md:81 states it is 0 once pending migrations have been run, and a dry run applies none
assert 1 == 0

### What should happen
- Step 3: HTTP **200** with a `pending` list — the documented read-only migration plan
  (:102-104, :117-118). This is what the row was reaching for; the documented route serves it.
- Step 5: HTTP **200**. The documented dry-run form carries `dry_run` in the body (:123), and
  `50_ingestion_and_collections.md:156` states it "prints pending schema migrations and
  changes nothing".
- Step 6 vs step 3: the `pending` list and `schema_version` are **unchanged** — ":156 changes
  nothing" — and `/status`'s `collections_schema_behind` is still `0` (:81). This is the "no
  changes" half of the row, asserted against the documented dry-run form.
- Step 4's status is **recorded only**. No assertion: `docs/` specifies no `GET` contract for
  this route.
- **Doc-gap reopening gate**: `docs/` contains no `GET` on `/collections/{name}/migrate` and
  no `dry_run` query parameter. Implemented as a scan of `docs/**/*.md` for a `dry_run=`
  query-string occurrence and for `GET` applied to `.../migrate`. If either ever appears, this
  assertion flips red and S381 must be re-implemented against the then-documented status,
  body, and side-effect contract instead of these stand-ins.

### Steps to reproduce
1. `mkdir -p /tmp/archon_s381_docs && printf '# S381\nThe quick brown fox jumps over the lazy dog.\n' > /tmp/archon_s381_docs/doc.md`
2. `curl -s -X POST -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H 'Content-Type: application/json' -d '{"path":"/tmp/archon_s381_docs"}' http://127.0.0.1:8765/collections/` — poll `GET /jobs/{job_id}` until `DONE`.
3. `curl -s -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" http://127.0.0.1:8765/collections/archon_s381_docs/migrations/pending`
4. `curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" "http://127.0.0.1:8765/collections/archon_s381_docs/migrate?dry_run=true"` — recorded, not asserted.
5. `curl -s -X POST -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H 'Content-Type: application/json' -d '{"dry_run": true}' http://127.0.0.1:8765/collections/archon_s381_docs/migrate`
6. `curl -s -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" http://127.0.0.1:8765/collections/archon_s381_docs/migrations/pending`
7. `archon-search collection remove archon_s381_docs`

### Evidence
```
E   AssertionError: /status reported collections_schema_behind=1 after the dry-run calls; 100_upgrading.md:81 states it is 0 once pending migrations have been run, and a dry run applies none
E   assert 1 == 0
```
