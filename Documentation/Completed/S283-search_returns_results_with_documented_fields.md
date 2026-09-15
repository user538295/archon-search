## Bug: collection add --wait completes, collection info shows a doc, search returns results

**ID**: S283-search_returns_results_with_documented_fields
**Scenario**: S283
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: POST /search returned 0: None
assert 0 == 200

### What should happen
- Step 1: exits `0`; stdout contains `Collection '<name>' ingested successfully.` (docs: `collection add` `--wait` prints this on DONE, exits `1` on FAILED).
- Step 2: the added collection appears with `docs=<n>` where `n >= 1` (docs: `collection list` prints `<name>  docs=<n>  chunks=<n>`).
- Step 3: exits `0` for the known collection (docs: `collection info` "exits `1` if unknown").
- Step 4: HTTP `200` (docs: `200` on success — an empty `results` is a valid 200 meaning "matched nothing", 60:48). For content just ingested, `results` is expected non-empty; each result carries all eight documented fields `doc_id`, `chunk_id`, `text`, `score`, `source_path`, `file_type`, `language`, `collection` (docs: `SearchResponse` result fields, 60:44) and `collection` equals the queried name.

### Steps to reproduce
1. `archon-search collection add <tmpdir> --wait`
2. `archon-search collection list`
3. `archon-search collection info <derived-name>`
4. `POST /search {"collection": "<derived-name>", "query": "..."}`

### Evidence
```
E   AssertionError: POST /search returned 0: None
E   assert 0 == 200
```
