## Bug: MCP `ingest_file` on a nonexistent path returns the `{error, code}` envelope

**ID**: S388-code_is_a_non_empty_string
**Scenario**: S388
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: `code` is None, not a machine-readable code string. 130_ttl_and_scoping.md:187 states MCP errors use the {"error": "...", "code": "..."} shape — `code` carries a string, and no page in docs/ permits null. 50_ingestion_and_collections.md:54-55 shows ingest_file failures do carry one (`code="file_too_large"`). The VALUE is not asserted: no document names a code for a missing path. payload: {"doc_id": "[REDACTED]", "chunks_created": 0, "status": "error", "error": "Failed to parse /private/tmp/archon_s388_missing.md: [Errno 2] No such file or directory: '/private/tmp/archon_s388_missing.md'", "warnings": [], "code": null}
assert (False)

### What should happen
- Step 3 does not succeed: the payload reports a failure rather than an indexed document.
- The payload carries a non-empty `error` string — the first half of the shape line 187 documents for MCP errors.
- The payload carries a `code` whose value is a **non-empty string** — the second half of that shape. `{"error": "...", "code": "..."}` gives `code` a string; nothing in `./docs/` permits `null`, and 50:54-55 shows `ingest_file` failures carrying a code (`file_too_large`). The specific value is not asserted, only that a machine-readable code is present, because no document names a code for a missing path.
- Step 4 lists the same collections as before the run, with `archon_test_docs` unchanged: the call indexed nothing.

### Steps to reproduce
1. `rm -f /tmp/archon_s388_missing.md` — make sure the path really is absent.
2. Open an MCP session on `POST http://127.0.0.1:8765/mcp/` with `Authorization: Bearer $ARCHON_SEARCH_API_KEY` (`initialize`, then `notifications/initialized`).
3. `tools/call` `ingest_file` with `{"path": "/tmp/archon_s388_missing.md", "collection": "archon_test_docs"}`.
4. `archon-search collection list`

### Evidence
```
E   AssertionError: `code` is None, not a machine-readable code string. 130_ttl_and_scoping.md:187 states MCP errors use the {"error": "...", "code": "..."} shape — `code` carries a string, and no page in docs/ permits null. 50_ingestion_and_collections.md:54-55 shows ingest_file failures do carry one (`code="file_too_large"`). The VALUE is not asserted: no document names a code for a missing path. payload: {"doc_id": "[REDACTED]", "chunks_created": 0, "status": "error", "error": "Failed to parse /private/tmp/archon_s388_missing.md: [Errno 2] No such file or directory: '/private/tmp/archon_s388_missing.md'", "warnings": [], "code": null}
E   assert (False)
E    +  where False = isinstance(None, str)
```
