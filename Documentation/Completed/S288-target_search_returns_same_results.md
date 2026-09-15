## Bug: Export a collection, import into a fresh isolated server, search returns same results

**ID**: S288-target_search_returns_same_results
**Scenario**: S288
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: source /search returned 0
assert 0 == 200

### What should happen
- Step 1: exits `0`; an `s288_corpus-*.tar.gz` archive is written under the source server's `<data_dir>/exports` (doc: "Done. Archive: …", lines 99-106).
- Step 3: exits `0`; stdout reports `imported=<n>, skipped=0, total=<n>` with `imported == total` (doc: lines 116-118; REST result `{"imported", "skipped", "total_in_archive"}`, line 151).
- Step 4: both searches return HTTP `200` with a non-empty `results` array; each result carries `doc_id`, `text`, and `score`. The set of result `text` values from the target equals the set from the source — the round-trip preserves the chunks (doc: archive contains "its chunks, vectors, per-chunk metadata", lines 10-11).

### Steps to reproduce
1. `archon-search export s288_corpus --wait --api-url <SRC_URL> --api-key <SRC_KEY>`
2. Copy the produced `<data_dir>/exports/s288_corpus-*.tar.gz` into the target server's data directory.
3. `archon-search import s288_corpus <TARGET_DATA_DIR>/exports/s288_corpus-*.tar.gz --wait --api-url <DST_URL> --api-key <DST_KEY>`
4. `POST /search {"collection": "s288_corpus", "query": "semantic search embeddings"}` against both the source and the target server.

### Evidence
```
E   AssertionError: source /search returned 0
E   assert 0 == 200
```
