## Bug: `collection migrate --apply` applies migrations; pending list empties

**ID**: S377-collections_schema_behind_is_zero
**Scenario**: S377
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
assert 1 == 0

### What should happen
- Step 3: HTTP **200** with a `pending` list (:102-104, `130_ttl_and_scoping.md:26-27`). This
  is the branch selector, not the subject: it decides which of :107/:108 step 4 must satisfy.
- Step 4: exit code **0**. `--apply` is the documented mutating form (:106-109,
  `50_ingestion_and_collections.md:156`); it proxies `POST /collections/<c>/migrate` (:110),
  whose documented outcomes are `200` (in-place, synchronous) and `202` (rewrite job) — both
  successes. The only documented failures are `422` (rewrite without `--backup-first`, :129;
  `export_rebuild`, :130) and `409` (an active `ReindexJob`, :132), and the preconditions
  exclude all three, so a non-zero exit is undocumented.
- Step 4: no Python traceback in the output.
- Step 5: HTTP **200** and `pending` is an **empty list** — :81's post-condition, restated
  per collection: after running pending migrations nothing is outstanding. This is the row's
  "pending list empties" claim, asserted as the documented end state.
- Step 6: `collections_schema_behind` is present and equals **0** (:81) after step 4.
- **Reopening gate**: step 3's `pending` list is asserted to be **empty**. On this build it
  always is, and that is precisely why the non-zero → 0 transition cannot be watched. The day
  a build ships a collection with a genuinely pending migration, this assertion flips red and
  S377 must be rewritten to assert the transition directly (non-empty at step 3, empty at
  step 5, `collections_schema_behind` decreasing). **No bug is filed either way**: an
  unreachable precondition is a test-environment limit, not an application defect.

### Steps to reproduce
1. `mkdir -p /tmp/archon_s377_docs && printf '# S377\nThe quick brown fox jumps over the lazy dog.\n' > /tmp/archon_s377_docs/doc.md`
2. `curl -s -X POST -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H 'Content-Type: application/json' -d '{"path":"/tmp/archon_s377_docs"}' http://127.0.0.1:8765/collections/` — poll `GET /jobs/{job_id}` until `DONE`.
3. `curl -s -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" http://127.0.0.1:8765/collections/archon_s377_docs/migrations/pending`
4. `archon-search collection migrate archon_s377_docs --apply; echo "exit=$?"`
5. `curl -s -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" http://127.0.0.1:8765/collections/archon_s377_docs/migrations/pending`
6. `curl -s -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" http://127.0.0.1:8765/status | python3 -m json.tool | grep collections_schema_behind`
7. `archon-search collection remove archon_s377_docs`

### Evidence
```
ve_count': 0}]}, 'store_schema_version': 1, 'collections_schema_behind': 1, 'maintenance': {'enabled': False, 'interval_hours': 0, 'last_run_at': '2026-08-06T19:45:37.361490+00:00', 'next_run_at': None, 'collection_health': [{'collection': 'archon_multitype', 'fts_optimized_at': '2026-08-06T19:45:37.468440+00:00', 'orphans_removed_last_run': 0, 'last_retry_at': None, 'last_error': None, 'mutations_since_recompute': 8, 'centroid_recompute_threshold': 10000, 'meta_chunk_count': 4, 'expired_chunks_removed_last_run': 0, 'communities_invalidated': False}, {'collection': 'archon_test_docs', 'fts_optimized_at': '2026-08-06T19:45:37.517786+00:00', 'orphans_removed_last_run': 0, 'last_retry_at': None, 'last_error': None, 'mutations_since_recompute': 12, 'centroid_recompute_threshold': 10000, 'meta_chunk_count': 3, 'expired_chunks_removed_last_run': 0, 'communities_invalidated': False}, {'collection': 'rest-docs', 'fts_optimized_at': '2026-08-06T19:45:37.547325+00:00', 'orphans_removed_last_run': 0, 'last_retry_at': None, 'last_error': None, 'mutations_since_recompute': 3, 'centroid_recompute_threshold': 10000, 'meta_chunk_count': 3, 'expired_chunks_removed_last_run': 0, 'communities_invalidated': False}, {'collection': 's051_cli_col', 'fts_optimized_at': '2026-08-06T19:45:37.573073+00:00', 'orphans_removed_last_run': 0, 'last_retry_at': None, 'last_error': None, 'mutations_since_recompute': 0, 'centroid_recompute_threshold': 10000, 'meta_chunk_count': 1, 'expired_chunks_removed_last_run': 0, 'communities_invalidated': False}, {'collection': 's051_http_col', 'fts_optimized_at': '2026-08-06T19:45:37.598731+00:00', 'orphans_removed_last_run': 0, 'last_retry_at': None, 'last_error': None, 'mutations_since_recompute': 1, 'centroid_recompute_threshold': 10000, 'meta_chunk_count': 1, 'expired_chunks_removed_last_run': 0, 'communities_invalidated': False}, {'collection': 's052_col', 'fts_optimized_at': '2026-08-06T19:45:37.618412+00:00', 'orphans_removed_last_run': 0, 'last_retry_at': None, 'last_error': None, 'mutations_since_recompute': 0, 'centroid_recompute_threshold': 10000, 'meta_chunk_count': 1, 'expired_chunks_removed_last_run': 0, 'communities_invalidated': False}, {'collection': 's054_col', 'fts_optimized_at': '2026-08-06T19:45:37.636322+00:00', 'orphans_removed_last_run': 0, 'last_retry_at': None, 'last_error': None, 'mutations_since_recompute': 0, 'centroid_recompute_threshold': 10000, 'meta_chunk_count': 1, 'expired_chunks_removed_last_run': 0, 'communities_invalidated': False}, {'collection': 's092_col', 'fts_optimized_at': '2026-08-06T19:45:37.650417+00:00', 'orphans_removed_last_run': 0, 'last_retry_at': None, 'last_error': None, 'mutations_since_recompute': 0, 'centroid_recompute_threshold': 10000, 'meta_chunk_count': 1, 'expired_chunks_removed_last_run': 0, 'communities_invalidated': False}, {'collection': 's093_col', 'fts_optimized_at': '2026-08-06T19:45:37.664385+00:00', 'orphans_removed_last_run': 0, 'last_retry_at': None, 'last_error': None, 'mutations_since_recompute': 1, 'centroid_recompute_threshold': 10000, 'meta_chunk_count': 1, 'expired_chunks_removed_last_run': 0, 'communities_invalidated': False}, {'collection': 'ttl_test_docs', 'fts_optimized_at': '2026-08-06T19:45:37.696514+00:00', 'orphans_removed_last_run': 0, 'last_retry_at': None, 'last_error': None, 'mutations_since_recompute': 4, 'centroid_recompute_threshold': 10000, 'meta_chunk_count': 2, 'expired_chunks_removed_last_run': 0, 'communities_invalidated': False}], 'expired_chunk_count': 0, 'last_expired_pruned_at': '2026-08-06T19:45:37.361490+00:00', 'last_graph_gc_at': None}, 'model_validation': {'embedder_ok': True, 'reranker_ok': True, 'provider_warnings': [], 'validated_at': '2026-08-06T19:58:06.623236Z'}, 'telemetry': None, 'mcp': {'enabled': True, 'bindAddress': '127.0.0.1:8765/mcp'}, 'search': {'max_fanout': 8, 'top_k_max': 100}, 'hyde': None, 'rag_fusion': None, 'failed_expired_ingest_count': 0, 'graph': None, 'code_parsers': None}
E   assert 1 == 0
```
