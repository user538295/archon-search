## Bug: `reindex-metadata --dry-run` reports counts and writes nothing

**ID**: S52-collection_resolvable_immediately_after_dry_run
**Scenario**: S52
**Severity**: medium
**Version**: archon-search, version 26.8.1751

### What happened
AssertionError: POST /search for 's052_col' returned 404 ({'detail': 'collection not found'}) immediately after `collection reindex-metadata s052_col --dry-run`. The doc says --dry-run reports counts and writes nothing, so it must not make the collection transiently unresolvable.
assert 404 == 200

### What should happen
- Step 1 exits 0; output reports chunk counts (numbers) without altering stored data
  (`UserManual/55_chunk_metadata_and_enrichment.md:162` — "`--dry-run` — report counts,
  write nothing"; :156 — "Preview counts without writing").
- Step 2 succeeds (HTTP 200) **immediately** after step 1 — a preview must not make the
  collection transiently unresolvable — and returns the same number of results as before
  the dry-run (collection is unchanged).

### Steps to reproduce
1. ```bash
   archon-search collection reindex-metadata s052_col --dry-run
   ```
2. ```bash
   curl -s -X POST http://127.0.0.1:8765/search \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"collection":"s052_col","query":"fox"}' | jq '.results | length'
   ```

### Evidence
```
E   AssertionError: POST /search for 's052_col' returned 404 ({'detail': 'collection not found'}) immediately after `collection reindex-metadata s052_col --dry-run`. The doc says --dry-run reports counts and writes nothing, so it must not make the collection transiently unresolvable.
E   assert 404 == 200
```
