## Bug: Code-aware chunking: `.py` ingest yields symbol metadata or a warning

**ID**: S274-code_metadata_or_warning
**Scenario**: S274
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: POST /search returned 0, body: None
assert 0 == 200

### What should happen
- The ingest job completes without error (job body is not null).
- Either:
  - (a) `code` extra **installed**: at least one search result has `_symbol_type` in its `metadata` dict; OR
  - (b) `code` extra **not installed**: the ingest job body contains a non-empty `warnings` list with a per-file note about the missing extra.
- Both outcomes are correct; the test passes on either branch.

### Steps to reproduce
1. Start an isolated server instance.
2. Ingest a small Python source file (`example.py`) into a new collection `code_test`.
3. Wait for the ingest job to complete.
4. ```bash
   curl -s -X POST <base_url>/search \
     -H "Authorization: Bearer <api_key>" \
     -H "Content-Type: application/json" \
     -d '{"collection":"code_test","query":"greet","include_metadata":true}'
   ```

### Evidence
```
E   AssertionError: POST /search returned 0, body: None
E   assert 0 == 200
```
