## Bug: `/status` monitoring contract

**ID**: 202607280949-S42-doc_count_chunk_count_path_hardcoded
**Scenario**: S42
**Severity**: medium
**Version**: archon-search, version 26.7.1708

### What happened
AssertionError: OperatorGuide/02 says /status hard-codes path/doc_count/chunk_count to ""/0/0, but the server returns real values: 'archon_test_docs'.doc_count=3 (doc says 0); 'archon_test_docs'.chunk_count=3 (doc says 0); 'repro_docs'.doc_count=3 (doc says 0); 'repro_docs'.chunk_count=3 (doc says 0); 'single-docs'.doc_count=1 (doc says 0); 'single-docs'.chunk_count=1 (doc says 0)
assert not ["'archon_test_docs'.doc_count=3 (doc says 0)", "'archon_test_docs'.chunk_count=3 (doc says 0)", "'repro_docs'.doc_count=3 (doc says 0)", "'repro_docs'.chunk_count=3 (doc says 0)", "'single-docs'.doc_count=1 (doc says 0)", "'single-docs'.chunk_count=1 (doc says 0)"]

### What should happen
- HTTP 200; JSON body.
- Top-level `running` is truthy, `pid` is an integer, `version` is a non-empty string.
- `collections` is a list; each entry has `name`, `status`, `processed_files`, `total_files`.
- Per the doc, the handler hard-codes `path`/`doc_count`/`chunk_count` to `""`/`0`/`0`
  — if present, they equal those values. A non-empty `path` or non-zero count is a
  documentation-vs-behavior discrepancy to report.

### Steps to reproduce
1. `curl -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" http://127.0.0.1:8765/status`

### Evidence
```
E   AssertionError: OperatorGuide/02 says /status hard-codes path/doc_count/chunk_count to ""/0/0, but the server returns real values: 'archon_test_docs'.doc_count=3 (doc says 0); 'archon_test_docs'.chunk_count=3 (doc says 0); 'repro_docs'.doc_count=3 (doc says 0); 'repro_docs'.chunk_count=3 (doc says 0); 'single-docs'.doc_count=1 (doc says 0); 'single-docs'.chunk_count=1 (doc says 0)
E   assert not ["'archon_test_docs'.doc_count=3 (doc says 0)", "'archon_test_docs'.chunk_count=3 (doc says 0)", "'repro_docs'.doc_count=3 (doc says 0)", "'repro_docs'.chunk_count=3 (doc says 0)", "'single-docs'.doc_count=1 (doc says 0)", "'single-docs'.chunk_count=1 (doc says 0)"]
```
