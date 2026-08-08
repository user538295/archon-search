## Bug: `/explain` `stage_timings_ms` carries the documented per-stage keys

**ID**: S345-documented_query_stage_keys_present
**Scenario**: S345
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: stage_timings_ms is missing documented stage(s) ['rerank'] (20_monitoring_and_alerts.md:82); present keys=['embed', 'fts', 'fuse', 'total', 'vector']
assert not ['rerank']

### What should happen
- HTTP 200.
- `stage_timings_ms` is a JSON object (`20_monitoring_and_alerts.md:82`, `80_explain_and_debugging.md:88`).
- With `rerank: true`, the object carries every stage name line 82 lists for the query path:
  `embed`, `vector`, `fts`, `fuse`, `rerank`, `total`.
- Every one of those six values is a number (int or float) and is `>= 0` — a wall time cannot be
  negative. No stricter type is asserted: the docs do not state one.

### Steps to reproduce
1. ```bash
   curl -sS -X POST http://127.0.0.1:8765/explain \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"query": "programming language", "collection": "archon_test_docs", "top_k": 3, "rerank": true}' \
     | jq .stage_timings_ms
   ```

### Evidence
```
E   AssertionError: stage_timings_ms is missing documented stage(s) ['rerank'] (20_monitoring_and_alerts.md:82); present keys=['embed', 'fts', 'fuse', 'total', 'vector']
E   assert not ['rerank']
```
