## Bug: Balanced profile server starts and search returns results with reranker scores

**ID**: S278-search_returns_results
**Scenario**: S278
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: POST /search returned 0, body: None
assert 0 == 200

### What should happen
- GET /health → HTTP 200.
- GET /ready → HTTP 200.
- POST /search → HTTP 200 with a non-empty `results` array.
- Every object in `results` has a `reranker_score` field that is not null (proves `Xenova/ms-marco-MiniLM-L-12-v2` ran).

### Steps to reproduce
1. ```bash
   curl -s $BALANCED_BASE_URL/health
   ```
2. ```bash
   curl -s $BALANCED_BASE_URL/ready
   ```
3. ```bash
   curl -s -X POST $BALANCED_BASE_URL/search \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"collection":"balanced_test","query":"fox"}'
   ```

### Evidence
```
E   AssertionError: POST /search returned 0, body: None
E   assert 0 == 200
```
