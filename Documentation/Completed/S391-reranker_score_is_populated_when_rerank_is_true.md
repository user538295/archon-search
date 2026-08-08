## Bug: MCP `search` documents neither `rerank` nor `reranker_score` (UNDOCUMENTED)

**ID**: S391-reranker_score_is_populated_when_rerank_is_true
**Scenario**: S391
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: 3 of 3 results have a null `reranker_score` with `rerank=true`; 80_explain_and_debugging.md:101 reserves null for rerank=false, and :103 makes the reranker score the final score when a reranker ran. Without this control the nulls in the rerank=false case could mean the MCP transport never populates the field at all. first offender: {"doc_id": "[REDACTED]", "chunk_id": "[REDACTED]-000000", "source_path": "/private/tmp/archon-test-docs/alpha.md", "text": "# Alpha
assert not [{'acl': None, 'acl_gate': {'allowed_principals': None, 'sidecar_path': None, 'source': 'collection_default', 'warning...ranker_score': None, ...}, 'chunk_id': '[REDACTED]-000000', ...}]

### What should happen
- **The bullet describes UNDOCUMENTED behavior for MCP `search`.** Step 2's advertised schema offers no `rerank` parameter, matching `60_searching.md:104`, which omits it from the tool's key inputs.
- **Documented, exercisable assertion (what IS specified):** step 3's payload reports `rerank: false` (`80_explain_and_debugging.md:88` lists `rerank` on every `ExplainResponse`) and **every** result's `breakdown.reranker_score` is `null` — exactly what `80:101` states for `rerank=false`. Each result's top-level `score` equals its `rrf_score`, per `80:103`: "the **final** score: the `reranker_score` when a reranker ran, otherwise the `rrf_score`".
- **Control:** step 4's payload reports `rerank: true` and every result's `breakdown.reranker_score` is non-`null` (`80:101`, `80:103`). Without it, the nulls in step 3 could equally mean the MCP transport never populates `reranker_score` at all.
- **Doc-gap reopening gate:** step 5 finds no line in `./docs/` pairing `reranker_score` with MCP, and the `| \`search\` |` row does not mention `rerank`. If a future doc gives MCP `search` a `rerank` input or a `reranker_score` result field, the paired test flips **red** so S391 is rewritten against that specification.
- No bug is filed: a missing documentation statement is not an application defect.
- `archon-search collection list` is unchanged: `explain` and `tools/list` are read-only.

### Steps to reproduce
1. Open an MCP session on `POST http://127.0.0.1:8765/mcp/` with `Authorization: Bearer $ARCHON_SEARCH_API_KEY` (`initialize`, then `notifications/initialized`).
2. `tools/list` — read the advertised `inputSchema` of the `search` tool.
3. `tools/call` `explain` with `{"query": "the quick brown fox", "collection": "archon_test_docs", "rerank": false}`.
4. `tools/call` `explain` with the same arguments but `"rerank": true`.
5. Grep `./docs/` for any line carrying both `reranker_score` and `MCP`, and read the `| \`search\` |` row of the "## MCP tools" table in `UserManual/60_searching.md`.

### Evidence
```
E   AssertionError: 3 of 3 results have a null `reranker_score` with `rerank=true`; 80_explain_and_debugging.md:101 reserves null for rerank=false, and :103 makes the reranker score the final score when a reranker ran. Without this control the nulls in the rerank=false case could mean the MCP transport never populates the field at all. first offender: {"doc_id": "[REDACTED]", "chunk_id": "[REDACTED]-000000", "source_path": "/private/tmp/archon-test-docs/alpha.md", "text": "# Alpha
The quick brown fox jumps over the lazy dog.
", "score": 0.03278688524590164, "breakdown": {"vector_rank": 0, "vector_score": 0.3836347460746765, "vector_score_kind": "distance", "fts_rank": 0, "fts_score": 2.9424874782562256, "fts_score_kind": "bm25", "rrf_
E   assert not [{'acl': None, 'acl_gate': {'allowed_principals': None, 'sidecar_path': None, 'source': 'collection_default', 'warning...ranker_score': None, ...}, 'chunk_id': '[REDACTED]-000000', ...}]
```
