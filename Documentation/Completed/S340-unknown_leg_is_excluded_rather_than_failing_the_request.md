## Bug: `/explain` fan-out reports a failed collection in `excluded_collections` instead of failing the request

**ID**: S340-unknown_leg_is_excluded_rather_than_failing_the_request
**Scenario**: S340
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: POST /explain with collections=['archon_s340_docs', 's340_unknown_collection'] returned 404 and failed the whole request. UserManual/60_searching.md:170 states a collection that fails (not found) is 'reported in the response excluded_collections[] rather than failing the whole request', and UserManual/80_explain_and_debugging.md:168's complete /explain status list (422/400/503/500/504) does not include this status. body={'detail': 'collection not found'}
assert 404 == 200

### What should happen
- Step 2 returns HTTP **`200`** — a fan-out over a valid collection is the supported case (80:38).
- Step 3 returns HTTP **`200`**, not an error status: 60:170 states a not-found collection is reported "rather than failing the whole request". `404` is additionally absent from `/explain`'s documented status-code list (80:168).
- The step-3 response body carries **`excluded_collections`** (80:88) containing **`s340_unknown_collection`** — 60:170 "Collections that fail (not found, metadata error) are reported in the response `excluded_collections[]`".
- The step-3 response still carries a `results` list from the valid leg — the whole request did not fail (60:170).

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
E   AssertionError: POST /explain with collections=['archon_s340_docs', 's340_unknown_collection'] returned 404 and failed the whole request. UserManual/60_searching.md:170 states a collection that fails (not found) is 'reported in the response excluded_collections[] rather than failing the whole request', and UserManual/80_explain_and_debugging.md:168's complete /explain status list (422/400/503/500/504) does not include this status. body={'detail': 'collection not found'}
E   assert 404 == 200
```
