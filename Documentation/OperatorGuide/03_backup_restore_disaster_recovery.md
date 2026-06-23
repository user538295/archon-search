**Purpose**: Define what to back up for `archon-search`, how to restore from a backup, and what disaster scenarios the current code does and does not support.
**Audience**: SREs and sysadmins responsible for data durability of `archon-search`.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2027-05-20

# Backup, Restore, and Disaster Recovery

`archon-search` keeps all persistent state under one directory: `~/.archon-search/`. There is no remote sink, no replication, and no export API in v1. Disaster recovery therefore reduces to a file-system backup of that directory plus the ingest sources it was built from. The roadmap item that would replace this with a structured export/import is `D2` (`Backlog/03_world_class_roadmap.md`).

## Principles

1. **The runtime directory is the database.** Lose `~/.archon-search/` and the only way back is to re-ingest from source. There is no schema migration or transactional repair path — see `Architecture/160…md` principle 5.
2. **Stop the service before snapshotting.** LanceDB writes and `IndexingStateStore` writes are not crash-isolated against a live snapshot: a snapshot taken under concurrent writes can capture LanceDB tables mid-write, and an in-flight ingest may not yet be flushed to disk. (`IndexingStateStore.write` itself replaces `.indexing_state.json` via an atomic `os.replace`, so the file is never torn at the filesystem level; A6 also closed the in-process cross-collection consistency race — `CON-3` in `Architecture/530_technical_debt_refactoring_roadmap.md`. The residual gap is on-disk durability under power-loss — an unflushed write can still be lost — tracked under A7/fsync.)
3. **The API key is part of the backup.** Restoring data without the key file invalidates every client that was holding the old token.
4. **There is no built-in export.** `archon-search` has no `export` job kind or `dump` CLI; the roadmap tracks this as `D1`/`D2`. Until then, file-system copy is the contract.

## What lives under `~/.archon-search/`

Verified against the codebase as of 2026-05-20:

| Path | Owner module | Content | Backup priority |
| --- | --- | --- | --- |
| `archon-search.toml` | `config.py:get_default_config_path` | All server configuration (host, port, db_path, collections, telemetry, namespaces). | Critical. |
| `.search.env` (mode 0600) | `key_manager.py:KEY_FILE` | `ARCHON_SEARCH_API_KEY=<hex>`. Auto-generated on first start if missing. | Critical — without this, clients lose access. |
| `keys.json` (mode 0600) | `key_manager.py:KeyStore` | Durable multi-key store: all managed API keys issued via D7 (`id`, `token_hash`, `namespace`, `label`, `expires_at`, `status`). Created on first `archon-search key create/rotate`. | Critical — without this, all managed keys must be reissued. |
| `search/` (default `db_path`) | `store.py` (LanceDB), `progress.py` (state) | LanceDB tables (vectors + FTS), per-collection metadata, centroids. | Critical. |
| `<db_path>/.indexing_state.json` | `progress.py:IndexingStateStore` (`self._state_file = self._state_dir / ".indexing_state.json"`, line 86) | Per-collection indexing progress, last_updated, trigger. Lives inside whatever `db_path` resolves to (default `~/.archon-search/search`). | Important — recoverable by re-sync but loses progress. |
| `archon-search-jobs.json` | `jobs/model.py:get_jobs_file()` (resolves lazily under `get_data_dir() / "archon-search-jobs.json"`; default `~/.archon-search/archon-search-jobs.json`) | Async job records. Crash recovery marks `RUNNING`/`CANCELLING` jobs as failed on next start. | Low — transient. |
| `logs/archon-search.log` | `config.py` (default `log_file = "~/.archon-search/logs/archon-search.log"`) | Server log. | Low — operational only. |
| `search-logs/YYYY-MM-DD.jsonl` | `telemetry/writer.py` | One file per UTC day; pruned after `[telemetry].retention_days`. | Optional — operator decision. |

`db_path`, `log_file`, and `[telemetry].log_dir` can be relocated via TOML; if you have changed them, back those locations up too. The key file location can be redirected with `ARCHON_SEARCH_KEY_FILE`.

## What is **not** currently supported

- **No export/import API.** There is no `/collections/{name}/export`, no `/jobs/import`, no CLI dump. The job kinds in `archon_search/jobs/model.py` cover ingest only. Roadmap item `D2`.
- **No incremental backup primitive.** LanceDB column files are append-mostly but the v1 archive format is not stable across LanceDB versions; treat backups as opaque snapshots tied to the `archon-search` version that produced them.
- **No cross-host migration tool.** Copying `~/.archon-search/` between machines works only if the destination has the same ONNX provider stack (CPU vs CUDA vs METAL — see `platform/runtime.py:SearchRuntime.detect_gpu_type`); ranking is otherwise unchanged.
- **No partial restore.** You restore the whole directory or you re-ingest. Per-collection restore is not implemented.
- **No Windows path support.** The runtime directory layout assumes POSIX (`PLT-1`).

## Backup procedure

### Cold backup (recommended)

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

`cp -a` (BSD/GNU) preserves modes, timestamps, and symlinks. `rsync -aH --delete` works equally well for repeat backups. Tarballs are fine for transport; gzip compresses the LanceDB column files only modestly.

### Hot backup (best-effort)

If you cannot stop the service:

1. Pause ingest by avoiding any `POST /ingest`, `POST /collections`, or `POST /collections/{name}/reindex` calls during the window.
2. Snapshot the directory; you may get a partially-updated `.indexing_state.json` (durability/atomicity gap tracked under A7 — A6 fixed only the in-process consistency race, not on-disk torn-write durability). On restore, run `archon-search sync` and accept that one in-flight ingest may need to be re-run.
3. LanceDB table snapshots taken under concurrent writes are not guaranteed consistent — the resulting backup may fail to open. Cold backups are the only supported method for high-confidence recovery.

### What to retain

Daily for 7 days plus weekly for 4 weeks is a reasonable starting point. Adjust to recovery-point objective:

- Re-ingestion from source is always available as a fallback; backups exist to avoid the recompute cost (`CON-4`: centroid recompute is O(chunks)).
- Telemetry JSONL is independently self-pruning; you do not need to back it up unless you have a downstream audit requirement.

## Restore procedure

```bash
# 1. Stop the service.
archon-search stop

# 2. Move the broken state aside (do not delete — see verification step).
mv ~/.archon-search ~/.archon-search.broken

# 3. Restore from backup.
cp -a /backups/archon-search/<timestamp>/.archon-search ~/

# 4. Re-tighten permissions on the key file (cp -a should preserve, but verify).
chmod 600 ~/.archon-search/.search.env

# 5. Start and verify.
archon-search start
curl -fsS http://127.0.0.1:8765/health
curl -fsS -H "Authorization: Bearer ${ARCHON_SEARCH_API_KEY}" \
     http://127.0.0.1:8765/status | jq '.collections[] | {name, status, error_count}'
```

If `/status` shows collections in `failed` or stale `indexing` states, run `archon-search sync` to reconcile. Verification checklist:

- `/health` returns 200.
- `/status` lists the expected collections with `status: up_to_date` or `not_yet_indexed`.
- A known-good search returns non-empty results (pipeline failures now surface as HTTP 500/504, not silent empty results — `CON-5` resolved in A3).
- `[telemetry].enabled = true` deployments: `GET /telemetry/stats` returns numbers, not `{"enabled": false}`.

## Disaster scenarios

| Scenario | Recovery |
| --- | --- |
| Lost key file but data intact | Delete the file; restart. `key_manager.py:_generate_and_write` mints a new key. **All existing clients break** until rekeyed. See `OperatorGuide/05_incident_runbook.md` "Key file lost". If `keys.json` (managed key store, D7) is also lost, all managed keys must be reissued via `archon-search key create`. |
| Lost data, key file intact | Restore `search/` from backup, or re-ingest from source. The key remains valid. |
| Lost both | Restore from backup. Without a backup, re-ingest from source and rotate the key (clients must update anyway). |
| LanceDB corruption (process won't start) | Restore `search/` from the last cold backup. If unavailable, drop `search/` and re-ingest. Roadmap item `D5` tracks integrity checks. |
| Telemetry directory full | `[telemetry].retention_days` enforces deletion; the pruner runs every 24h from process start (`telemetry/pruner.py`). Restart the service to force a prune. See `OperatorGuide/05_incident_runbook.md` "Telemetry log explosion". |
| Host loss | Reinstall on the new host (`pip install archon-search`; `uv tool install archon-search` is also commonly used #Unverified — not documented in this repo), restore `~/.archon-search/`, run `archon-search install` to register the OS service. Re-verify ONNX providers — `platform/runtime.py:SearchRuntime.detect_gpu_type` distinguishes CUDA / METAL / NONE; whether `archon-search install` itself triggers GPU re-detection at install time is #Unverified. |

## Related documents

- `Architecture/160_operational_readiness_monitoring_and_reliability.md` — install lifecycle, runbooks.
- `Architecture/130_data_architecture_and_persistence.md` — on-disk layout details.
- `Architecture/150_security_and_privacy_architecture.md` — key file permissions, telemetry path leakage.
- `OperatorGuide/05_incident_runbook.md` — triage for the disaster scenarios above.
- `Backlog/03_world_class_roadmap.md` `D1`, `D2`, `D5` — planned export/import and integrity work.
