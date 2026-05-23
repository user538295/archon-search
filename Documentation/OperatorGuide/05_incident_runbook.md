**Purpose**: Triage steps for the failures that actually occur in production `archon-search` deployments, using only the existing endpoints, logs, and CLI.
**Audience**: SREs and sysadmins responding to an `archon-search` incident.
**Status**: Draft
**Last reviewed**: 2026-05-23
**Next review**: 2027-05-23

# Incident Runbook

This runbook lists the failure modes the codebase is known to produce today, paired with concrete triage steps. The signal surface is narrow (`OperatorGuide/02_monitoring_and_alerts.md`) so many incidents resolve to "read the log, restart the service". For the canonical install-and-supervision context see `Architecture/160_operational_readiness_monitoring_and_reliability.md`.

## Principles

1. **Start with `/health`, then `/status`, then the log.** Three commands cover the first decision branch in nearly every incident.
2. **Restart is a legitimate first step.** The router cache (`CON-2`) and the lack of cache-bust APIs mean restart is the only currently supported recovery for several issues.
3. **Search pipeline failures are now visible as 5xx.** Since A3, `POST /search` returns `504` on timeout and `500` on any other pipeline exception — empty `results` with `200 OK` always means "no hits", not "search broke". Telemetry entries carry `status="timeout"` or `status="internal_error"`.
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

**Symptoms**: `/search` may surface a LanceDB lock/IO condition as `503` (from the meta-lookup branch), `500` (pipeline exception re-raised after A3), or `504` (pipeline timeout); or `archon-search start` fails to connect to the store; or two processes started against the same `db_path`. Check the log for LanceDB lock/IO messages — the ERROR log carries `event_type="search_pipeline_failure"` or `event_type="search_timeout"`.

**Triage**:

1. Confirm only one `archon-search` is running: `pgrep -af archon-search`. Two processes against one `db_path` is a hard fault; stop the older one.
2. Check disk: LanceDB needs free space and inotify capacity. `df -h` on the partition holding `db_path`.
3. If a stale lock file remains after an unclean shutdown, restart resolves it. There is no manual lock-clearing tool. #Unverified (LanceDB lock-file semantics are external to this repo).
4. As a last resort, restore `search/` from backup (`OperatorGuide/03_backup_restore_disaster_recovery.md`).

### Search returns empty (genuine no-hits result)

**Symptoms**: `POST /search` returns `200 OK` with `results: []` and `acl_filtered: false` for queries that previously returned hits. No 5xx.

Since A3, `200` with empty `results` means the pipeline ran without error and found no hits — pipeline failures now surface as `500` or `504` (see [LanceDB lock contention or "table busy"](#lancedb-lock-contention-or-table-busy) above). If you're seeing empty results on a query that used to return hits, triage the data and routing layers rather than the pipeline itself.

**Triage**:

1. Confirm the collection is in the caller's namespace: `GET /collections`.
2. `GET /status` — look for `status: "failed"` or non-zero `error_count` on that collection; a high `error_count` often means a recent ingest partially failed and the index is incomplete.
3. `GET /indexing-state` — check `error` and `error_count` fields.
4. If telemetry is enabled, `GET /telemetry/entries?collection=<name>&endpoint=search&status=ok` — confirm that the result_count is 0, not that the entry is absent (which would indicate the route errored before writing the telemetry entry).
5. Re-run with a known-good query. Persistent emptiness with `200 OK` and no `error_kind` in telemetry indicates either an empty/unindexed collection or a routing miss.
6. If the cause is router staleness after a recent reindex (`CON-2`), restart the service to repopulate `MultiCollectionRouter._cached_metadata` (`router.py:50, :69-70, :124`). This is a private one-shot in-process cache with no public bust API; restart is the only supported recovery. #Unverified (operator-symptom mapping to router staleness is inferential; cache mechanics are verified in source).

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
