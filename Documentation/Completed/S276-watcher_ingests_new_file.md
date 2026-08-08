## Bug: Watcher auto-sync: new file appears with `ingested_by="watcher"`

**ID**: S276-watcher_ingests_new_file
**Scenario**: S276
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: Expected ingested_by='watcher' on watcher-written chunk; got ingested_by='http' (UserManual/50_ingestion_and_collections.md — Watcher behavior)
assert 'http' == 'watcher'

- watcher

### What should happen
- The ingest job in step 2 completes without error (job body is not null).
- Within 30 seconds of writing the new file, a search for `xyzzy_watcher_canary_1234` returns at least one result.
- The first result has `ingested_by` equal to `"watcher"`.

### Steps to reproduce
1. Start an isolated server with `[collections]\nwatch = true` in `archon-search.toml`.
2. Ingest a seed directory into collection `watch_test` via `POST /ingest` and wait for the job to reach DONE.
3. Write a new file containing unique content `xyzzy_watcher_canary_1234` to the same directory.
4. Poll `POST /search` for up to 30 seconds until results for query `xyzzy_watcher_canary_1234` appear in collection `watch_test`.

### Evidence
```
E   AssertionError: Expected ingested_by='watcher' on watcher-written chunk; got ingested_by='http' (UserManual/50_ingestion_and_collections.md — Watcher behavior)
E   assert 'http' == 'watcher'
E     
E     - watcher
E     + http
```
