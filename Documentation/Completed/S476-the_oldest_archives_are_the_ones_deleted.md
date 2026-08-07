## Bug: Backup `keep` rotation deletes the oldest archive once the keep count is exceeded

**ID**: S476-the_oldest_archives_are_the_ones_deleted
**Scenario**: S476
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: the oldest archives ['s476_keep_docs.backup.20260101T000000Z.tar.gz', 's476_keep_docs.backup.20260102T000000Z.tar.gz'] survived rotation after a successful backup with keep = 2 — 40_backup_restore_disaster_recovery.md:52 states the OLDER ones are deleted. remaining=['s476_keep_docs.backup.20260101T000000Z.tar.gz', 's476_keep_docs.backup.20260102T000000Z.tar.gz', 's476_keep_docs.backup.20260103T000000Z.tar.gz', 's476_keep_docs.backup.20260806T214310Z.tar.gz']
assert not ['s476_keep_docs.backup.20260101T000000Z.tar.gz', 's476_keep_docs.backup.20260102T000000Z.tar.gz']

### What should happen
- Step 4: HTTP `202`, the collection appears in `queued`, and the job reaches `DONE` — a **successful** backup, which is the condition line 52 attaches rotation to.
- Step 5: exactly `keep` (= 2) files matching `{collection}.backup.*.tar.gz` remain. Four existed at the moment the backup succeeded (three seeded plus the one just written), so "older ones are deleted after each successful backup" (line 52) requires two deletions.
- Step 5: the two survivors are the **newest** by timestamp — the archive the backup just wrote, and `…20260103T000000Z….tar.gz`. Line 52 says the *older* ones are deleted.
- Step 5: `{collection}-manual-export.tar.gz` still exists. Line 64 states rotation "only deletes files matching `{collection}.backup.*.tar.gz`", so a manual export in the same directory must be untouched.

### Steps to reproduce
1. Start the isolated instance with `[backup] interval_hours = 24, keep = 2`; ingest one collection.
2. Create `<data_dir>/backups/default/` and write three archives named `{collection}.backup.20260101T000000Z.tar.gz`, `…20260102T000000Z….tar.gz`, `…20260103T000000Z….tar.gz`, with mtimes ordered oldest-first to match their timestamps.
3. Write one non-matching file in the same directory: `{collection}-manual-export.tar.gz`.
4. `POST /backup/trigger`; poll `GET /jobs/{job_id}` until the backup job is `DONE`.
5. List `<data_dir>/backups/default/`.

### Evidence
```
E   AssertionError: the oldest archives ['s476_keep_docs.backup.20260101T000000Z.tar.gz', 's476_keep_docs.backup.20260102T000000Z.tar.gz'] survived rotation after a successful backup with keep = 2 — 40_backup_restore_disaster_recovery.md:52 states the OLDER ones are deleted. remaining=['s476_keep_docs.backup.20260101T000000Z.tar.gz', 's476_keep_docs.backup.20260102T000000Z.tar.gz', 's476_keep_docs.backup.20260103T000000Z.tar.gz', 's476_keep_docs.backup.20260806T214310Z.tar.gz']
E   assert not ['s476_keep_docs.backup.20260101T000000Z.tar.gz', 's476_keep_docs.backup.20260102T000000Z.tar.gz']
```
