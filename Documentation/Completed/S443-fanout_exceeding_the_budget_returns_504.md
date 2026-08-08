## Bug: `POST /search` returns `504` when the fan-out exceeds `[search] fanout_timeout_seconds`

**ID**: S443-fanout_exceeding_the_budget_returns_504
**Scenario**: S443
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: POST /search over a 3-collection fan-out returned 200 after 1.245s with fanout_timeout_seconds=0.001; 30_configuration.md:80 documents that exceeding the whole-fan-out timeout returns HTTP 504 (also 60_searching.md:53, 80_explain_and_debugging.md:168, OperatorGuide/90_incident_runbook.md:91). body={"results": [{"doc_id": "[REDACTED]", "chunk_id": "[REDACTED]-000000", "text": "# s443_c 1
assert 200 == 504

### What should happen
- Step 3: **HTTP `504`** — the whole fan-out cannot complete inside one millisecond (it embeds the query, runs three retrieval legs and a cross-encoder rerank), so it exceeds `fanout_timeout_seconds` and the documented consequence at 30:80 / 80:168 / 60:53 / 90:91 applies.
- Step 3: the elapsed wall-clock time of the request is far above `0.001 s`, which is the evidence that the configured budget really was exceeded rather than the fan-out having finished within it.
- Step 4: the server exits at config load with `fanout_timeout_seconds must be > 0, got 0.0` — proving the key is read and validated by this build, so a `200` at step 3 cannot be explained by the setting having been ignored as unknown.

### Steps to reproduce
1. Start an isolated server whose config carries:
   ```toml
   [search]
   fanout_timeout_seconds = 0.001
   ```
2. Ingest `s443_a`, `s443_b`, `s443_c`, each with two markdown files mentioning "Acme quarterly earnings report finance team".
3. `curl -sS -o /dev/null -w '%{http_code} %{time_total}\n' -X POST "$BASE/search" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{"collections":["s443_a","s443_b","s443_c"],"query":"quarterly earnings report finance team"}'`
4. Start a second isolated server with `fanout_timeout_seconds = 0` and observe that it refuses to boot.

### Evidence
```
E   AssertionError: POST /search over a 3-collection fan-out returned 200 after 1.245s with fanout_timeout_seconds=0.001; 30_configuration.md:80 documents that exceeding the whole-fan-out timeout returns HTTP 504 (also 60_searching.md:53, 80_explain_and_debugging.md:168, OperatorGuide/90_incident_runbook.md:91). body={"results": [{"doc_id": "[REDACTED]", "chunk_id": "[REDACTED]-000000", "text": "# s443_c 1
Acme quarterly earnings report section 1 finance team.
", "score": 7.145888805389404, "source_path": "/private/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-iso-_9zbl7e_/seed-s443_c/doc1.md"
E   assert 200 == 504
```
