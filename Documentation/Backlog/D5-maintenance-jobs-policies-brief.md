# Feature Brief: D5 — Maintenance Jobs and Policies

## Problem

Over time, archon-search collections accumulate stale FTS indexes, orphaned chunks from deleted source files, and failed ingest jobs that never self-heal — with no automated mechanism to detect or remediate any of this, and no operator visibility into collection health beyond raw chunk counts.

## Goal

A configurable in-process maintenance loop runs on a scheduled interval, detects and remediates known degradation patterns (stale FTS, orphaned chunks, failed-ingest retry), and surfaces a per-collection health summary via `GET /status` — so operators can run archon-search for months without manual intervention.

## Users & Context

DevOps engineers and developers running archon-search as a persistent service. They are not actively monitoring the process — they expect it to self-heal within configured bounds. They discover problems through the status API, logs, or slow search results. They interact with maintenance via config TOML and the status endpoint; they rarely trigger it manually.

## Core Flow

1. **Automatic maintenance** — Operator sets `[maintenance] interval_hours = 24` in `archon-search.toml` → in-process `MaintenanceLoop` wakes on interval → iterates all collections → runs each enabled policy → logs actions and outcomes → updates per-collection health state in `.maintenance-state.json`.
2. **FTS compaction** — For each collection, MaintenanceLoop calls `store.optimize_fts(collection)` → incremental FTS update removes deleted rows from the index → no job is created; this is a synchronous in-loop operation (fast, O(delta)).
3. **Orphan chunk cleanup** — MaintenanceLoop calls `store.list_chunks_raw(collection)` → filters chunks whose `source_path` no longer exists on disk → calls `store.delete_document()` for each orphaned source path (batched by source path, not by chunk) → calls `store.optimize_fts()` after deletions → logs count of orphaned chunks removed.
4. **Failed-ingest retry** — MaintenanceLoop reads `JobStore.list_jobs(status=FAILED, kind=ingest)` → filters jobs younger than `retry_max_age_hours` and with fewer than `retry_max_attempts` attempts → re-enqueues each as a new `IngestJob` with `source="maintenance"` → records retry lineage (original `job_id` → new `job_id`) in `.maintenance-state.json`.
5. **Health summary** — `GET /status` gains a `maintenance` object with `enabled`, `last_run_at`, `next_run_at`, and `collection_health` — one entry per collection with `fts_optimized_at`, `orphans_removed`, `last_retry_at`, and `integrity_ok`.
6. **Manual trigger** — Operator calls `POST /maintenance/trigger` → MaintenanceLoop runs one immediate pass on all collections → returns `202` with a summary of actions taken (non-blocking; result visible in `GET /status`).
7. **CLI status** — `archon-search maintenance status` prints last-run time, next scheduled run, and per-collection health table. `archon-search maintenance run` triggers an immediate pass (blocking with `--wait`, non-blocking by default).

## In Scope

- `[maintenance]` TOML config section — `interval_hours` (int, 0 = disabled), `fts_optimize` (bool, default true), `orphan_cleanup` (bool, default true), `failed_ingest_retry` (bool, default true), `retry_max_attempts` (int, default 3), `retry_max_age_hours` (int, default 72), `exclude` (list of `{namespace}/{collection}` or `{collection}` patterns matching the existing backup exclude pattern).
- `MaintenanceConfig` dataclass in `config.py` following the `BackupConfig` pattern.
- In-process `MaintenanceLoop` background task in `archon_search/jobs/maintenance_loop.py`, following the `BackupLoop` two-loop pattern: a **trigger loop** that wakes every `interval_hours` and a **completion loop** that is not needed in v1 (all maintenance operations are synchronous/in-loop) — so `MaintenanceLoop` uses a single loop, unlike `BackupLoop`.
- `MaintenanceLoop` instantiated in FastAPI `lifespan` handler (same pattern as `BackupLoop`), stored on `app.state.maintenance_loop`.
- FTS optimize: per-collection call to `store.optimize_fts(collection)` inside the maintenance pass. Skips collections where `optimize_fts` raises `FTSIndexNotFoundError` (logs WARNING, not ERROR — expected for empty collections).
- Orphan chunk cleanup: per-collection scan of `store.list_chunks_raw(collection)`, de-duplicate by `source_path`, check `Path(source_path).exists()` for each, call `store.delete_document(collection, source_path)` for missing paths, call `store.optimize_fts(collection)` after any deletions. Skips collections managed by the file watcher (see Open Questions).
- Failed-ingest retry: query `JobStore` for FAILED IngestJobs with `created_at > now - retry_max_age_hours` and `retry_count < retry_max_attempts`. Re-enqueue via `_pipeline.ingest_file(...)` or equivalent — the same path used by the original ingest. Record original `job_id` in the new job's metadata for traceability. Increment a `retry_count` field on the job (or track in `.maintenance-state.json`).
- `.maintenance-state.json` state file at `get_data_dir() / ".maintenance-state.json"` — JSON object with `last_run_at` (ISO-8601), `next_run_at` (ISO-8601), and `collection_health: { "{namespace}/{collection}": { fts_optimized_at, orphans_removed_last_run, last_retry_at, error } }`. Written atomically after each maintenance pass.
- `POST /maintenance/trigger` REST endpoint in new `routes_maintenance.py` — triggers an immediate maintenance pass, returns `202` with `{ "status": "triggered" }`. Pass runs asynchronously; result visible in `GET /status`.
- `GET /status` extended with a `maintenance` object (parallel to the existing `backup` object): `enabled`, `interval_hours`, `last_run_at`, `next_run_at`, `collection_health` (list, namespace-scoped to caller).
- `MaintenanceConfig` added to `StatusResponse` schema in `schemas.py`.
- `archon-search maintenance status` CLI subcommand (reads `.maintenance-state.json` + optionally calls `GET /status`).
- `archon-search maintenance run` CLI subcommand (calls `POST /maintenance/trigger`, `--wait` polls until `last_run_at` updates).
- `archon-search.toml.example` updated with `[maintenance]` section.
- CLAUDE.md, API reference (`Architecture/600_api_reference_or_public_interface.md`), and architecture docs updated.
- `BREAKING.md` entry if `GET /status` schema change is breaking (it is additive, so likely not, but must be documented).

## Out of Scope

- **Integrity checks (hash/doc-count verification)** — The roadmap mentions "periodic integrity checks." This is deferred: it requires defining what integrity means (doc count matches manifest? chunk hashes match source files?), has no existing infrastructure, and is the highest-complexity item in the set. Defer to D5.1.
- **LanceDB vector table compaction (non-FTS)** — LanceDB does not expose an explicit vector-table compact/vacuum at the Python level beyond `optimize()` on the FTS index. No additional compaction is possible in v1.
- **Stale-collection detection as a distinct feature** — "Stale" is handled implicitly: FTS optimize and orphan cleanup address the root causes. A separate "stale collection" warning in the status output is deferred (covered by Open Questions).
- **MCP tools for maintenance** — REST and CLI coverage is sufficient for v1. MCP deferred.
- **Per-collection maintenance schedule overrides** — Global config + exclude list is the v1 model, matching the backup pattern.
- **Retry for ReindexJob, ExportJob, ImportJob** — Only IngestJob retry in v1. Other job kinds have `POST /jobs/{job_id}/resume` already.
- **Orphan cleanup for watcher-managed collections** — Watcher already handles sync for watched directories. Maintenance loop must skip collections that have an active watcher to avoid conflicts (see Open Questions).
- **Notification hooks on maintenance actions** — No webhooks or external alerting in v1.

## Key Decisions

- **Single trigger loop, no completion loop**: Unlike BackupLoop, maintenance operations are all synchronous within the loop (FTS optimize, orphan scan, job re-enqueue). No async jobs are dispatched, so there is nothing to poll for completion. `MaintenanceLoop` uses one loop, not two.
- **Orphan detection via `Path.exists()`, not manifest**: The sync manifest tracks collections, not individual files. Chunk-level orphan detection must check `source_path` existence directly. This is safe for local filesystem paths but will always flag remote URLs as non-existent — skip orphan cleanup for chunks where `source_path` is a URL (starts with `http://` or `https://`).
- **Failed-ingest retry via new IngestJob, not resume**: `POST /jobs/{job_id}/resume` exists for ExportJob/ImportJob only (checkpoint-based). IngestJob has no checkpoint mechanism. Retry creates a fresh IngestJob, preserving the original failed job for audit history. Retry count is tracked in `.maintenance-state.json` keyed by original file path, not job ID — so retries across server restarts are bounded correctly.
- **Maintenance runs at lower priority than all user operations**: Maintenance operations (FTS optimize, orphan delete) must acquire the per-collection lock (`store._collection_locks`) the same way ingest does. If a collection is locked, MaintenanceLoop skips it on this pass and retries next interval. This prevents contention with live ingest.
- **`source = "maintenance"` on retried IngestJobs**: Extends the existing `source` field pattern (`"user"`, `"backup"`) for consistent job provenance tracking in `GET /jobs`.
- **`interval_hours = 0` disables the loop entirely**: MaintenanceLoop exits immediately after instantiation. `POST /maintenance/trigger` still works when the loop is disabled — it runs one pass on demand.
- **Exclude list matches the backup pattern exactly**: `{namespace}/{collection}` for exact match, bare `{collection}` for all-namespace match. Same code path as backup exclusion.

## Edge Cases & Constraints

- **`optimize_fts()` raises `FTSIndexNotFoundError`**: log WARNING, skip FTS optimize for that collection this pass. Expected for empty collections or collections that have never been searched.
- **Orphan scan on large collection**: `list_chunks_raw()` does a full table scan. With 1M+ chunks, this could be slow. MaintenanceLoop should log elapsed time. If > 60 seconds, log a WARNING suggesting the operator increase `interval_hours` or reduce collection size.
- **`source_path` is a URL**: Skip `Path.exists()` check. Log at DEBUG level. No orphan deletion.
- **Collection locked by active ingest during maintenance pass**: skip the collection, log at DEBUG level, no retry until next scheduled pass.
- **Retried IngestJob fails again**: retry count in `.maintenance-state.json` is incremented. After `retry_max_attempts`, the job is no longer retried and is logged at WARNING with the original file path and failure reason.
- **`retry_max_age_hours` is 0**: retry is effectively disabled (no FAILED jobs are young enough to qualify) — same as setting `failed_ingest_retry = false`. Config loader emits WARNING.
- **MaintenanceLoop trigger while a previous pass is still running**: single-loop design naturally prevents overlap — the loop sleeps only after completing the full pass. Long passes delay the next scheduled run; they do not overlap.
- **`POST /maintenance/trigger` called while loop pass is in progress**: returns `202` and the in-progress pass completes normally. The trigger does not interrupt or restart the current pass.
- **Server restart mid-maintenance pass**: orphan deletions already committed to the store are permanent. FTS optimize is idempotent. Retry jobs already created will run normally. No rollback needed.
- **All collections excluded**: MaintenanceLoop runs, logs INFO "no eligible collections", writes `last_run_at`, sleeps until next interval.
- **`.maintenance-state.json` missing or corrupt on startup**: MaintenanceLoop initializes a fresh state (no history). No error. First pass runs on schedule.

## Open Questions

- **Should orphan cleanup skip watcher-managed collections entirely?** The file watcher in `watcher.py` already handles sync for watched directories — but it debounces events and may lag. If the watcher missed a deletion (e.g., server was down), maintenance orphan cleanup would be the recovery path. Proposal: run orphan cleanup even on watcher-managed collections, but log the overlap at DEBUG so operators can see if it's firing frequently (which would indicate a watcher issue). Alternative: skip watcher-managed collections entirely and document this as a watcher responsibility. Needs decision before implementation.
- **Stale-collection detection — define "stale"**: The roadmap includes this, but the codebase already has `last_indexed`, `mutations_since_recompute`, and `described_at_doc_count`. Should D5 add a stale-collection WARNING to `GET /status` (e.g., "collection not indexed in 30 days" or "centroid drift > threshold")? Or is stale detection implicit in the FTS/orphan remediation? Proposal: add a `stale_threshold_days` config field in v1 and surface a `stale: true` flag per collection in the health summary, with no automated action — just visibility.
- **Retry count tracking — job metadata vs state file**: Should `retry_count` live as a field on the `IngestJob` model (requires job schema change) or in `.maintenance-state.json` keyed by source file path? The state-file approach survives job eviction (jobs are evicted after 7 days) but couples retry tracking to file paths, not job IDs. Job metadata approach is cleaner but requires a schema migration on the jobs JSON. Needs decision.
- **Integrity check scope for D5 vs D5.1**: The roadmap says "periodic integrity checks." Should v1 include a lightweight check (e.g., `chunk_count` in `CollectionMeta` matches `store.count_chunks()`) surfaced in the health summary? Or defer all integrity checks to a follow-up? Proposal: include a single lightweight check — metadata chunk count vs actual LanceDB row count — as it is O(1) and already has the primitives. Anything requiring file hash verification is deferred.
- **`retry_max_attempts` semantics — per-file or per-job?**: If the same file fails ingest 3 times (across 3 restart cycles), each as a new job, does `retry_max_attempts = 3` mean 3 total maintenance-triggered retries, or 3 per session? Keying on source file path in `.maintenance-state.json` gives per-file total across sessions, which is safer — it prevents indefinite retry of a permanently broken file. This is the recommended approach but should be confirmed.

## Future Iterations

- Integrity checks: re-open collection, validate doc count vs manifest, optionally re-hash source files against indexed content.
- Stale-collection automated remediation: trigger a reindex if `mutations_since_recompute` exceeds a configurable threshold.
- MCP tools: `trigger_maintenance()` and `get_maintenance_status()`.
- Per-collection maintenance schedule overrides.
- Notification hooks: webhook or log-sink on maintenance completion with summary payload.
- Disk-space pre-flight: warn if available disk < estimated orphan-cleanup savings.
- Dedicated `max_concurrent_maintenance` setting for multi-collection parallelism within a pass.

## Recommendation

Build this now, but scope it tightly to three concrete operations: FTS optimize, orphan chunk cleanup, and failed-ingest retry. These three cover 90% of the real operational pain and all have existing primitives in the codebase — no new store abstractions needed. The hardest design choice is orphan-vs-watcher interaction and retry-count tracking; resolve those in the Open Questions before implementation starts. Do not slip integrity checks into v1 — "periodic integrity checks" sounds simple but requires defining correctness criteria, and getting that wrong is worse than deferring it. The `MaintenanceLoop` pattern is a direct simplification of `BackupLoop` (single loop, no completion polling) and will take a fraction of the implementation effort.
