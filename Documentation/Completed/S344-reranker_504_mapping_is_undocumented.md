## Bug: `/explain` `504` on reranker timeout (status mapping UNDOCUMENTED)

**ID**: S344-reranker_504_mapping_is_undocumented
**Scenario**: S344
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: the docs now link the reranker to a 504 — re-implement S344 against the documented trigger instead of these positive-path proxies:
docs/OperatorGuide/90_incident_runbook.md:91: - **HTTP 504** — the pipeline call timed out (>30 s). ERROR record with `event_type="search_timeout"`, message `search pipeline timed out`. Telemetry entry: `st
assert not ['docs/OperatorGuide/90_incident_runbook.md:91: - **HTTP 504** — the pipeline call timed out (>30 s). ERROR record with `event_type="search_timeout"`, message `search pipeline timed out`. Telemetry entry: `st']

### What should happen
- **The bullet describes UNDOCUMENTED behavior.** `/explain`'s only documented `504` trigger is a fan-out timeout (80:168); `/search`'s is a whole-pipeline timeout of ~30 s (60:53). Neither is reranker-specific, no reranker deadline is configurable (the only search timeout knob is `[search] fanout_timeout_seconds`, OperatorGuide/80_capacity_and_performance.md:45), and the reranker's own documented failure status is `500` (60:52). Asserting the bullet's reranker-timeout `504` would rest on invented behavior, which the project's Hard Rules forbid — so it is not asserted.
- **Documented, exercisable assertion (the stand-in for the bullet):** step 2 returns HTTP **`200`** — **not** `504`. A single-collection request runs no fan-out, so the one documented `/explain` `504` trigger (80:168) does not apply, and the reranked pipeline completes.
- **Documented, exercisable assertion:** step 3 also returns HTTP **`200`** — **not** `504`. This request does traverse the fan-out path (`collections[]`, 80:38; names de-duplicated, 60:169), so it is bounded by `[search] fanout_timeout_seconds` (default 30.0, OperatorGuide/80_capacity_and_performance.md:45) — and a reranked leg over a one-document collection completes well inside it.
- **Doc-gap reopening gate:** no line under `./docs/` links `rerank`/`reranker` to a `504`, and no `reranker_timeout`-style setting is documented. If a future document introduces either, the paired test flips **red** so S344 is re-implemented against the then-documented trigger and knob rather than these positive-path proxies.
- No bug is filed — a missing specification is a documentation gap, not an application defect.

### Steps to reproduce
1. Create and register `/tmp/archon_s344_docs` with one small document; wait for the ingest job to reach `DONE`. Note the server-derived name (`$C`).
2. Single-collection reranked explain:
   ```bash
   curl -sS -w '\n%{http_code}\n' -X POST http://127.0.0.1:8765/explain \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H 'Content-Type: application/json' \
     -d "{\"query\":\"fox\",\"collection\":\"$C\",\"top_k\":5,\"rerank\":true}"
   ```
3. The same query down the **fan-out** path — the one documented `504` trigger on `/explain`:
   ```bash
   curl -sS -w '\n%{http_code}\n' -X POST http://127.0.0.1:8765/explain \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H 'Content-Type: application/json' \
     -d "{\"query\":\"fox\",\"collections\":[\"$C\"],\"top_k\":5,\"rerank\":true}"
   ```
4. Cross-reference every `rerank`-mentioning line under `./docs/` against `504`, and search for a `reranker_timeout`-style setting.

### Evidence
```
E   AssertionError: the docs now link the reranker to a 504 — re-implement S344 against the documented trigger instead of these positive-path proxies:
E     docs/OperatorGuide/90_incident_runbook.md:91: - **HTTP 504** — the pipeline call timed out (>30 s). ERROR record with `event_type="search_timeout"`, message `search pipeline timed out`. Telemetry entry: `st
E   assert not ['docs/OperatorGuide/90_incident_runbook.md:91: - **HTTP 504** — the pipeline call timed out (>30 s). ERROR record with `event_type="search_timeout"`, message `search pipeline timed out`. Telemetry entry: `st']
```
