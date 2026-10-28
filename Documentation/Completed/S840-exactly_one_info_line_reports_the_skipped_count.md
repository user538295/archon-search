## Bug: directory ingest logs one INFO line counting the files skipped in locations the server never reads

**ID**: S840-exactly_one_info_line_reports_the_skipped_count
**Scenario**: S840
**Severity**: medium
**Version**: archon-search, version 26.10.2267

### What happened
Failed: bug: bugs/S840-exactly_one_info_line_reports_the_skipped_count.md — no INFO line about skipped files (50_ingestion_and_collections.md:61); log lines=['2026-10-27T23:18:35Z INFO archon_search.server.app API key authentication enabled (source: env var)', '2026-10-27T23:18:37Z INFO archon_search.server.app MCP HTTP endpoint mounted at /mcp', '2026-10-27T23:18:39Z INFO archon_search.server.warmup model warm-up complete (1 embedding model + reranker)']

### What should happen
- Step 2: exactly one `INFO` line mentions the skipped files and states the count `10` (`50:61`).

### Steps to reproduce
1. `curl -sS -X POST http://127.0.0.1:$PORT/ingest -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d "{\"path\":\"$TREE\",\"collection\":\"s840\"}"`; poll `GET /jobs/{id}` until `DONE`.
2. `grep -i skipped $DATA_DIR/logs/archon-search.log`

### Evidence
```
E   Failed: bug: bugs/S840-exactly_one_info_line_reports_the_skipped_count.md — no INFO line about skipped files (50_ingestion_and_collections.md:61); log lines=['2026-10-27T23:18:35Z INFO archon_search.server.app API key authentication enabled (source: env var)', '2026-10-27T23:18:37Z INFO archon_search.server.app MCP HTTP endpoint mounted at /mcp', '2026-10-27T23:18:39Z INFO archon_search.server.warmup model warm-up complete (1 embedding model + reranker)']
```
