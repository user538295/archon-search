## Bug: `applied_filters` on a multi-collection fan-out response (single shared echo, not per-leg)

**ID**: S308-fanout_with_filter_returns_200
**Scenario**: S308
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: multi-collection filtered POST /search returned 0: None
assert 0 == 200

### What should happen
- HTTP `200` (multi-collection fan-out with a valid filter succeeds; line 44 + lines 157-172).
- The response genuinely fanned out over both legs, not degenerating to one: `excluded_collections` is empty. Per line 170 a collection that fails (not found, metadata error) is reported in `excluded_collections[]` rather than failing the request, so an empty list is the ranking-independent proof both `s308_alpha` and `s308_beta` were live fan-out legs (line 44 lists `excluded_collections`; line 170 defines it). `results` is also non-empty (the `.md` filter admits every seeded doc and "widget" matches them). Without this the four `applied_filters` assertions below would still pass on a silently single-leg response — line 144 makes the echo identical on single- and multi-collection responses.
- `applied_filters` is non-`null` and deserialises to a **JSON object** (a Python `dict` / `SearchFilters` mapping) — the documented shape "echoing the parsed, normalised `SearchFilters`" (line 140), present on multi-collection responses (line 144). A scalar, string, array, or `null` fails.
- `applied_filters["file_type"] == "md"` — the submitted `".md"` echoed back **normalised** (leading dot stripped, lowercased; lines 128 + 143). This is a single flat echo of the submitted `SearchFilters`, identical in shape to a single-collection response.
- The object is **not** keyed per collection: neither `"s308_alpha"` nor `"s308_beta"` appears as a top-level key of `applied_filters`. The documented echo carries `SearchFilters` field names (`file_type`, …), not collection names — so a per-collection / per-leg mapping would contradict lines 140/144.
- **Undocumented (no assertion made)**: the docs describe `applied_filters` as one shared echo object and never document a per-collection ("per leg") breakdown of it. The Coverage-Gap bullet's phrase "shape per leg" therefore has no documented basis; this scenario asserts the documented single-shared-echo shape instead and makes no claim about any hypothetical per-leg structure beyond ruling out collection-name keys as above. No assertion is made on whether the other `SearchFilters` fields (`source_path_prefix`, `language`, …) appear as keys when only `file_type` is submitted — their presence/absence is undocumented.

### Steps to reproduce
1. Ingest `.md` documents (all mentioning "widget") into two collections, `s308_alpha` and `s308_beta`.
2. `POST /search {"collections": ["s308_alpha", "s308_beta"], "query": "widget", "filters": {"file_type": ".md"}}`

### Evidence
```
E   AssertionError: multi-collection filtered POST /search returned 0: None
E   assert 0 == 200
```
