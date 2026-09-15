## Bug: Backup → wipe data dir → restore → search returns pre-backup results

**ID**: S291-search_returns_pre_backup_results_after_wipe_and_restore
**Scenario**: S291
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: pre-backup /search=0: []
assert 0 == 200

### What should happen
- Step 1: HTTP 200; `results` non-empty; each result carries `doc_id`, `text`, `score`.
- Step 7: service starts; `GET /health` returns 200.
- Step 8: HTTP 200; `results` non-empty (a lost/empty index would surface as empty results or 500/504, per the doc's verification checklist); the set of returned `doc_id`s equals the pre-backup set — the restored runtime directory is the same database with unchanged ranking.

### Steps to reproduce
1. `curl -fsS -X POST http://127.0.0.1:8765/search -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H "Content-Type: application/json" -d '{"collection":"<name>","query":"<known query>"}'`  (baseline — record the returned `doc_id`s)
2. `archon-search stop`
3. `cp -a ~/.archon-search "$DEST/"`  (cold snapshot)
4. `rm -rf ~/.archon-search`  (wipe — simulate whole-instance data loss)
5. `cp -a "$DEST/.archon-search" ~/`  (restore)
6. `chmod 600 ~/.archon-search/.search.env`
7. `archon-search start`
8. Repeat the step-1 search.

### Evidence
```
E   AssertionError: pre-backup /search=0: []
E   assert 0 == 200
```
