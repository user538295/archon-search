## Bug: Backup `keep` rotation deletes the oldest archive once the keep count is exceeded

**ID**: S476-only_keep_archives_remain
**Scenario**: S476
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: 4 archives matching s476_keep_docs.backup.*.tar.gz remain after a successful backup with [backup].keep = 2 — 40_backup_restore_disaster_recovery.md:52 states older ones are deleted after each successful backup, and :64 states rotation deletes files matching that glob in the namespace directory. remaining=['s476_keep_docs.backup.20260101T000000Z.tar.gz', 's476_keep_docs.backup.20260102T000000Z.tar.gz', 's476_keep_docs.backup.20260103T000000Z.tar.gz', 's476_keep_docs.backup.20260806T214310Z.tar.gz'], seeded=['s476_keep_docs.backup.20260101T000000Z.tar.gz', 's476_keep_docs.backup.20260102T000000Z.tar.gz', 's476_keep_docs.backup.20260103T000000Z.tar.gz'], job={"job_id": "973f7873-03a6-4fb6-835b-999ec4b516ef", "status": "DONE", "created_at": "2026-08-06T21:43:10.042449+00:00", "updated_at": "2026-08-06T21:43:12.560893+00:00", "result": {"archive_path": "/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-iso-frzrgfkq/backups/default/s476_keep_docs.backup.20260806T214310Z.tar.gz"}, "error": null, "namespace": "default", "progress": {"processed": 1, "total": 1, "phase": "packaging"}, "source": "backup", "source_path": "", "collection": "s476_keep_docs", "retry_count": 0, "output_path": "/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-iso-frzrgfkq/backups/default/s476_keep_docs.backup.20260806T214310Z.tar.gz", "archive_path": null, "kind": null, "migrations_applied": null, "backup_confirmed": null, "job_type": "export"}
assert 4 == 2

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
E   AssertionError: 4 archives matching s476_keep_docs.backup.*.tar.gz remain after a successful backup with [backup].keep = 2 — 40_backup_restore_disaster_recovery.md:52 states older ones are deleted after each successful backup, and :64 states rotation deletes files matching that glob in the namespace directory. remaining=['s476_keep_docs.backup.20260101T000000Z.tar.gz', 's476_keep_docs.backup.20260102T000000Z.tar.gz', 's476_keep_docs.backup.20260103T000000Z.tar.gz', 's476_keep_docs.backup.20260806T214310Z.tar.gz'], seeded=['s476_keep_docs.backup.20260101T000000Z.tar.gz', 's476_keep_docs.backup.20260102T000000Z.tar.gz', 's476_keep_docs.backup.20260103T000000Z.tar.gz'], job={"job_id": "973f7873-03a6-4fb6-835b-999ec4b516ef", "status": "DONE", "created_at": "2026-08-06T21:43:10.042449+00:00", "updated_at": "2026-08-06T21:43:12.560893+00:00", "result": {"archive_path": "/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-iso-frzrgfkq/backups/default/s476_keep_docs.backup.20260806T214310Z.tar.gz"}, "error": null, "namespace": "default", "progress": {"processed": 1, "total": 1, "phase": "packaging"}, "source": "backup", "source_path": "", "collection": "s476_keep_docs", "retry_count": 0, "output_path": "/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-iso-frzrgfkq/backups/default/s476_keep_docs.backup.20260806T214310Z.tar.gz", "archive_path": null, "kind": null, "migrations_applied": null, "backup_confirmed": null, "job_type": "export"}
E   assert 4 == 2
E    +  where 4 = len(['s476_keep_docs.backup.20260101T000000Z.tar.gz', 's476_keep_docs.backup.20260102T000000Z.tar.gz', 's476_keep_docs.backup.20260103T000000Z.tar.gz', 's476_keep_docs.backup.20260806T214310Z.tar.gz'])
```
