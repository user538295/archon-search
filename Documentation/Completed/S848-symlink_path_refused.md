## Bug: Path ingest reads regular files only: FIFOs and symlinks are refused

**ID**: S848-symlink_path_refused
**Scenario**: S848
**Severity**: medium
**Version**: archon-search, version 26.10.2267

### What happened
Failed: bug: bugs/S848-symlink_path_refused.md — 50_ingestion...md:59 requires a symlink to be refused; got job DONE error=None result={'warnings': []}

### What should happen
- Step 1: refused — either a `4xx`, or a job that ends `FAILED` (never `DONE`); the collection is absent or holds 0 documents.
- Step 2: refused the same way — `50_ingestion...md:59`.
- Step 3: `doc_count` of `c-dir` is `1` (only `real.md`; the symlink is not read) — `06_hardening_checklist.md:100`.

### Steps to reproduce
1. `POST /ingest {"path": "<tmp>/fifo.md", "collection": "c-fifo"}`.
2. `POST /ingest {"path": "<tmp>/link.md", "collection": "c-link"}`.
3. `POST /ingest {"path": "<tmp>/d", "collection": "c-dir"}`, then `GET /collections/c-dir`.

### Evidence
```
E   Failed: bug: bugs/S848-symlink_path_refused.md — 50_ingestion...md:59 requires a symlink to be refused; got job DONE error=None result={'warnings': []}
```
