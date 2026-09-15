## Bug: `indexed_after` / `indexed_before` accept a full RFC 3339 datetime string

**ID**: S305-indexed_after_rfc3339_past_returns_all
**Scenario**: S305
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: unfiltered POST /search returned 0: None
assert 0 == 200

### What should happen
- Step 2: HTTP 200; `results` non-empty (baseline).
- Step 3: HTTP 200 — a full RFC 3339 datetime string is accepted (not `422`) for `indexed_after`; `results` count equals the baseline (all chunks were indexed after 2020, so "after this timestamp" passes them all).
- Step 4: HTTP 200 with `results == []` — no chunk was indexed after 2099 (proves the datetime was parsed and applied, not silently ignored).
- Step 5: HTTP 200 — accepted for `indexed_before`; `results` count equals the baseline (all chunks indexed before 2099).
- Step 6: HTTP 200 with `results == []` — no chunk was indexed before 2020.
- Step 7: `indexed_after = T − 1s` → HTTP 200, `results` non-empty (chunk is after that instant); `indexed_after = T + 1s` → HTTP 200, `results == []` (chunk is before that instant). This second-level boundary is expressible only by a full datetime, so it confirms the time-of-day component of the RFC 3339 string is honored — not just the date.

Note: the docs specify coercion only for *date-only* strings ("start-of-day / end-of-day UTC"); the exact stored/echoed form of a full RFC 3339 datetime is not documented, so this scenario asserts only the documented acceptance (HTTP 200) and after/before-this-timestamp filter semantics — not any `applied_filters` echo value. The filename retains the "date_coercion" token from the coverage-gap bullet it derives from; the behavior under test is full-datetime acceptance, which is *not* coerced.

### Steps to reproduce
1. Ingest `alpha.md`, `beta.md` (both mention "fox") into a collection.
2. `POST /search {"collection": "...", "query": "fox"}` (no filter) — record the baseline result count and one result's `indexed_at`.
3. `POST /search {"collection": "...", "query": "fox", "filters": {"indexed_after": "2020-01-01T00:00:00Z"}}`
4. `POST /search {"collection": "...", "query": "fox", "filters": {"indexed_after": "2099-01-01T00:00:00Z"}}`
5. `POST /search {"collection": "...", "query": "fox", "filters": {"indexed_before": "2099-01-01T00:00:00Z"}}`
6. `POST /search {"collection": "...", "query": "fox", "filters": {"indexed_before": "2020-01-01T00:00:00Z"}}`
7. Using the recorded `indexed_at` T: `POST /search` with `indexed_after` set to a full datetime one second BEFORE T, then again one second AFTER T.

### Evidence
```
E   AssertionError: unfiltered POST /search returned 0: None
E   assert 0 == 200
```
