## Bug: `POST /search` `503` while the reranker is warming up (UNDOCUMENTED — no such window is specified)

**ID**: S444-reranker_warmup_503_is_undocumented
**Scenario**: S444
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
AssertionError: the set of docs mentioning warm-up changed: ['docs/OperatorGuide/20_monitoring_and_alerts.md', 'docs/OperatorGuide/80_capacity_and_performance.md', 'docs/OperatorGuide/90_incident_runbook.md', 'docs/UserManual/100_jobs_and_async_operations.md', 'docs/UserManual/10_installation.md', 'docs/UserManual/140_running_with_docker.md', 'docs/UserManual/160_troubleshooting.md', 'docs/UserManual/20_wizard.md', 'docs/UserManual/40_running_the_server.md', 'docs/UserManual/65_graph_search.md'] (expected only ['docs/UserManual/10_installation.md'], the --skip-preload install note). A reranker warm-up window may now be documented — re-implement S444 against the documented status and message instead of this positive-path proxy.
assert ['docs/Operat...cker.md', ...] == ['docs/UserMa...tallation.md']

At index 0 diff: 'docs/OperatorGuide/20_monitoring_and_alerts.md' != 'docs/UserManual/10_installation.md'
Left contains 9 more items, first extra item: 'docs/OperatorGuide/80_capacity_and_performance.md'
Use -v to get more diff

### What should happen
- **The bullet describes UNDOCUMENTED behavior.** There is no specified interval in which `/search` answers `503` because the reranker is still loading. Nothing below asserts such a `503`; fabricating one would rest on invented behavior, which the project's Hard Rules forbid.
- **Documented, exercisable assertion (what IS specified):** the first `POST /search` on a freshly booted instance — the request that actually loads the reranker — returns **HTTP `200`**, not `503`. `OperatorGuide/90_incident_runbook.md:15` fixes the meaning of that `200`: "HTTP 200 with `results: []` means the pipeline succeeded but matched nothing — not a failure signal", and the seeded corpus matches the query, so `results` is non-empty. This is the observable that stands in for the bullet's claim: a booted server serves the first search rather than rejecting it while warming.
- The first search's response body carries none of the documented `503` markers — no `metadata_store_error` and no "metadata store could not be reached" (60:51; 90:92) — so a `200` here is not masking one of the documented `503` causes.
- **Doc-gap reopening gate:** the only file under `./docs/` matching `warm.?up|warming` is `UserManual/10_installation.md` (the `--skip-preload` install line). If any other doc ever gains a warm-up mention, this assertion flips red so S444 is re-implemented against the then-documented status, body and window instead of this positive-path proxy. No bug is filed: a missing doc is not an app defect.

### Steps to reproduce
1. Start an isolated server and ingest one small collection.
2. As soon as `GET /health` returns `200`, issue the **first** `POST /search`: `curl -sS -w '\n%{http_code}\n' -X POST "$BASE/search" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{"collection":"s444_docs","query":"quarterly earnings report finance team"}'`
3. `grep -rniE "warm.?up|warming" ./docs/`

### Evidence
```
E   AssertionError: the set of docs mentioning warm-up changed: ['docs/OperatorGuide/20_monitoring_and_alerts.md', 'docs/OperatorGuide/80_capacity_and_performance.md', 'docs/OperatorGuide/90_incident_runbook.md', 'docs/UserManual/100_jobs_and_async_operations.md', 'docs/UserManual/10_installation.md', 'docs/UserManual/140_running_with_docker.md', 'docs/UserManual/160_troubleshooting.md', 'docs/UserManual/20_wizard.md', 'docs/UserManual/40_running_the_server.md', 'docs/UserManual/65_graph_search.md'] (expected only ['docs/UserManual/10_installation.md'], the --skip-preload install note). A reranker warm-up window may now be documented — re-implement S444 against the documented status and message instead of this positive-path proxy.
E   assert ['docs/Operat...cker.md', ...] == ['docs/UserMa...tallation.md']
E     
E     At index 0 diff: 'docs/OperatorGuide/20_monitoring_and_alerts.md' != 'docs/UserManual/10_installation.md'
E     Left contains 9 more items, first extra item: 'docs/OperatorGuide/80_capacity_and_performance.md'
E     Use -v to get more diff
```
