## Bug: `/explain` `503` when the reranker is not ready (status mapping UNDOCUMENTED — the docs say the opposite)

**ID**: S343-reranker_503_mapping_is_undocumented
**Scenario**: S343
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: the docs now link the reranker to a 503 — re-implement S343 against the documented status and trigger instead of this positive-path proxy:
docs/OperatorGuide/80_capacity_and_performance.md:123: - `eager_load_embedders` (false). When true, ONNX weights are reconstructed at startup — for the embedder cache *and* the reranker cross-encoder — removing the 
assert not ['docs/OperatorGuide/80_capacity_and_performance.md:123: - `eager_load_embedders` (false). When true, ONNX weights are reconstructed at startup — for the embedder cache *and* the reranker cross-encoder — removing the ']

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
E   AssertionError: the docs now link the reranker to a 503 — re-implement S343 against the documented status and trigger instead of this positive-path proxy:
E     docs/OperatorGuide/80_capacity_and_performance.md:123: - `eager_load_embedders` (false). When true, ONNX weights are reconstructed at startup — for the embedder cache *and* the reranker cross-encoder — removing the 
E   assert not ['docs/OperatorGuide/80_capacity_and_performance.md:123: - `eager_load_embedders` (false). When true, ONNX weights are reconstructed at startup — for the embedder cache *and* the reranker cross-encoder — removing the ']
```
