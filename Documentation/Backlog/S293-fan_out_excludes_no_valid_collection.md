## Bug: Multi-collection fan-out: search across two collections in one call

**ID**: S293-fan_out_excludes_no_valid_collection
**Scenario**: S293
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: POST /search fan-out returned 0: None
assert 0 == 200

### What should happen
- Step 2: HTTP 200; `excluded_collections == []` (both requested collections exist, so none is excluded); `results` non-empty; each result carries the documented fields `doc_id`, `chunk_id`, `text`, `score`, `source_path`, `file_type`, `language`, `collection`; every result's `collection` is one of the requested names (`alpha` or `beta`).
- Step 3: HTTP 422 (both `collection` and `collections` supplied — mutually exclusive).
- Step 4: HTTP 422 (neither supplied).

Note: the docs do not guarantee that a fan-out returns at least one result from *every* requested collection — the pipeline returns `[database].top_k_return` results ranked across the fan-out — so "results from both" is verified via the documented `excluded_collections == []` contract, not by requiring a chunk from each collection.

### Steps to reproduce
1. Ingest collection `alpha` (content about volcanoes) and collection `beta` (content about submarines).
2. `POST /search {"collections": ["alpha", "beta"], "query": "..."}`
3. `POST /search {"collection": "alpha", "collections": ["alpha", "beta"], "query": "..."}`
4. `POST /search {"query": "..."}` (neither `collection` nor `collections`)

### Evidence
```
E   AssertionError: POST /search fan-out returned 0: None
E   assert 0 == 200
```
