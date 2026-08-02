**Purpose**: Triage steps for the failures that actually occur in production `archon-search` deployments, using only the existing endpoints, logs, and CLI.
**Audience**: SREs and sysadmins responding to an `archon-search` incident.
**Status**: Draft
**Last reviewed**: 2026-07-29
**Next review**: 2027-07-29

# Incident Runbook

This runbook lists the failure modes the codebase produces today, each paired with concrete triage steps. The signal surface is narrow (see [20_monitoring_and_alerts.md](20_monitoring_and_alerts.md)), so many incidents resolve to "read the log, restart the service".

## Principles

1. **Start with `/health`, then `/status`, then the log.** Three commands cover the first decision branch in nearly every incident.
2. **Restart is a legitimate first step.** It clears LanceDB lock-file remnants and re-loads models. The `/route` path builds a fresh router per request, so router-cache staleness is not a reason to restart.
3. **Search pipeline errors surface as 5xx.** `POST /search` returns HTTP 500 on pipeline failure, HTTP 504 on timeout, and HTTP 503 when collection metadata is unreachable (see below — this was formalised in A3). HTTP 200 with `results: []` means the pipeline succeeded but matched nothing — not a failure signal.
4. **Re-ingest is the rollback.** There is no transactional repair.

## First-five-minutes checklist

```bash
# 1. Liveness (auth-exempt)
curl -fsS http://127.0.0.1:8765/health || echo "process down"

# 2. Readiness — storage + model-probe status (auth-exempt)
curl -fsS http://127.0.0.1:8765/ready | jq

# 3. Status — per-collection progress, error counts, ETA (Bearer required)
curl -fsS -H "Authorization: Bearer ${ARCHON_SEARCH_API_KEY}" \
     http://127.0.0.1:8765/status | jq

# 4. Logs
tail -n 200 ~/.archon-search/logs/archon-search.log     # macOS
journalctl --user -u archon-search -n 200 --no-pager    # Linux
```

If `/health` is up and `/status` shows no rising `error_count`, look at telemetry (`GET /telemetry/stats`) and recent ingest activity. Logging locations and formats are documented in [30_logging.md](30_logging.md).

## Failure modes

### Stuck job (ingest or reindex)

**Symptoms**: a collection sits at `status: "indexing"`, `processed_files` stops advancing, `eta_seconds` grows unboundedly, no log entries for that job for several minutes.

**Triage**:

1. `GET /jobs/{job_id}` for the suspect job. Job IDs surface in the response of `POST /ingest`, `POST /collections/`, `POST /collections/{name}/reindex`, `POST /sync`, and the `--wait` output of the matching CLI proxies.
2. If `status` is `RUNNING` or `QUEUED` but log activity has stopped, `DELETE /jobs/{job_id}`. On the next tick the job marks itself cancelled.
3. If `DELETE` returns immediately, the job already terminated — inspect the final status.
4. Re-issue the ingest. Terminal jobs are evicted after 7 days by the job store.
5. A process restart marks any in-flight (`RUNNING`/`CANCELLING`) job as failed via the crash-recovery path. Use this if the asyncio task itself is unresponsive — `DELETE` only flips the stored status, so an unresponsive task will not honour cancellation and a restart is the actual recovery.

Underlying causes typically logged in `archon-search.log`: parser failure on a specific document, model-load timeout, or disk-full on `db_path`. See [50_maintenance_and_jobs.md](50_maintenance_and_jobs.md) for the job lifecycle and the `FAILED`/`FAILED_EXPIRED` distinction (below).

### Failed-ingest retry exhaustion (`FAILED_EXPIRED`)

**Symptoms**: a job shows `status: "FAILED_EXPIRED"` in `GET /jobs?status=FAILED_EXPIRED` or in `/status`; it is never retried.

**Triage**: this is terminal, not a fault to clear. The `MaintenanceLoop` retries `FAILED` ingest jobs until either they exhaust `[maintenance].retry_max_attempts` (default 3) or age past `[maintenance].retry_max_age_hours` (default 72), at which point they transition to `FAILED_EXPIRED` and are never re-enqueued (`jobs/maintenance_loop.py`). Read the original failure from the job record, fix the root cause (bad document, disk, model), and re-issue the ingest manually. Retry knobs live in [50_maintenance_and_jobs.md](50_maintenance_and_jobs.md).

### Watcher loop / churn

**Symptoms**: high CPU when no one is searching; `error_count` rising on a collection without an explicit ingest call; log spam from the watcher.

**Triage**:

1. Identify the watched paths from the `[collections]` block in `~/.archon-search/archon-search.toml`.
2. Check whether an external tool is rewriting files in those paths (editor autosave, indexer, syncthing).
3. Mitigate: set `[collections].watch = false` and restart, then trigger ingest manually via `archon-search sync`. There is no event-rate limiter today.
4. If the watcher is stuck (no events despite filesystem changes), restart the service. `watcher.py` exposes no health flag.

### LanceDB lock contention or "table busy"

**Symptoms**: `/search` surfaces a LanceDB lock/IO condition as either a `503` (metadata-lookup branch) or a `500` (pipeline path); or `archon-search start` fails to connect to the store; or two processes are running against the same `db_path`. Check the log for LanceDB lock/IO messages — do not rely on the HTTP status alone.

**Not a fault**: `POST /collections/` returning `503 {"detail": "store busy; retry in N seconds"}` with a `Retry-After` header, or `POST /ingest` returning `503 {"error": "store_busy", ...}` with `Retry-After: 30`, is the intentional signal that a reindex holds the per-collection ingest lock. Honour `Retry-After` and retry; ingest to a *different* collection is unaffected. Treat a sustained store-busy window like a stuck reindex (above).

**Triage**:

1. Confirm only one server is running: `pgrep -af archon-search`. Two processes against one `db_path` is a hard fault — stop the older one.
2. Check disk and inotify headroom: `df -h` on the partition holding `db_path`.
3. A stale lock file after an unclean shutdown is cleared by a restart. There is no manual lock-clearing tool.
4. As a last resort, restore `search/` from backup — see [40_backup_restore_disaster_recovery.md](40_backup_restore_disaster_recovery.md).

### Search returns HTTP 500 / 503 / 504

**Symptoms**: `POST /search` returns 500, 503, or 504 instead of results.

**Diagnosis** (log strings and telemetry statuses verified against `server/routes_search.py`):

- **HTTP 500** — a pipeline stage failed (embedder, store query, or reranker). The server logs at ERROR with `event_type="search_pipeline_failure"` and message `search pipeline failed: <ExceptionClass>` (full traceback attached). Telemetry entry: `endpoint="search"`, `status="internal_error"`.
- **HTTP 504** — the pipeline call timed out (>30 s). ERROR record with `event_type="search_timeout"`, message `search pipeline timed out`. Telemetry entry: `status="timeout"`.
- **HTTP 503** — collection metadata could not be reached (body `{"detail": "service unavailable: metadata store could not be reached", "code": "metadata_store_error"}`). ERROR message `search: meta lookup failed for collection ...`. **No telemetry entry is emitted** — triage as a store connectivity issue (see LanceDB lock contention above).
- **HTTP 200 + `results: []`** — success, no matches. Not a failure.

**Triage**:

1. Confirm the collection is in the caller's namespace: `GET /collections/`.
2. `GET /status` and `GET /indexing-state` — look for `status: "failed"` or a non-zero `error_count`.
3. Grep the log for `search pipeline failed` and `meta lookup failed`.
4. If telemetry is on, `GET /telemetry/entries` and filter `endpoint="search"` with `status="internal_error"` or `status="timeout"`.
5. Verify the store is reachable and the embedding model is loaded (`GET /ready` → `checks.models`).
6. Restart if pipeline stages are in a bad state.

**Escalation**: if 500s or 504s persist after a restart, file an issue with the full ERROR record, the `GET /status` snapshot, and `archon-search --version`.

### Provider / model validation failing (`/ready`, `/status`)

**Symptoms**: `GET /ready` reports `checks.models: "fail"` or `"warn"`; `GET /status.model_validation` shows `embedder_ok: false` or `reranker_ok: false`, or a populated `provider_warnings` list.

**Triage** (D6, verified against `server/routes_ready.py`):

- The model probe runs in the background at startup and **never blocks boot or raises** — the server accepts requests while the probe is `pending`.
- `checks.models` priority is strict: **FAIL** (an embedder or reranker model could not load) > **WARN** (both loaded, but a provider fallback warning was emitted) > **OK**. `pending` means the probe has not produced a result yet.
- **FAIL** → the configured `embedding_model` / `reranker_model` cannot be loaded (bad name, missing model cache, no network for first download). Fix the model name in `[database]` or pre-warm the fastembed cache, then restart. Search will surface 500s until a model loads.
- **WARN** → models loaded but an LLM provider fell back (e.g. HyDE / RAG Fusion provider unreachable). Read `provider_warnings` for the specific provider and check `[hyde]` / `[rag_fusion]` credentials and reachability.

### Graph search failures

Graph search (`graph_mode`) requires `[graph].enabled = true`. See [60_graph_operations.md](60_graph_operations.md) for building and maintaining graphs.

- **`local` / `global` before communities are built** → `POST /search` returns **422** with `{"code": "graph_communities_not_built", ...}` (`GraphCommunitiesNotBuiltError`). Build communities first: `POST /graph/{collection}/rebuild-communities` or `archon-search graph build-communities <collection> --wait`.
- **`ppr` with no matching entities** → not an error. The PPR walk falls back to a normal hybrid search and returns `ppr_entities_matched: 0` in the response (`pipeline._search_ppr_mode`). If you expected graph-weighted results, the query terms did not match any extracted entity — re-ingest to populate the graph, or broaden the query.
- **Community rebuild job ends `FAILED` with a `leidenalg`/`igraph` install message** → the Leiden libraries are optional and imported lazily inside `community_builder.py`; a missing install surfaces only when a rebuild runs (it never blocks startup). Install the graph extra: `pip install archon-search[graph]`, then re-run the rebuild.
- **Startup WARNING listing orphan graph tables** → after upgrading, pre-namespacing `_archon_graph_{col}_*` tables (single-underscore separator) are orphaned by the namespaced `_archon_graph_{ns}__{col}_*` scheme. The startup scan logs a WARNING naming them (`check_and_warn_legacy_graph_tables`, never raises). Delete them manually once you have confirmed the collections re-graphed correctly.

### Telemetry log explosion

**Symptoms**: disk usage growing under `~/.archon-search/search-logs/`; many JSONL files older than `retention_days`.

**Triage**:

1. Confirm telemetry is on: `GET /telemetry/stats` returns numbers, not `{"enabled": false}`.
2. Confirm `[telemetry].retention_days` (default 30).
3. The pruner runs once immediately when its background task starts, then every 24 h. Restarting triggers another prune. Today's file is intentionally exempt from deletion.
4. To free space immediately: `archon-search stop`, delete the oldest JSONL files by hand (you must exclude today's file yourself — the pruner exemption does not protect manual deletion), then `archon-search start`.
5. If `skipped_lines > 0` on `/telemetry/stats`, a line failed schema validation — inspect the day's file (malformed lines are logged at WARNING).

To turn telemetry off without losing data: set `[telemetry].enabled = false` and restart. Existing JSONL files remain; reader endpoints then return `{"enabled": false}`.

### `export_enabled = true` quietly does nothing

**Symptoms**: operator sets `[telemetry].export_enabled = true` expecting remote shipping; nothing happens.

**Triage**: this is a known gap, not a bug. The config loader logs a warning at startup and coerces the value to `false` (`config.py`); no external transmission occurs. Remove the line or accept the no-op.

### Key file lost or compromised

Key rotation is owned by [70_key_management_and_rotation.md](70_key_management_and_rotation.md) — the summary below is the incident-response path.

**Symptoms**: clients receive `401 Unauthorized`; the key file is gone; or a key has been published.

**Rotate (live, no restart)**:

```bash
archon-search key rotate --grace 60s   # duration string; POST /keys/rotate takes integer grace_seconds
```

1. The new raw token prints to stdout once — record it immediately.
2. **In-flight 401s are expected during rotation.** With `[auth].rotate_grace_seconds > 0` (or a `--grace` window), the old key keeps working until the grace window expires, so clients can be updated without a hard cutover; with grace `0` the old key is rejected at once. Set `[auth].rotate_grace_seconds` to cover your slowest client's update cadence.
3. Update every client (CLI users, MCP integrations, monitoring scraper) before the grace window closes.

**When `ARCHON_SEARCH_API_KEY` env var is set**: `POST /keys/rotate` returns **409** (`Cannot rotate: ARCHON_SEARCH_API_KEY env var is set...`) because the env var always overrides `.search.env` in the running process. Rotate by changing the env var and restarting instead.

`POST /keys/rotate` requires a JSON body (`KeyRotateRequest`; the integer `grace_seconds` field is optional). FastAPI validates the body **before** the env-var check runs, so a plain `curl` with no body returns **422** (`{"loc": ["body"], "msg": "Field required"}`), not the 409 above — send an empty body and `Content-Type: application/json` to reach the env-var check:

```bash
curl -i -X POST http://127.0.0.1:8765/keys/rotate \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{}'
```

**Key file lost entirely**: on next start, `key_manager.py` mints a fresh key and **all existing clients break**. For emergency restoration set `ARCHON_SEARCH_API_KEY=<known-value>` in the service environment and restart — the env var takes precedence over the file. This first-start recreation is the only key path that requires a restart.

### Migration stuck or schema-migration job failing

**Symptoms**: `POST /collections/{name}/migrate` job sits in `RUNNING`, or `GET /collections/{name}/migrations/pending` reports pending migrations after an upgrade.

**Triage**: schema migrations that are not applied at startup must be run explicitly after upgrading (`POST /collections/{name}/migrate`, 202 job — or `archon-search collection migrate`). A stuck migration job is triaged exactly like any stuck job (above): inspect `GET /jobs/{id}`, cancel if hung, fix the underlying cause, re-run. The full upgrade and migration procedure — including which migrations are startup-applied vs operator-run — lives in [100_upgrading.md](100_upgrading.md) and the [../MigrationGuide/](../MigrationGuide/05_data_migration.md).

### Service will not start

- **macOS**: `launchctl load ~/Library/LaunchAgents/com.archon.search.plist`, then read `~/.archon-search/logs/archon-search.log`.
- **Linux**: `systemctl --user status archon-search`, then `journalctl --user -u archon-search -e`.
- **Port already in use**: `lsof -i :8765`; stop the conflicting process or change `[server].port` (there is no `--port` run flag — use the config key or `ARCHON_SEARCH_PORT`).

## Escalation

There is no on-call rotation for a local service. Escalation is reproduction plus an issue containing: the relevant log excerpt, the `GET /status` snapshot, `archon-search --version`, and the reproduction steps.

## Related documents

- [00_index.md](00_index.md) — OperatorGuide table of contents.
- [20_monitoring_and_alerts.md](20_monitoring_and_alerts.md) — what to alert on so these runbooks fire predictably.
- [30_logging.md](30_logging.md) — log locations, formats, and levels.
- [40_backup_restore_disaster_recovery.md](40_backup_restore_disaster_recovery.md) — restore steps referenced from LanceDB lock contention.
- [50_maintenance_and_jobs.md](50_maintenance_and_jobs.md) — job lifecycle, retry knobs, `FAILED_EXPIRED`.
- [60_graph_operations.md](60_graph_operations.md) — building and maintaining graphs and communities.
- [70_key_management_and_rotation.md](70_key_management_and_rotation.md) — full key issuance and rotation procedures.
- [100_upgrading.md](100_upgrading.md) — upgrade and schema-migration procedure.
- [../UserManual/160_troubleshooting.md](../UserManual/160_troubleshooting.md) — user-side troubleshooting overlap.
