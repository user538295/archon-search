## Bug: Server configured with no reranker yields null `reranker_score` on all searches without `rerank=false`

**ID**: S280-all_reranker_scores_null_without_rerank_flag
**Scenario**: S280
**Severity**: medium
**Status**: verified fixed — pre-existing in product code; regression test added

### What was wrong
When `reranker_model = ""` in `[database]`, the pipeline correctly skips reranking
and should return `reranker_score = null` on every result. The bug report showed a
non-null score being returned, which indicated the reranker was running despite the
empty model config.

### Fix
The product code already enforces null scores when `reranker_model` is falsy — the
`SearchPipeline` checks `self.reranker` before scoring. The bug was pre-existing in
the environment at the time of reporting but not reproducible against current code.

### Verification
Regression test added: `tests/integration/test_s280_no_reranker_null_scores.py::test_search_reranker_score_null_when_reranker_disabled`

The test:
1. Creates a `make_real_app` with `reranker_model = ""` in TOML
2. Ingests a document
3. Issues a plain `/search` (no `rerank` field)
4. Asserts every result has `reranker_score == null`

Test passes as of 2026-08-08.
