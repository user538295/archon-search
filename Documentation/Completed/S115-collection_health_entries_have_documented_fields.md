## Bug: `archon-search maintenance status --json` returns structured JSON

**ID**: S115-collection_health_entries_have_documented_fields
**Scenario**: S115
**Severity**: medium
**Version**: archon-search, version 26.8.1751

### What happened
AssertionError: collection_health[0] missing documented fields ['expired_chunks_removed_last_run', 'communities_invalidated']; keys=['centroid_recompute_threshold', 'collection', 'fts_optimized_at', 'last_error', 'last_retry_at', 'meta_chunk_count', 'mutations_since_recompute', 'orphans_removed_last_run']
assert not ['expired_chunks_removed_last_run', 'communities_invalidated']

### What should happen
- Exits 0.
- Output is valid JSON (parseable with `python3 -m json.tool`).
- JSON contains a `collection_health` array (or equivalent top-level key); each entry
  includes all seven fields `OperatorGuide/50_maintenance_and_jobs.md:96` names —
  `fts_optimized_at`, `orphans_removed_last_run`, `last_retry_at`, `last_error`,
  `meta_chunk_count`, `expired_chunks_removed_last_run`, and `communities_invalidated`.
  The doc writes that list about `GET /status`; `:100` binds the CLI to it ("`archon-search
  maintenance status` renders the same data").

### Steps to reproduce
1. `archon-search maintenance status --json`

### Evidence
```
E   AssertionError: collection_health[0] missing documented fields ['expired_chunks_removed_last_run', 'communities_invalidated']; keys=['centroid_recompute_threshold', 'collection', 'fts_optimized_at', 'last_error', 'last_retry_at', 'meta_chunk_count', 'mutations_since_recompute', 'orphans_removed_last_run']
E   assert not ['expired_chunks_removed_last_run', 'communities_invalidated']
```
