# Feature Brief: D1+D2 — Collection Export/Import with Formalized Job Contract

## Problem

Operators cannot back up, migrate, or restore individual collections — there is no export/import mechanism. When a job crashes mid-run there is no way to resume it, and there is no visibility into a queue of waiting jobs. This surfaces most painfully when trying to move a large collection between environments or recover from a failed operation.

## Goal

An operator can export a collection to a `.tar.gz` archive on the server's local disk and import it back (into the same or a different instance) via REST or CLI. Long-running jobs survive process restarts by resuming from a persisted checkpoint. The job system supports a soft queue so jobs wait rather than fail when the system is busy.

## Users & Context

Operators and developers with direct server access — running Docker, managing a self-hosted instance, or migrating between environments. They have disk access to the server, are comfortable with a CLI, and expect predictable, inspectable output. They are not running this from a remote browser.

## Core Flow

**Export:**
1. Operator calls `POST /collections/{name}/export` with an optional `{"output_path": "/data/exports/"}`.
2. Server validates `output_path` is within the operator-configured allowlist (see Edge Cases). Creates an `ExportJob`, returns `202` with `job_id`.
3. If another export/import job is running, the new job enters `QUEUED` status; the scheduler promotes it when a slot opens.
4. The job transitions `QUEUED → RUNNING`. Documents are written line-by-line to a temp file `<output_path>/.export-<job_id>.jsonl.tmp`; structured progress (`processed`, `total`, `phase`) is written to the job record every 100 documents.
5. On `DONE`, the temp JSONL and `manifest.json` are packaged into `<output_path>/<collection>-<timestamp>.tar.gz`, the temp file is deleted, and the job transitions to `DONE` with the archive path in `result`.
6. Operator polls `GET /jobs/{job_id}` to track progress, or uses `archon-search export <collection>` which polls until done and prints the output path.

**Import:**
1. Operator calls `POST /collections/{name}/import` with `{"path": "/data/exports/my-export.tar.gz"}`.
2. Server validates `path` is within the allowlist. Reads `manifest.json`, checks `schema_version` and `active_embedding_model`. Rejects with `422` on mismatch (unless `ignore_schema_version=true`; embedding model mismatch always rejects).
3. Creates an `ImportJob`, returns `202` with `job_id`. Same queue semantics as export.
4. Job ingests documents from `documents.jsonl` in batches, checkpointing after each batch. Lines that fail validation are handled per `on_error` (`"fail"` stops the job; `"skip"` logs and continues).
5. On `DONE`, the collection is fully populated and indexed. The ImportJob calls `pipeline.recompute_collection_meta()` to rebuild centroid and description_embedding. `result` contains `{"imported": int, "skipped": int, "total_in_archive": int}`.
6. Operator tracks via `GET /jobs/{job_id}` or `archon-search import <collection> <path>`.

**Resume after crash:**
1. On process restart, any job in `RUNNING` or `CANCELLING` is transitioned to `FAILED` (existing behavior), but the checkpoint (`progress` field) is preserved in the job record.
2. Operator calls `POST /jobs/{job_id}/resume`; the existing job record is mutated (`FAILED → QUEUED`), preserving `created_at` and the checkpoint in `progress`. The scheduler re-enqueues from last checkpoint.

## In Scope

- `ExportJob` and `ImportJob` as new job kinds (alongside existing `IngestJob`, `ReindexJob`, `DeleteJob`)
- New `QUEUED` status and a background scheduler that moves `QUEUED → RUNNING` when execution slots are free
- Configurable concurrent job limit for export/import jobs (default: 1, configurable via `[jobs].max_concurrent_bulk` in `archon-search.toml`; existing ingest/reindex/delete jobs are unaffected and continue dispatching immediately)
- Structured progress field on all jobs: `{"processed": int, "total": int, "phase": str}`. Field is `Optional[dict] = None` on all job types — existing job types leave it `None`.
- Valid `phase` values: ExportJob — `'reading'`, `'writing'`, `'packaging'`; ImportJob — `'validating'`, `'ingesting'`, `'indexing'`
- Resume via `POST /jobs/{job_id}/resume` — re-activates the existing job record (`FAILED → QUEUED`), preserving `created_at` and checkpoint in `progress`

  `POST /jobs/{job_id}/resume` contract:
  - Returns `202 Accepted` with the job's `JobResponse` (same job_id, status now QUEUED)
  - If `progress` is null (failed before first checkpoint): restarts from beginning
  - If job is not in `FAILED` state: returns `409 Conflict` with error 'job is not in FAILED state'
  - If the archive/temp file path no longer exists: returns `422` with error 'source archive not found; cannot resume'
  - Namespace check: 404 if caller's namespace does not match the job's namespace

- Export format: `.tar.gz` containing `manifest.json` + `documents.jsonl` (one doc per line; vectors as base64-encoded little-endian float32 arrays). Two-phase write: documents stream to `<output_path>/.export-<job_id>.jsonl.tmp` (appendable, checkpointable); final tar.gz is only assembled on successful completion.
- `manifest.json` schema: `{"archon_search_version": str, "schema_version": int, "collection": str, "exported_at": str, "doc_count": int, "active_embedding_model": str, "description": str}`. `active_embedding_model` is the per-collection actual model (the source of truth for import validation); the server-wide config model is not included. The centroid is NOT exported — it is recomputed by calling `pipeline.recompute_collection_meta()` as the final step of the ImportJob, before transitioning to DONE.
- **`documents.jsonl` per-line schema** (one JSON object per line):
  ```json
  {
    "doc_id": "<str>",
    "chunk_id": "<str>",
    "text": "<str>",
    "vector": "<str: base64-encoded little-endian float32 array, standard base64 with padding>",
    "vector_dtype": "float32",
    "vector_dim": <int>,
    "source_path": "<str: absolute path on source machine — preserved as-is; may be invalid on target>",
    "indexed_at": "<str: ISO 8601 UTC>",
    "file_type": "<str>",
    "language": "<str>",
    "metadata": <dict>,
    "acl": [<str>, ...],
    "custom_score": <float> | null
  }
  ```
  `metadata` is re-serialized as a JSON object (not a string) in JSONL, even though stored as a JSON string in LanceDB. `acl` is exported as-is — operators must review ACL values when importing into a different multi-tenant deployment.
- Strict schema version check on import; `ignore_schema_version: bool` (default: false) overrides the schema_version check only. Embedding model mismatch always rejects with `422` — this check cannot be bypassed.
- Import into an existing collection is rejected by default with `409`; `force_overwrite: bool` (default: false) drops all existing documents and re-imports
- `on_error: "fail" | "skip"` (default: `"fail"`) controls behavior on corrupt JSONL lines. When `"skip"`, completed job carries skipped count in `result.skipped`.
- `GET /jobs` list endpoint:
  - Query parameters: `status` (optional, repeatable), `kind` (optional, repeatable), `limit` (int, default 50, max 200), `cursor` (opaque string for cursor-based pagination)
  - Namespace inferred from bearer token (never a query parameter — it is a security boundary)
  - Response: `{"items": [JobResponse, ...], "next_cursor": str | null, "total": int}`
  - Sort order: `created_at` descending; `total` reflects matching count before pagination
- `archon-search export <collection> [--output-dir PATH] [--wait]` CLI command
- `archon-search import <collection> <path> [--force-overwrite] [--ignore-schema-version] [--on-error=fail|skip] [--wait]` CLI command
  `--wait` behavior: polls `GET /jobs/{job_id}` every 2 seconds until terminal status; prints structured progress (`phase`, `processed/total`) on each poll if available; exits with code 0 on `DONE`, code 1 on `FAILED`/`CANCELLED`; on CTRL+C, stops polling but does NOT cancel the job (job continues on server). Without `--wait`, CLI prints the job_id and exits 0 immediately.
- REST + MCP parity: `export_collection(collection, output_path="")` and `import_collection(collection, path, force_overwrite=False, ignore_schema_version=False, on_error="fail")` MCP tools
- `BREAKING.md` entries for any new response fields that change existing job schemas

## Out of Scope

- **Multi-collection export in one job** — `fan out N ExportJob`s instead; a CLI `--all` flag can automate this later
- **Multipart file upload** — operators have disk access; remote upload is a follow-up
- **URL-based import** — adds network timeout complexity; defer to Phase E
- **Migration job kind (D3)** — depends on D1 infrastructure but is a separate brief; schema migration is a distinct concern from backup/restore
- **Maintenance jobs (D5)** — separate brief; compaction, orphan cleanup, and retry policies are independent
- **Queue priority ordering** — FIFO is sufficient for v1; priority scheduling is a D5/E concern
- **Resume for existing IngestJob/ReindexJob kinds** — retrofit per-kind checkpointing after export/import validates the pattern
- **Compression of vector data** — base64 float32 is readable and sufficient; columnar/Parquet export is a follow-up optimization

## Key Decisions

- **D1+D2 as one initiative**: The job contract extension is only meaningful when shaped by real job kinds. Export/import IS the contract driver — building D1 standalone would be premature abstraction.
- **One collection per job**: Aligns with the per-collection ingest model; partial-failure reasoning is simpler; a CLI wrapper handles "export all."
- **JSONL archive**: Human-readable, appendable, version-independent, and checkpointable by line offset. Parquet is faster but trades inspectability and checkpoint simplicity for compression.
- **Strict schema version**: Silent schema drift is worse than a failed import. `ignore_schema_version=true` is the explicit escape hatch for version checks only; embedding model mismatch is never bypassable.
- **Three separate import flags instead of one `force`**: `force_overwrite`, `ignore_schema_version`, and `on_error` are orthogonal safety decisions and must be independently controllable. A single `force` flag that conflates them is a footgun.
- **Server-local path only**: Export/import is an operator tool. Operators have disk access. Multipart upload adds HTTP complexity with no gain for the primary use case.
- **Soft queue (QUEUED state + scheduler) for bulk jobs only**: Accept all export/import jobs; limit concurrency via `[jobs].max_concurrent_bulk`. Existing ingest/reindex/delete jobs dispatch immediately — no behavioral regression. Better than rejecting jobs (which requires client-side retry logic) and simpler than a priority queue.
- **`max_concurrent_bulk` config key**: Renamed from `max_concurrent` to make the scope explicit — it applies to ExportJob and ImportJob only.
- **Scheduler lifecycle**: Runs as an `asyncio.Task` in FastAPI lifespan (alongside existing background tasks). On startup, the scheduler begins its 5-second tick loop immediately — no special sweep is needed since QUEUED jobs are not affected by crash recovery (only RUNNING and CANCELLING are reset to FAILED on restart). The scheduler will pick up any pre-existing QUEUED jobs within the first tick. On shutdown, receives `CancelledError` via lifespan teardown — QUEUED jobs remain QUEUED in the job store and are picked up on next boot.
- **PENDING state semantics in the scheduler**: For export/import bulk jobs, the scheduler promotes `QUEUED → RUNNING` directly — PENDING is bypassed. PENDING remains the initial state for all existing job kinds (ingest/reindex/delete) which are dispatched immediately without queuing. This makes the state machine explicit: QUEUED means 'waiting for a bulk slot'; PENDING means 'dispatched immediately, not yet started'. Bulk jobs never enter PENDING.
- **Resume re-activates the existing job** (`FAILED → QUEUED`), not creates a new one. `created_at` is preserved from original submission. `progress` carries the checkpoint across the `FAILED → QUEUED → RUNNING` cycle.
- **Resume via explicit POST, not automatic restart**: Automatic resume on restart is surprising behavior. Explicit `POST /jobs/{job_id}/resume` is predictable and auditable.
- **`progress` field is optional on all job types** (`Optional[dict] = None`). Added to the `IngestJob` base dataclass with default `None` for backward compatibility — existing persisted jobs deserialize fine. `JobResponse` gains `progress: dict | None = None`. `job_to_dict()` includes `'progress': job.progress`. ExportJob and ImportJob set it during execution; IngestJob/ReindexJob/DeleteJob leave it None.
- **ExportJob/ImportJob require `create_export()` / `create_import()` factory methods on `JobStore`**: Unlike `create()` (which hardcodes `status=PENDING`) or `create_reindex()`, these new factory methods create jobs with `status=JobStatus.QUEUED` as initial state. `ExportJob` carries additional fields: `collection: str`, `output_path: str`, `progress: Optional[dict] = None`. `ImportJob` carries: `collection: str`, `archive_path: str`, `force_overwrite: bool`, `ignore_schema_version: bool`, `on_error: str`, `progress: Optional[dict] = None`. The `job_to_dict()` function must be updated to serialize `progress` (already noted as a Key Decision). The `_serialize_job()` method in `JobStore` must handle `ExportJob` and `ImportJob` discriminators (`job_type: 'export'` and `job_type: 'import'`), and `_load()` must deserialize them back to the correct class — the same discriminator pattern used for `'reindex'` and `'delete'` today.
- **Eviction policy: `_evict_old()` MUST be patched to exclude non-terminal statuses before the scheduler ships.** The current implementation evicts ALL jobs older than 7 days regardless of status. The required change: `JobStore._evict_old()` filters candidates to only those in terminal states (`DONE`, `FAILED`, `CANCELLED`) before applying the age cutoff. Jobs in `QUEUED`, `PENDING`, `RUNNING`, or `CANCELLING` are never evicted. This is a prerequisite for QUEUED status — without it, starvation can silently delete a waiting job.
- **Export/import workers use the server's shared SearchStore**: Workers run as `asyncio.Task` instances within the FastAPI server's event loop — the same pattern as existing ingest jobs. Import workers do NOT create a new `SearchStore` — they use the same `app.state.store` instance as ingest jobs (passed in at job dispatch time, same as `routes_jobs.py:160`). This ensures per-collection `asyncio.Lock` coordination is shared with the main ingest path. Export workers also use the shared store for reads (no write coordination needed, but reusing the instance avoids a second LanceDB connection). On job dispatch, the store reference is passed to the worker coroutine as a parameter.
- **Structured progress on all jobs**: The checkpoint data is already being written — surfacing it on `GET /jobs/{job_id}` costs nothing and makes resume testable.

## Edge Cases & Constraints

- **Path safety on export/import paths**: Both `output_path` (export) and `path` (import) are validated to be within an operator-configurable allowlist (default: `get_data_dir()` and its subdirectories). The existing `validate_ingest_path()` in `_path_safety.py` is extended with an `allowed_base_dirs` check. The allowlist check uses `path.resolve().is_relative_to(allowed_dir)` for each allowed base directory, applied to both file paths (import) and directory paths (export output_path), regardless of whether the path is a file or directory. Paths outside the allowlist are rejected with `400 Bad Request`. This prevents path traversal writes/reads to arbitrary filesystem locations.
- **Export of empty collection**: Valid — produces a `.tar.gz` with `manifest.json` and an empty `documents.jsonl`. Import of an empty archive is a no-op (succeeds with `doc_count: 0`).
- **Tar archive assembly**: The final `.tar.gz` is assembled by explicit `tarfile.add()` calls naming exactly two members: `manifest.json` (written fresh) and `documents.jsonl` (the renamed content of the `.export-<job_id>.jsonl.tmp` temp file). No directory scan or glob is used. The temp file's content is streamed into the archive as `documents.jsonl` — the `.tmp` filename never appears inside the archive. The temp file is deleted after successful packaging.
- **Archive entry validation on import**: Before processing, all tar archive members are validated: reject any entry whose name is not exactly `manifest.json` or `documents.jsonl`, contains `..`, is absolute, or resolves outside the extraction directory. Import uses Python 3.12's `tarfile.data_filter` (the project minimum is Python 3.12) or equivalent safe extraction. Archives with unexpected members are rejected with `422`.
- **Import into existing collection (default)**: Rejected with `409 Conflict`. `force_overwrite: true` drops all existing documents and re-imports. No partial overwrite.
- **Process restart mid-export**: The temp JSONL file (`<output_path>/.export-<job_id>.jsonl.tmp`) is preserved on crash. Job transitions to `FAILED` but `progress` is preserved. On resume, the exporter re-opens the temp file and appends from `progress.processed`. The final `.tar.gz` is not created until export completes successfully. If the temp file is missing on resume, the job restarts from scratch.
- **Export cancellation**: The temp JSONL file is deleted when an export job is cancelled. No partial archive is left on disk.
- **Archive corruption**: `ImportJob` validates the manifest and each JSONL line on read. Behavior on corrupt lines is controlled by `on_error`: `"fail"` stops the job at the first bad line number; `"skip"` logs and continues.
- **Import result accounting**: A completed import job's `result` field always contains `{"imported": int, "skipped": int, "total_in_archive": int}`. If `skipped > 0`, this is surfaced in CLI output and MCP result. The job status is `DONE` regardless of skipped count (since `on_error='skip'` was explicitly requested), but `skipped > 0` is a visible warning.
- **Embedding model mismatch on import**: On import, `manifest.active_embedding_model` is compared against the target collection's configured model (or the server's default if the collection doesn't exist yet). If they differ, import is rejected with `422`. This check cannot be bypassed by `ignore_schema_version=true` — vector dimension/space incompatibility cannot be forced.
- **`source_path` on cross-machine import**: `source_path` values are preserved as exported. On the target machine they may point to non-existent paths. This is accepted — `source_path` is metadata, not a live file reference. `search_with_context` and watcher-based sync will not follow these paths correctly after cross-machine import; operators must re-ingest from the target filesystem to restore live path references.
- **QUEUED job starvation**: No timeout on `QUEUED` state in v1. If `max_concurrent_bulk = 1` and a job runs for an hour, queued jobs wait. Add a configurable `queue_timeout_seconds` in a follow-up.
- **Namespace isolation**: `GET /jobs` respects the bearer token's namespace; cross-namespace jobs are invisible (404 on direct fetch, excluded from list).
- **`schema_version` value**: Must be a project-level constant (not derived from the package version), incremented manually when the export schema changes. First value: `1`.

## Open Questions

_All resolved._

- **Checkpoint granularity**: Every 100 documents, configurable via `[jobs].checkpoint_interval`. Maps directly to the `processed` counter; one `atomic_write_json` per 100 docs bounds worst-case re-work without excess I/O. Align N with ingest batch size.
- **Scheduler tick rate**: 5-second asyncio sleep loop (`_SCHEDULER_TICK_SECONDS = 5` named constant). Jobs run in minutes; 5-second promotion latency is imperceptible. No startup sweep needed — QUEUED jobs are unaffected by crash recovery and are picked up within the first tick.
- **MCP tool signatures**: Non-blocking, return `JobResponse` immediately. `export_collection(collection: str, output_path: str = "") -> JobResponse` and `import_collection(collection: str, path: str, force_overwrite: bool = False, ignore_schema_version: bool = False, on_error: str = "fail") -> JobResponse`. Empty string on `output_path` signals "use server default" — avoids a nullable string in the MCP schema.
- **`output_path` default**: `get_data_dir() / "exports"`, created on first use. Follows the established runtime layout convention; `ARCHON_SEARCH_DATA_DIR` relocates it automatically in Docker.

## Future Iterations

- **Multipart upload** for import from remote clients (Phase E)
- **`archon-search export --all`** fan-out CLI command
- **Resume for IngestJob/ReindexJob** — retrofit the checkpoint pattern once export/import validates it
- **Queue priority** — when D5 maintenance jobs land, priority scheduling becomes relevant
- **Parquet export** — optional `--format parquet` for large collections where compression matters
- **URL-based import** — `{"url": "https://..."}` for cloud storage integration

## Recommendation

This is the right initiative to open Phase D — export/import is a concrete, operator-visible feature that forces the job contract to be real rather than speculative. The hardest part is the checkpoint + resume path: get the `progress` field design right and the rest follows. The one thing that must not be compromised is the checkpoint granularity decision — building resume on top of coarse checkpoints is painful to retrofit. Nail that in the plan before writing a line of code.
