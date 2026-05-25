**Purpose**: Define what to probe, what each `archon-search` endpoint actually tells you, and which alert rules are worth wiring up on a single host.
**Audience**: SREs and sysadmins operating `archon-search` in production.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2027-05-20

# Monitoring and Alerts

`archon-search` exposes four operator-relevant endpoints: `GET /health`, `GET /status`, `GET /indexing-state`, and the `/telemetry/*` pair. They are deliberately narrow — each surfaces one slice of state, and several important conditions are **not** covered. Treat this doc as the contract for what alerting can and cannot rely on. The endpoint inventory in `Architecture/160_operational_readiness_monitoring_and_reliability.md` is the source of truth; this doc adds operator-facing thresholds and known gaps.

## Principles

1. **No SLO is declared.** Latency p50/p95 from `/telemetry/stats` are *regression guards*, not service-level objectives. See `Architecture/160…md` "Reliability targets".
2. **`/health` is a liveness probe, not a readiness probe.** It does not check storage, model warm-status, or watcher health. Tracked as roadmap item `B2` in `Backlog/03_world_class_roadmap.md`. #Unverified (roadmap item ID not re-verified in this pass)
3. **All endpoints except `/health` require Bearer auth.** Monitoring scrapers must be granted a bona-fide API key — there is no scoped read-only credential.
4. **Telemetry is opt-in.** If `[telemetry].enabled = false`, `/telemetry/*` returns `{"enabled": false}` and you have no latency or success-rate data to alert on.

## Endpoint matrix

| Endpoint | Auth | Reflects | Does **not** reflect |
| --- | --- | --- | --- |
| `GET /health` | None | Process is up and accepting HTTP (`routes_health.py`). | Storage connectivity, model load status, watcher state, indexing progress. |
| `GET /status` | Bearer | Service top-level fields `running`, `pid`, `version`, plus per-collection progress: `status`, `processed_files`, `total_files`, `eta_seconds`, `error`, `error_count`, `watching` (`routes_status.py:22-86`); namespace-filtered. The schema also exposes `path`, `doc_count`, `chunk_count` per collection, but the handler hard-codes these to `""`/`0`/`0` (`routes_status.py:67-69`) — do **not** alert on them. | Search latency, recent failures, telemetry health, queue depth. |
| `GET /indexing-state` | Bearer | Raw machine-readable indexing state per collection (`routes_state.py`); same data as `/status` minus the service header. | Anything not in `.indexing_state.json`. Returns `{"collections": {}, "last_updated": null, "trigger": null}` when no state file exists (`routes_state.py:20-21`). |
| `GET /telemetry/stats?since=&until=` | Bearer | `total_queries`, `success_rate`, p50/p95 `latency_ms`, `by_endpoint`, `by_collection`, `error_breakdown`, `skipped_lines` (schema: `schemas_telemetry.py:39-52`; handler: `routes_telemetry.py:22-38`). | Anything when `enabled = false` — body is `{"enabled": false}`. |
| `GET /telemetry/entries?...` | Bearer | Paginated entries with `collection`/`endpoint`/`status`/`error_kind` filters; `limit` capped at 200 (`routes_telemetry.py:41-78`). | The raw query string — by structural invariant (`telemetry/entry.py`). |

### What `/health` does and does not catch

`GET /health` returns `{"status":"running","version":"<vcs version>"}` (`routes_health.py:18-20`). It performs no internal checks. In particular it will return 200 even when:

- LanceDB is unreachable (search will then 503 via `routes_search.py:86-90`).
- The embedder or reranker model has not been loaded yet.
- The watcher loop has died.
- The job-store file is corrupt.
- The telemetry writer queue is dropping entries.

A passing `/health` only proves the HTTP listener is alive. For the readiness gap see roadmap item `B2`. #Unverified (roadmap item ID not re-verified in this pass)

### What `/status` exposes

`/status` is the richest single endpoint. It joins three sources (`routes_status.py:32-79`): (1) `SearchStore.get_all_collections_meta()`, (2) `IndexingStateStore.read()` from `db_path/.indexing_state.json`, and (3) the `[collections]` list in config. The result is namespace-filtered. Field semantics:

- `status`: one of the four `IndexingStatus` enum values `pending`, `in_progress`, `done`, `failed` (`archon_search/progress.py:22-26`), or the synthetic literal `not_yet_indexed` which `/status` writes when no progress entry exists for a known collection (`routes_status.py:71`).
- `processed_files` / `total_files`: monotonic counters reset per ingest. `processed_files < total_files` with `status == "in_progress"` is normal.
- `eta_seconds`: `compute_eta_seconds(cp)` — `None` if not enough samples or already complete.
- `error` / `error_count`: last error message and lifetime count for that collection. A non-null `error` does not necessarily mean the collection is unusable — re-ingest may succeed.

## Suggested alert rules

These are starting thresholds for a single-host deployment. Tune to local noise; none of these correspond to a contractual SLO.

| Severity | Signal | Threshold | Rationale |
| --- | --- | --- | --- |
| Critical | `GET /health` 5xx or connection refused | 2 consecutive failures, 60s apart | Process or supervisor failure. |
| Critical | `GET /status` 5xx | 3 consecutive failures | Auth or storage broken; user search broken too. |
| Warning | `error_count` increases on any collection in `/status` | Δ ≥ 1 per 10 minutes | Ingest path is repeatedly failing for that source. |
| Warning | `status == "in_progress"` with `processed_files` unchanged | Stuck > 30 min | Likely a hung ingest job; see `OperatorGuide/05_incident_runbook.md`. |
| Warning | `/telemetry/stats` `success_rate < 0.95` over last 24h | When telemetry is enabled | Pipeline regression — post-A3 these surface as HTTP 500/504 with `status="internal_error"` or `status="timeout"` telemetry entries; see "Search pipeline failures surface as 5xx" below. |
| Warning | `/telemetry/stats` `latency_ms.p95` regresses ≥ 50% vs. 7d baseline | Rolling window | Slow path; correlate with model load, ingest activity, disk pressure. |
| Warning | `skipped_lines > 0` in `/telemetry/stats` | Any non-zero | Schema-invalid JSONL lines; investigate the day's file under `~/.archon-search/search-logs/`. |
| Info | Telemetry log dir growth > 100 MB/day | Per disk-monitor | Adjust `[telemetry].retention_days` (default 30). |

### Search pipeline failures surface as 5xx (CON-5 resolved in A3)

Pre-A3, `POST /search` returned `200 OK` with `results: []` and `acl_filtered: false` when the pipeline raised — alerting on HTTP status alone would miss the regression. **A3 resolved this**: pipeline stage exceptions now return HTTP 500 (bare re-raise; plain-text `Internal Server Error` body — not JSON), pipeline timeouts return HTTP 504 with `{"detail": "Search timed out"}`. Both paths emit a telemetry entry with `endpoint="search"`, `status="internal_error"` or `status="timeout"` accordingly, and an ERROR-level log record on `archon.search` with `event_type="search_pipeline_failure"` or `event_type="search_timeout"`. Alerts can now key on 5xx rate plus the telemetry `status` field.

Notes:
- The 503 meta-lookup path (`routes_search.py:86-90`) is **unchanged** by A3: it returns `{"detail": "service unavailable"}` and emits **no** telemetry entry (only a logger.error message). Alerts that count "search failures" via `/telemetry/entries` will not see meta-lookup failures — pair with a 503-rate alert sourced from access logs or a reverse proxy.
- `HTTP 200` with `results: []` now unambiguously means the pipeline ran successfully and found no matching documents; it is **not** a failure signal.

See `BREAKING.md` `[next release]` — `POST /search` pipeline-exception behavior, and the resolved CON-5 entry in `Architecture/530_technical_debt_refactoring_roadmap.md`.

## Scraping recipes

### Loopback probe (no auth needed, liveness only)

```bash
curl -fsS http://127.0.0.1:8765/health
```

Exit non-zero on failure. Suitable for `launchd`/`systemd` health checks and for the reverse proxy's upstream probe.

### Status snapshot (auth required)

```bash
curl -fsS -H "Authorization: Bearer ${ARCHON_SEARCH_API_KEY}" \
     http://127.0.0.1:8765/status \
  | jq '{running, version, collections: [.collections[] | {name, status, error_count, eta_seconds}]}'
```

### Telemetry rate-and-latency snapshot

```bash
curl -fsS -H "Authorization: Bearer ${ARCHON_SEARCH_API_KEY}" \
     "http://127.0.0.1:8765/telemetry/stats?since=$(date -u -v-1d +%F)" \
  | jq '{total_queries, success_rate, p50: .latency_ms.p50, p95: .latency_ms.p95, skipped_lines}'
```

If the response is `{"enabled": false}`, telemetry is off in config and no further metrics are available.

## Logs

A single rolling file at `~/.archon-search/logs/archon-search.log` (macOS, confirmed at `platform/macos.py:73`) or `journalctl --user -u archon-search` (Linux #Unverified — `platform/linux.py` not re-verified in this pass). There is no rotation policy in v1 beyond what the OS supplies — accepted risk recorded in `Architecture/140_error_handling_strategy.md`. #Unverified (no Python-side rotation found in cursory reads, but logging config not exhaustively inspected). Structured logs and rotation are tracked as roadmap item `B7`. #Unverified (roadmap item ID not re-verified in this pass)

## Related documents

- `Architecture/160_operational_readiness_monitoring_and_reliability.md` — authoritative endpoint catalogue.
- `Architecture/140_error_handling_strategy.md` — failure taxonomy.
- `OperatorGuide/05_incident_runbook.md` — what to do when an alert fires.
- `Backlog/03_world_class_roadmap.md` `B1`, `B2`, `B7` — observability gaps and planned work. #Unverified (roadmap item IDs not re-verified in this pass)
