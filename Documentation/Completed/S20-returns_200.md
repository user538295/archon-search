## Bug: Search filter by `file_type`

**ID**: S20-returns_200
**Scenario**: S20
**Severity**: medium
**Version**: archon-search, version 26.8.1800

### What happened
AssertionError: Filtered search returned 404
assert 404 == 200

### What should happen
- HTTP 200.
- All returned `source_path` values end with `.md`.
- `applied_filters` in response has `file_type: "md"` (leading dot stripped, lowercased).

### Steps to reproduce
```bash
curl -s -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection":"archon_test_docs","query":"fox","filters":{"file_type":"md"}}'
```

### Evidence
```
E   AssertionError: Filtered search returned 404
E   assert 404 == 200
```

---

### Analysis — Not a product defect (feature-level)

**Verdict:** environmental cascade, not a product defect.

The search returned "collection not found" only because the collection was never created in this run — the earlier add step (S07) was rejected due to leftover state from a previous run, so there was nothing to search. This is a knock-on effect of that environmental condition, not a search defect.

**Verified:** starting from a clean setup, the collection is created and this search returns the expected results (including a result referencing beta.md, with all expected fields).

**Recommendation:** run each smoke scenario against a fresh, empty data directory so the collection exists before search runs.
