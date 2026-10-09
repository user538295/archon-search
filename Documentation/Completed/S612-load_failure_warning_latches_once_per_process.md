## Bug: Checkpoint load-failure WARNING logged once per prose document, not once per process

**ID**: S612-load_failure_warning_latches_once_per_process
**Scenario**: S612
**Severity**: low
**Version**: archon-search, version 26.10.2150

### What happened
With [graph].enabled = true and the checkpoint cache <data-dir>/models/graph blocked (a file, not a directory), two ingest requests on the same process (acme.md + grafana.md + deploy.py, then billing.md) logged the documented WARNING 'prose extraction backend failed to load' three times — once per prose document — instead of once per process.

### What should happen
OperatorGuide/90_incident_runbook.md:119 names 'prose extraction backend failed to load' as the log line to grep for a failed checkpoint load; :129 says the server logs the matching WARNING 'once per process — a single line, not one per file'; OperatorGuide/60_graph_operations.md:26,60,62 say one WARNING is logged per process (not once per file) and a genuine load failure latches once per process.

### Steps to reproduce
1. Start the isolated instance as described above.
2. Ingest a two-file corpus containing proper nouns (so prose extraction would otherwise fire) via `POST /ingest`, and wait for the job to reach a terminal status.
3. Read the terminal job body's `result.warnings`.
4. Ingest a second, separate one-file corpus into a different collection on the SAME running instance, and wait for that job too.
5. Read the server log for the checkpoint-load WARNING.

### Evidence
```
2026-10-08T11:26:53Z WARNING archon_search.graph_extractor GraphExtractor: prose extraction backend failed to load
2026-10-08T11:26:58Z WARNING archon_search.graph_extractor GraphExtractor: prose extraction backend failed to load
2026-10-08T11:27:12Z WARNING archon_search.graph_extractor GraphExtractor: prose extraction backend failed to load
(both ingest jobs reached DONE; result.warnings carried the sanitized 'graph prose extraction model is unavailable' notice)
```
