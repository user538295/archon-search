## Bug: MCP `search` documents neither `rerank` nor `reranker_score` (UNDOCUMENTED)

**ID**: S391-no_doc_pairs_reranker_score_with_mcp
**Scenario**: S391
**Severity**: medium
**Version**: archon-search, version 26.10.2181

### What happened
AssertionError: docs/ now carries a line pairing `reranker_score` with MCP, so the result field S391 records as UNDOCUMENTED for MCP `search` may now be specified. Rewrite S391 against that specification. Hits:
OperatorGuide/90_incident_runbook.md:92: - **No response at all** (client-side timeout, "connection failed", `status 0`) — not in the 500/503/504 family, and the most likely cold-start symptom. The war
assert not ['OperatorGuide/90_incident_runbook.md:92: - **No response at all** (client-side timeout, "connection failed", `status 0`) — not in the 500/503/504 family, and the most likely cold-start symptom. The war']

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
E   AssertionError: docs/ now carries a line pairing `reranker_score` with MCP, so the result field S391 records as UNDOCUMENTED for MCP `search` may now be specified. Rewrite S391 against that specification. Hits:
E     OperatorGuide/90_incident_runbook.md:92: - **No response at all** (client-side timeout, "connection failed", `status 0`) — not in the 500/503/504 family, and the most likely cold-start symptom. The war
E   assert not ['OperatorGuide/90_incident_runbook.md:92: - **No response at all** (client-side timeout, "connection failed", `status 0`) — not in the 500/503/504 family, and the most likely cold-start symptom. The war']
```
