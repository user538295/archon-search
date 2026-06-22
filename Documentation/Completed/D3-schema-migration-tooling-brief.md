# Feature Brief: D3 — Schema migration tooling (item 21)

## Problem

When the archon-search chunk schema evolves (new columns, renamed fields, changed embedding dimension), operators are forced to re-ingest every document from source files to pick up the change. Five idempotent add-column migrations already run silently at startup (`migrate_namespace`, `migrate_description_embedding`, `migrate_centroid_sum`, `migrate_per_collection_model`, `migrate_acl` in `store.py`), but they are ad-hoc, opaque to operators, not tracked in any job, not observable via REST, and carry no documented rollback path. There is no formal mechanism for changes that require data rewrites (e.g., re-embedding after an embedding model upgrade) and no guarantee that a migration attempt that fails mid-way can be recovered without re-ingesting from scratch.

## Goal

An operator can apply a schema migration to a collection via a `MigrationJob` — tracked, resumable, and observable through the same job REST and CLI surface as export/import jobs. The outcome: (a) additive structural changes (new nullable columns) apply in-place in under a second with no data rewrite; (b) data-rewrite changes (re-embedding, field backfill) run as a checkpointed async job with structured progress, can be resumed after a crash, and never require touching the original source files; (c) every migration is classified as in-place, rewrite, or export-rebuild so operators know upfront what the job entails; (d) rollback rules are documented per migration kind with an explicit "this cannot be undone automatically — take a backup first" gate for destructive kinds.

## Users & Context

- **Operators of self-hosted instances** upgrading archon-search between minor versions. They have REST or CLI access, understand that "this updates the collection schema," and want to know whether they need to take a backup before proceeding.
- **Developers** evolving the chunk or metadata schema during a feature build who need a sanctioned, repeatable path to migrate existing collections in dev, staging, and production without re-parsing source files.
- The operator acts from the CLI or via REST after reading a migration dry-run report. They are not mid-ingest; the collection exists and is queryable.

## Core Flow

1. Operator runs `archon-search collection migrate <name> --dry-run` (or `GET /collections/{name}/migrations/pending`). Server inspects current LanceDB table schema against the canonical schema, returns a list of pending migrations with kind (`in_place` / `rewrite` / `export_rebuild`) and estimated impact.
2. For `in_place` migrations only: operator runs `archon-search collection migrate <name> --apply` without `--backup-first`. Server applies add-column operations synchronously, returns `200` with a summary.
3. For `rewrite` or `export_rebuild` migrations: server requires confirmation that a backup exists (`--backup-first` flag on CLI; `{"backup_confirmed": true}` in REST body). Without it, the request is rejected with a clear error.
4. Operator runs `archon-search collection migrate <name> --apply --backup-first` (or `POST /collections/{name}/migrate`). Server creates a `MigrationJob` with `status=QUEUED`, returns `202` with `job_id`.
5. `MigrationJob` transitions `QUEUED → RUNNING`. Structured progress (`processed`, `total`, `phase`) is written to the job record every 100 chunks (same checkpoint interval as `ImportJob`).
6. Valid phases: `'detecting'` (schema introspection), `'rewriting'` (per-chunk data transforms), `'reindexing'` (FTS rebuild), `'finalizing'` (metadata + centroid recompute).
7. On `DONE`, the collection is fully migrated. The job result carries `{"migrated_chunks": int, "migrations_applied": [str], "kind": str}`.
8. On failure: job transitions to `FAILED`, checkpoint in `progress` is preserved. Operator calls `POST /jobs/{job_id}/resume` to re-enqueue from the last checkpoint.
9. Operator tracks via `GET /jobs/{job_id}` or `archon-search collection migrate <name> --wait` which polls until terminal status.

## In Scope

- **`MigrationJob` kind**: new `dataclass` extending `IngestJob`, with `collection: str`, `kind: Literal["in_place", "rewrite", "export_rebuild"]`, `migrations_applied: list[str]`, `backup_confirmed: bool`, persisted via the existing `JobStore` discriminator pattern (`job_type: 'migration'`).
- **`STORE_SCHEMA_VERSION` constant** in `store.py`: integer incremented with each structural schema change. Persisted in `_archon_collection_meta` as a new `schema_version` column (added via the standard idempotent add-column pattern, defaulting to `0` for pre-D3 collections). This is distinct from `EXPORT_SCHEMA_VERSION` (the archive format version in `export_archive.py`).
- **Schema introspection**: `SearchStore.pending_migrations(collection)` compares the live LanceDB schema against the canonical schema (from `_schema()`) and the `schema_version` column in `_archon_collection_meta`. Returns a list of `MigrationSpec` objects: `{name: str, kind: Literal["in_place", "rewrite", "export_rebuild"], description: str}`.
- **In-place migration execution**: `SearchStore.apply_in_place_migrations(collection, specs)` — runs `add_columns()` for each spec synchronously, catches the "already exists" `RuntimeError` (same defense-in-depth as existing `migrate_*` methods), updates `schema_version` on `_archon_collection_meta`.
- **Rewrite migration execution**: `SearchStore.apply_rewrite_migration(collection, spec, progress_cb)` — reads all chunks in batches, transforms each batch, writes back via `add_or_update_batch()`, calls `progress_cb(processed, total, phase)` every 100 chunks.
- **At-startup migration behavior**: in-place migrations continue to apply silently at startup (same as today). Rewrite and export-rebuild migrations are NOT applied at startup — they surface only in `pending_migrations()` output and require explicit operator action.
- **`GET /collections/{name}/migrations/pending`** REST endpoint: returns `{"collection": str, "pending": [MigrationSpec], "schema_version": int}` with `200`. Returns `404` if collection does not exist. No auth change — same Bearer token requirement.
- **`POST /collections/{name}/migrate`** REST endpoint: accepts `{"backup_confirmed": bool, "dry_run": bool}`. `dry_run=true` returns the `MigrationSpec` list without creating a job (same as the GET endpoint but POST for symmetry). `backup_confirmed` is required for `rewrite` or `export_rebuild` kinds and rejected (`422`) otherwise.
- **`MigrationJob` joins the `QUEUED` scheduler**: same `JobScheduler` and `max_concurrent_bulk` limit as `ExportJob`/`ImportJob`. `MigrationJob` never enters `PENDING`.
- **Resume via `POST /jobs/{job_id}/resume`**: existing endpoint, no changes — the `JobStore.transition` state machine handles `MigrationJob` the same as `ExportJob`.
- **CLI command**: `archon-search collection migrate <name> [--dry-run] [--apply] [--backup-first] [--wait]`. `--dry-run` is the default behavior if neither `--dry-run` nor `--apply` is passed (no silent mutations). `--wait` polls `GET /jobs/{job_id}` every 2 seconds until terminal, prints `phase: processed/total` on each poll, exits `0` on `DONE`, `1` on `FAILED`/`CANCELLED`.
- **Documented rollback rules** (in `BREAKING.md` and inline in `MigrationSpec.description`):
  - `in_place`: rollback is automatic — old code ignores unknown nullable columns; no data is rewritten. Downgrade is safe without any action.
  - `rewrite`: rollback requires restoring from backup. The job gate (`backup_confirmed`) enforces this before the job is created.
  - `export_rebuild`: same rollback as rewrite. Not implemented as a job in D3 (see Out of Scope) but classified and reported to operators so they know to take a backup before upgrading.
- **`STORE_SCHEMA_VERSION` bump policy** documented in `CLAUDE.md` and `BREAKING.md`: every structural change to `_schema()` or `_meta_schema()` increments `STORE_SCHEMA_VERSION`. The migration author must add a corresponding `MigrationSpec` entry to `SearchStore.pending_migrations()`.
- **Existing startup migrations formalized**: the five existing `migrate_*()` methods are classified as `in_place` and wired into the new `pending_migrations()` / `apply_in_place_migrations()` path. They continue to run at startup via `_run_startup_migrations()` (new method that calls `apply_in_place_migrations`). No behavior change for existing deployments.
- **`GET /jobs` filter support for `kind=migration`**: `routes_jobs.py` already supports `kind` filtering via the `kind` query parameter. `MigrationJob` registers as `job_type: 'migration'`, so `GET /jobs?kind=migration` works without code changes to the list endpoint — only serialization/deserialization in `JobStore` needs updating.
- **Tests**:
  - Unit: `pending_migrations()` returns empty list when schema matches `STORE_SCHEMA_VERSION`; returns correct specs when behind.
  - Unit: `apply_in_place_migrations()` calls `add_columns()` for each spec; is idempotent (second call is a no-op, no error raised).
  - Unit: `POST /collections/{name}/migrate` with `backup_confirmed=false` and a `rewrite` kind migration pending returns `422`.
  - Unit: `MigrationJob` serialization/deserialization round-trip in `JobStore` (`job_to_dict()` / `_load()`).
  - Unit: `MigrationJob` crash recovery — job in `RUNNING` on load transitions to `FAILED`, checkpoint preserved.
  - Unit: `POST /jobs/{job_id}/resume` on `FAILED` `MigrationJob` transitions to `QUEUED`.
  - Unit: `GET /collections/{name}/migrations/pending` returns `404` on unknown collection.
  - Integration (`@pytest.mark.integration`, real LanceDB in `tmp_path`): end-to-end `apply_in_place_migrations()` on a real collection; end-to-end `apply_rewrite_migration()` with progress callback assertions; `pending_migrations()` returns empty after all migrations applied; idempotent double-apply.
- **`BREAKING.md` entry**: `JobResponse` gains `migrations_applied: list[str] | None` and `backup_confirmed: bool | None` (additive, nullable for all other job kinds). `JobStatus` gains `QUEUED` member if not already present (it was added in D1 — no change needed). New REST endpoints are additive.

## Out of Scope

- **`export_rebuild` migration execution as a job** — the kind is classified and reported, but the actual job execution (export → transform → import cycle) is deferred. Operators who encounter `export_rebuild` migrations must take a backup and re-ingest manually. Reason: this requires chaining `ExportJob → transform → ImportJob`, a multi-job saga that is a follow-up (D5 or later).
- **Cross-collection migration** — one `MigrationJob` per collection, same as the export/import model. A CLI `--all` flag can automate fan-out later.
- **Automatic rollback execution** — rollback for `rewrite` kinds requires restoring from a backup, which is the operator's responsibility. D3 documents this; it does not automate it.
- **Schema migration for the metadata table (`_archon_collection_meta`)** — the five existing startup migrations already cover all known metadata table changes. No new metadata-table schema changes are planned for D3. Reason: metadata table migrations are low-risk and can continue as startup migrations; classifying them via the new system is a cleanup that can follow.
- **Embedding-model-change re-embedding** — reindexing after an `active_embedding_model` change is already handled by `ReindexJob`. D3 does not duplicate this. The `MigrationJob` detects an embedding model mismatch and surfaces it as a `rewrite` migration kind, but the execution delegates to `ReindexJob` creation rather than re-implementing re-embedding. Reason: avoids duplication; `ReindexJob` already has the checkpoint + resume path for re-embedding.
- **LanceDB version migrations** — LanceDB itself may evolve its on-disk format between versions. This is out of scope; LanceDB's own migration tooling handles it.
- **Remote / URL archive transforms** — all migration operations are local, server-side. No network fetches.
- **Priority queue for `MigrationJob` vs other bulk jobs** — `MigrationJob` joins the existing FIFO queue behind `ExportJob`/`ImportJob` with no priority differentiation. Priority scheduling is a D5 concern.
- **Dry-run for rewrite migrations** — dry-run reports which migrations would run (via `pending_migrations()`), but does not simulate the data transform. Reason: simulating a full rewrite is as expensive as doing it; operators use the migration classification (and doc count) to assess cost.

## Key Decisions

- **`STORE_SCHEMA_VERSION` is separate from `EXPORT_SCHEMA_VERSION`**: `EXPORT_SCHEMA_VERSION` (currently `1`, in `export_archive.py`) tracks the archive file format. `STORE_SCHEMA_VERSION` tracks the LanceDB table schema. They evolve independently — an archive format change does not imply a store schema change and vice versa.
- **In-place migrations still run at startup**: preserving the existing silent-at-startup behavior for additive changes keeps zero-downtime upgrades possible. Operators never need to do anything for `in_place` migrations. Only `rewrite` and `export_rebuild` require explicit action.
- **`backup_confirmed` flag, not an automated backup gate**: D3 does not trigger an automatic backup before a `rewrite` migration. Automating it would couple `MigrationJob` to the backup infrastructure, add latency, and make the job harder to reason about. The flag is a deliberate forcing function: operators must confirm they have a backup before D3 creates the job.
- **`MigrationJob` delegates re-embedding to `ReindexJob`**: re-embedding already has a correct, checkpointed, tested implementation. Duplicating it inside `MigrationJob` for the embedding-model-change case would be dead code accumulation. D3's `rewrite` classification for embedding-model changes surfaces the need; the job creation path calls `JobStore.create_reindex()` and returns the `ReindexJob` id to the caller.
- **`export_rebuild` classified but not executed**: classifying migrations that cannot be done in-place without source files is essential for operator planning. Silently rejecting or ignoring them would be worse. Implementing the export-transform-import saga in D3 would balloon scope; deferred correctly.
- **`MigrationSpec` added to `pending_migrations()`, not a separate registry**: a global migration registry (like Alembic's `versions/` directory) is overkill for a single-process embedded store. Schema version integer + a function that returns specs is sufficient and avoids a new abstraction layer.
- **Dry-run is the default CLI behavior**: `archon-search collection migrate <name>` without a flag reports pending migrations only. Silent apply would be a footgun — operators must explicitly pass `--apply`. Same principle as `--dry-run` being the safe default in the existing `reindex-metadata` command (A1).

## Edge Cases & Constraints

- **Pre-D3 collections** (`schema_version` column absent): `pending_migrations()` reads `schema_version` from the metadata table. If the column is absent, it defaults to `0` (the same defense-in-depth pattern used by all existing `migrate_*` methods). All migrations with `introduced_at > 0` appear as pending.
- **Migration applied to an empty collection**: `apply_rewrite_migration()` with zero chunks completes immediately with `migrated_chunks=0`. Not an error.
- **Migration job cancelled mid-rewrite**: job transitions to `CANCELLED`. The collection is left partially migrated — `schema_version` is not updated until the job reaches `DONE`. A resumed job restarts from the last checkpoint (100-chunk granularity), so chunks already rewritten are overwritten again (idempotent because the transform is deterministic).
- **Concurrent ingest during a rewrite migration**: `MigrationJob` acquires the per-collection `asyncio.Lock` (introduced in A1) for the duration of the `rewriting` phase. Concurrent ingest into the same collection waits or times out with `503`. Cross-collection operations are unaffected.
- **`ReindexJob` already running when migration detects embedding-model mismatch**: `POST /collections/{name}/migrate` returns `409 Conflict` if a `ReindexJob` with `status` in `{QUEUED, RUNNING}` already exists for the collection.
- **`STORE_SCHEMA_VERSION` is a module-level constant**, not a per-collection value: all collections share the same target schema version. A collection's current version is stored in its `_archon_collection_meta.schema_version` row.
- **LanceDB `add_columns()` is append-only**: LanceDB does not support dropping or renaming columns. Migrations that rename or drop a column are always `export_rebuild` kind and cannot be executed in D3. This is a hard constraint imposed by the storage engine.
- **Cross-process safety**: the per-collection lock is in-process only (A1 constraint). Running two `archon-search` processes against the same LanceDB database and applying migrations concurrently is unsupported and may corrupt the schema version tracking. Documented limitation; unchanged from A1.

## Open Questions

_All questions resolved._

- **Import-time blocking for pre-D3 archives** → No archives exist yet; no action needed. D3 does not bump `EXPORT_SCHEMA_VERSION`. If D3 writes `schema_version` into the manifest, absent fields default to `0` (same defense-in-depth pattern used elsewhere).
- **First concrete `rewrite` migration** → D3 ships as pure infrastructure. Integration tests use a synthetic `MigrationSpec` with a deterministic dummy transform to prove the checkpoint/resume path. The `ingested_by` normalization rewrite is the natural first real migration but lands as a separate follow-up after the infrastructure is proven in production.
- **`MigrationJob.source` field** → Add `source: Literal["user", "auto"] = "user"` now. `JobResponse.source` is already `str | None`; `list_queued_bulk` already sorts on `source`; omitting it would create asymmetry that D5 must repair under time pressure.
- **Concurrency limit for `MigrationJob`** → Share the existing `max_concurrent_bulk` pool. Default `1` already prevents concurrent bulk operations. Document in `archon-search.toml.example` that the limit covers export, import, and migration jobs.
- **`STORE_SCHEMA_VERSION` in `/health` or `/status`** → Add `store_schema_version: int` and `collections_schema_behind: int` to `GET /status`. `/health` stays minimal (liveness probe, no DB reads). `/status` already reads `_archon_collection_meta` for all collections, so `collections_schema_behind` is a zero-cost aggregate. Per-collection detail remains in `GET /collections/{name}/migrations/pending`.

## Future Iterations

- **`export_rebuild` job execution**: implement the export → transform → import saga as a composed multi-job flow (D5 or later).
- **Automatic migration on startup for `rewrite` kinds**: opt-in config flag (`[migration] auto_apply_rewrites = false`) that would trigger `MigrationJob` creation at startup for eligible collections. Deferred because the operational risk is too high for a v1.
- **Migration history log**: persist a `migrations_history` entry on `_archon_collection_meta` (list of `{name, applied_at, duration_s}`) so operators can audit what was applied and when. Not needed for the first iteration.
- **Priority queue**: `MigrationJob` behind other user-triggered bulk jobs so an in-progress export is not delayed. Deferred to D5 priority scheduling.
- **Resume for `IngestJob` and `ReindexJob`**: the checkpoint pattern proven in D1/D2 and extended in D3 should eventually be retrofitted to the original job kinds.

## Recommendation

D3 is the right feature to build now. The five ad-hoc startup migrations in `store.py` will proliferate as the schema evolves, and the first time one of them needs to rewrite data (not just add a column), there is no safe path without full re-ingest. The job infrastructure from D1 and the export/import pattern from D2 provide an exact template — `MigrationJob` is a third application of the same pattern, not a new abstraction. The hardest part is drawing the line clearly between `in_place` (always safe, always auto), `rewrite` (safe with backup gate, run as job), and `export_rebuild` (classify only, execute later). That classification is the core deliverable of D3, and it must not be compromised — every future schema change will be categorized against it.
