**Purpose**: Define what to probe, what each `archon-search` endpoint actually tells you, and which alert rules are worth wiring up on a single host.
**Audience**: SREs and sysadmins operating `archon-search` in production.
**Status**: Draft
**Last reviewed**: 2026-07-29
**Next review**: 2027-07-29

# Monitoring and Alerts

`archon-search` exposes a small set of operator-relevant endpoints: the unauthenticated liveness/readiness pair `GET /health` and `GET /ready`, the authenticated `GET /status` and `GET /indexing-state`, and the `/telemetry/*` pair. Each surfaces one slice of state, and several important conditions are **not** covered. Treat this doc as the contract for what alerting can and cannot rely on. The endpoint inventory in [`../Architecture/160_operational_readiness_monitoring_and_reliability.md`](../Architecture/160_operational_readiness_monitoring_and_reliability.md) is the source of truth; this doc adds operator-facing thresholds and known gaps.

## Principles

1. **No SLO is declared.** Latency p50/p95 from `/telemetry/stats` are *regression guards*, not service-level objectives.
2. **Liveness and readiness are distinct.** `GET /health` proves only that the event loop answers (liveness). `GET /ready` proves that the storage connection is serviceable (readiness). Neither forces a model load. See "Liveness vs readiness" below.
3. **Two endpoints are unauthenticated: `/health` and `/ready`.** Every other endpoint requires a Bearer token — monitoring scrapers must be granted a bona-fide API key. There is no scoped read-only credential. The two unauthenticated probes are deliberately terse and leak no collection names, paths, or counts.
4. **Telemetry is opt-in.** If `[telemetry].enabled = false`, `/telemetry/*` returns `{"enabled": false}` and you have no latency or success-rate data to alert on.

## Endpoint matrix

| Endpoint | Auth | Reflects | Does **not** reflect |
| --- | --- | --- | --- |
| `GET /health` | None | Process is up and accepting HTTP; body is `{"status":"running","version":"<vcs>", "mcp": {...}\|null}` (`routes_health.py`). | Storage connectivity, model warm-status, watcher state, indexing progress, queue depth. |
| `GET /ready` | None | Single gating check — storage connectivity. `200` `{ready:true, checks:{storage:"ok", models:...}}` when the storage ping succeeds; `503` `{ready:false, ...}` when it fails (`routes_ready.py`). `checks.models` is informational only and never changes the status code. | Anything beyond the coarse `storage`/`models` check flags — no names, paths, counts, or queue depth (by design). |
| `GET /status` | Bearer | Richest endpoint. Service fields `running`, `pid`, `version`; per-collection `path`, `doc_count`, `chunk_count`, `status`, `processed_files`, `total_files`, `eta_seconds`, `error`, `error_count`, `watching`; plus the `readiness`, `model_validation`, `backup`, `maintenance`, `telemetry`, `graph`, and other sub-objects (`routes_status.py`); namespace-filtered. | Search latency (see `/telemetry/*`), recent per-query failures. |
| `GET /indexing-state` | Bearer | Raw machine-readable indexing state per collection (`routes_state.py`); the same progress data as `/status` minus the service header and sub-objects. | Anything not in `.indexing_state.json`. Returns `{"collections": {}, "last_updated": null, "trigger": null}` when no state file exists (`routes_state.py:20-21`). |
| `GET /telemetry/stats?since=&until=` | Bearer | `total_queries`, `success_rate`, p50/p95 `latency_ms`, `by_endpoint`, `by_collection`, `error_breakdown`, `skipped_lines`, `truncated_count` (`routes_telemetry.py`). | Anything when `enabled = false` — body is `{"enabled": false}`. |
| `GET /telemetry/entries?...` | Bearer | Paginated entries with `collection`/`endpoint`/`status`/`error_kind` filters; `limit` capped at 200 (`routes_telemetry.py`). | The raw query string — by structural invariant (`telemetry/entry.py`). |

For exhaustive request/response fields, treat `GET /openapi.json` as authoritative rather than enumerating them here.

## Liveness vs readiness

`archon-search` ships a documented liveness-vs-readiness split (feature B2). The two probes answer different questions and both are reachable without the API key.

### `GET /health` — liveness (what it does not catch)

`GET /health` returns the moment the event loop can answer. It performs **no** internal checks, so it returns `200` even when:

- LanceDB is unreachable (search will then fail — see "Search pipeline failures" below).
- The embedder or reranker model has not been loaded yet.
- The watcher loop has died.
- The job-store file is corrupt.
- The telemetry writer queue is dropping entries.

A passing `/health` only proves the HTTP listener is alive. Use it for `launchd`/`systemd`/reverse-proxy liveness and the install-time up-check — never as a readiness gate.

### `GET /ready` — readiness (storage + eager warm-up; everything else informational)

`/ready` runs **two gating checks**: a storage ping (`SearchStore.ping()`, TTL-cached, timeout-guarded) and, only when `[database].eager_load_embedders = true`, whether that eager warm-up is still pending. It returns `200 {ready:true}` once the store is connected and no warm-up is outstanding, and `503 {ready:false}` when the ping fails or times out, or while the warm-up task is still building the ONNX models (`routes_ready.py`). The `503` body is the `ReadinessResponse` model itself, **not** the `{"detail": ...}` error envelope — a 503 here is an expected "not ready yet" signal, not a pipeline error. Distinguish the two causes from the body: `checks.storage: "fail"` is a datastore fault; `checks.storage: "ok"` with `checks.models: "pending"` is just warm-up in progress.

Everything else — model-validation outcome, watcher liveness, index-build state, queue depth — is **reported informationally on `/status`** and does **not** gate `/ready`. In particular:

- Cold models (`embedder_warm: false` / `reranker_warm: false`) are normal right after start and keep `/ready` at `200` on the default lazy-load path (`eager_load_embedders = false`). Reading warm-status never triggers a load (lazy-load contract preserved).
- A `warn`/`fail` model-validation result (`checks.models`) is informational and never flips `/ready` to `503` — the lazy-load contract means a search may still succeed.
- A `failed` collection surfaces only as the `collections_failed` count on `/status`; it never flips `/ready` to `503`.
- A missing/corrupt indexing-state file never gates readiness (search reads LanceDB, not the state file).

Point supervisors and load balancers at `/ready` for "route traffic / restart" decisions — it acts on the status code without holding the API key.

### The rich `readiness` block on `/status`

The authenticated `GET /status` carries a `readiness` sub-object (`ReadinessDetail`, `routes_status.py:177`) with the informational numbers the terse `/ready` body withholds:

- `storage_connected` (bool)
- `embedder_warm` / `reranker_warm` (bool; side-effect-free)
- `jobs.pending` / `jobs.running` (queue-depth counts)
- `collections_indexing` / `collections_failed` (counts derived from indexing state)
- `watcher` — `{running: false}` until a live watcher is wired into the server; when present, `{running: true, watching: [...]}`. Note: the per-collection `watching` flag (`= config.watch`) reflects configured intent and can read `true` while `readiness.watcher.running` is `false`. This is expected, not a bug.

## Provider and model validation surface

A background task validates the configured embedder and reranker (and their ONNX / LLM providers) shortly after startup, without blocking boot (feature D6; `model_validation.py`). An invalid reranker/ONNX provider or an unloadable model surfaces in two places:

- **`GET /ready`** — the informational `checks.models` field: `pending` while validation runs, `ok` when both models load cleanly, `warn` when a provider fallback occurred (e.g. CoreML unavailable → CPU), `fail` when a model could not load (`routes_ready.py:12-32`). This never changes the `200`/`503` status code.
- **`GET /status`** — the `model_validation` sub-object: `{embedder_ok, reranker_ok, provider_warnings: [...], validated_at}` (`routes_status.py:308-323`). While validation is pending, the `*_ok` flags and `validated_at` are `null` (unknown is distinct from failed). `provider_warnings` carries the actionable messages (e.g. `"configured ONNX providers not available: CoreMLExecutionProvider"`).

Alert on `model_validation.reranker_ok == false` or a non-empty `provider_warnings` to catch a GPU/provider misconfiguration before it fails a user query. HyDE / RAG-Fusion LLM provider reachability is reported separately under the `hyde` / `rag_fusion` sub-objects (`key_available`, `provider`).

## Stage-level latency and request-correlation IDs

Feature B1 adds two observability surfaces, controlled by the `[observability]` config section (`config.py:60-62`, `stage_timings_enabled` default `true`, `request_id_header` default `X-Request-ID`):

- **Per-stage timings** (`embed`, `vector`, `fts`, `fuse`, `rerank`, `total`, plus ingest `parse`/`persist`) are emitted as structured log records and surfaced on the `POST /explain` response as `stage_timings_ms`. Use them to answer "which stage got slow?" without a profiler.
- **A request-correlation ID** is minted (or an inbound `X-Request-ID` honored) on every response and threaded into telemetry and logs, so a single request can be traced end-to-end.

These are diagnostics, not an SLA. Detailed field shapes and log-record attributes live in [`30_logging.md`](30_logging.md).

## What `/status` exposes (path and doc-count correctness)

`/status` joins three sources (`routes_status.py:82-113`): (1) `SearchStore.get_all_collections_meta()`, (2) `IndexingStateStore.read()` from `db_path/.indexing_state.json`, and (3) the config-derived collection paths. The result is namespace-filtered. Per-collection field semantics:

- `path`: the config-resolved absolute storage path (e.g. `~/.archon-search/collections/my-docs`), or `""` when the collection has no configured path (ad-hoc-ingested or config-removed). Fixed in feature `2026-07-15-100` — it is no longer a hardcoded placeholder.
- `doc_count`: the cached `meta.doc_count` (fast, may lag a live recount). `chunk_count`: a **live** store count (`count_chunks`); a failure here logs a warning and reports `0` rather than 500-ing the endpoint.
- `status`: one of the `IndexingStatus` values `pending`, `in_progress`, `done`, `failed`, or the synthetic `not_yet_indexed` when no progress entry exists for a known collection (`routes_status.py:157`).
- `processed_files` / `total_files`: monotonic counters reset per ingest. `processed_files < total_files` with `status == "in_progress"` is normal.
- `eta_seconds`: `compute_eta_seconds(cp)` — `None` if not enough samples or already complete.
- `error` / `error_count`: last error message and lifetime count for that collection. A non-null `error` does not necessarily mean the collection is unusable — re-ingest may succeed.

## Suggested alert rules

Starting thresholds for a single-host deployment. Tune to local noise; none correspond to a contractual SLO.

| Severity | Signal | Threshold | Rationale |
| --- | --- | --- | --- |
| Critical | `GET /health` 5xx or connection refused | 2 consecutive failures, 60s apart | Process or supervisor failure. |
| Critical | `GET /ready` 503 | 2 consecutive failures, 60s apart | Storage connection down — search is broken even though the process is live. |
| Critical | `GET /status` 5xx | 3 consecutive failures | Auth or storage broken; user search broken too. |
| Warning | `/status` `model_validation.reranker_ok == false` or non-empty `provider_warnings` | Any | Provider/model misconfiguration; first search will fail or fall back. |
| Warning | `error_count` increases on any collection in `/status` | Δ ≥ 1 per 10 minutes | Ingest path is repeatedly failing for that source. |
| Warning | `status == "in_progress"` with `processed_files` unchanged | Stuck > 30 min | Likely a hung ingest job; see [`90_incident_runbook.md`](90_incident_runbook.md). |
| Warning | `/status` `readiness.jobs.pending` climbing without draining | Sustained growth | Queue backing up; check job workers via [`50_maintenance_and_jobs.md`](50_maintenance_and_jobs.md). |
| Warning | `/telemetry/stats` `success_rate < 0.95` over last 24h | When telemetry is enabled | Pipeline regression — surfaces as HTTP 500/504 with `status="internal_error"`/`"timeout"` telemetry entries; see below. |
| Warning | `/telemetry/stats` `latency_ms.p95` regresses ≥ 50% vs. 7d baseline | Rolling window | Slow path; correlate with model load, ingest activity, disk pressure. |
| Warning | `skipped_lines > 0` in `/telemetry/stats` | Any non-zero | Schema-invalid JSONL lines; investigate the day's file under `~/.archon-search/search-logs/`. |
| Info | Telemetry log dir growth > 100 MB/day | Per disk-monitor | Adjust `[telemetry].retention_days` (default 30). |

### Search pipeline failures surface as 5xx (CON-5 resolved in A3)

Pre-A3, `POST /search` returned `200 OK` with `results: []` when the pipeline raised — alerting on HTTP status alone would miss the regression. **A3 resolved this**: pipeline stage exceptions now return HTTP 500 (bare re-raise; plain-text `Internal Server Error` body — not JSON), and pipeline timeouts return HTTP 504 with `{"detail": "Search timed out"}`. Both paths emit a telemetry entry with `endpoint="search"` and `status="internal_error"` or `status="timeout"`, plus an ERROR-level log record on `archon.search` (`event_type="search_pipeline_failure"` / `"search_timeout"`). Alerts can key on 5xx rate plus the telemetry `status` field.

Notes:
- The 503 meta-lookup path in `routes_search.py` is **unchanged** by A3: it returns `{"detail": "service unavailable"}` and emits **no** telemetry entry (only a `logger.error`). Alerts that count "search failures" via `/telemetry/entries` will not see meta-lookup failures — pair with a 503-rate alert sourced from access logs or a reverse proxy.
- `HTTP 200` with `results: []` now unambiguously means the pipeline ran successfully and found no matching documents; it is **not** a failure signal.

See `BREAKING.md` and the resolved CON-5 entry in [`../Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md).

## Scraping recipes

### Liveness probe (no auth)

```bash
curl -fsS http://127.0.0.1:8765/health
```

Exit non-zero on failure. Suitable for `launchd`/`systemd` health checks and the reverse proxy's upstream probe.

### Readiness probe (no auth)

```bash
curl -fsS http://127.0.0.1:8765/ready
```

`curl -f` exits non-zero on the `503` "not ready" response, so a load balancer or supervisor can gate traffic on it without holding the API key.

### Status snapshot (auth required)

```bash
curl -fsS -H "Authorization: Bearer ${ARCHON_SEARCH_API_KEY}" \
     http://127.0.0.1:8765/status \
  | jq '{running, version,
         readiness: {storage: .readiness.storage_connected,
                     jobs: .readiness.jobs, watcher: .readiness.watcher.running},
         model_validation,
         collections: [.collections[] | {name, path, doc_count, status, error_count, eta_seconds}]}'
```

### Telemetry rate-and-latency snapshot

```bash
curl -fsS -H "Authorization: Bearer ${ARCHON_SEARCH_API_KEY}" \
     "http://127.0.0.1:8765/telemetry/stats?since=$(date -u -v-1d +%F)" \
  | jq '{total_queries, success_rate, p50: .latency_ms.p50, p95: .latency_ms.p95, skipped_lines}'
```

If the response is `{"enabled": false}`, telemetry is off in config and no further metrics are available.

## Logs

The default rolling log lives under `~/.archon-search/logs/` (macOS) or is captured by `journalctl --user -u archon-search` (Linux). Structured records (stage timings, correlation IDs, search-failure events) and the full logging layout are covered in [`30_logging.md`](30_logging.md).

## Related documents

- [`00_index.md`](00_index.md) — OperatorGuide reading order.
- [`30_logging.md`](30_logging.md) — log layout, stage-timing records, and request-correlation IDs.
- [`50_maintenance_and_jobs.md`](50_maintenance_and_jobs.md) — job queue, maintenance loop, and `/status` maintenance sub-object.
- [`60_graph_operations.md`](60_graph_operations.md) — graph status fields and community rebuilds.
- [`90_incident_runbook.md`](90_incident_runbook.md) — what to do when an alert fires.
- [`../UserManual/40_running_the_server.md`](../UserManual/40_running_the_server.md) — starting and running the server.
- [`../Architecture/160_operational_readiness_monitoring_and_reliability.md`](../Architecture/160_operational_readiness_monitoring_and_reliability.md) — authoritative endpoint catalogue and reliability targets.
