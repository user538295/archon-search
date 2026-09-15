## Bug: source_path_glob matching no docs returns 200 with empty results

**ID**: S302-no_matching_glob_returns_200_empty_results
**Scenario**: S302
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: control POST /search returned 0: None
assert 0 == 200

### What should happen
- Step 2 (precondition): HTTP 200 with non-empty `results`. If the bare query matches nothing, the scenario is skipped (an empty filtered result would prove nothing).
- Step 3: HTTP 200 (a `fnmatch` glob that matches no source path is a successful "no match" outcome, not an error — line 48).
- Step 3: `results` is an empty array.
- Step 3: `applied_filters` echoes `source_path_glob` == `/no/such/dir/that/matches/nothing/*`, proving the server parsed and acknowledged the filter rather than silently dropping it (no normalisation is documented for `source_path_glob`, so verbatim echo is expected). That the filter was actually applied to the result set is proven by Step 2 vs Step 3 (non-empty unfiltered → empty filtered), not by the echo alone.

### Steps to reproduce
1. Ingest a document into a collection.
2. Control: `POST /search {"collection": "<name>", "query": "quick brown fox"}` (no filter) — confirm it returns results, so an empty filtered result is attributable to the filter.
3. `POST /search {"collection": "<name>", "query": "quick brown fox", "filters": {"source_path_glob": "/no/such/dir/that/matches/nothing/*"}}`

### Evidence
```
E   AssertionError: control POST /search returned 0: None
E   assert 0 == 200
```
