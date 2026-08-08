## Bug: `/explain` `503` when the reranker is not ready (status mapping UNDOCUMENTED — the docs say the opposite)

**ID**: S343-results_carry_a_non_null_reranker_score
**Scenario**: S343
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: 1 of 1 results carry a null `reranker_score` despite rerank=true; UserManual/80_explain_and_debugging.md:101 documents null only when `rerank=false`. first offender={'doc_id': '[REDACTED]', 'chunk_id': '[REDACTED]-000000', 'source_path': '/private/tmp/archon_s343_docs/doc.md', 'text': '# S343
assert not [{'acl': None, 'acl_gate': {'allowed_principals': None, 'sidecar_path': None, 'source': 'collection_default', 'warning...ranker_score': None, ...}, 'chunk_id': '[REDACTED]-000000', ...}]

### What should happen
- **The bullet describes UNDOCUMENTED behavior.** No shipped document maps reranker readiness to a `503` on `/explain`; the documented mapping is `500` for a reranker exception (60:52; 90_incident_runbook.md:114) and `503` for meta-lookup/router failures (80:168; 60:51). Asserting the bullet's `503` would rest on invented behavior, which the project's Hard Rules forbid — so it is not asserted.
- **Documented, exercisable assertion:** `GET /ready` returns HTTP **`200`** — model warmth does not gate readiness (OperatorGuide/20_monitoring_and_alerts.md:49, :51, :53). This is the documented reason no "reranker not ready" gate exists to produce a `503`.
- **Documented, exercisable assertion (the stand-in for the bullet):** `POST /explain` with `rerank=true` returns HTTP **`200`** — **not** `503` — on the running server, and every returned result's `breakdown` carries a **non-null `reranker_score`** (80:101 — "The cross-encoder second-stage score; `null` when `rerank=false`", so a non-null value proves the cross-encoder ran). A reranker that runs and answers is precisely what the bullet's `503` would contradict.
- **Doc-gap reopening gate:** no line under `./docs/` links `rerank`/`reranker` to a `503`. If a future document introduces that mapping, the paired test flips **red** so S343 is re-implemented against the then-documented status and trigger rather than this positive-path proxy.
- No bug is filed — a missing specification is a documentation gap, not an application defect.

### Steps to reproduce
1. Create and register `/tmp/archon_s343_docs` with one small document; wait for the ingest job to reach `DONE`. Note the server-derived name (`$C`).
2. `curl -s -w '\n%{http_code}\n' http://127.0.0.1:8765/ready`
3. ```bash
   curl -sS -w '\n%{http_code}\n' -X POST http://127.0.0.1:8765/explain \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H 'Content-Type: application/json' \
     -d "{\"query\":\"fox\",\"collection\":\"$C\",\"top_k\":5,\"rerank\":true}" | python3 -m json.tool
   ```
4. Cross-reference every `rerank`-mentioning line under `./docs/` against `503`.

### Evidence
```
E   AssertionError: 1 of 1 results carry a null `reranker_score` despite rerank=true; UserManual/80_explain_and_debugging.md:101 documents null only when `rerank=false`. first offender={'doc_id': '[REDACTED]', 'chunk_id': '[REDACTED]-000000', 'source_path': '/private/tmp/archon_s343_docs/doc.md', 'text': '# S343
The quick brown fox jumps over the lazy dog.
', 'score': 0.03278688524590164, 'breakdown': {'vector_rank': 0, 'vector_score': 0.6314820051193237, 'vector_score_kind': 'distance', 'fts_rank': 0, 'fts_score': 0.28768211603164673, 'fts_score_kind': 'bm25', 'rrf_score': 0.03278688524590164, 'reranker_score': None}, 'file_type': 'md', 'indexed_at': '2026-08-06T20:55:30.097935Z', 'updated_at': '2026-08-06T20:55:29.848490+00:00', 'ingested_by': 'http', 'language': '', 'metadata': {'_heading': 'S343', '_section_path': 'S343'}, 'acl': None, 'collection': 'archon_s343_docs', 'graph_provenance': None, 'acl_gate': {'allowed_principals': None, 'source': 'collection_default', 'sidecar_path': None, 'warnings': []}}
E   assert not [{'acl': None, 'acl_gate': {'allowed_principals': None, 'sidecar_path': None, 'source': 'collection_default', 'warning...ranker_score': None, ...}, 'chunk_id': '[REDACTED]-000000', ...}]
```
