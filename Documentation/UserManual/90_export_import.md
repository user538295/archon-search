**Purpose**: Move a collection between instances (or back it up) as a portable `.tar.gz` archive.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-07-29 / **Next review**: 2027-07-29

# Export and import

## What export/import does

`archon-search` can package a single collection — its chunks, vectors, per-chunk
metadata, and ACLs — into one self-contained `.tar.gz` archive on the **server's local
disk**, then unpack it back into the same instance or a different one. Both operations
run as background jobs: the REST call returns `202` immediately with a `job_id`, and you
track progress via the jobs surface (see [Tracking with `--wait` / `jobs status`](#tracking-a-job)).

Use it to:

- **Migrate** a collection between environments (e.g. prod → dev, or laptop → server).
- **Share a corpus** with another team by handing over one archive file.
- **Seed a new instance** from a known-good collection instead of re-ingesting from source.
- **Back up** an individual collection out-of-band from the server-wide backup loop
  (for whole-instance disaster recovery, see the operator guide linked below).

### What's in the archive

Each `.tar.gz` contains exactly two members:

| Member | Contents |
|---|---|
| `manifest.json` | `archon_search_version`, `schema_version`, `collection`, `exported_at`, `doc_count`, `active_embedding_model`, `description`, `lancedb_version` |
| `documents.jsonl` | One JSON object per chunk; vectors are base64-encoded little-endian float32 arrays |

The **centroid is not exported** — the import job recomputes collection metadata
(centroid + description embedding) as its final step. `source_path` values are preserved
as-is; on a different machine they may point at paths that no longer exist. That is
accepted — `source_path` is metadata, not a live reference. To restore live path
references (for `search_with_context` or the watcher), re-ingest from the target
filesystem after import.

### Constraints to know before you start

- **Server-local paths only.** Both the export output directory and the import source
  path must resolve **inside the server's data directory** (`$ARCHON_SEARCH_DATA_DIR`,
  default `~/.archon-search/`). Paths outside it are rejected with `400`. There is no
  multipart upload — the operator running the server needs disk access.
- **One collection per job.** To move several collections, run several jobs.
- **Embedding-model match is mandatory.** Import rejects with `422` if the archive's
  `active_embedding_model` differs from the target server's configured model. This check
  **cannot be bypassed** — vector spaces are not interchangeable.
- **Schema-version match** is checked too, but can be bypassed with
  `ignore_schema_version` when you accept the risk.

## CLI

Both commands are HTTP proxies to a running server, so `archon-search serve` (or `start`)
must be up. They accept `--api-url` (default `http://localhost:8765`) and `--api-key`
(falls back to `ARCHON_SEARCH_API_KEY` or the key file).

### Export

```
archon-search export COLLECTION [--output-dir PATH] [--wait/--no-wait]
                                [--timeout SECONDS] [--api-url URL] [--api-key KEY]
```

| Option | Default | Meaning |
|---|---|---|
| `--output-dir PATH` | server data dir `/exports` | Directory to write the archive into. |
| `--wait / --no-wait` | `--no-wait` | Poll until the job is terminal and print progress. |
| `--timeout SECONDS` | `300` | Max seconds to wait (only with `--wait`). On timeout the CLI exits `0` and prints a recovery hint — the job keeps running on the server. |

Without `--wait`, the command prints the `job_id` and exits `0` immediately.

### Import

```
archon-search import COLLECTION PATH [--force-overwrite/--no-force-overwrite]
                                     [--ignore-schema-version/--no-ignore-schema-version]
                                     [--on-error fail|skip] [--wait/--no-wait]
                                     [--api-url URL] [--api-key KEY]
```

| Option | Default | Meaning |
|---|---|---|
| `--force-overwrite` | off | Drop all existing documents and re-import. Without it, importing into an existing collection fails with `409`. |
| `--ignore-schema-version` | off | Import even if the archive's `schema_version` differs. Does **not** bypass the embedding-model check. |
| `--on-error fail\|skip` | `fail` | `fail` aborts at the first corrupt JSONL line; `skip` logs it and continues, surfacing the count in the result. |
| `--wait / --no-wait` | `--no-wait` | Poll until terminal and print progress + `imported/skipped/total`. |

`PATH` is the absolute path to the `.tar.gz` **on the server**. These three flags are
deliberately independent — `force_overwrite`, `ignore_schema_version`, and `on_error`
are orthogonal safety decisions.

### Worked example: migrate `handbook` from prod to dev

On the **prod** server, export and wait for the archive path:

```bash
archon-search export handbook --output-dir /data/exports --wait \
  --api-url https://prod.internal:8765 --api-key "$PROD_KEY"
# Export job submitted: 5f3c...
# [reading] 0/0
# [writing] 4200/8600
# [packaging] 8600/8600
# Done. Archive: /data/exports/handbook-20260729T101500Z.tar.gz
```

Copy the archive into the dev server's data directory (e.g. `scp` into
`/data/exports/`), then import it there:

```bash
archon-search import handbook /data/exports/handbook-20260729T101500Z.tar.gz --wait \
  --api-url http://dev.internal:8765 --api-key "$DEV_KEY"
# Import job submitted: 9a1b...
# [validating] 0/8600
# [ingesting] 8600/8600
# [indexing] 8600/8600
# Done. imported=8600, skipped=0, total=8600
```

## REST

Both endpoints require a `Bearer` token and return `202` with a `JobResponse`
(`status: QUEUED`). The archive path is not accepted from the caller on export — the
server computes `<output_dir>/<collection>-<timestamp>.tar.gz`.

**Export** — `POST /collections/{name}/export`

```bash
curl -X POST https://host:8765/collections/handbook/export \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"output_path": "/data/exports"}'
# 202 {"job_id": "5f3c...", "status": "QUEUED", ...}
```

`output_path` is optional (defaults to `<data_dir>/exports`). A path outside the data
dir returns `400`; an unknown collection returns `404`.

**Import** — `POST /collections/{name}/import`

```bash
curl -X POST https://host:8765/collections/handbook/import \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"path": "/data/exports/handbook-20260729T101500Z.tar.gz",
       "force_overwrite": false, "ignore_schema_version": false, "on_error": "fail"}'
```

Import status codes: `400` (path outside allowlist), `409` (collection exists and
`force_overwrite` is false), `422` (archive missing/corrupt, embedding-model mismatch, or
schema-version mismatch). On success the job's `result` carries
`{"imported", "skipped", "total_in_archive"}`.

For the exhaustive request/response fields, `GET /openapi.json` is authoritative.

## MCP

The MCP surface mirrors REST with two non-blocking tools that return the QUEUED job dict:

- `export_collection(collection, output_path="")` — empty `output_path` uses the server
  default (`<data_dir>/exports`).
- `import_collection(collection, path, force_overwrite=False, ignore_schema_version=False, on_error="fail")`

Both apply the same path-safety, embedding-model, and schema-version checks as REST and
return a structured error (`code` = `path_unsafe`, `not_found`, `collection_exists`,
`embedding_model_mismatch`, …) on rejection. Poll `GET /jobs/{job_id}` for progress.

## Tracking a job

Export and import are **checkpointed, resumable jobs**. Progress is written to the job
record as `{processed, total, phase}` every `[jobs].checkpoint_interval` documents
(default 100). Phases are:

- **Export**: `reading` → `writing` → `packaging`
- **Import**: `validating` → `ingesting` → `indexing`

Because they're bulk jobs, they may sit in `QUEUED` behind another export/import until a
slot frees up (concurrency is capped by `[jobs].max_concurrent_bulk`, default 1). If the
server restarts mid-run, the job becomes `FAILED` but its checkpoint is preserved — call
`POST /jobs/{job_id}/resume` to re-enqueue from the last checkpoint (export re-opens its
`.export-<job_id>.jsonl.tmp` temp file; import skips already-ingested batches).

Track a job without `--wait` using the CLI:

```bash
archon-search jobs status <job_id>
```

See [Jobs and async operations](100_jobs_and_async_operations.md) for the full job
lifecycle, list/filter/resume semantics, and the `QUEUED` scheduler.

## Related documents

- [UserManual index](00_index.md)
- [Jobs and async operations](100_jobs_and_async_operations.md) — job lifecycle, resume, `--wait`
- [Ingestion and collections](50_ingestion_and_collections.md) — building collections from source
- [Backup, restore, disaster recovery](../OperatorGuide/40_backup_restore_disaster_recovery.md) — whole-instance DR vs. per-collection export
- [API reference (REST + MCP + CLI)](../Architecture/600_api_reference_or_public_interface.md)
