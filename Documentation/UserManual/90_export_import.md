**Purpose**: Move a collection between instances (or back it up) as a portable `.tar.gz` archive.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-10-20 / **Next review**: 2027-10-20

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
| `documents.jsonl` | One JSON object per chunk — every stored chunk field, including `acl`, `scopes` and the ACL provenance (`acl_source`, `acl_sidecar_path`, `acl_warning`); vectors are base64-encoded little-endian float32 arrays. Archives from older servers lack the provenance fields and import with them unset |

The **centroid is not exported** — the import job recomputes the collection metadata (centroid and counts) from the imported vectors. The manifest's `description` is **not** restored: the imported collection starts without one. Before reporting `DONE` the job warms the embedder serving the collection plus the reranker (best-effort, capped at 180 s), so the first single-collection search after `--wait` returns runs the full hybrid ranking rather than an FTS-only fallback (S288); multi-collection search uses the global embedder, which the server warms at startup. `source_path` values are preserved as-is; on a different machine they may point at paths that no longer exist. That is accepted — `source_path` is metadata, not a live reference. To restore live path references (for `search_with_context` or the watcher), re-ingest from the target filesystem after import — when those paths differ from the archived ones, the imported rows stay beside the re-ingested ones (see [Imported chunks and maintenance](#imported-chunks-and-maintenance)). Every imported chunk is marked `"_imported": "true"` in its `metadata`, so maintenance does not delete it for a missing file — see [Imported chunks and maintenance](#imported-chunks-and-maintenance).

### Constraints to know before you start

- **Server-local paths only.** The export output directory must resolve **inside the
  server's data directory** (`$ARCHON_SEARCH_DATA_DIR`, default `~/.archon-search/`); the
  REST/CLI import source path may also resolve inside the configured
  `[backup].output_dir`, so a scheduled-backup archive written to an off-tree backup root
  stays restorable (the MCP `import_collection` tool does not widen this). Paths
  outside those are rejected with `400`. There is no multipart upload — the operator
  running the server needs disk access.
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
| `--on-error fail\|skip` | `fail` | `fail` aborts at the first bad document; `skip` logs it, drops that one document and continues, surfacing the count in the result. A bad document is a corrupt JSONL line, a `vector` that is missing, empty or not base64-encoded float32, a `vector` with a NaN component (`corrupt doc (vector) at line N: a component is NaN`; LanceDB refuses it for the whole batch, while infinities are stored), a missing required field (`doc_id`, `chunk_id`, `text`, `source_path`, `indexed_at`), a `chunk_id` that is not a string of the form `<64 lowercase hex>-<6 ASCII digits>`, a `doc_id`, `text`, `source_path`, `indexed_at`, `file_type`, `language` or `updated_at` that is neither a string nor null (stored beside the other documents' strings, a number or a list would fail its whole batch), a wrongly typed `acl`, `scopes`, `acl_warning` (list of strings or null), `acl_source`, `acl_sidecar_path` or `ingested_by` (string or null), a string in any of these fields or list items except `ingested_by` holding a lone surrogate (JSON `"\ud800"`, which UTF-8 cannot encode; one in `metadata` is stored JSON-escaped), a `metadata` that is not a JSON object of string values within the metadata limits (as an object, or as a string holding one; an empty string imports as no metadata), a `custom_score` that does not convert to a number, or an `expires_at` that is neither null nor an ISO-8601 timestamp string. `indexed_at` and `updated_at` are not parsed: a string is stored as archived, except that an empty or null `updated_at` is stored as the document's `indexed_at`. An empty, legacy or unknown `ingested_by` imports as `cli`. A non-empty `vector` that decodes in full to another number of floats than the archive's first valid document's fails the import even with `skip`, with `corrupt doc (vector) at line N: its dimension differs from the archive's first valid document`: that first document sets the collection's dimension, so were it the corrupt one, skipping would drop every other document. An empty `vector`, or one whose byte length is not a multiple of 4, is a bad document instead (`missing or not base64-encoded float32`), skipped under `skip`. |
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

Both apply the same embedding-model and schema-version checks as REST and return a
structured error (`code` = `path_unsafe`, `not_found`, `collection_exists`,
`embedding_model_mismatch`, …) on rejection. Poll `GET /jobs/{job_id}` for progress.

Path safety differs in one respect: `import_collection` accepts archives inside the data
directory **only**. The `[backup].output_dir` allowance described under
[Constraints](#constraints-to-know-before-you-start) is REST-only — restore an off-tree
backup archive with `POST /collections/{name}/import` (or the `archon-search import` CLI,
which calls it).

## Tracking a job

Export and import are **checkpointed, resumable jobs**. Progress is written to the job
record as `{processed, total, phase}` every `[jobs].checkpoint_interval` documents
(default 100). Phases are:

- **Export**: `reading` → `writing` → `packaging`
- **Import**: `validating` → `ingesting` → `indexing`

Because they're bulk jobs, they may sit in `QUEUED` behind another export/import until a slot frees up (concurrency is capped by `[jobs].max_concurrent_bulk`, default 1). An import holds its slot through the final model warm-up too, so on a server whose models are still building, a queued export or backup can wait up to that warm-up's ceiling longer. If the server restarts mid-run, the job becomes `FAILED` but its checkpoint is preserved — call `POST /jobs/{job_id}/resume` to re-enqueue from the last checkpoint (export re-opens its `.export-<job_id>.jsonl.tmp` temp file; import resumes after the last archive line it had consumed, so documents skipped before the checkpoint are neither re-imported nor dropped from the `skipped` count). See [Resuming an import](#resuming-an-import) for when a resumed import continues and when it starts over.

Cancelling an import (`DELETE /jobs/{job_id}`) during that final warm-up ends the job `CANCELLED`, but every document is already written, so the imported collection stays — as with an ingest cancelled at the same point. To import the archive again, pass `--force-overwrite` (`force_overwrite=true`); without it the import is refused with `409` `collection_exists`.

Track a job without `--wait` using the CLI:

```bash
archon-search jobs status <job_id>
```

See [Jobs and async operations](100_jobs_and_async_operations.md) for the full job
lifecycle, list/filter/resume semantics, and the `QUEUED` scheduler.

### Resuming an import

- A resumed import continues into the partially imported collection only while that collection is still the one this job wrote: it must still hold the archive's first valid document unchanged, and no other import job — in any namespace, whether it finished or not — may have started writing to a collection of the same name since this job last did. A collection re-ingested from the same source files does not count.
- When it continues, it skips the archive lines already consumed and does not store again the documents it finds already in the collection — including ones written just before the crash, ahead of their checkpoint. One edge case can still add duplicates: resuming with a smaller `[jobs].checkpoint_interval` than the failed run used, after documents near the checkpoint were deleted from the collection, can re-add part of one batch.
- Otherwise — the collection was deleted, recreated by someone else, taken over by a later import, or no longer holds that first document — the server logs a warning and the import starts over from the beginning of the archive. If nothing is left under the name it imports the whole archive; if anything is there, the job fails with `collection '<name>' already exists and was not written by this import; start a new import with force_overwrite=true` — even if the job was started with `force_overwrite=true`, since the collection may now hold another import's data, which only a new import may replace.
- A job that failed before it claimed the collection (with no checkpoint) resumes as a fresh import: it fails `already exists` on an existing collection, or replaces it when the job was started with `force_overwrite=true`. A job claims the collection right before its first write to it, once the archive's manifest and first valid document have been checked: a job rejected at that stage (for example, for an embedding-model mismatch) has not claimed it, while one that failed later on a bad document under `on_error="fail"` has, and resumes as above.
- A resumed import that fails before its next checkpoint — for example, the archive is rejected because the server's embedding model changed — keeps its previous checkpoint, so you can fix the cause and resume again. The exception is a resume that started over: its checkpoint is reset to the beginning of the archive when it starts writing.
- A resume after a crash that happened before the job's first checkpoint is handled as above. Before, it ran as a fresh import: it failed `already exists` without `force_overwrite=true`, and with it replaced the collection with a full re-import.

## Imported chunks and maintenance

The archive keeps the exporting host's `source_path` values, and maintenance orphan cleanup (`[maintenance].orphan_cleanup`, on by default) deletes documents whose `source_path` does not exist on this host. It runs whenever a maintenance pass runs: on schedule when `[maintenance].interval_hours` is above 0 (the default, 0, schedules none), or when triggered by `POST /maintenance/trigger` or `archon-search maintenance run`. Without a marker, the first pass after importing another machine's archive would delete nearly the whole collection. So:

- **Every imported chunk is marked.** The import adds `"_imported": "true"` to each chunk's `metadata`, beside the archived keys (an archived `_imported` with any other value is overwritten). It shows up in `POST /explain` results' `metadata` always, and in search results' `metadata` when you ask for it with `include_metadata`. Search filters cannot select on it.
- **Orphan cleanup never deletes a marked chunk**, as it never deletes inline-ingested ones (`"_inline": "true"`). A `source_path` with no marked chunk is swept as before; at a missing path with a marked chunk, at most the unmarked rows under the `doc_id` derived from the path as resolved here are deleted — see [Orphan cleanup rules](../OperatorGuide/50_maintenance_and_jobs.md#orphan-cleanup-rules).
- **The marker needs room among the 50 metadata fields.** An archived chunk whose `metadata` holds 50 fields other than `_imported` is refused with `` malformed doc at line N: metadata has no room for the `_imported` marker (at most 50 fields) `` (the import fails, or skips it under `on_error=skip`). One holding 50 fields including `_imported` imports: the marker overwrites it and adds no key. One holding more than 50 is refused as before, with `` malformed doc at line N: a required field is missing or has the wrong type ``.
- **Re-ingesting the same path can end the marking.** A document's `doc_id` is the SHA-256 of its resolved `source_path`. When an archived path resolves to the same path on this host (typically a same-host restore), ingesting it from disk replaces that document's imported rows with unmarked ones; from then on orphan cleanup treats it like any disk-ingested document. Two limits: an ingest that yields no chunks, or stops before writing (for example on a fatal graph-extraction error), leaves the imported rows; and collection sync and the watcher re-ingest only files that are new or changed since their recorded sync while their recorded sync state survives and the collection's embedding model and chunk size are unchanged (otherwise sync re-ingests every file), so an unchanged file keeps its imported rows until you ingest it explicitly — `POST /ingest` with its `path` (or the MCP `ingest_file` / `ingest_directory` tools), or `POST /collections/{name}/reindex` for a collection with a configured source directory, all of which ingest every file regardless of its mtime.
- **Re-ingesting from other paths leaves the imported rows.** When this host keeps the files elsewhere (the usual cross-machine case), re-ingest writes new documents under the new paths and the imported rows stay beside them, still marked — duplicates in search results. Remove them by dropping the imported collection (`DELETE /collections/{name}`, which removes the whole collection) before re-ingesting into a fresh one, or delete each imported document by its `doc_id` with the MCP `delete_document` tool. `GET /collections/{name}/documents` lists `doc_id` and `source_path` but no metadata, so tell imported documents apart by their exporting-host `source_path`, or by the `_imported` key in `POST /explain` results (or search results with `include_metadata`), which carry each chunk's `doc_id`.
- **Export keeps the marker**, so importing an export of an imported collection keeps it too.
- **Same-host restores stay exempt.** Imported chunks are never swept, even when their source paths are local, so deleting such a file later does not make orphan cleanup remove its imported chunks until that path has been re-ingested. This exemption is the orphan sweep's only: a collection sync that tracked the file still deletes that document when the file disappears.
- **Collections imported before the marker existed carry none** and are still deleted by orphan cleanup when their paths are missing. Import them again with `--force-overwrite` to mark them, or list them in `[maintenance].exclude` — which skips every per-collection maintenance policy for them (FTS optimize, orphan cleanup, expired-chunk pruning, graph GC), not just orphan cleanup.
- **Imports resumed across the upgrade.** An import that failed before the marker existed and is resumed after it keeps the rows it wrote before unmarked, so they are still swept, and the resume itself can be refused or start over when the archive's first document hits the field limit above. Import the archive again with `--force-overwrite` instead of resuming.

## Related documents

- [UserManual index](00_index.md)
- [Jobs and async operations](100_jobs_and_async_operations.md) — job lifecycle, resume, `--wait`
- [Ingestion and collections](50_ingestion_and_collections.md) — building collections from source
- [Backup, restore, disaster recovery](../OperatorGuide/40_backup_restore_disaster_recovery.md) — whole-instance DR vs. per-collection export
- [Maintenance and jobs](../OperatorGuide/50_maintenance_and_jobs.md) — orphan cleanup and the other maintenance policies
- [API reference (REST + MCP + CLI)](../Architecture/600_api_reference_or_public_interface.md)
