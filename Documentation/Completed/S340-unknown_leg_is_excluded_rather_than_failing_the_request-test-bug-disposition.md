## Bug: `/explain` fan-out reports a failed collection in `excluded_collections` instead of failing the request

**ID**: S340-unknown_leg_is_excluded_rather_than_failing_the_request
**Scenario**: S340
**Severity**: test-bug (no product defect)
**Version**: archon-search, version 26.8.1916

### Disposition

**Test bug — not a product bug.** The product behaves correctly. The test assertion was wrong.

The original assertion:
```python
assert 's340_unknown_collection' in excluded
```
checked whether a **string** is present in a **list of dicts**. Since `excluded_collections` items are objects (`{'name': '...', 'reason': '...'}`) rather than bare strings, this `in` check always evaluates to `False` even when the collection is correctly listed. The product returned exactly what was required.

The fix (applied to `tests/test_s340_explain_excluded_collections.py`, line 103):
```python
assert any(e.get("name") == _UNKNOWN for e in excluded)
```

### What happened

The test assertion `assert 's340_unknown_collection' in excluded` failed because `excluded_collections` contains dict objects, not strings. The evidence line makes this explicit:

```
assert 's340_unknown_collection' in [{'name': 's340_unknown_collection', 'reason': 'not_found'}]
```

The collection IS present in `excluded_collections` — it just appears as `{'name': 's340_unknown_collection', 'reason': 'not_found'}`, not as the bare string `'s340_unknown_collection'`.

### What should happen

- The product correctly returns HTTP **`200`** with `excluded_collections` containing `{'name': 's340_unknown_collection', 'reason': 'not_found'}`, satisfying UserManual/60_searching.md:170 ("Collections that fail (not found, metadata error) are reported in the response `excluded_collections[]` rather than failing the whole request").
- The test must check for presence by name: `any(e.get("name") == _UNKNOWN for e in excluded)`.

### Documentation alignment

The docs (60:170, 80:88) state that `excluded_collections[]` is an array of failed fan-out legs, but do not specify item shape. The product returns items as `{name, reason}` objects, which is a superset of the documented guarantee and contains no contradiction. The `reason: "not_found"` field is informational and correct.

### Steps to reproduce

1. Create and register `/tmp/archon_s340_docs` with one small document; wait for the ingest job to reach `DONE`. Note the server-derived name (`$C`).
2. Baseline — a fan-out over the one valid collection:
   ```bash
   curl -sS -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8765/explain \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H 'Content-Type: application/json' \
     -d "{\"query\":\"fox\",\"collections\":[\"$C\"],\"top_k\":5}"
   ```
3. Fan-out with one valid and one unregistered collection:
   ```bash
   curl -sS -w '\n%{http_code}\n' -X POST http://127.0.0.1:8765/explain \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H 'Content-Type: application/json' \
     -d "{\"query\":\"fox\",\"collections\":[\"$C\",\"s340_unknown_collection\"],\"top_k\":5}" \
     | python3 -m json.tool
   ```

### Evidence

```
E   AssertionError: the unregistered collection 's340_unknown_collection' is absent from excluded_collections ([{'name': 's340_unknown_collection', 'reason': 'not_found'}]); UserManual/60_searching.md:170 documents failed fan-out legs as being reported there. body keys=['rerank', 'routing', 'collection', 'acl_filtered', 'results', 'near_misses', 'excluded_collections', 'embedding_model', 'hyde_applied', 'stage_timings_ms', 'rag_fusion_applied', 'rag_fusion_queries_used', 'rag_fusion_attempted', 'rag_fusion_failure_reason', 'rag_fusion_sub_queries', 'graph_mode_applied', 'ppr_entities_matched']
E   assert 's340_unknown_collection' in [{'name': 's340_unknown_collection', 'reason': 'not_found'}]
```

The list already contains the entry. The operator was wrong.
