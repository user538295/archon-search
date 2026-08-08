## Bug: Watcher end-to-end: added file becomes a searchable document

**ID**: S292-added_file_becomes_searchable_document
**Scenario**: S292
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: Watcher did not make the new file 's292_new.md' searchable within 30 seconds; no result with that source_path for query 'plugh_watcher_e2e_5678' in collection 'watch_e2e' (UserManual/50_ingestion_and_collections.md — Watcher behavior)
assert None is not None

### What should happen
- The ingest job in step 2 completes without error (job body is not null).
- Within 30 seconds of writing the new file, `POST /search` returns HTTP 200 and — among the results — a hit whose `source_path` references the newly written file `s292_new.md`. Matching on that source path (rather than the top result) proves the watcher indexed the new document end-to-end, not the seed.
- That matching result carries the documented result fields: `doc_id`, `chunk_id`, `text`, `score`, `source_path`, and `collection`, with `collection` equal to `watch_e2e`.

### Steps to reproduce
1. Start an isolated server with `[collections]\nwatch = true` in `archon-search.toml`.
2. Ingest a seed directory into collection `watch_e2e` via `POST /ingest` and wait for the job to reach DONE.
3. Write a new file `s292_new.md` containing unique content `plugh_watcher_e2e_5678` into the same watched directory.
4. Poll `POST /search` for up to 30 seconds, scanning every result, until one whose `source_path` references `s292_new.md` appears for query `plugh_watcher_e2e_5678` in collection `watch_e2e`.

### Evidence
```
E   AssertionError: Watcher did not make the new file 's292_new.md' searchable within 30 seconds; no result with that source_path for query 'plugh_watcher_e2e_5678' in collection 'watch_e2e' (UserManual/50_ingestion_and_collections.md — Watcher behavior)
E   assert None is not None
```
