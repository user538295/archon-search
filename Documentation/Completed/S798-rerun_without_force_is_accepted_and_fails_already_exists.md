## Bug: Cancelling an import during `ingesting` ends `CANCELLED` with the batches written so far left in place; a re-run without `--force-overwrite` is accepted (`202`) and ends `FAILED` with `collection '<name>' already exists; …`

**ID**: S798-rerun_without_force_is_accepted_and_fails_already_exists
**Scenario**: S798
**Severity**: medium
**Version**: archon-search, version 26.10.2267

### What happened
Failed: bug: bugs/S798-rerun_without_force_is_accepted_and_fails_already_exists.md — after a mid-ingesting cancel the re-run without force_overwrite must be accepted with 202 (90_export_import.md:196, 100_jobs_and_async_operations.md:64); the product answered 409 {'error': 'collection_exists', 'detail': "Collection 's798_target' already exists; use force_overwrite=true to overwrite"}

### What should happen
- Step 2: HTTP `202` for the DELETE; the CLI exits `2` with stderr containing `Import CANCELLED: the collection may be partially or fully imported; re-run the same import command with --force-overwrite to restore a complete copy.` (90:104); the job ends `CANCELLED` with `progress.phase` `ingesting` and `processed` < 30 000; `s798_target` is listed with `0 < doc_count < 30 000` — the batches written so far stay (90:196, 100:64).
- Step 3: HTTP `202` (not `409`), and the job ends `FAILED` with `error` `collection 's798_target' already exists; use force_overwrite=true to overwrite` (90:196, 100:64).
- Step 4: exit code `2` (90:196).
- Step 5: exit `0` and `Done. imported=30000`; `s798_target` lists `doc_count` 30 000 — `--force-overwrite` restores the complete copy (90:104, 90:196).

### Steps to reproduce
1. `archon-search import s798_target <archive> --wait --api-url http://127.0.0.1:<iso-port> --api-key <iso-key>` in the background. Poll `GET /jobs?status=RUNNING` until the import job shows `progress.phase == "ingesting"` with `processed > 0`.
2. `curl -s -X DELETE -H "Authorization: Bearer <iso-key>" http://127.0.0.1:<iso-port>/jobs/<job_id>`; wait for the CLI to exit; poll `GET /jobs/<job_id>` until terminal; `curl -s -H "Authorization: Bearer <iso-key>" http://127.0.0.1:<iso-port>/collections/`.
3. `curl -s -w '\n%{http_code}\n' -X POST -H "Authorization: Bearer <iso-key>" -H "Content-Type: application/json" -d '{"path": "<archive>"}' http://127.0.0.1:<iso-port>/collections/s798_target/import` — poll the job until terminal.
4. `archon-search import s798_target <archive> --wait --api-url … --api-key …`
5. `archon-search import s798_target <archive> --force-overwrite --wait --api-url … --api-key …`; then `GET /collections/`.

### Evidence
```
E   Failed: bug: bugs/S798-rerun_without_force_is_accepted_and_fails_already_exists.md — after a mid-ingesting cancel the re-run without force_overwrite must be accepted with 202 (90_export_import.md:196, 100_jobs_and_async_operations.md:64); the product answered 409 {'error': 'collection_exists', 'detail': "Collection 's798_target' already exists; use force_overwrite=true to overwrite"}
```
