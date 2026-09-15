## Bug: source_path_prefix filter: results restricted to matching paths

**ID**: S299-results_restricted_to_prefix
**Scenario**: S299
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: unfiltered POST /search returned 0: None
assert 0 == 200

### What should happen
- Step 2: HTTP 200. The unfiltered top-K should include at least one `guide_` and one `manual_` source_path so the prefix genuinely excludes some docs. This is a corpus/ranking precondition (the returned count is `[database].top_k_return`, server-configured); if a whole group is absent from the top-K the scenario is skipped, not failed.
- Step 3: HTTP 200; `results` is non-empty; **every** result's `source_path` starts with `<prefix>`.
- Step 3: `applied_filters` is non-null and echoes `source_path_prefix` == `<prefix>` (REST echoes the parsed filters).

### Steps to reproduce
1. Ingest `guide_alpha.md`, `guide_beta.md`, `manual_gamma.md`, `manual_delta.md` (all mention "widget") into a collection.
2. `POST /search {"collection": "...", "query": "widget"}` (no filter) — confirm results include both a `guide_` and a `manual_` path; derive `<prefix>` = the directory of a `guide_` result + `guide`.
3. `POST /search {"collection": "...", "query": "widget", "filters": {"source_path_prefix": "<prefix>"}}`

### Evidence
```
E   AssertionError: unfiltered POST /search returned 0: None
E   assert 0 == 200
```
