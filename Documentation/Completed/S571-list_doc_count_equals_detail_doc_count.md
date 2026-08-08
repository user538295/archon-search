## Bug: `GET /collections/` `doc_count` must match `GET /collections/{name}` for the same collection

**ID**: S571-list_doc_count_equals_detail_doc_count
**Scenario**: S571
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: GET /collections/ reports doc_count=0 while GET /collections/s571_src reports doc_count=3 for the same collection at the same moment — UserManual/50_ingestion_and_collections.md:167 documents the two routes as list/detail views of one entity and OperatorGuide/20_monitoring_and_alerts.md:92 defines doc_count as the single cached meta.doc_count ('may lag a live recount' covers a stale cache, not two views of the same cache disagreeing). list_entry={'name': 's571_src', 'path': '/private/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-iso-yy_x1rj1/s571_src', 'description': '', 'doc_count': 0, 'chunk_count': 3, 'namespace': 'default', 'status': 'not_yet_indexed', 'active_embedding_model': 'BAAI/bge-small-en-v1.5', 'needs_reindex': False} detail={'name': 's571_src', 'path': '/private/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-iso-yy_x1rj1/s571_src', 'description': '', 'doc_count': 3, 'chunk_count': 3, 'namespace': 'default', 'status': 'not_yet_indexed', 'active_embedding_model': 'BAAI/bge-small-en-v1.5', 'needs_reindex': False, 'pending_embedding_model': None, 'reindex_job_id': None, 'centroid_present': True, 'last_indexed': None, 'acl_protected_count': 0, 'acl_open_count': 3, 'default_ttl_seconds': None, 'schema_version': 1}
assert 0 == 3

### What should happen
- Step 3 and step 4 both return `200`, and the list entry's `doc_count` **equals** the detail view's `doc_count` for that collection (50:167 — list and detail are two views of one entity; 20:92 — `doc_count` is one cached `meta.doc_count`).
- The shared value is the collection's document count, i.e. non-zero for the 3-file corpus that just finished ingesting (20:92 defines the field as a doc count; 160:63 relies on these counters reflecting ingested content).
- Step 5 exits `0` and prints `<name>  docs=<n>  chunks=<n>` (50:100) with `<n>` equal to the detail view's `doc_count` — the CLI is a straight proxy of `GET /collections/`, so a populated collection must not print `docs=0`.

**Observed (2026-08-06, archon-search 26.8.1848)**: `GET /collections/` reports `doc_count: 0` while `GET /collections/{name}` reports `doc_count: 3` and `GET /status` reports `doc_count: 3` for the same collection at the same moment; `collection list` prints `s571b_src  docs=0  chunks=3`. Reproduced identically for an ad-hoc `POST /ingest` collection (list `0`, detail `1`). The zero is permanent, not a lag. Filed as a bug — the assertions above stay as the documentation states them.

### Steps to reproduce
1. Start an isolated instance; write 3 `.md` files into a directory.
2. `POST /collections/ {"path": "<dir>"}` → `202`; poll `GET /jobs/{job_id}` until `DONE`.
3. `GET /collections/` with `Authorization: Bearer <key>` — read the entry's `doc_count`/`chunk_count`.
4. `GET /collections/{name}` immediately after — read its `doc_count`/`chunk_count`.
5. `archon-search collection list --api-url <url> --api-key <key>`.

### Evidence
```
E   AssertionError: GET /collections/ reports doc_count=0 while GET /collections/s571_src reports doc_count=3 for the same collection at the same moment — UserManual/50_ingestion_and_collections.md:167 documents the two routes as list/detail views of one entity and OperatorGuide/20_monitoring_and_alerts.md:92 defines doc_count as the single cached meta.doc_count ('may lag a live recount' covers a stale cache, not two views of the same cache disagreeing). list_entry={'name': 's571_src', 'path': '/private/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-iso-yy_x1rj1/s571_src', 'description': '', 'doc_count': 0, 'chunk_count': 3, 'namespace': 'default', 'status': 'not_yet_indexed', 'active_embedding_model': 'BAAI/bge-small-en-v1.5', 'needs_reindex': False} detail={'name': 's571_src', 'path': '/private/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-iso-yy_x1rj1/s571_src', 'description': '', 'doc_count': 3, 'chunk_count': 3, 'namespace': 'default', 'status': 'not_yet_indexed', 'active_embedding_model': 'BAAI/bge-small-en-v1.5', 'needs_reindex': False, 'pending_embedding_model': None, 'reindex_job_id': None, 'centroid_present': True, 'last_indexed': None, 'acl_protected_count': 0, 'acl_open_count': 3, 'default_ttl_seconds': None, 'schema_version': 1}
E   assert 0 == 3
```
