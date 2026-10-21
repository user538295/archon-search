**Purpose**: Explain the in-process maintenance loop, its policies and config, how to force a pass, and how per-collection maintenance health surfaces to operators.
**Audience**: DevOps engineers and SREs running `archon-search` as a persistent service.
**Status**: Draft
**Last reviewed**: 2026-10-21
**Next review**: 2027-07-29

# Maintenance and Jobs

`archon-search` self-heals a handful of known degradation patterns without operator intervention. An in-process `MaintenanceLoop` wakes on a configured interval, walks every non-excluded collection, applies each enabled policy, and records what it did in a per-collection health summary you can read over the API and CLI. You are expected to configure it once and let it run for months.

This guide covers the loop, its `[maintenance]` config, the manual trigger, and where health surfaces. For the general async job model (job kinds, statuses, `GET /jobs`, resume/cancel) see the user-facing [Jobs and async operations](../UserManual/100_jobs_and_async_operations.md).

## What the loop remediates

Each pass runs the enabled policies below. Four run **per non-excluded collection**; failed-ingest retry runs **once per pass** across all namespaces and collections.

| Policy | Config toggle | What it does |
|---|---|---|
| FTS optimize | `fts_optimize` | Incrementally compacts each collection's full-text index, dropping deleted rows. Synchronous, `O(delta)`. A collection with no FTS index yet (empty / never searched) is skipped with a WARNING (`FTSIndexNotFoundError`), not an error. |
| Orphan chunk cleanup | `orphan_cleanup` | Scans chunks, groups them by `source_path`, and deletes documents whose `source_path` no longer exists on disk. URL-sourced chunks and chunks not ingested from a file on this host (inline-ingested or imported) are never deleted — see [Orphan cleanup rules](#orphan-cleanup-rules). Runs an FTS optimize after any deletion. |
| Expired-chunk pruning | `prune_expired_chunks` | Removes chunks whose `expires_at` is in the past (TTL). Requires the E2a schema migration (`POST /collections/{name}/migrate`) before TTL chunks are stored. See [TTL and scoping](../UserManual/130_ttl_and_scoping.md). |
| Graph GC + async community rebuild | `graph_gc` | Prunes stale mention rows, removes orphaned graph nodes (and their edges) and sweeps mention-derived edges whose entities no longer co-occur in any chunk, then (when `[graph].gc_rebuild_communities = true`) enqueues an async per-collection community rebuild. Skipped entirely when `graph_gc = false` or `[graph].enabled = false`. See [Graph operations](60_graph_operations.md). |
| Failed-ingest retry | `failed_ingest_retry` | Re-enqueues eligible FAILED ingest jobs as fresh jobs with `source="maintenance"`; ages out or exhausts the rest to terminal `FAILED_EXPIRED` (see below). |

Maintenance is lower priority than all user work: it takes the same per-collection lock ingest uses, and skips (until the next interval) any collection currently locked by a live ingest. Auxiliary graph writes never fail a pass — they log a WARNING and continue.

## Orphan cleanup rules

This is the authoritative statement of what the orphan sweep deletes (`MaintenanceLoop._run_orphan_cleanup`, `archon_search/jobs/maintenance_loop.py`); other documents summarize it and link here.

- **Skipped paths.** Empty `source_path` values and URLs (`http://` / `https://`) are never treated as orphans.
- **Marked chunks are never deleted.** A chunk not ingested from a file on this host carries a marker in its `metadata` (`NOT_DISK_INGESTED_CHUNK_MARKERS`, `archon_search/constants.py`): `_inline: "true"` on inline-ingested chunks (`POST /ingest` with a `documents` payload) and `_imported: "true"` on chunks written by `import`, whose `source_path` names a file on the exporting host or, after a same-host restore, here. Search filters cannot select on either key.
- **Which `doc_id` a deletion hits.** The sweep deletes through `SearchStore.delete_by_source_path`, which removes only the `doc_id` derived from the path: the SHA-256 of the path as resolved on this host (`store.doc_id_for_source_path`). The same resolved path gives the same `doc_id` on any host, but resolution follows this host's symlinks (e.g. macOS `/tmp` → `/private/tmp`), so an identical absolute path can resolve, and hash, differently on two hosts.
- **A missing `source_path` with no marked chunk:** its document (the `doc_id` derived from the path) is deleted, as before the markers existed.
- **A missing `source_path` whose chunks are all marked:** skipped; nothing is deleted.
- **A missing `source_path` with both marked and unmarked chunks:** only the unmarked rows under the `doc_id` derived from the path are deleted, and none when a marked row has that `doc_id`. Unmarked rows under any other `doc_id` stay.
- **Errors.** A failure on one path — checking whether it exists, deriving its `doc_id`, or deleting it — is logged as WARNING and the sweep continues with the next path.
- **Sync is separate.** The exemption is the sweep's only: a collection sync that tracked a file still deletes its document when the file disappears.

An imported document stays marked until its path is re-ingested from disk under the archived `doc_id` (typically a same-host restore) and that ingest writes chunks; under other paths the imported rows stay, still marked. Collections imported before the `_imported` marker existed carry none and are swept. Re-ingest details, limits and remedies: [Imported chunks and maintenance](../UserManual/90_export_import.md#imported-chunks-and-maintenance).

## Configuration — `[maintenance]`

Defaults verified against `archon_search/config.py` (`MaintenanceConfig`) and `archon-search.toml.example`:

```toml
[maintenance]
interval_hours = 0          # 0 = scheduled loop DISABLED (default). >0 = run every N hours.
fts_optimize = true
orphan_cleanup = true
failed_ingest_retry = true
prune_expired_chunks = true
graph_gc = true
retry_max_attempts = 3      # per source file, per collection, across restarts
retry_max_age_hours = 72    # only retry FAILED jobs created within this window
exclude = []                # e.g. ["scratch", "tenants/staging"]
```

Notes:

- **`interval_hours = 0` disables the scheduled loop entirely** — but `POST /maintenance/trigger` still runs a one-off pass on demand.
- **`exclude`** matches the backup exclude pattern: a bare name (`scratch`) excludes that collection across all namespaces; a qualified `namespace/collection` (`tenants/staging`) excludes exactly one.
- Passes never overlap. The single-loop design sleeps only after a full pass completes; a long pass simply delays the next scheduled run.
- A missing or corrupt `.maintenance-state.json` on startup is not an error — the loop initializes fresh state and the first pass runs on schedule.

## Failed-ingest retry and `FAILED_EXPIRED`

Retry is bounded so a permanently broken file cannot be retried forever. Counts are keyed by `{namespace}/{collection}/{source_path}` in `.maintenance-state.json`, so the bound survives server restarts and job eviction. A source path whose latest job reaches `DONE` has its counter reset, restoring retry eligibility after a fix.

Each pass, for every FAILED ingest job:

1. **Aged out** — created before `now - retry_max_age_hours` → transition to terminal `FAILED_EXPIRED` (never retried again).
2. **Retry exhausted** — retry count `>= retry_max_attempts` (still within the age window) → transition to `FAILED_EXPIRED`.
3. **Eligible** — young enough and under the attempt cap → re-enqueued as a new job with `source="maintenance"`, and the per-file counter is incremented.

`FAILED_EXPIRED` is a terminal status: such jobs are never re-enqueued and surface in `GET /jobs?status=FAILED_EXPIRED`. Setting `retry_max_age_hours = 0` effectively disables retry (no job is ever young enough).

## Forcing an immediate pass

### REST

```bash
curl -s -X POST http://localhost:8765/maintenance/trigger \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY"
```

Returns `202` with `{"status": "triggered"}`, or `{"status": "already_triggered"}` when a trigger is already pending or a pass is running. The trigger is non-blocking and never interrupts an in-progress pass — results appear in `GET /status`. This works even when `interval_hours = 0`.

### CLI

```bash
# One-off pass (non-blocking by default)
archon-search maintenance run

# Block until the pass completes (polls GET /status until last_run_at changes)
archon-search maintenance run --wait --timeout 120

# Current state — offline-capable (reads .maintenance-state.json), merges live GET /status when reachable
archon-search maintenance status
archon-search maintenance status --json
```

`maintenance run --wait` exits `0` on success or timeout and exits `2` if the completed pass reported an error in any collection's `last_error`. On timeout it prints a hint to re-check with `maintenance status`. Both subcommands accept `--api-url` / `--api-key` (falling back to `ARCHON_SEARCH_API_KEY` or the key file).

## Where maintenance health surfaces

`GET /status` carries a `maintenance` object (namespace-scoped to the caller) parallel to `backup`:

- `enabled`, `interval_hours`, `last_run_at`, `next_run_at`
- `expired_chunk_count`, `last_expired_pruned_at`
- `collection_health[]` — one entry per collection with `fts_optimized_at`, `orphans_removed_last_run`, `last_retry_at`, `last_error`, `meta_chunk_count`, `expired_chunks_removed_last_run`, and `communities_invalidated`

`GET /openapi.json` is the authoritative schema. Wire `last_error` (non-null on any collection means the last pass failed there) and a stale `last_run_at` into alerting — see [Monitoring and alerts](20_monitoring_and_alerts.md).

`archon-search maintenance status` renders the same data as a per-collection health table and works offline (from the state file) when the server is down. When the server is reachable it also prints a "Model validation" block (`model_validation` in `--json`) mirroring `GET /status`, showing `pending` until validation completes.

## State file

Loop state persists in `.maintenance-state.json` under the data dir (`~/.archon-search/`, or `$ARCHON_SEARCH_DATA_DIR`). It holds `last_run_at`, `next_run_at`, `collection_health`, per-file `retry_counts`, and expiry/graph-GC timestamps, and is written atomically (temp file + `os.replace`) after each pass. It is safe to delete — the loop rebuilds it and loses only history, not data.

## Related documents

- [OperatorGuide index](00_index.md)
- [Monitoring and alerts](20_monitoring_and_alerts.md)
- [Backup, restore, and disaster recovery](40_backup_restore_disaster_recovery.md)
- [Graph operations](60_graph_operations.md) — graph GC and community rebuild
- [Jobs and async operations](../UserManual/100_jobs_and_async_operations.md) — the user job model
