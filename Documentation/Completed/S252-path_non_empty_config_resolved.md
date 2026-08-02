## Bug: `/status` extended per-collection fields: `path`, `doc_count`, `chunk_count`, `error`, `error_count`

**ID**: S252-path_non_empty_config_resolved
**Scenario**: S252
**Severity**: medium
**Version**: archon-search, version 26.8.1751

### What happened
AssertionError: path empty (regression per doc): entry={'name': 'archon_test_docs', 'path': '', 'doc_count': 3, 'chunk_count': 3, 'status': 'not_yet_indexed', 'watching': False, 'eta_seconds': None, 'processed_files': 0, 'total_files': 0, 'error': None, 'error_count': 0, 'needs_reindex': False, 'warning': None, 'community_count': 0, 'last_built_at': None}
assert (True and '')

### What should happen
- HTTP 200; `collections` is a list with at least one entry for `archon_test_docs`.
- For the `archon_test_docs` collection:
  - `path` is a non-empty **absolute** path. Per `OperatorGuide/20_monitoring_and_alerts.md:91` the value is "the config-resolved absolute storage path (e.g. `~/.archon-search/collections/my-docs`), or `""` when the collection has no configured path". The `~/.archon-search/collections/...` value is an **example**, not a required prefix — a collection registered from `/tmp/archon-test-docs` correctly reports that path. The hardcoded-placeholder bug was fixed in feature `2026-07-15-100`, so an empty string `""` here (for a collection that *has* a configured path) is a regression to report.
  - `doc_count` is a non-negative integer (cached, may lag a live recount — but should be > 0 after ingest).
  - `chunk_count` is a non-negative integer (live count; > 0 after ingest; a value of 0 paired with non-zero `doc_count` may indicate a count failure logged as a warning, not a 500).
  - `error` is either `null` or a string (last error message). A non-null `error` does not necessarily mean the collection is unusable per the doc.
  - `error_count` is a non-negative integer (lifetime count for this collection).
  - `eta_seconds` is either `null` (not enough samples or already complete) or a numeric value during active ingest.

### Steps to reproduce
1. `curl -s -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" http://127.0.0.1:8765/status`

### Evidence
```
E   AssertionError: path empty (regression per doc): entry={'name': 'archon_test_docs', 'path': '', 'doc_count': 3, 'chunk_count': 3, 'status': 'not_yet_indexed', 'watching': False, 'eta_seconds': None, 'processed_files': 0, 'total_files': 0, 'error': None, 'error_count': 0, 'needs_reindex': False, 'warning': None, 'community_count': 0, 'last_built_at': None}
E   assert (True and '')
E    +  where True = isinstance('', str)
```
