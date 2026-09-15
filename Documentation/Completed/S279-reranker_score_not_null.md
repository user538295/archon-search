## Bug: Max profile server starts, search works, and chunk_size=1024 is configured

**ID**: S279-reranker_score_not_null
**Scenario**: S279
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: status=0 body=None
assert (0 == 200)

### What should happen
- GET /health → HTTP 200.
- GET /ready → HTTP 200.
- POST /search → HTTP 200 with a non-empty `results` array.
- Every object in `results` has a `reranker_score` field that is not null (proves `BAAI/bge-reranker-base` ran).
- The server config file (`archon-search.toml`) contains `chunk_size = 1024` or `chunk_size=1024` (the max profile's documented chunk size).

### Steps to reproduce
1. ```bash
   curl -s $MAX_BASE_URL/health
   ```
2. ```bash
   curl -s $MAX_BASE_URL/ready
   ```
3. ```bash
   curl -s -X POST $MAX_BASE_URL/search \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"collection":"max_test","query":"fox"}'
   ```
4. ```bash
   grep chunk_size ~/.archon-search/archon-search.toml
   ```

### Evidence
```
E   AssertionError: status=0 body=None
E   assert (0 == 200)
```
