# D2 — Scheduled Backup & Export/Import Completion
**Purpose**: Fix the broken export/import dispatch closure that causes every job to fail, add a `source` priority field to the job model, and ship an in-process `BackupLoop` with REST/CLI surfaces for automated collection backup with rotation.
**Audience**: Operators running archon-search as a persistent service who need disaster recovery, migration, and automated periodic snapshots.
**Status**: To Do

---

## Background

The D1-D2 plan implemented the full export/import machinery (workers, endpoints, CLI, MCP tools) but left a deliberate no-op dispatch closure in `app.py:285–291` that marks every promoted job FAILED with `workers_not_deployed`. This was a placeholder; the real workers (`_export_task`, `_import_task`) exist in `routes_export.py` but are never called. This plan fixes that defect and adds the scheduled backup feature on top.

## Goal

After this plan: `POST /collections/{name}/export` and `POST /collections/{name}/import` complete successfully end-to-end. Operators can configure `[backup] interval_hours = 24, keep = 7` in `archon-search.toml` and collection archives appear in `~/.archon-search/backups/{namespace}/` automatically, with rotation enforced, backup jobs deprioritized behind manual operations, and full observability via `GET /status` and `archon-search backup status`.

---

## Scope

### In Scope
- `BackupConfig` dataclass + `SearchConfig.backup` + `archon-search.toml.example` update
- `source: Literal["user", "backup"] = "user"` on `ExportJob` + `ImportJob`; `create_export()`/`create_import()` gain `source` parameter; `_load()` backward-compat `setdefault`
- `list_queued_bulk()` sort key changes to `(source_priority, created_at)`; `GET /jobs?source=backup` filter
- `job_to_dict()` refactored to include `source`, `collection`, `output_path`, `archive_path` (fixes pre-existing D1-D2 serialization bug); `JobResponse` gains these as nullable optional fields
- `lancedb_version` added to export manifest via `importlib.metadata.version('lancedb')` (null on failure)
- Real dispatch closure wired in `create_app()` lifespan handler (replacing no-op); `JobScheduler.dispatch_fn` made a reassignable attribute
- `BackupLoop` class (`archon_search/jobs/backup_loop.py`) with trigger loop, completion loop, deduplication, state file, rotation
- `BackupLoop` wired into `create_app()` lifespan; stored on `app.state.backup_loop`
- `POST /backup/trigger` endpoint + `BackupTriggerResponse`/`SkippedItem` schemas
- `GET /status` backup object extension + `BackupStatusDetail`/`CollectionBackupStatus` schemas
- `archon-search backup` Click group (`--now` flag + `status` subcommand, offline-capable)
- BREAKING.md entries for `JobResponse` and `StatusResponse` schema additions
- OpenAPI snapshot regenerated

### Out of Scope
- External scheduling (systemd timer, launchd)
- Per-collection backup config overrides
- LanceDB version enforcement on import (version written to manifest only)
- Remote backup destinations (S3, GCS)
- Backup integrity verification
- Disk space pre-flight check
- Dedicated `max_concurrent_backup` setting

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 6.1 — Final verification & documentation update].

---

## What does NOT change
- `_export_task()` and `_import_task()` logic in `routes_export.py` — called by the new dispatch closure, not modified
- `JobScheduler._tick()` internal logic — only the `dispatch_fn` attribute is reassigned after construction
- Existing `POST /collections/{name}/export` and `POST /collections/{name}/import` request/response contracts (202 + job dict) — `job_to_dict()` adds new nullable fields, which is additive
- Job eviction policy (terminal jobs older than 7 days)
- Archive file format (tar.gz with manifest.json + documents.jsonl) and schema version

---

## Known limitations / accepted trade-offs
- Missed backups while server is down are not recovered (state file preserves last-backup-at; if overdue on restart, one immediate tick fires)
- Disk space is not pre-checked before backup; export worker fails with OSError if full
- `POST /backup/trigger` only covers the caller's namespace; multi-namespace operators must call once per namespace
- `output_dir` allowlist validation is tautological (backup output_dir is in its own allowlist); dotdot/absolute-path checks still apply; operator misconfiguration not guarded
- `max_concurrent_bulk = 1` means many collections drain serially; no per-backup concurrency limit in v1

---

## Architecture

### New modules
- **`archon_search/jobs/backup_loop.py`** — `BackupLoop` class; two `async` loops (trigger + completion) run under `asyncio.gather()`; owns `.backup-state.json` read/write; calls `job_store.create_export()` with `source='backup'`; triggers rotation on DONE

### Modified modules
- **`archon_search/config.py`** — new `BackupConfig` dataclass; `SearchConfig.backup: BackupConfig`
- **`archon_search/types.py`** — `source: Literal["user","backup"] = "user"` on `ExportJob` and `ImportJob`
- **`archon_search/jobs/store.py`** — `create_export()`/`create_import()` gain `source` param; `list_queued_bulk()` sort key; `_load()` backward-compat
- **`archon_search/jobs/model.py`** — `job_to_dict()` adds `source`, `collection`, `output_path`, `archive_path`
- **`archon_search/jobs/export_archive.py`** — `_build_manifest()` adds `lancedb_version`
- **`archon_search/server/app.py`** — `scheduler.dispatch_fn` reassigned in lifespan; `BackupLoop` instantiated in lifespan; `app.state.backup_loop` set
- **`archon_search/server/schemas.py`** — `JobResponse` new nullable fields; `SkippedItem`, `BackupTriggerResponse`, `CollectionBackupStatus`, `BackupStatusDetail` added; `StatusResponse.backup` field
- **`archon_search/server/routes_jobs.py`** — `GET /jobs` gains `source` query param
- **`archon_search/server/routes_status.py`** — populates `StatusResponse.backup` from `app.state.backup_loop`
- **`archon_search/cli/main.py`** — registers `backup_cmd`

### New modules (server/CLI)
- **`archon_search/server/routes_backup.py`** — `POST /backup/trigger`
- **`archon_search/cli/backup_cmd.py`** — `backup` Click group with `--now` and `status`

### Config keys (all under `[backup]` TOML section)
| Key | Type | Default | Notes |
|---|---|---|---|
| `interval_hours` | `int` | `0` | 0 = disabled |
| `keep` | `int` | `7` | 0 = never rotate |
| `exclude` | `list[str]` | `[]` | bare `{col}` or `{ns}/{col}` patterns |
| `output_dir` | `str` | `""` | empty → `get_data_dir() / "backups"` |

### Key interfaces
```python
# BackupLoop
class BackupLoop:
    def __init__(self, job_store: JobStore, search_store: SearchStore,
                 config: BackupConfig, data_dir: Path) -> None: ...
    async def run(self) -> None: ...           # asyncio.gather(_trigger_loop, _completion_loop)
    def is_collection_in_flight(self, ns: str, col: str) -> bool: ...
    def track(self, job_id: str, ns: str, col: str) -> None: ...

# Updated signatures
def create_export(self, collection: str, output_path: str, tmp_path: str,
                  namespace: str = DEFAULT_NAMESPACE,
                  source: Literal["user", "backup"] = "user") -> ExportJob: ...
def create_import(self, collection: str, archive_path: str, force_overwrite: bool,
                  ignore_schema_version: bool, on_error: str,
                  namespace: str = DEFAULT_NAMESPACE,
                  source: Literal["user", "backup"] = "user") -> ImportJob: ...

# New schemas
class SkippedItem(BaseModel):
    collection: str
    reason: str

class BackupTriggerResponse(BaseModel):
    queued: list[str]
    skipped: list[SkippedItem]

class CollectionBackupStatus(BaseModel):
    collection: str
    last_backup_at: str | None
    archive_count: int

class BackupStatusDetail(BaseModel):
    enabled: bool
    interval_hours: int
    last_tick_at: str | None
    next_run_at: str | None
    collections_excluded: list[str]
    collection_status: list[CollectionBackupStatus]
```

---

## Task breakdown

### Phase 1 — Data model & config
> **Releasable**: after Task 1.4 — job responses include subclass fields, priority sort is active, and config is loadable.

#### Task 1.1 — `BackupConfig` dataclass + config validation + toml.example
- [x] **File**: `archon_search/config.py`, `archon-search.toml.example`
- **Depends on**: nothing
- **Description**:
  - Add `BackupConfig` dataclass with `interval_hours: int = 0`, `keep: int = 7`, `exclude: list[str] = field(default_factory=list)`, `output_dir: str = ""` (empty string means "use default at load time")
  - Add `backup: BackupConfig = field(default_factory=BackupConfig)` to `SearchConfig`
  - In `load_config()` post-processing: resolve `output_dir` to `str(get_data_dir() / "backups")` when empty
  - Config loader validation: emit `logging.warning` if `backup.interval_hours > 0` and `backup.keep == 0` (backups enabled but rotation off — unbounded disk growth risk)
  - Config loader validation: emit `logging.error` and fall back to default `output_dir` if the configured value has fewer than 3 path components (e.g. `/`, `/tmp`, `/backup`) — prevents rotation from scanning near-root directories
  - Add `[backup]` section to `archon-search.toml.example` with commented-out defaults and explanatory comments
  - `tests/test_config_defaults.py` snapshot must include the new `backup` key
- **Releasable**: `BackupConfig` loadable from toml; validation warnings fire correctly
- **Tests (TDD)** — `tests/test_config_backup.py`:
  - Unit: `test_backup_defaults` — default `BackupConfig` has `interval_hours=0, keep=7, exclude=[], output_dir=<data_dir>/backups`
  - Unit: `test_output_dir_resolved_when_empty` — empty `output_dir` resolves to `get_data_dir() / "backups"`
  - Unit: `test_warning_on_keep_zero_with_interval` — `interval_hours=24, keep=0` emits WARNING
  - Unit: `test_error_on_shallow_output_dir` — `output_dir="/tmp"` emits ERROR and falls back to default
  - Unit: `test_shallow_output_dir_three_components_ok` — `output_dir="/mnt/nfs/backups"` is accepted
  - Unit: `test_exclude_patterns_load` — bare and qualified patterns parse correctly
  - Unit: `test_config_snapshot_includes_backup` — snapshot test updated
  - Checkpoint: `uv run pytest tests/test_config_backup.py tests/test_config_defaults.py -x`

#### Task 1.2 — `source` field on `ExportJob`/`ImportJob` + `create_export()`/`create_import()` + `_load()` backward compat
- [x] **File**: `archon_search/types.py`, `archon_search/jobs/store.py`
- **Depends on**: nothing
- **Description**:
  - Add `source: Literal["user", "backup"] = "user"` to `ExportJob` dataclass (after existing fields)
  - Add `source: Literal["user", "backup"] = "user"` to `ImportJob` dataclass (after existing fields)
  - Do NOT add to `IngestJob` base — `source` is only meaningful for bulk jobs
  - `create_export()`: add `source: Literal["user", "backup"] = "user"` parameter; pass to `ExportJob` constructor
  - `create_import()`: same — add `source` parameter with default `"user"`
  - `_load()`: add `item.setdefault("source", "user")` for both `export` and `import` job types, immediately before `ExportJob(**item)` / `ImportJob(**item)` construction. Apply `setdefault` ONLY for export/import types, not for ingest/reindex/delete (those dataclasses have no `source` field and would raise `TypeError` on unexpected kwarg)
  - `_write_atomic()` via `dataclasses.asdict()` will now include `source` on re-serialization — this is intentional
- **Releasable**: export/import jobs carry a `source` field; existing serialized jobs load correctly with default `"user"`
- **Tests (TDD)** — `tests/test_job_source_field.py`:
  - Unit: `test_export_job_default_source_is_user` — `create_export(...)` returns job with `source="user"`
  - Unit: `test_export_job_backup_source` — `create_export(..., source="backup")` returns job with `source="backup"`
  - Unit: `test_import_job_default_source_is_user` — same for import
  - Unit: `test_import_job_backup_source` — same for import
  - Unit: `test_load_legacy_export_job_gets_user_source` — JSON without `source` key loads with `source="user"` via setdefault
  - Unit: `test_load_legacy_import_job_gets_user_source` — same for import
  - Unit: `test_load_does_not_add_source_to_ingest_job` — `IngestJob` records load without TypeError
  - Checkpoint: `uv run pytest tests/test_job_source_field.py -x`

#### Task 1.3 — `list_queued_bulk()` priority sort + `GET /jobs?source` filter
- [x] **File**: `archon_search/jobs/store.py`, `archon_search/server/routes_jobs.py`
- **Depends on**: Task 1.2
- **Description**:
  - `list_queued_bulk()`: change sort key from `lambda j: j.created_at` to `lambda j: (0 if j.source == "user" else 1, j.created_at)`. User-sourced jobs sort before backup-sourced jobs; FIFO is preserved within each tier. This is the `JobStore` API contract change noted in the brief.
  - `routes_jobs.py` `GET /jobs`: add `source: list[str] = Query(default=[])` parameter alongside existing `status` and `kind`. When `source` is non-empty, filter jobs using `getattr(j, "source", None) in source_set`. Apply AFTER namespace filter, BEFORE kind filter. Values are `"user"` and `"backup"`.
  - Existing `test_list_queued_bulk_fifo` tests will need updating to account for priority sort (backup jobs after user jobs)
- **Releasable**: scheduler promotes user-sourced jobs before backup-sourced jobs; `GET /jobs?source=backup` returns only backup jobs
- **Tests (TDD)** — `tests/test_job_priority_sort.py`, `tests/test_jobs_list_resume.py` (update):
  - Unit: `test_user_job_before_backup_job` — user job created after backup job still sorts first
  - Unit: `test_fifo_within_user_tier` — two user jobs maintain FIFO by `created_at`
  - Unit: `test_fifo_within_backup_tier` — two backup jobs maintain FIFO by `created_at`
  - Unit: `test_mixed_queue_ordering` — 3 jobs: backup(T1), user(T2), user(T3) → order is user(T2), user(T3), backup(T1)
  - Integration: `test_get_jobs_source_filter_backup` — `GET /jobs?source=backup` returns only backup-sourced jobs
  - Integration: `test_get_jobs_source_filter_user` — `GET /jobs?source=user` returns only user-sourced jobs
  - Integration: `test_get_jobs_source_filter_combined` — `source=user&source=backup` returns both
  - Checkpoint: `uv run pytest tests/test_job_priority_sort.py tests/test_jobs_list_resume.py -x`

#### Task 1.4 — `job_to_dict()` refactor + `JobResponse` new fields + `lancedb_version` in manifest
- [x] **File**: `archon_search/jobs/model.py`, `archon_search/server/schemas.py`, `archon_search/jobs/export_archive.py`, `tests/server/openapi_snapshot.json`, `BREAKING.md`
- **Depends on**: Task 1.2
- **Description**:
  - `job_to_dict()`: add four new nullable fields using `getattr`:
    - `"source": getattr(job, "source", None)` — `str | None`
    - `"collection": getattr(job, "collection", None)` — `str | None`
    - `"output_path": getattr(job, "output_path", None)` — `str | None` (ExportJob only)
    - `"archive_path": getattr(job, "archive_path", None)` — `str | None` (ImportJob only)
  - `JobResponse` Pydantic model: add `source: str | None = None`, `collection: str | None = None`, `output_path: str | None = None`, `archive_path: str | None = None` as optional nullable fields
  - `export_archive.py` → `_build_manifest()`: add `"lancedb_version"` field. Implementation: `importlib.metadata.version("lancedb")` wrapped in `try/except PackageNotFoundError` — write `None` and `logger.warning("Could not determine lancedb version")` on failure
  - Regenerate `tests/server/openapi_snapshot.json` with `uv run --python 3.12 python -c "..."` (per existing convention)
  - Add two entries to `BREAKING.md`:
    1. `JobResponse` gains nullable fields `source`, `collection`, `output_path`, `archive_path` (strict-schema clients will see new fields; additive but documented per project convention)
    2. `StatusResponse` will gain `backup: BackupStatusDetail | None` field (added in Task 4.2)
- **Releasable**: `GET /jobs` and all job creation endpoints return subclass fields; export archives include lancedb version in manifest
- **Tests (TDD)** — `tests/test_job_serialization.py`:
  - Unit: `test_export_job_dict_includes_collection` — `job_to_dict(ExportJob(...))` returns `collection` key
  - Unit: `test_export_job_dict_includes_output_path` — includes `output_path` (empty string when not yet done)
  - Unit: `test_export_job_dict_includes_source` — includes `source="user"` by default
  - Unit: `test_import_job_dict_includes_archive_path` — includes `archive_path`
  - Unit: `test_ingest_job_dict_source_is_null` — `IngestJob` serialization returns `source=None`
  - Unit: `test_lancedb_version_in_manifest` — manifest dict includes `lancedb_version` key (string or None)
  - Unit: `test_lancedb_version_null_on_package_not_found` — mock `importlib.metadata.version` to raise; manifest has `lancedb_version=None` and WARNING logged
  - Checkpoint: `uv run pytest tests/test_job_serialization.py -x`

---

### Phase 2 — Dispatch closure fix
> **Releasable**: after Task 2.1 — export and import jobs actually execute end-to-end.

#### Task 2.1 — Wire real dispatch closure in `create_app()` lifespan
- [x] **File**: `archon_search/server/app.py`, `archon_search/jobs/scheduler.py`
- **Depends on**: Task 1.2
- **Description**:
  - `JobScheduler`: make `dispatch_fn` a reassignable instance attribute. Change `__init__` to store it as `self.dispatch_fn` (already an attribute; just ensure `_tick()` reads `self.dispatch_fn` not a closed-over local). No type signature change needed — it was always an attribute.
  - In `create_app()` lifespan handler, after all state is initialized (search_store, pipeline, embedder_cache — happens before the `yield` at line ~193), add:
    ```python
    from archon_search.server.routes_export import _export_task, _import_task
    from archon_search.types import ExportJob, ImportJob

    def _real_dispatch(job: ExportJob | ImportJob) -> None:
        if isinstance(job, ExportJob):
            task = asyncio.create_task(
                _export_task(job, app.state.job_store, app.state.search_store, config)
            )
        else:
            task = asyncio.create_task(
                _import_task(
                    job, app.state.job_store, app.state.search_store,
                    app.state.pipeline, app.state.embedder_cache, config
                )
            )
        scheduler.register_task(task)

    if scheduler is not None:
        scheduler.dispatch_fn = _real_dispatch
    ```
  - Remove `_no_op_dispatch` closure from `run_server()` (lines 285–291)
  - `run_server()` still creates `JobScheduler` with a temporary no-op or `None` dispatch — simplest is a no-op that logs a warning if somehow called before lifespan (defensive but shouldn't happen in practice). Use `lambda job: logger.warning("dispatch called before lifespan")` as the initial placeholder.
  - The `config` variable in the closure comes from the `create_app(config, ...)` parameter — already in scope.
  - `scheduler` in the closure comes from the `create_app(..., scheduler=scheduler)` parameter — already in scope.
- **Releasable**: export and import jobs transition QUEUED → RUNNING → DONE/FAILED; archives are written to disk
- **Tests (TDD)** — `tests/test_dispatch_wiring.py`:
  - Integration: `test_export_job_reaches_done` — create collection, POST /export, poll until DONE (or timeout 30s), verify archive exists on disk; **no mocking of dispatch_fn**
  - Integration: `test_import_job_reaches_done` — export a collection, import it back, poll until DONE, verify collection restored
  - Integration: `test_scheduler_dispatch_fn_is_real_after_lifespan` — after `create_app()` starts, `scheduler.dispatch_fn` is not the no-op placeholder
  - Checkpoint: `uv run pytest tests/test_dispatch_wiring.py -x --no-cov` (these are slow integration tests)

---

### Phase 3 — BackupLoop
> **Releasable**: after Task 3.2 — scheduled backups fire automatically; state file maintained; rotation works.

#### Task 3.1 — `BackupLoop` class
- [x] **File**: `archon_search/jobs/backup_loop.py`
- **Depends on**: Task 1.2, Task 2.1
- **Description**:
  - New module `archon_search/jobs/backup_loop.py`
  - `_BACKUP_COMPLETION_POLL_SECONDS: int = 60`
  - `BackupLoop.__init__(self, job_store: JobStore, search_store: SearchStore, config: BackupConfig, data_dir: Path) -> None`:
    - `self._job_store = job_store`
    - `self._search_store = search_store`
    - `self._config = config`
    - `self._state_file: Path = data_dir / ".backup-state.json"`
    - `self._in_flight: dict[str, tuple[str, str]] = {}` — job_id → (namespace, collection)
    - `self._last_tick_at: str | None = None` — ISO-8601 string; updated on every tick attempt
  - `is_collection_in_flight(self, ns: str, col: str) -> bool`: returns `True` if any value in `self._in_flight.values()` equals `(ns, col)`
  - `track(self, job_id: str, ns: str, col: str) -> None`: adds `job_id → (ns, col)` to `_in_flight`
  - `_load_state(self) -> dict[str, str]`: reads `_state_file`; returns `{}` on missing/corrupt; parses JSON mapping `{ns}/{col} → ISO-8601`
  - `_save_state(self, state: dict[str, str]) -> None`: atomic write — write to `{_state_file}.tmp` then `os.replace`
  - `_is_excluded(self, ns: str, col: str) -> bool`: checks `config.exclude` list; pattern is `{ns}/{col}` for exact match or bare `{col}` for cross-namespace match
  - `_rotate(self, ns: str, col: str) -> None`:
    - `keep = self._config.keep`; if `keep == 0`, return immediately (never rotate)
    - `ns_dir = Path(self._config.output_dir) / ns`; if not exists, return
    - List files matching `f"{col}.backup.*.tar.gz"` in `ns_dir`; sort by filename (timestamp is sortable ISO-8601 format `%Y%m%dT%H%M%SZ`)
    - Delete all but the `keep` most recent; log each deletion at INFO level: `"Rotation: deleted backup archive {path}"`
  - `async _trigger_loop(self) -> None`:
    - On startup: read state file; check if any collection is overdue (`now - last_backup_at >= interval_hours * 3600`); if yes, fire one immediate tick before sleeping
    - If `interval_hours == 0`: do the startup overdue check (for any `_in_flight` draining), then return — no periodic ticks
    - Loop: update `_last_tick_at = datetime.now(timezone.utc).isoformat()`; try enumerate+enqueue; on any exception log ERROR and skip; sleep `interval_hours * 3600` seconds; repeat
    - Enumerate: `await self._search_store.list_collections()`; group by `.namespace`; for each `(ns, col)` not excluded: run two-part dedup check (synchronous, no awaits between check and enqueue):
      1. `self.is_collection_in_flight(ns, col)` → skip if True
      2. check `self._job_store.list_queued_bulk()` for any job with `source="backup"` and `(ns, col)` match → skip if found
      - If neither: compute archive path `str(Path(self._config.output_dir) / ns / f"{col}.backup.{ts}.tar.gz")`, compute tmp path, call `self._job_store.create_export(col, output_path, tmp_path, namespace=ns, source="backup")`; call `self.track(job.job_id, ns, col)`
  - `async _completion_loop(self) -> None`:
    - Loop: for each `(job_id, (ns, col))` in `list(self._in_flight.items())`:
      - `job = self._job_store.get(job_id)` (or equivalent lookup); if None, remove from `_in_flight` and continue
      - If `job.status == DONE`: load state, set `state[f"{ns}/{col}"] = job.updated_at`, save state; `self._rotate(ns, col)`; remove from `_in_flight`; log `INFO "Backup completed for {ns}/{col}; archive: {job.output_path}"`
      - If `job.status == FAILED`: log `ERROR "Backup failed for {ns}/{col}: {job.error}"`; remove from `_in_flight` (do NOT update `last_backup_at`)
      - If `job.status == CANCELLED`: log `INFO "Backup cancelled for {ns}/{col}"`; remove from `_in_flight`
    - Sleep `_BACKUP_COMPLETION_POLL_SECONDS` seconds; repeat
  - `async run(self) -> None`: `await asyncio.gather(self._trigger_loop(), self._completion_loop())`
  - Timestamp format for archive filenames: `datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")` — second-precision, filesystem-safe, lexicographically sortable
- **Releasable**: `BackupLoop` is unit-testable and handles all documented edge cases
- **Tests (TDD)** — `tests/test_backup_loop.py`:
  - Unit: `test_is_collection_in_flight_true` — returns True when job exists for (ns, col)
  - Unit: `test_is_collection_in_flight_namespace_scoped` — `("default", "docs")` does not block `("tenants", "docs")`
  - Unit: `test_track_adds_to_in_flight` — `track(job_id, ns, col)` makes `is_collection_in_flight` return True
  - Unit: `test_is_excluded_bare_pattern` — bare `"docs"` excludes `("default", "docs")` and `("tenants", "docs")`
  - Unit: `test_is_excluded_qualified_pattern` — `"default/docs"` excludes only `("default", "docs")`
  - Unit: `test_is_excluded_unknown_collection_not_excluded` — unknown collection returns False
  - Unit: `test_rotate_keep_n_deletes_oldest` — with `keep=2` and 4 archives, deletes 2 oldest; logs INFO for each
  - Unit: `test_rotate_keep_zero_does_nothing` — `keep=0` → no files deleted
  - Unit: `test_rotate_no_dir_is_noop` — non-existent namespace dir → no error
  - Unit: `test_load_state_missing_file_returns_empty` — missing state file → `{}`
  - Unit: `test_load_state_corrupt_file_returns_empty` — JSON parse error → `{}`
  - Unit: `test_save_state_atomic_write` — temp file created then replaced
  - Unit: `test_dedup_check_in_flight` — trigger loop skips collection if already in `_in_flight`
  - Unit: `test_dedup_check_queued_bulk` — trigger loop skips collection if a QUEUED backup job exists in store
  - Unit: `test_completion_loop_removes_done_job` — DONE job removed from `_in_flight`, state file updated
  - Unit: `test_completion_loop_removes_failed_job_preserves_last_backup_at` — FAILED → removed from `_in_flight`; `last_backup_at` not updated; ERROR logged
  - Unit: `test_completion_loop_removes_cancelled_job` — CANCELLED → removed; no state update
  - Unit: `test_trigger_loop_interval_zero_exits_after_startup` — `interval_hours=0` → trigger loop returns without periodic ticks
  - Unit: `test_trigger_loop_fires_immediate_if_overdue` — state file shows backup 25h ago with `interval_hours=24` → immediate tick fires
  - Unit: `test_trigger_loop_skips_tick_on_list_collections_error` — exception in `list_collections` → ERROR logged, loop sleeps, continues
  - Checkpoint: `uv run pytest tests/test_backup_loop.py -x`

#### Task 3.2 — Wire `BackupLoop` in `create_app()` lifespan + `app.state.backup_loop`
- [ ] **File**: `archon_search/server/app.py`
- **Depends on**: Task 3.1, Task 2.1
- **Description**:
  - In `create_app()` lifespan handler, after `search_store` and `job_store` are ready (and after dispatch_fn is reassigned):
    ```python
    from archon_search.jobs.backup_loop import BackupLoop
    backup_loop = BackupLoop(
        job_store=app.state.job_store,
        search_store=app.state.search_store,
        config=config.backup,
        data_dir=get_data_dir(),
    )
    app.state.backup_loop = backup_loop
    if config.backup.interval_hours > 0 or True:  # always start; trigger loop exits if interval=0
        task = asyncio.create_task(backup_loop.run())
        app.state._background_tasks.add(task)
        task.add_done_callback(app.state._background_tasks.discard)
    ```
  - Always instantiate and start `BackupLoop.run()` — the trigger loop handles the `interval_hours=0` case by self-exiting; the completion loop always runs to drain any `_in_flight` from prior sessions
  - `BackupLoop` receives `config.backup` (the `BackupConfig` instance), not the full `SearchConfig`
  - `output_dir` in `BackupConfig` must be resolved to an absolute path (done in Task 1.1 config loading); `BackupLoop` uses it directly via `Path(config.backup.output_dir)`
- **Releasable**: BackupLoop runs alongside the server; auto-backs up collections per config interval
- **Tests (TDD)** — `tests/test_backup_loop_lifespan.py`:
  - Integration: `test_backup_loop_stored_on_app_state` — after `create_app()` startup, `app.state.backup_loop` is a `BackupLoop` instance
  - Integration: `test_backup_loop_is_running_as_background_task` — a task is in `app.state._background_tasks` running `backup_loop.run()`
  - Integration: `test_backup_loop_cancelled_on_shutdown` — app lifespan shutdown cancels the backup_loop task without error
  - Integration: `test_backup_loop_disabled_when_interval_zero` — with `interval_hours=0`, BackupLoop is still present on `app.state` but trigger loop does not enqueue any jobs
  - Checkpoint: `uv run pytest tests/test_backup_loop_lifespan.py -x`

---

### Phase 4 — Backup REST API
> **Releasable**: after Task 4.2 — operators can trigger backups via REST and inspect backup state via `GET /status`.

#### Task 4.1 — `POST /backup/trigger` endpoint + new schemas
- [ ] **File**: `archon_search/server/routes_backup.py` (new), `archon_search/server/schemas.py`, `archon_search/server/app.py`, `tests/server/openapi_snapshot.json`
- **Depends on**: Task 3.2
- **Description**:
  - `schemas.py`: add `SkippedItem(BaseModel)` with `collection: str` and `reason: str`
  - `schemas.py`: add `BackupTriggerResponse(BaseModel)` with `queued: list[str]` and `skipped: list[SkippedItem]`
  - New file `routes_backup.py`: `router = APIRouter()` with `POST /backup/trigger`:
    ```python
    @router.post("/backup/trigger", status_code=202, response_model=BackupTriggerResponse,
                 responses={401: {"model": ErrorDetail}})
    async def trigger_backup(request: Request) -> BackupTriggerResponse:
    ```
    - Auth: uses existing `require_auth` dependency (same pattern as other routes)
    - `ns = request.state.namespace`
    - `backup_loop: BackupLoop = request.app.state.backup_loop`
    - `config: SearchConfig = request.app.state.config`
    - Enumerate all collections in namespace via `request.app.state.search_store.list_collections()`; filter to `ns`
    - For each `(ns, col)`:
      - If `backup_loop._is_excluded(ns, col)`: append `SkippedItem(collection=col, reason="excluded")` → skip
      - Else if `backup_loop.is_collection_in_flight(ns, col)`: append `SkippedItem(collection=col, reason="already_active")` → skip
      - Else if any QUEUED backup job for `(ns, col)` in `job_store.list_queued_bulk()`: append `SkippedItem(collection=col, reason="already_queued")` → skip
      - Else: compute archive path in `{output_dir}/{ns}/{col}.backup.{ts}.tar.gz`, create tmp path, call `job_store.create_export(col, output_path, tmp_path, namespace=ns, source="backup")`; call `backup_loop.track(job.job_id, ns, col)`; append `job.job_id` to `queued`
    - Return `BackupTriggerResponse(queued=queued, skipped=skipped)` as 202
  - Register router in `app.py`: `app.include_router(backup_router, prefix="/backup", dependencies=[Depends(require_auth)])`
  - Regenerate OpenAPI snapshot
- **Releasable**: `POST /backup/trigger` callable; returns queued job IDs and skipped collections
- **Tests (TDD)** — `tests/test_routes_backup.py`:
  - Integration: `test_trigger_backup_returns_202_with_job_ids` — non-excluded collection → 202, `queued` non-empty
  - Integration: `test_trigger_backup_excluded_collection_skipped` — collection in `exclude` list → `skipped` with `reason="excluded"`
  - Integration: `test_trigger_backup_already_active_skipped` — collection with RUNNING backup in `_in_flight` → `reason="already_active"`
  - Integration: `test_trigger_backup_already_queued_skipped` — QUEUED backup job exists → `reason="already_queued"`
  - Integration: `test_trigger_backup_namespace_scoped` — only enqueues collections in caller's namespace
  - Integration: `test_trigger_backup_unauthenticated_returns_401` — no Bearer token → 401
  - Integration: `test_trigger_backup_tracks_job_in_backup_loop` — after call, `backup_loop.is_collection_in_flight(ns, col)` returns True
  - Checkpoint: `uv run pytest tests/test_routes_backup.py -x`

#### Task 4.2 — `GET /status` backup extension + `BackupStatusDetail` schema
- [ ] **File**: `archon_search/server/schemas.py`, `archon_search/server/routes_status.py`, `tests/server/openapi_snapshot.json`
- **Depends on**: Task 3.2
- **Description**:
  - `schemas.py`: add `CollectionBackupStatus(BaseModel)` with `collection: str`, `last_backup_at: str | None`, `archive_count: int`
  - `schemas.py`: add `BackupStatusDetail(BaseModel)` with `enabled: bool`, `interval_hours: int`, `last_tick_at: str | None`, `next_run_at: str | None`, `collections_excluded: list[str]`, `collection_status: list[CollectionBackupStatus]`
  - `StatusResponse`: add `backup: BackupStatusDetail | None = None`
  - `routes_status.py` `GET /status`: populate `backup` field:
    - `backup_loop = getattr(request.app.state, "backup_loop", None)` — graceful if not present
    - If `backup_loop` is None: `backup = None`
    - Else:
      - `enabled = config.backup.interval_hours > 0`
      - `last_tick_at = backup_loop._last_tick_at`
      - `next_run_at = (datetime.fromisoformat(last_tick_at) + timedelta(hours=config.backup.interval_hours)).isoformat() if last_tick_at else None`
      - `collections_excluded = config.backup.exclude`
      - Load state file via `backup_loop._load_state()` → `{ns}/{col} → timestamp}`
      - Get live collections for caller's namespace via `search_store.list_collections()`; filter by `ns = request.state.namespace`
      - Merge: for each live collection `col`, look up `f"{ns}/{col}"` in state → `last_backup_at` or `None`
      - Count archives: `archive_count = len(list((Path(config.backup.output_dir) / ns).glob(f"{col}.backup.*.tar.gz")))` — returns 0 if directory absent
      - Build `collection_status` list; build `BackupStatusDetail`
  - Regenerate OpenAPI snapshot
- **Releasable**: `GET /status` returns backup state; operators can monitor from REST clients
- **Tests (TDD)** — `tests/test_routes_status_backup.py`:
  - Integration: `test_status_includes_backup_object_when_enabled` — `interval_hours=1` → `backup` is non-null with correct fields
  - Integration: `test_status_backup_enabled_false_when_interval_zero` — `interval_hours=0` → `backup.enabled=False`
  - Integration: `test_status_collection_status_includes_never_backed_up` — collection with no state file entry → `last_backup_at=None, archive_count=0`
  - Integration: `test_status_collection_status_archive_count` — create fake archive files in output_dir → `archive_count` reflects actual files
  - Integration: `test_status_collection_status_namespace_scoped` — returns only collections in caller's namespace
  - Integration: `test_status_next_run_at_computed_from_last_tick` — `last_tick_at` set → `next_run_at = last_tick_at + interval_hours`
  - Integration: `test_status_backup_null_without_backup_loop` — `app.state.backup_loop` absent → `backup=None`
  - Checkpoint: `uv run pytest tests/test_routes_status_backup.py -x`

---

### Phase 5 — Backup CLI
> **Releasable**: after Task 5.1 — `archon-search backup --now` and `archon-search backup status` are usable.

#### Task 5.1 — `archon-search backup` Click group
- [ ] **File**: `archon_search/cli/backup_cmd.py` (new), `archon_search/cli/main.py`
- **Depends on**: Task 4.1, Task 4.2
- **Description**:
  - New file `backup_cmd.py`:
    - `@click.group("backup", invoke_without_command=True)` with `@click.pass_context`
    - `@click.option("--now", is_flag=True, default=False, help="Trigger immediate backup of all non-excluded collections")`
    - `@click.option("--wait", is_flag=True, default=False, help="Poll until all triggered backup jobs complete (requires --now)")`
    - `@click.option("--api-url", default="http://localhost:8765")`
    - `@click.option("--api-key", envvar="ARCHON_SEARCH_API_KEY")` (falls back to key file using existing `_load_api_key()` helper)
    - Group body: if `ctx.invoked_subcommand` is None and `--now` is False → `click.echo(ctx.get_help())`; if `--now` → call `_trigger_backup(api_url, api_key, wait)`
    - `_trigger_backup(api_url, api_key, wait)`: POST to `{api_url}/backup/trigger`; on 202 print each queued job_id (one per line), then print skipped collections with reason; if `--wait`, poll each job_id via `GET /jobs/{job_id}` every 2s until DONE/FAILED/CANCELLED; on FAILED exit with code 1; on DONE print `Backup completed for all collections`
    - `@backup_cmd.command("status")` subcommand with `--json` flag, `--api-url`, `--api-key`:
      - **Offline-capable**: always read `.backup-state.json` and count archives from disk first (using `get_data_dir()`)
      - If server reachable (`GET {api_url}/status`): merge `last_tick_at` and `next_run_at` from response into output
      - If server unreachable: show collection-level timestamps and archive counts; show `[server unavailable]` for `last_tick_at`/`next_run_at`
      - Text output (default): print `Backup: enabled (interval=24h, keep=7)` or `Backup: disabled`; then one line per collection: `{col}: last backup {ts or "never"}, {count} archive(s)`
      - JSON output (`--json`): emit JSON matching `BackupStatusDetail` schema (minus `enabled`/`interval_hours` if server unavailable — include from config file if readable)
      - When `interval_hours=0` in config: print `Backup: disabled`; `--json` outputs `{"enabled": false, ...}`
  - `main.py`: `from archon_search.cli.backup_cmd import backup_cmd`; `main.add_command(backup_cmd)`
- **Releasable**: `archon-search backup --now` and `archon-search backup status` work end-to-end
- **Tests (TDD)** — `tests/test_cli_backup.py`:
  - Unit: `test_backup_now_prints_job_ids` — mock `httpx.post` → 202 with queued jobs; CliRunner verifies job IDs printed
  - Unit: `test_backup_now_prints_skipped` — response includes skipped → CLI prints collection + reason
  - Unit: `test_backup_now_wait_polls_until_done` — mock multiple `httpx.get` responses (QUEUED → RUNNING → DONE); CliRunner verifies completion message
  - Unit: `test_backup_now_wait_exits_1_on_failed` — final status FAILED → exit code 1
  - Unit: `test_backup_bare_prints_help` — `archon-search backup` without `--now` or subcommand → help text printed, exit 0
  - Unit: `test_backup_status_offline` — mock missing state file → prints `Backup: disabled` or zero collections; no server call
  - Unit: `test_backup_status_with_state_file` — create temp state file → prints `last backup {ts}` per collection
  - Unit: `test_backup_status_json_flag` — `--json` → valid JSON output matching schema
  - Unit: `test_backup_status_server_unavailable_degrades_gracefully` — httpx timeout → shows offline data + `[server unavailable]`
  - Checkpoint: `uv run pytest tests/test_cli_backup.py -x`

---

### Phase 6 — Final verification & documentation

#### Task 6.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to discover and update all affected documentation:
    - `CLAUDE.md` (project root): update MCP tool count if changed, CLI subcommands list, architecture section
    - `Documentation/Architecture/600_api_reference_or_public_interface.md`: add `POST /backup/trigger`, update `GET /status` and `GET /jobs`, update CLI commands table
    - `Documentation/Architecture/120_services_and_integration_architecture.md`: add BackupLoop subsection alongside JobScheduler
    - `Documentation/Architecture/110_component_catalog_and_layer_breakdown.md`: add `backup_loop.py`
    - `Documentation/UserManual/` (any backup/restore guide): update with new CLI commands and config section
    - `BREAKING.md`: verify entries for `JobResponse` and `StatusResponse` are present (added in Task 1.4)
    - `archon-search.toml.example`: verify `[backup]` section is present (added in Task 1.1)
    - `tests/server/openapi_snapshot.json`: verify final snapshot reflects all new endpoints and schema changes
  - Verify all acceptance criteria below are met before marking complete
- **Releasable**: after this task, the feature is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - **AC1**: Full test suite passes with ≥85% coverage: `uv run pytest` exits 0
  - **AC2**: Dispatch fix — `POST /collections/{name}/export` on a real collection produces QUEUED → RUNNING (within 10s) → DONE; archive `.tar.gz` file exists on disk with correct manifest including `lancedb_version`
  - **AC3**: Dispatch fix — `POST /collections/{name}/import` on a valid archive produces QUEUED → RUNNING → DONE; collection is restored
  - **AC4**: Priority sort — a backup-sourced `ExportJob` in QUEUED state yields to a user-sourced `ExportJob` added later: `list_queued_bulk()` returns user job first
  - **AC5**: `GET /jobs` response for an `ExportJob` includes non-null `source`, `collection`, `output_path` (when DONE); `GET /jobs?source=backup` returns only backup jobs
  - **AC6**: `POST /backup/trigger` returns 202 `{queued, skipped}`; calling twice for same collection returns `reason="already_active"` or `"already_queued"` on second call
  - **AC7**: `GET /status` response includes `backup` object with `collection_status` listing all collections in caller's namespace; `archive_count` matches actual files in `{output_dir}/{namespace}/`
  - **AC8**: `archon-search backup --now` calls `POST /backup/trigger` and prints queued job IDs and skipped collections
  - **AC9**: `archon-search backup status` prints collection-level backup state without requiring server; `--json` emits valid JSON
  - **AC10**: `BREAKING.md` contains entries for `JobResponse` field additions (`source`, `collection`, `output_path`, `archive_path`) and `StatusResponse.backup` addition
  - **AC11**: `archon-search.toml.example` contains `[backup]` section with all four config keys and comments
  - **AC12**: OpenAPI snapshot matches the running server's `GET /openapi.json`
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked.
