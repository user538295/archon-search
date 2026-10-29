**Purpose**: Define what to back up for `archon-search`, how to restore it, and which disaster scenarios the current code does and does not support — covering both the raw file-system snapshot path and the scheduled `.tar.gz` backup loop.
**Audience**: SREs and sysadmins responsible for data durability of `archon-search`.
**Status**: Draft
**Last reviewed**: 2026-10-25
**Next review**: 2027-10-20

# Backup, Restore, and Disaster Recovery

`archon-search` keeps all persistent state under one directory: `~/.archon-search/` (or `$ARCHON_SEARCH_DATA_DIR`). Two backup paths exist:

1. **File-system snapshot** of the runtime directory — the whole-instance recovery contract. Fast, complete, but tied to the machine and the `archon-search`/LanceDB version that produced it.
2. **Scheduled collection backups** (the in-process `BackupLoop`) — per-collection portable `.tar.gz` archives, rotated automatically. These double as the migration/portability primitive; see [Export & import](../UserManual/90_export_import.md).

Disaster recovery for a lost host still reduces to restoring the runtime directory plus the ingest sources it was built from. The `.tar.gz` archives add per-collection restore and cross-instance migration on top.

## Principles

1. **The runtime directory is the database.** Lose `~/.archon-search/` with no backup and the only way back is to re-ingest from source. There is no transactional repair path.
2. **Stop the service before snapshotting.** LanceDB writes and `IndexingStateStore` writes are not crash-isolated against a live file-system snapshot: a snapshot under concurrent writes can capture LanceDB tables mid-write, and an in-flight ingest may not yet be flushed. (`IndexingStateStore` replaces its state file via an atomic `os.replace`, so that one file is never torn at the filesystem level; the residual gap is on-disk durability under power loss.) Scheduled `.tar.gz` backups avoid this — the export worker reads a consistent view per collection — but they cover collection data only, not keys/config.
3. **The API key is part of the backup.** Restoring data without the key file (and `keys.json`) invalidates every client holding the old token. The `.tar.gz` archives do **not** include keys or config.
4. **Archives are opaque and version-tied.** The `.tar.gz` format is not stable across LanceDB versions; each archive records `lancedb_version` in its manifest for forensics, but there is no import-time compatibility enforcement.

## What lives under `~/.archon-search/`

| Path | Content | Backup priority |
| --- | --- | --- |
| `archon-search.toml` | All server configuration (host, port, db_path, collections, `[backup]`, telemetry, namespaces). | Critical. |
| `.search.env` (mode 0600) | `ARCHON_SEARCH_API_KEY=<hex>`. Auto-generated on first start if missing. | Critical — without it, clients lose access. |
| `keys.json` (mode 0600) | Managed multi-key store (`id`, `token_hash`, `namespace`, `admin`, `label`, `expires_at`, `status`). Created on first `archon-search key create/rotate`. | Critical — without it, all managed keys must be reissued. |
| `search/` (default `db_path`) | LanceDB tables (vectors + FTS), per-collection metadata, centroids, graph tables. | Critical. |
| `<db_path>/.indexing_state.json` | Per-collection indexing progress. | Important — recoverable by re-sync, but loses progress. |
| `archon-search-jobs.json` | Async job records. `RUNNING`/`CANCELLING` jobs are marked failed on next start. A corrupt file is **not** repaired automatically — it logs `JobStore: corrupt jobs file … — resetting` at ERROR on every boot (not just once) until fixed; job history is lost but the index, keys, and config are unaffected. Recover by stopping the service, deleting `archon-search-jobs.json`, and restarting. | Low — transient. |
| `archon-search-sync-suppressed.json` | Sticky sentinel for the startup-sync crash-loop guard (`sync_suppression.py`); holds a single `suppressed_at` timestamp. Present whenever the automatic startup sync is suppressed (`GET /status` → `sync_result: "suppressed"`); written on the boot the guard first fires and re-checked on every boot after, so suppression survives a restart. Deleting it **re-enables the startup sync on the next boot** — equivalent to skipping the operator's `POST /sync` resume step, so only delete it once the underlying crash cause is actually fixed. | Low — holds no data beyond one timestamp; deleting it changes startup *behavior*, not index state. |
| `.backup-state.json` | `BackupLoop` state: `{namespace}/{collection} → ISO-8601 last_backup_at`. | Low — rebuildable from disk. |
| `.maintenance-state.json` | `MaintenanceLoop` state. | Low — transient. |
| `backups/` (default `[backup].output_dir`) | Scheduled `.tar.gz` archives under `{namespace}/`. | Depends — a backup *of* backups is usually redundant; keep off-host instead. |
| `exports/<namespace>/` (`<data dir>/exports/`) | Archives written by `archon-search export` / `POST /export`, one directory per namespace (mode 0700, files 0600), named `<collection>-<timestamp>.tar.gz`. A flat `exports/*.tar.gz` file is a pre-upgrade export. | Depends — contains the collection's documents in plain text; move off-host or delete when done. |
| `logs/archon-search.log` | Server log. | Low — operational only. |
| `search-logs/YYYY-MM-DD.jsonl` | Telemetry, one file per UTC day; self-pruning after `[telemetry].retention_days`. | Optional. |

`[backup].output_dir` can be relocated via TOML, and so can `db_path`, `log_file` and `[telemetry].log_dir` — but only while `ARCHON_SEARCH_DATA_DIR` is unset: when it is set (the Docker image, a service installed by `archon-search install`) those three always live under it. If you have moved any of them outside `~/.archon-search/`, back those locations up too. The key file location can be redirected with `ARCHON_SEARCH_KEY_FILE`. Setting `ARCHON_SEARCH_DATA_DIR` relocates the entire tree (see [Data architecture](../Architecture/130_data_architecture_and_persistence.md)).

## Scheduled backups (`BackupLoop`)

When `[backup].interval_hours > 0`, an in-process loop periodically exports **every collection** (per namespace) to a `.tar.gz` archive and rotates old ones. Disabled by default.

### Configuration (`[backup]`)

Verified against `archon_search/config.py` (`BackupConfig`) and `archon-search.toml.example`:

| Key | Default | Meaning |
| --- | --- | --- |
| `interval_hours` | `0` | Backup cadence. `0` disables the periodic trigger loop entirely. |
| `keep` | `7` | Archives retained per collection; older ones are deleted after each successful backup. `0` = never rotate (archives accumulate — unbounded disk growth; the loader logs a WARNING when combined with `interval_hours > 0`). |
| `exclude` | `[]` | Collections to skip. Accepts a bare `{collection}` (matches across all namespaces) or a qualified `{namespace}/{collection}`. |
| `output_dir` | `""` | Archive root. Empty resolves to `~/.archon-search/backups/`. Must be at least 3 path components deep (guards rotation from scanning near-root dirs); a shallower value logs a warning and falls back to the default. |

```toml
[backup]
interval_hours = 24
keep = 7
exclude = ["scratch", "tenants/staging"]
output_dir = "/mnt/nfs/archon-search/backups"
```

Archives are written to `{output_dir}/{namespace}/{collection}.backup.{timestamp}.tar.gz`. The per-namespace subdirectory and the literal `.backup.` separator prevent collisions between namespaces and with manual `export` archives; rotation only deletes files matching `{collection}.backup.*.tar.gz` in that namespace directory.

Behavioral notes:
- On startup, if any collection is overdue (`now - last_backup_at >= interval_hours`), the loop fires an immediate catch-up tick. Backups missed while the server was down are **not** retroactively filled — only the next-due tick fires.
- Backup jobs carry `source="backup"` and sort **behind** any `source="user"` job in the FIFO queue, so a scheduled backup never blocks an interactive ingest/import. With the default `[jobs].max_concurrent_bulk = 1`, N collections drain serially (~N × export time).
- `BackupLoop` covers **all** namespaces automatically. Failed/cancelled backups do not update `last_backup_at` and are retried on the next tick.

### Forcing a backup now

```bash
# CLI (default namespace only — uses the local key file, i.e. the default/admin key)
archon-search backup --now            # non-blocking, prints queued job IDs
archon-search backup --now --wait     # polls each job to completion (--timeout SECONDS, default 300)

# REST (admin key only; the caller's own namespace)
curl -fsS -X POST -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
     http://127.0.0.1:8765/backup/trigger
```

`POST /backup/trigger` needs an **admin** key (the default key, or a managed key created with `admin`); a TOML `[namespaces]` token or a non-admin managed key gets `403`. It returns `202` with `{queued: [...], skipped: [{collection, reason}]}` for the collections of the caller's namespace; skip reasons are `excluded`, `already_active`, `already_queued`, or `enqueue_failed`. The CLI and the default key therefore cover the `default` namespace only. Other namespaces are covered by the scheduled `BackupLoop`, which backs up every namespace on its own; a manual run for one of them needs an admin managed key in that namespace.

`backup --now --wait` exits `0` when every job ends `DONE` (or the `--timeout` elapses with nothing failed or cancelled), `2` when any job ends `FAILED`, `FAILED_EXPIRED` or `CANCELLED`, and `1` on a connection, auth, or HTTP error — see the [CLI reference](../Architecture/600_api_reference_or_public_interface.md#cli-commands).

### Checking backup state

```bash
archon-search backup status          # human-readable; works offline
archon-search backup status --json    # machine-readable, for monitoring checks
```

`backup status` reads `.backup-state.json` and counts archives on disk directly, so it works even when the server is down. When the server is reachable it also merges `last_tick_at` / `next_run_at` from `GET /status` (whose `backup` object carries the same fields). See [Monitoring & alerts](20_monitoring_and_alerts.md).

## Backup procedure (file-system snapshot)

### Cold backup (recommended for whole-instance recovery)

```bash
# 1. Stop the service so writers are quiescent.
archon-search stop

# 2. Snapshot the runtime directory.
DEST="/backups/archon-search/$(date -u +%Y-%m-%dT%H-%M-%SZ)"
mkdir -p "$DEST"
cp -a ~/.archon-search "$DEST/"

# 3. Verify mode 0600 on the key file survived the copy.
ls -l "$DEST/.archon-search/.search.env"

# 4. Restart.
archon-search start
```

`cp -a` (BSD/GNU) preserves modes, timestamps, and symlinks. `rsync -aH --delete` works equally well for repeat backups. Tarballs are fine for transport; gzip compresses LanceDB column files only modestly.

### Hot backup (best-effort)

If you cannot stop the service:

1. Prefer the scheduled `.tar.gz` path — `archon-search backup --now` gives a per-collection consistent export without stopping writers (keys and config still need a separate copy).
2. If you must snapshot the directory live, avoid `POST /ingest`, `POST /collections`, and reindex/migrate calls during the window. You may capture a partially-updated `.indexing_state.json`; on restore, run `archon-search sync` and accept that one in-flight ingest may need re-running.
3. LanceDB table snapshots taken under concurrent writes are not guaranteed consistent — the resulting backup may fail to open. Cold snapshots are the only high-confidence file-system method.

### What to retain

Daily for 7 days plus weekly for 4 weeks is a reasonable starting point; tune to your recovery-point objective. Re-ingestion from source is always a fallback — backups exist to avoid the recompute cost (centroids are recomputed O(chunks)). Telemetry JSONL self-prunes and rarely needs backing up.

## Restore procedure

### Whole instance (file-system)

```bash
# 1. Stop the service.
archon-search stop

# 2. Move the broken state aside (do not delete — see verification step).
mv ~/.archon-search ~/.archon-search.broken

# 3. Restore from backup.
cp -a /backups/archon-search/<timestamp>/.archon-search ~/

# 4. Re-tighten permissions on the key file (cp -a should preserve, but verify).
chmod 600 ~/.archon-search/.search.env
chmod 600 ~/.archon-search/keys.json   # if present

# 5. Start and verify.
archon-search start
curl -fsS http://127.0.0.1:8765/health
curl -fsS -H "Authorization: Bearer ${ARCHON_SEARCH_API_KEY}" \
     http://127.0.0.1:8765/status | jq '.collections[] | {name, status, error_count}'
```

If `/status` shows collections in `failed` or stale `in_progress` states, run `archon-search sync` (admin key) to reconcile.

Verification checklist:
- `/health` returns 200.
- `/status` lists the expected collections with `status: done` or `not_yet_indexed`.
- A known-good search returns non-empty results (pipeline failures surface as HTTP 500/504, not silent empty results).
- `[telemetry].enabled = true` deployments: `GET /telemetry/stats` (admin key) returns numbers, not `{"enabled": false}`.

### Single collection (from a `.tar.gz` archive)

For per-collection restore, cross-instance migration, or recovering one collection without touching the rest, import a scheduled backup or manual export archive:

```bash
archon-search import <collection> /mnt/nfs/archon-search/backups/default/<collection>.backup.<timestamp>.tar.gz --wait
```

An import reads an archive only from the caller's own directories: `<data dir>/exports/<namespace>/` and `<[backup].output_dir>/<namespace>/`. Any other path, whether a file exists there or not, answers `400 outside_allowed_dirs` (the admin key has no cross-namespace access, because the manifest binds an archive to the namespace that exported it, and a manifest naming another namespace answers `422 archive_namespace_mismatch`). To restore an archive from elsewhere, copy it into the right directory first (`install -m 0600 <file> ~/.archon-search/exports/<namespace>/`). `output_path` no longer exists on export (REST, MCP, CLI): the archive always lands in `exports/<namespace>/`, and `archon-search export` has no `--output-dir` flag.

**Pre-upgrade archives.** An archive written before manifests carried a `namespace` is accepted only from the caller's own export or backup directory, so scheduled backups under `<output_dir>/<namespace>/` made by an older build restore as before. A pre-upgrade file sitting flat in `<data dir>/exports/` is importable only by the `default` namespace; move it into `exports/<namespace>/` for any other namespace. Take a fresh backup before any upgrade regardless (see [Upgrading](100_upgrading.md)).

With `--wait`, import exits `0` on `DONE` or when `--timeout` (default 300 s) elapses with the job still running (check it with `archon-search jobs status <job_id>`), and `2` on `FAILED`, `FAILED_EXPIRED` or `CANCELLED` (a cancelled import may leave a partial collection — re-run the same import command with `--force-overwrite`); for a large archive pass a longer `--timeout`. See [Export & import](../UserManual/90_export_import.md#import).

Import ingests with checkpointing (resumable via `POST /jobs/{id}/resume`). Replacing an existing collection (`--force-overwrite`) is refused — the import ends `FAILED` with `force_overwrite refused: …`, dropping nothing — while another job (an ingest, reindex, import, migration, metadata reindex or community rebuild) may still write that collection; wait for it to finish, then resume the failed import. The full export/import workflow, archive layout, and manifest fields are documented in [Export & import](../UserManual/90_export_import.md).

A collection restored by import is exempt from maintenance orphan cleanup: every imported chunk carries the `_imported` metadata marker, so deleting one of its source files later does not make the orphan sweep remove that document. The marker ends per path once that path is re-ingested from disk on this host, resolves to the archived `doc_id` (typically a same-host restore) and yields chunks. Collection sync re-ingests only new or changed files while its recorded sync state (file mtimes in `.indexing_state.json`) survives and the collection's embedding model and chunk size are unchanged — otherwise it re-ingests every file (`archon_search/sync.py`, `_check_collection_changes`). So when that state survived the restore, an unchanged file needs an explicit `POST /ingest` of the path, or `POST /collections/{name}/reindex` when the collection has a configured source directory. Rows whose paths do not exist here stay until you delete them. See [Imported chunks and maintenance](../UserManual/90_export_import.md#imported-chunks-and-maintenance).

## Disaster scenarios

| Scenario | Recovery |
| --- | --- |
| Lost key file, data intact | Delete `.search.env`; restart — a new key is minted. **All existing clients break** until rekeyed. If the file exists but holds a key that is not at least 32 lowercase hex characters (or cannot be read), the server refuses to start with a `ConfigError` and leaves the file alone — fix or delete it. If `keys.json` is also lost, reissue managed keys via `archon-search key create`. See [Incident runbook](90_incident_runbook.md). |
| Lost data, key file intact | Restore `search/` from a file-system backup, or re-import affected collections from `.tar.gz` archives, or re-ingest from source. The key stays valid. |
| Lost both | Restore from a file-system backup. Without one, re-ingest from source and rotate the key. |
| LanceDB corruption (process won't start) | Restore `search/` from the last cold backup; or drop `search/` and re-import from `.tar.gz` archives / re-ingest. |
| One collection corrupted | Delete and re-import that collection from its latest `.backup.` archive — no need to restore the whole instance. |
| Telemetry directory full | `[telemetry].retention_days` enforces deletion; the pruner runs every 24h from process start. Restart to force a prune. See [Incident runbook](90_incident_runbook.md). |
| Backup disk filling up | Ensure `[backup].keep > 0` (rotation on). `keep = 0` with `interval_hours > 0` accumulates archives without limit — the config loader warns about this. |
| Host loss / migration | Reinstall on the new host (`pip install archon-search`), restore `~/.archon-search/` **or** import `.tar.gz` archives, run `archon-search install` to register the OS service. Re-check `[database].device` suits the new host (`auto` picks CUDA, then Apple MPS, then CPU; an explicit accelerator the host lacks steps down to CPU with a warning); model weights are not in an archive and are downloaded again into `<data dir>/models` on first use; ranking is otherwise unchanged. See [Upgrading](100_upgrading.md). |

## Related documents

- [OperatorGuide index](00_index.md) — folder TOC and reading order.
- [Maintenance & jobs](50_maintenance_and_jobs.md) — the `MaintenanceLoop`, job queue, and async operation model that `BackupLoop` rides on.
- [Incident runbook](90_incident_runbook.md) — triage for the disaster scenarios above.
- [Upgrading](100_upgrading.md) — version upgrades and host migration.
- [Export & import (User Manual)](../UserManual/90_export_import.md) — the `.tar.gz` export/import portability flow used for per-collection restore and migration.
- [Data architecture & persistence](../Architecture/130_data_architecture_and_persistence.md) — on-disk layout details.
