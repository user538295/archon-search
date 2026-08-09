## Bug: MCP `ingest_file` accepts `chunk_ttl_seconds` and the TTL takes effect

**ID**: S469-docs_still_specify_no_ingest_file_result_shape
**Scenario**: S469
**Severity**: medium
**Version**: archon-search, version 26.8.1931

### What happened
AssertionError: the documentation now specifies a result shape for MCP `ingest_file` (['UserManual/50_ingestion_and_collections.md:180']) — S469 must be rewritten to assert that shape directly instead of the indirect `expiring` observable. This is a doc-gap gate, not an application bug.
assert not ['UserManual/50_ingestion_and_collections.md:180']

### What should happen
- Step 2 — **accepted**: the response is **not** the documented MCP error envelope. `130_ttl_and_scoping.md:187` reserves `{"error": "...", "code": "..."}` for rejections, so on acceptance both `error` and `code` are empty/null.
- Step 3 — **the TTL took effect**: HTTP 200, and the `expiring` listing for a 24-hour window is non-empty and contains the ingested `source_path`. A chunk expiring in 3600 s is due within 24 h, so `130_ttl_and_scoping.md:79` requires it to be listed. Without a per-request TTL the chunk would have no expiry at all (`:39` — the fallback is "null (no expiry)") and could not appear.
- Step 4 — **the TTL range rule applies identically** (`:187`): `chunk_ttl_seconds: 0` is rejected with the MCP error envelope — both `error` and `code` non-empty. `:43` states the REST parameter is "validated to a positive integer range", and `:187` extends every validation rule to MCP. This is the control that gives step 2's "accepted" assertion teeth.

**Not asserted — and why**: no `code` string, because no doc names the code for an out-of-range TTL. No echo of `chunk_ttl_seconds` in the result, and no job status, because no doc specifies either (see the corrections above).

**Doc-gap reopening gate**: no documentation specifies MCP `ingest_file`'s result shape. If `docs/` ever gains one — any line naming the tool together with a result field such as `job_id`, `doc_id`, or `chunks_created` — this scenario must be rewritten to assert that shape directly instead of the indirect `expiring` observable. The test fails if such a line appears.

### Steps to reproduce
1. Start an isolated instance; open an MCP session against `POST <base_url>/mcp/`.
2. `tools/call` `ingest_file` with `{"path": ".../ttl.md", "collection": "s469_ttl_docs", "chunk_ttl_seconds": 3600}`.
3. `GET <base_url>/collections/s469_ttl_docs/expiring?within_hours=24` with the bearer key.
4. `tools/call` `ingest_file` again with `chunk_ttl_seconds: 0` into a separate collection (out of the documented positive range).

### Evidence
```
E   AssertionError: the documentation now specifies a result shape for MCP `ingest_file` (['UserManual/50_ingestion_and_collections.md:180']) — S469 must be rewritten to assert that shape directly instead of the indirect `expiring` observable. This is a doc-gap gate, not an application bug.
E   assert not ['UserManual/50_ingestion_and_collections.md:180']
```
