**Purpose**: Triage steps for the failures that actually occur in production `archon-search` deployments, using only the existing endpoints, logs, and CLI.
**Audience**: SREs and sysadmins responding to an `archon-search` incident.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2027-05-20

# Incident Runbook

This runbook lists the failure modes the codebase is known to produce today, paired with concrete triage steps. The signal surface is narrow (`OperatorGuide/02_monitoring_and_alerts.md`) so many incidents resolve to "read the log, restart the service". For the canonical install-and-supervision context see `Architecture/160_operational_readiness_monitoring_and_reliability.md`.

## Principles

1. **Start with `/health`, then `/status`, then the log.** Three commands cover the first decision branch in nearly every incident.
2. **Restart is a legitimate first step.** The router cache (`CON-2`) and the lack of cache-bust APIs mean restart is the only currently supported recovery for several issues.
3. **Search pipeline errors surface as 5xx.** `POST /search` returns HTTP 500 on pipeline failure and HTTP 504 on timeout (`CON-5` resolved in A3). HTTP 200 with `results: []` means the pipeline succeeded but found no matching documents — it is not a failure signal.
4. **Re-ingest is the rollback.** There is no transactional repair (`Architecture/160…md` principle 5).

## First-five-minutes checklist

```bash
# 1. Liveness
curl -fsS http://127.0.0.1:8765/health || echo "process down"

# 2. Status (per-collection progress, errors, ETA)
curl -fsS -H "Authorization: Bearer ${ARCHON_SEARCH_API_KEY}" \
     http://127.0.0.1:8765/status | jq

# 3. Logs
tail -n 200 ~/.archon-search/logs/archon-search.log     # macOS
journalctl --user -u archon-search -n 200 --no-pager    # Linux
```

If `/health` is up and `/status` shows no `error_count` increase, look at telemetry (`/telemetry/stats`) and recent ingest activity.

## Failure modes

### Stuck job (ingest or reindex)

**Symptoms**: a collection sits at `status: "indexing"`, `processed_files` stops advancing, `eta_seconds` grows unboundedly, no log entries for the corresponding job for several minutes.

**Triage**:

1. `GET /jobs/{job_id}` for the suspect job (job ids surface in the response of `POST /ingest`, `POST /collections`, `POST /collections/{name}/reindex`).
2. If `status` is `RUNNING` or `PENDING` but log activity has stopped, `DELETE /jobs/{job_id}` — this transitions it to `CANCELLING`. On the next tick the job marks itself `CANCELLED` (`routes_jobs.py:119-157`).
3. If `DELETE` returns `404` or `200` immediately, the job already terminated; check `archon-search-jobs.json` for the final status.
4. Re-issue the ingest. Note: `JobStore` evicts terminal jobs after 7 days (`jobs/store.py:_EVICTION_DAYS`).
5. Process restart marks any `RUNNING`/`CANCELLING` jobs as failed via the crash-recovery path in `JobStore` (`_CRASH_STATUSES`, `jobs/store.py:16, :96`). Use this if the asyncio task itself is unresponsive — note that `DELETE /jobs/{job_id}` only flips the stored status; an unresponsive task will not honour cancellation, so restart is the actual recovery. #Unverified (the "unresponsive task" scenario is inferential).

Underlying causes typically logged in `archon-search.log`: parser failure on a specific document, model-load timeout, disk-full on `db_path`.

### Watcher loop / churn

**Symptoms**: high CPU when no one is searching; `error_count` increasing on a collection without an explicit ingest call; log spam from the watcher.

**Triage**:

1. Identify the watched paths from the `[collections]` block in `~/.archon-search/archon-search.toml`.
2. Check whether an external tool is rewriting files in those paths (editor autosave, indexer, syncthing).
3. Temporary mitigation: set `[collections].watch = false` and restart; trigger ingest manually via `archon-search sync`. There is no event-rate limiter today.
4. If the watcher itself is stuck (no events flowing despite filesystem changes), restart the service. `watcher.py` does not expose a health flag — gap tracked under `B2`.

### Key file lost or compromised

**Symptoms**: clients receive `401 Unauthorized`; or the key file no longer exists; or the key has been published.

**Triage — rotate**:

1. Stop the service: `archon-search stop`.
2. Edit or replace `~/.archon-search/.search.env`, preserving mode `0600`:
   ```bash
   printf 'ARCHON_SEARCH_API_KEY=%s\n' "$(python -c 'import secrets;print(secrets.token_hex(32))')" \
     > ~/.archon-search/.search.env
   chmod 600 ~/.archon-search/.search.env
   ```
3. Start the service: `archon-search start`.
4. Update every client (CLI users, MCP integrations, monitoring scraper).

**Triage — lost**:

1. If `.search.env` is deleted entirely, `key_manager.py:_generate_and_write` mints a new key on next start. **All existing clients break**.
2. Alternative: set `ARCHON_SEARCH_API_KEY=<known-value>` in the service environment and restart — env-var takes precedence over the file (`key_manager.py:25-36`). Useful for emergency restoration.
3. There is no live-reload — every rotation requires a restart. Tracked as `SEC-1` (rotation primitives), roadmap `D7`.

### LanceDB lock contention or "table busy"

**Symptoms**: `/search` may surface a LanceDB lock/IO condition as either a `503` (from the meta-lookup branch) or an `HTTP 500` (pipeline-path exception); or `archon-search start` fails to connect to the store; or two processes started against the same `db_path`. Check the log for LanceDB lock/IO messages — do not rely on the HTTP status alone.

Distinct from the above: `POST /ingest` or `POST /collections/` returning `503` with body `{"error": "store_busy", ...}` and header `Retry-After: 30` is **not** a fault — it is the expected, intentional signal that a reindex currently holds the per-collection ingest lock. The caller should honour `Retry-After` and retry; ingest to a *different* collection is unaffected. Treat a sustained `store_busy` window the same as a stuck reindex (see "Stuck job" above).

**Triage**:

1. Confirm only one `archon-search` is running: `pgrep -af archon-search`. Two processes against one `db_path` is a hard fault; stop the older one.
2. Check disk: LanceDB needs free space and inotify capacity. `df -h` on the partition holding `db_path`.
3. If a stale lock file remains after an unclean shutdown, restart resolves it. There is no manual lock-clearing tool. #Unverified (LanceDB lock-file semantics are external to this repo).
4. As a last resort, restore `search/` from backup (`OperatorGuide/03_backup_restore_disaster_recovery.md`).

### Search returns HTTP 500/503/504 (pipeline failure — `CON-5` resolved in A3; 503 is the unchanged meta-lookup path)

**Symptoms**: `POST /search` returns HTTP 500, HTTP 503, or HTTP 504 instead of expected results; or `/telemetry/entries` shows entries with `endpoint="search"` and `status="internal_error"` or `status="timeout"`. Note: HTTP 503 from the meta-lookup branch produces no telemetry entry and no `event_type` log field — check the raw server log for the `"search: meta lookup failed"` ERROR message instead.

**Diagnosis**:

- HTTP 500 indicates a pipeline stage failed (embedder, store query, or reranker). Look in server logs for a record at ERROR level with `event_type="search_pipeline_failure"` from logger `archon.search`. This record will contain the exception class name and full traceback.
- HTTP 504 indicates the pipeline call timed out (>30 s). Look for a record at ERROR level with `event_type="search_timeout"` from logger `archon.search`.
- The telemetry endpoint `/telemetry/entries` will show entries with `endpoint="search"`, `status="internal_error"` (for pipeline exceptions) or `status="timeout"` (for timeouts).
- HTTP 503 from the meta-lookup branch (`"search: meta lookup failed"` in the log) means collection metadata was unavailable. No telemetry entry is emitted; triage as a store connectivity issue (see "LanceDB lock contention" above).
- HTTP 200 with `results: []` means the pipeline succeeded but found no matching documents — it is not a failure signal.

**Action**:

1. Filter telemetry: `GET /telemetry/entries` and look for `endpoint="search"` + `status="internal_error"` or `status="timeout"`.
2. Check server logs for `event_type="search_pipeline_failure"` or `event_type="search_timeout"` to get exception details and traceback.
3. Verify the store (LanceDB) is healthy and accessible.
4. Verify the embedding model is loaded.
5. Restart the server if pipeline stages are in a bad state.

**Escalation**: if HTTP 500s or 504s persist after a restart, file an issue with the full log excerpt (including the `search_pipeline_failure` or `search_timeout` record), the `GET /status` snapshot, and `archon-search --version`.

### Telemetry log explosion

**Symptoms**: disk usage growing under `~/.archon-search/search-logs/`; `df` warnings; many JSONL files older than `retention_days`.

**Triage**:

1. Confirm telemetry is on: `GET /telemetry/stats` returns numbers, not `{"enabled": false}`.
2. Confirm `[telemetry].retention_days` in config; default is 30.
3. The pruner runs `prune_once()` immediately when its background task starts, then sleeps 24h between subsequent runs (`telemetry/pruner.py:63-70`). A fresh service has already pruned once; restarting triggers another prune (since the in-process schedule is lost), but does not "unblock" a delayed first run. Today's file is intentionally exempt from deletion (`telemetry/pruner.py:44-45`).
4. If you need to free space immediately: `archon-search stop`, delete the oldest JSONL files manually — you must exclude today's file yourself, the pruner's exemption does not protect manual `rm` — then `archon-search start`. The reader tolerates gaps (`routes_telemetry.py` derives the window from `since`/`until`).
5. If `skipped_lines > 0` on `/telemetry/stats`, one or more lines in the day's file failed schema validation. Inspect the relevant file under `~/.archon-search/search-logs/`. Malformed lines are logged at `WARNING` by `TelemetryReader.read_entries`.

To turn telemetry off without losing existing data: set `[telemetry].enabled = false` and restart. Existing JSONL files remain on disk; the reader endpoints will return `{"enabled": false}` regardless.

### `export_enabled = true` quietly does nothing

**Symptoms**: operator sets `[telemetry].export_enabled = true` expecting remote shipping or rejection; nothing happens.

**Triage**: this is the documented `TEL-1` gap. The config loader logs a warning at startup and coerces the value to `false` (`config.py:209-217`). The example TOML (`archon-search.toml.example:67-71`) already describes this no-op behavior. Action: remove the line from config, or accept the no-op. Roadmap fix is queued in `Architecture/530_technical_debt_refactoring_roadmap.md` "Planned refactors".

### Service will not start

See `Architecture/160…md` "Service will not start" for the macOS / Linux specifics. Quick summary:

- macOS: `launchctl load ~/Library/LaunchAgents/com.archon.search.plist` and read `~/.archon-search/logs/archon-search.log`.
- Linux: `systemctl --user status archon-search` then `journalctl --user -u archon-search -e`.
- Port already in use: `lsof -i :8765`; either stop the conflicting process or change `[server].port`.

## Escalation

There is no on-call rotation for a local service. Escalation is reproduction + filing an issue with: log excerpt, `GET /status` snapshot, `archon-search --version`, and the steps to reproduce. Reference the relevant debt ID from `Architecture/530_technical_debt_refactoring_roadmap.md` if applicable.

## Related documents

- `Architecture/160_operational_readiness_monitoring_and_reliability.md` — install / supervision context.
- `Architecture/140_error_handling_strategy.md` — error taxonomy and status-code mapping.
- `OperatorGuide/02_monitoring_and_alerts.md` — what to alert on so these runbooks fire predictably.
- `OperatorGuide/03_backup_restore_disaster_recovery.md` — restore steps referenced from "LanceDB lock contention".
- `Documentation/UserManual/07_troubleshooting.md` — user-side troubleshooting overlap.
