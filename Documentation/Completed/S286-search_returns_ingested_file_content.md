## Bug: Single-file ingest via `archon-search ingest --path <file> --collection <name> --wait`

**ID**: S286-search_returns_ingested_file_content
**Scenario**: S286
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: POST /search returned 0: None
assert 0 == 200

### What should happen
- Step 1: exits `0` (docs: `--wait` polls `GET /jobs/{id}` until terminal and exits `1` only on a non-DONE terminal state).
- Step 2: HTTP `200`; `results` array is non-empty; each result includes `doc_id`, `text`, `score`, and `collection` (docs: SearchResponse result fields). The returned `collection` equals `<name>`.

### Steps to reproduce
1. `archon-search ingest --path <file> --collection <name> --wait`
2. `POST /search {"collection": "<name>", "query": "<content phrase>"}`

### Evidence
```
E   AssertionError: POST /search returned 0: None
E   assert 0 == 200
```
