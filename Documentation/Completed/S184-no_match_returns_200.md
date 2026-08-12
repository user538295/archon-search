## Bug: `POST /search` succeeds (200, not 5xx) for a no-match query

**ID**: S184-no_match_returns_200
**Scenario**: S184
**Severity**: medium
**Version**: archon-search, version 26.8.1956

### What happened
AssertionError: POST /search returned 504 (not 200); body={'detail': 'Search timed out'}
assert 504 == 200

### What should happen
- HTTP 200 (not 500, 503, or 504) — the pipeline succeeded.
- Response JSON has a `results` key whose value is a JSON array.

### Steps to reproduce
1. `curl -s -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection":"archon_test_docs","query":"xyzzy_no_such_term_9999"}'`

### Evidence
```
E   AssertionError: POST /search returned 504 (not 200); body={'detail': 'Search timed out'}
E   assert 504 == 200
```
