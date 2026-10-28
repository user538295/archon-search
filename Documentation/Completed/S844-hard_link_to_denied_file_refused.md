## Bug: A symlink, hard link or different letter case does not bypass the ingest deny list

**ID**: S844-hard_link_to_denied_file_refused
**Scenario**: S844
**Severity**: medium
**Version**: archon-search, version 26.10.2267

### What happened
Failed: bug: bugs/S844-hard_link_to_denied_file_refused.md — SecurityGuide/06_hardening_checklist.md:92 — a hard link to .env must still be refused with 400 'path is unsafe: denied_location'; got 202: {'job_id': '18822fe3-4210-446d-a98b-e99a4820cc56', 'status': 'PENDING', 'created_at': '2026-10-27T23:19:10.648430+00:00', 'updated_at': '2026-10-27T23:19:10.648430+00:00', 'result': None, 'error': None, 'namespace': 'default', 'progress': None, 'source': 'user', 'source_path': '/private/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/ips-9em5srvx/hard.md', 'collection': 's844', 'retry_count': 0, 'output_path': None, 'archive_path': None, 'kind': None, 'migrations_applied': None, 'backup_confirmed': None, 'job_type': 'ingest'}

### What should happen
- Each call answers `400` `{"detail": "path is unsafe: denied_location"}` — `SecurityGuide/06_hardening_checklist.md:92` ("Matching is by device and inode, so a symlink, a hard link or a different letter case does not bypass it"); `50_ingestion...md:82`.

### Steps to reproduce
1. `POST /ingest {"path": "<tmp>/sym.md", "collection": "c"}` with the admin key.
2. `POST /ingest {"path": "<tmp>/.ENV", "collection": "c"}`.
3. `POST /ingest {"path": "<tmp>/hard.md", "collection": "c"}`.

### Evidence
```
E   Failed: bug: bugs/S844-hard_link_to_denied_file_refused.md — SecurityGuide/06_hardening_checklist.md:92 — a hard link to .env must still be refused with 400 'path is unsafe: denied_location'; got 202: {'job_id': '18822fe3-4210-446d-a98b-e99a4820cc56', 'status': 'PENDING', 'created_at': '2026-10-27T23:19:10.648430+00:00', 'updated_at': '2026-10-27T23:19:10.648430+00:00', 'result': None, 'error': None, 'namespace': 'default', 'progress': None, 'source': 'user', 'source_path': '/private/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/ips-9em5srvx/hard.md', 'collection': 's844', 'retry_count': 0, 'output_path': None, 'archive_path': None, 'kind': None, 'migrations_applied': None, 'backup_confirmed': None, 'job_type': 'ingest'}
```
