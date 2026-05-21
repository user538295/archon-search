# Review: OperatorGuide/02_monitoring_and_alerts.md

## Summary

Reviewed `Documentation/OperatorGuide/02_monitoring_and_alerts.md` against
`archon_search/server/routes_health.py`, `routes_status.py`, `routes_state.py`,
`routes_search.py`, `routes_telemetry.py`, `schemas_telemetry.py`,
`archon_search/config.py`, `archon_search/progress.py`, and
`archon_search/platform/macos.py`.

Document is largely accurate. Main defects: the list of `status` enum strings
in the `/status` description is wrong (current enum values are different from
what is documented), the response body shape claimed for an empty
`/indexing-state` is wrong, and one line-range reference is stale.

## Inaccuracies (numbered)

1. **Wrong `status` enum values (line 44).** Doc lists `indexing`,
   `up_to_date`, `failed`, `not_yet_indexed` as values "from the indexing
   pipeline". Actual `IndexingStatus` (`archon_search/progress.py:22-26`) has
   only four members with values `pending`, `in_progress`, `done`, `failed`.
   `not_yet_indexed` is not an enum value at all — it is a synthetic literal
   the `/status` route writes when no progress entry exists for a collection
   (`routes_status.py:71`). `indexing` and `up_to_date` are not produced
   anywhere.

2. **Wrong empty-state response shape for `/indexing-state` (line 24).** Doc
   says it "Returns empty `{}` body when no state file exists." Actual handler
   (`routes_state.py:20-21`) returns
   `IndexingStateResponse(collections={}, last_updated=None, trigger=None)`,
   i.e. `{"collections": {}, "last_updated": null, "trigger": null}` — not
   `{}`.

3. **Stale line-range citation for stats handler (line 25).** Doc cites
   `routes_telemetry.py:22-38` for the stats fields. The handler does occupy
   lines 22-38, but the listed fields (`total_queries`, `success_rate`, p50/p95
   `latency_ms`, `by_endpoint`, `by_collection`, `error_breakdown`,
   `skipped_lines`) live on the `StatsResponse` schema in
   `archon_search/server/schemas_telemetry.py:39-52`, not in the handler file
   at all. The citation points to the wrong file for the field list.

4. **Scraping recipe relies on undocumented `/status` top-level fields (line
   87).** The `jq` filter selects `.running`, `.version`, `.collections[]`.
   `running` and `version` are valid (`StatusResponse` in
   `routes_status.py:81-86` and `schemas.py`), but the doc never names them in
   the "Endpoint matrix" row for `/status` (line 23), which only enumerates
   per-collection progress fields. Not an inaccuracy in the recipe itself, but
   the matrix row is incomplete: `/status` also returns `running`, `pid`,
   `version` at the response top level.

5. **`/status` "Does not reflect" omission (line 23).** Doc says `/status`
   does not reflect "Search latency, recent failures, telemetry health, queue
   depth." Accurate, but it also does not reflect `doc_count`, `chunk_count`,
   or `path` for each collection — those fields are present in the schema but
   the handler hard-codes `path=""`, `doc_count=0`, `chunk_count=0`
   (`routes_status.py:67-69`). Operators may try to alert on
   `doc_count == 0` and be misled. Worth calling out as a gap, not a strict
   inaccuracy.

## Verified claims

- `/health` requires no auth and returns
  `{"status": "running", "version": "<vcs version>"}` — confirmed
  (`routes_health.py:18-20`).
- `/health` performs no internal checks (LanceDB, embedder, reranker, watcher,
  job store, telemetry writer) — confirmed: the handler body is a single
  return, with no I/O.
- `/status` joins `SearchStore.get_all_collections_meta()`,
  `IndexingStateStore.read()`, and the `[collections]` config list, and
  namespace-filters — confirmed (`routes_status.py:32-59`).
- `/status` per-collection fields `status`, `processed_files`, `total_files`,
  `eta_seconds`, `error`, `error_count` — confirmed
  (`routes_status.py:44-50`, `65-78`).
- `eta_seconds` comes from `compute_eta_seconds(cp)` and is `None` when not
  in-progress / insufficient samples — confirmed (`progress.py:159` early
  return when `cp.status != IndexingStatus.IN_PROGRESS`).
- `/indexing-state` data source is `db_path/.indexing_state.json` via
  `state_store.read()`, namespace-filtered — confirmed
  (`routes_state.py:18-32`).
- `/telemetry/stats` returns `{"enabled": false}` when telemetry is disabled
  — confirmed (`routes_telemetry.py:29-30` returns `DisabledResponse()`, and
  `DisabledResponse` in `schemas_telemetry.py:64-65` has only `enabled: bool
  = False`).
- `/telemetry/stats` fields `total_queries`, `success_rate`, p50/p95
  `latency_ms`, `by_endpoint`, `by_collection`, `error_breakdown`,
  `skipped_lines` — confirmed in `StatsResponse`
  (`schemas_telemetry.py:39-52`).
- `/telemetry/entries` filters and `limit` cap of 200 — confirmed
  (`routes_telemetry.py:46-51`, `limit: Annotated[int, Query(ge=1, le=200)] =
  50`).
- All endpoints except `/health` require Bearer auth — consistent with
  responses-declared `401: {"model": ErrorDetail}` on `/status` and
  `/indexing-state`. (Middleware not re-read; claim left as "verified by
  schema annotations" only.)
- Telemetry log directory default `~/.archon-search/search-logs/` —
  confirmed (`config.py:24` `log_dir: str = "~/.archon-search/search-logs"`).
- Telemetry `retention_days` default 30 — confirmed (`config.py:22`).
- macOS log path `~/.archon-search/logs/archon-search.log` — confirmed
  (`platform/macos.py:73`).
- Silent-empty-result behavior in `POST /search`: handler returns 200 with
  `results=[]`, `acl_filtered=False` on exception — confirmed
  (`routes_search.py:82-84`). Line citation in doc is correct.
- 503 via meta-lookup failure path in `routes_search.py:71` — confirmed
  (same file, that exact line returns 503).
- Default server port 8765 (used in the scraping recipes) — confirmed
  (`config.py:31`).

## Unverifiable / ambiguous

- The Linux log location claim (`journalctl --user -u archon-search`) was not
  verified against `platform/linux.py` in this review; only the macOS path was
  checked.
- The doc references roadmap items `B2`, `B7`, `CON-5`, `A4`. Their existence
  in `Backlog/03_world_class_roadmap.md` and
  `Architecture/530_technical_debt_refactoring_roadmap.md` was not verified
  here; the review scope was the endpoint and metric claims.
- "No rotation policy in v1 beyond what the OS supplies" — plausible (no
  Python-side rotation found in the cursory reads done), but the logging
  config was not exhaustively inspected.
- Alert thresholds (line 53 table) are operator suggestions, not derived from
  code, so "accuracy" is not meaningful — only the underlying metric
  references were checked, and those resolve to real fields.
- The bearer-auth claim for `/telemetry/*` is implicit (no `responses=` map
  declaring 401 on those routes); auth is enforced by middleware not read in
  this review.
