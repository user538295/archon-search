## Bug: `GET /collections/` `doc_count` must match `GET /collections/{name}` for the same collection

**ID**: S571-cli_collection_list_prints_the_detail_doc_count
**Scenario**: S571
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: `collection list` printed docs=0 while GET /collections/s571_src reports doc_count=3 — the CLI proxies GET /collections/ (UserManual/50_ingestion_and_collections.md:100), and that route must report the same cached doc_count as the detail view (50:167, 20_monitoring_and_alerts.md:92). stdout: 's571_src  docs=0  chunks=3
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
E   AssertionError: `collection list` printed docs=0 while GET /collections/s571_src reports doc_count=3 — the CLI proxies GET /collections/ (UserManual/50_ingestion_and_collections.md:100), and that route must report the same cached doc_count as the detail view (50:167, 20_monitoring_and_alerts.md:92). stdout: 's571_src  docs=0  chunks=3
'
E   assert 0 == 3
E    +  where 0 = int('0')
E    +    where '0' = <built-in method group of re.Match object at 0x10aa88bf0>('docs')
E    +      where <built-in method group of re.Match object at 0x10aa88bf0> = <re.Match object; span=(0, 26), match='s571_src  docs=0  chunks=3'>.group
```
