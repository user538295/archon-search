## Bug: Collection add --wait: full ingest and search round-trip

**ID**: S281-search_returns_results_for_ingested_content
**Scenario**: S281
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: POST /search returned 0: None
assert 0 == 200

### What should happen
- Step 1: exits 0; stdout contains `"ingested successfully"`.
- Step 2: `doc_count` ≥ 2; `chunk_count` > 0.
- Step 3: HTTP 200; `results` array is non-empty; each result has `doc_id`, `text`, and `score`.

### Steps to reproduce
1. `archon-search collection add <tmpdir> --wait`
2. `archon-search collection info <derived-name>`
3. `POST /search {"collection": "<derived-name>", "query": "..."}`

### Evidence
```
E   AssertionError: POST /search returned 0: None
E   assert 0 == 200
```

---

### Resolution — fixed

**Verdict:** product defect, fixed.

**Root cause.** An ingest job only ever exercises the *embedder*. The job therefore reached
`JobStatus.DONE` — the signal `collection add --wait` polls for, then prints
`"ingested successfully"` on — while the cross-encoder reranker was still cold. The next
`POST /search` awaited the one-off ONNX build behind `Reranker._warmup_lock`, and because that
warm-up is deliberately taken *outside* `_SEARCH_TIMEOUT_SECONDS` (S184), the request produced no
response at all inside a normal client read budget. The harness recorded that as `status 0`, which
reads like a connection failure rather than the model load it actually was.

**Fix.** `archon_search/server/routes_jobs.py` gained `_INGEST_WARMUP_WAIT_SECONDS = 180.0` and
`_await_models_warm(pipeline, job_id)`, called from both `_default_ingest_task` and
`_default_ingest_task_with_lock` immediately after ingest dispatch and before the job is marked
`DONE`. The wait is best-effort: `warmup_models` swallows model errors, and a timeout only logs a
WARNING and falls back to the old behaviour (the first search pays the build cost). The content is
ingested either way, so a slow or wedged model build can never fail an ingest job.

**Regression test.**
`tests/integration/test_s281_add_wait_then_search_cold_reranker.py::test_s281_search_answers_after_collection_add_wait_reports_success`.

**Residual gap — not S281.** `routes_search.py` still awaits `pipeline.warmup_models(embedder)`
outside the route's own `_SEARCH_TIMEOUT_SECONDS`, so searching a *pre-existing* collection right
after a server restart — no fresh ingest, no `/ready` probe — can still hang past a client timeout.
That is the shared root cause behind the still-open S278, S283, S286, S288, S299 and S302, and is
tracked there rather than here.
