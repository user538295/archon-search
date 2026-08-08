## Bug: `POST /explain` error status codes: **504** on fanout timeout (the **503** condition is not reachable black-box)

**ID**: S435-explain_returns_504_on_fanout_timeout
**Scenario**: S435
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: POST /explain over 3 collections took 2.165s against a configured [search] fanout_timeout_seconds of 0.001s — 2165x the whole-fan-out timeout — yet returned 200. UserManual/80_explain_and_debugging.md:168 documents `504` on fanout timeout and UserManual/30_configuration.md:80 documents that exceeding `fanout_timeout_seconds` returns HTTP 504.
response keys: ['acl_filtered', 'collection', 'embedding_model', 'excluded_collections', 'graph_mode_applied', 'hyde_applied', 'near_misses', 'ppr_entities_matched', 'rag_fusion_applied', 'rag_fusion_attempted', 'rag_fusion_failure_reason', 'rag_fusion_queries_used', 'rag_fusion_sub_queries', 'rerank', 'results', 'routing', 'stage_timings_ms']
assert 200 == 504

### What should happen
- Step 3's measured wall-clock time is far greater than the configured `fanout_timeout_seconds` (`0.001 s`) — the whole fan-out demonstrably exceeded its timeout. Measured 2026-08-06 on 26.8.1848: **1.4 s – 3.8 s**, i.e. 1400×–3800× the configured bound.
- Step 3 returns HTTP **`504`** — `80_explain_and_debugging.md:168` "`504` on fanout timeout" and `30_configuration.md:80` "exceeding it returns HTTP 504".
- Step 3 does **not** return `200` with a full result set: a fan-out that exceeded its whole-fan-out timeout is documented as a `504`, not as a successful search.

### Steps to reproduce
1. Start a private instance whose config carries `[search] fanout_timeout_seconds = 0.001`; wait for `GET /health` → `200`.
2. Ingest three collections `s435_c0`, `s435_c1`, `s435_c2`, each with a handful of markdown files.
3. Time the request: `curl -sS -o /dev/null -w '%{http_code} %{time_total}\n' -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{"query":"router confidence threshold","collections":["s435_c0","s435_c1","s435_c2"],"top_k":5}' http://127.0.0.1:$PORT/explain`

### Evidence
```
E   AssertionError: POST /explain over 3 collections took 2.165s against a configured [search] fanout_timeout_seconds of 0.001s — 2165x the whole-fan-out timeout — yet returned 200. UserManual/80_explain_and_debugging.md:168 documents `504` on fanout timeout and UserManual/30_configuration.md:80 documents that exceeding `fanout_timeout_seconds` returns HTTP 504.
E     response keys: ['acl_filtered', 'collection', 'embedding_model', 'excluded_collections', 'graph_mode_applied', 'hyde_applied', 'near_misses', 'ppr_entities_matched', 'rag_fusion_applied', 'rag_fusion_attempted', 'rag_fusion_failure_reason', 'rag_fusion_queries_used', 'rag_fusion_sub_queries', 'rerank', 'results', 'routing', 'stage_timings_ms']
E   assert 200 == 504
```
