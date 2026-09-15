## Bug: Watcher auto-sync: new file appears with `ingested_by="watcher"`

**ID**: S276-watcher_ingests_new_file
**Scenario**: S276
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: Expected ingested_by='watcher' on watcher-written chunk; got ingested_by='reindex' (UserManual/50_ingestion_and_collections.md — Watcher behavior)
assert 'reindex' == 'watcher'

- watcher

### What should happen
- `readiness.watcher.running` becomes `true` (setup error if not).
- Within 60 seconds of writing the new file, a search returns a result with `source_path` matching `canary.md`.
- That result has `ingested_by` equal to `"watcher"`.

### Steps to reproduce
1. Create a directory with a seed file (`seed.md`).
2. Start an isolated server with `[collections]\nwatch = true\ncollections = ["/path/to/dir"]` in `archon-search.toml`.
3. Poll `GET /status` until `readiness.watcher.running` is `true` and `watching` is non-empty (up to 30 s). Read the collection name from `watching[0]`.
4. Write a new file `canary.md` containing unique content `xyzzy_watcher_canary_1234` to the watched directory.
5. Poll `POST /search` for up to 60 seconds until a result whose `source_path` contains `canary.md` appears for the query `xyzzy_watcher_canary_1234`.

### Evidence
```
E   AssertionError: Expected ingested_by='watcher' on watcher-written chunk; got ingested_by='reindex' (UserManual/50_ingestion_and_collections.md — Watcher behavior)
E   assert 'reindex' == 'watcher'
E     
E     - watcher
E     + reindex
```
