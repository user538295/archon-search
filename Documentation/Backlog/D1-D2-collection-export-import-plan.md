# D1-D2 — Collection Export/Import with Formalized Job Contract
**Purpose**: Enable operators to back up, migrate, and restore individual collections via a `.tar.gz` export/import mechanism, underpinned by a formalized job contract with QUEUED scheduling, structured progress, and resume-from-checkpoint.
**Audience**: archon-search contributors implementing D1+D2; reviewers of the resulting PRs.
**Status**: To Do

---

## Background

Every existing `IngestJob`, `ReindexJob`, and `DeleteJob` dispatches immediately via `asyncio.create_task()` — no queue, no progress visibility, no resume path. The job store lacks a `progress` field and evicts jobs regardless of status. There is no `GET /jobs` list endpoint, no way to resume a crashed long-running job, and no export or import mechanism. D1 and D2 are developed together because the export/import job kinds are the contract driver — building the scheduler without real job kinds would be premature abstraction.

Full design rationale: `Documentation/Backlog/D1-D2-collection-export-import-brief.md`.

---

## Goal

An operator can `POST /collections/{name}/export`, receive a job_id, poll `GET /jobs/{job_id}` for structured progress, and get a `.tar.gz` archive on the server disk when done. They can `POST /collections/{name}/import` to restore it. If the server crashes mid-job, `POST /jobs/{job_id}/resume` picks up from the last checkpoint. Export and import jobs are soft-queued behind `[jobs].max_concurrent_bulk` (default 1); existing ingest/reindex/delete jobs are unaffected.

---

## Scope

### In Scope
- `QUEUED` status added to `JobStatus`
- `progress: Optional[dict] = None` on `IngestJob` base
- `ExportJob` and `ImportJob` dataclasses with full field specs
- `JobsConfig` section (`max_concurrent_bulk: int = 1`, `checkpoint_interval: int = 100`)
- `EXPORT_SCHEMA_VERSION: int = 1` project constant
- `JobStore` updates: `_evict_old()` terminal-only guard, `create_export()`, `create_import()`, serialization discriminators, `update_progress()`
- `job_to_dict()` and `JobResponse` updated to include `progress`
- `validate_export_path()` (allowlist-based) + `validate_archive_members()` (zip-slip guard)
- `ExportArchiveWriter` (two-phase: temp JSONL → tar.gz) and `ImportArchiveReader` (validated streaming)
- `JobScheduler` (5-second tick, QUEUED → RUNNING dispatch for bulk jobs only)
- `_export_task()` and `_import_task()` async workers
- `POST /collections/{name}/export`, `POST /collections/{name}/import`
- `GET /jobs` list endpoint (cursor pagination, filterable, namespace-scoped)
- `POST /jobs/{job_id}/resume`
- `export_collection` and `import_collection` MCP tools
- `archon-search export` and `archon-search import` CLI commands
- `BREAKING.md` entries for `progress` field and new `QUEUED` status

### Out of Scope
- Multi-collection export/import in one job
- Multipart file upload / URL-based import
- Migration job kind (D3), maintenance jobs (D5)
- Queue priority ordering
- Resume for existing IngestJob/ReindexJob kinds
- Parquet/compressed vector export

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 9.1 — Final verification & documentation update].

---

## What does NOT change
- `routes_jobs.py` `GET /jobs/{job_id}` and `DELETE /jobs/{job_id}` behavior
- Existing `IngestJob`, `ReindexJob`, `DeleteJob` dispatch via immediate `asyncio.create_task()` — no queue for these kinds
- `load_thresholds()`, `load_live_thresholds()` in `archon_search/eval/`
- `tests/conftest.py`, `_path_safety.py` `PathUnsafeError` interface
- CI workflows (`archon-search-pr.yml`, `archon-search-release.yml`)

---

## Known limitations / accepted trade-offs
- Export resume requires the `.jsonl.tmp` temp file to survive the crash; if it's deleted externally, resume restarts from scratch
- `source_path` values in the archive are absolute paths from the source machine — cross-machine imports produce dangling paths for watcher sync and `search_with_context`
- QUEUED job starvation has no timeout in v1 (follow-up: `queue_timeout_seconds` config)
- `on_error="skip"` completes with `DONE` even if lines are skipped; skipped count is in `result`
- Centroid is not exported; it is recomputed post-import via `recompute_collection_meta()`

---

## Architecture

### New files
- `archon_search/jobs/export_archive.py` — `ExportArchiveWriter`, `ImportArchiveReader`, `EXPORT_SCHEMA_VERSION`
- `archon_search/jobs/scheduler.py` — `JobScheduler`
- `archon_search/server/routes_export.py` — `POST /collections/{name}/export` and `POST /collections/{name}/import`
- `archon_search/cli/export_cmd.py` — `export_cmd` and `import_cmd` Click commands
- `tests/test_job_store_queued.py` — JobStore QUEUED/eviction/factory tests
- `tests/test_export_archive.py` — writer/reader unit tests
- `tests/test_scheduler.py` — scheduler unit tests
- `tests/test_routes_export.py` — export/import route integration tests
- `tests/test_jobs_list_resume.py` — GET /jobs and POST /jobs/resume tests
- `tests/test_cli_export.py` — CLI export/import tests

### Modified files
- `archon_search/types.py` — add `QUEUED` to `JobStatus`; add `progress` to `IngestJob`; add `ExportJob`, `ImportJob`
- `archon_search/config.py` — add `JobsConfig` dataclass + TOML loading
- `archon_search/jobs/store.py` — `_evict_old()` + `create_export()` + `create_import()` + serialization + `update_progress()`
- `archon_search/jobs/model.py` — `job_to_dict()` adds `progress`
- `archon_search/server/schemas.py` — `JobResponse` adds `progress`
- `archon_search/_path_safety.py` — `validate_export_path()`, `validate_archive_members()`
- `archon_search/server/app.py` — register scheduler in lifespan; register export routes
- `archon_search/server/mcp.py` — `export_collection`, `import_collection` tools
- `archon_search/cli/main.py` — register `export_cmd`, `import_cmd`
- `BREAKING.md` — document `progress` field + `QUEUED` status

### Key signatures

```python
# archon_search/types.py
class JobStatus(str, Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"       # new: bulk job waiting for a slot
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    CANCELLING = "CANCELLING"

@dataclass
class IngestJob:
    job_id: str
    status: JobStatus
    created_at: str
    updated_at: str
    result: dict | None = None
    error: str | None = None
    namespace: str = DEFAULT_NAMESPACE
    progress: dict | None = None  # new: {"processed": int, "total": int, "phase": str}

@dataclass
class ExportJob(IngestJob):
    collection: str = ""
    output_path: str = ""   # final .tar.gz path (set on DONE)
    tmp_path: str = ""      # .export-<job_id>.jsonl.tmp path

@dataclass
class ImportJob(IngestJob):
    collection: str = ""
    archive_path: str = ""
    force_overwrite: bool = False
    ignore_schema_version: bool = False
    on_error: str = "fail"  # "fail" | "skip"
```

```python
# archon_search/config.py
@dataclass
class JobsConfig:
    max_concurrent_bulk: int = 1
    checkpoint_interval: int = 100
# Loaded from [jobs] in archon-search.toml
```

```python
# archon_search/jobs/store.py  (new methods)
def create_export(self, collection: str, output_path: str, tmp_path: str,
                  namespace: str = DEFAULT_NAMESPACE) -> ExportJob: ...
def create_import(self, collection: str, archive_path: str, force_overwrite: bool,
                  ignore_schema_version: bool, on_error: str,
                  namespace: str = DEFAULT_NAMESPACE) -> ImportJob: ...
def update_progress(self, job_id: str, processed: int, total: int, phase: str) -> None: ...
def list_queued_bulk(self) -> list[ExportJob | ImportJob]: ...
```

```python
# archon_search/jobs/export_archive.py
EXPORT_SCHEMA_VERSION: int = 1

class ExportArchiveWriter:
    def __init__(self, tmp_path: Path) -> None: ...
    def write_doc(self, doc: dict) -> None: ...   # appends one JSON line + \n
    @property
    def lines_written(self) -> int: ...
    def finalize(self, manifest: dict, archive_path: Path) -> None: ...
    def cleanup(self) -> None: ...  # deletes tmp_path if it exists

class ImportArchiveReader:
    def __init__(self, archive_path: Path) -> None: ...
    def read_manifest(self) -> dict: ...           # validates tar entries, returns manifest dict
    def iter_docs(self, skip: int = 0) -> Iterator[dict]: ...  # streams docs from documents.jsonl
```

```python
# archon_search/_path_safety.py  (new functions)
def validate_export_path(raw: str, allowed_base_dirs: list[Path]) -> Path:
    """Extends validate_ingest_path with allowed_base_dirs check.
    Raises PathUnsafeError(reason='outside_allowed_dirs') if resolved path is not
    relative to any allowed_base_dir (uses Path.is_relative_to())."""

def validate_archive_members(tf: tarfile.TarFile) -> None:
    """Raises PathUnsafeError(reason='unsafe_tar_member') if any member:
    - is not exactly 'manifest.json' or 'documents.jsonl'
    - has an absolute name
    - contains '..' in any path component
    Uses tarfile.data_filter (Python 3.12+)."""
```

```python
# archon_search/jobs/scheduler.py
_SCHEDULER_TICK_SECONDS: int = 5

class JobScheduler:
    def __init__(self,
                 store: JobStore,
                 max_concurrent: int,
                 dispatch_fn: Callable[[ExportJob | ImportJob], None]) -> None: ...
    async def run(self) -> None:
        """5-second tick loop. On each tick: count active bulk tasks,
        promote QUEUED bulk jobs up to max_concurrent. Handles CancelledError cleanly."""
    @property
    def active_count(self) -> int: ...
```

### Config keys
- `[jobs].max_concurrent_bulk` — `int`, default `1`; controls how many export/import jobs run concurrently
- `[jobs].checkpoint_interval` — `int`, default `100`; number of documents between progress writes

### API contracts

**`POST /collections/{name}/export`** — 202 `JobResponse` | 400 (path unsafe) | 404 (collection not found) | 409 (collection locked)
Request body: `{"output_path": str}` (optional; default `get_data_dir() / "exports"`)

**`POST /collections/{name}/import`** — 202 `JobResponse` | 400 (path unsafe/archive invalid) | 404 | 409 (collection exists and `force_overwrite=false`) | 422 (schema/model mismatch)
Request body: `{"path": str, "force_overwrite": bool = false, "ignore_schema_version": bool = false, "on_error": "fail"|"skip" = "fail"}`

**`GET /jobs`** — 200 `{"items": list[JobResponse], "next_cursor": str | null, "total": int}`
Query: `status=` (repeatable), `kind=` (repeatable: ingest/reindex/delete/export/import), `limit=50` (max 200), `cursor=`
Namespace from auth token (not a query param).

**`POST /jobs/{job_id}/resume`** — 202 `JobResponse` (status=QUEUED) | 404 (wrong namespace) | 409 (not FAILED) | 422 (archive/tmp file missing)

---

## Task breakdown

### Phase 1 — Data Model & Config
> **Releasable**: after Task 1.5; the data model is complete and all existing tests still pass. Nothing user-visible yet.

#### Task 1.1 — Add `QUEUED` to `JobStatus` and `ExportJob`/`ImportJob` to `types.py`
- [x] **File**: `archon_search/types.py`
- **Depends on**: nothing
- **Description**:
  - Insert `QUEUED = "QUEUED"` into `JobStatus` between `PENDING` and `RUNNING`
  - Add `progress: dict | None = None` to `IngestJob` with a keyword-only default (must come after existing fields with defaults: `result`, `error`, `namespace`)
  - Add `ExportJob(IngestJob)` with fields: `collection: str = ""`, `output_path: str = ""`, `tmp_path: str = ""`
  - Add `ImportJob(IngestJob)` with fields: `collection: str = ""`, `archive_path: str = ""`, `force_overwrite: bool = False`, `ignore_schema_version: bool = False`, `on_error: str = "fail"`
  - Export `ExportJob`, `ImportJob` in `__all__` (or confirm module has no `__all__` — it doesn't, so just add the classes)
- **Releasable**: after this task, the type definitions are importable and existing code that constructs `IngestJob(**item)` still works (new `progress` field has default `None`)
- **Tests (TDD)** — `tests/test_types_queued.py` (new file):
  - Unit: `test_ingest_job_progress_defaults_none` — `IngestJob(job_id="x", status=JobStatus.PENDING, created_at="t", updated_at="t")` has `progress is None`
  - Unit: `test_ingest_job_with_progress` — constructing with `progress={"processed": 5, "total": 10, "phase": "reading"}` works
  - Unit: `test_export_job_fields` — `ExportJob` has all parent fields + `collection`, `output_path`, `tmp_path`
  - Unit: `test_import_job_fields` — `ImportJob` has all parent fields + `collection`, `archive_path`, `force_overwrite`, `ignore_schema_version`, `on_error`
  - Unit: `test_queued_status_in_enum` — `JobStatus.QUEUED` exists and `JobStatus("QUEUED") == JobStatus.QUEUED`
  - Unit: `test_existing_job_construction_unchanged` — `IngestJob`, `ReindexJob`, `DeleteJob` construct as before (no regression)
  - Checkpoint: `uv run pytest tests/test_types_queued.py -v --no-cov -n0`

#### Task 1.2 — Add `[jobs]` config section to `SearchConfig`
- [x] **File**: `archon_search/config.py`
- **Depends on**: nothing
- **Description**:
  - Add `@dataclass class JobsConfig:` with `max_concurrent_bulk: int = 1` and `checkpoint_interval: int = 100`
  - Add `jobs: JobsConfig = field(default_factory=JobsConfig)` to `SearchConfig`
  - In `_apply_toml()`: parse `[jobs]` section from TOML if present; set `jobs.max_concurrent_bulk` and `jobs.checkpoint_interval` from integer values (raise `ConfigError` if not int or ≤ 0)
  - In `load_config()`: no env-var override needed for jobs config (operator uses TOML)
  - Export `JobsConfig` in `archon_search/config.py`'s public surface
- **Releasable**: after this task, `config.jobs.max_concurrent_bulk` and `config.jobs.checkpoint_interval` are accessible
- **Tests (TDD)** — `tests/test_config_jobs.py` (new file):
  - Unit: `test_jobs_config_defaults` — `SearchConfig()` has `jobs.max_concurrent_bulk == 1` and `jobs.checkpoint_interval == 100`
  - Unit: `test_jobs_config_from_toml` — TOML with `[jobs]\nmax_concurrent_bulk = 3\ncheckpoint_interval = 50` is parsed correctly
  - Unit: `test_jobs_config_invalid_zero` — `max_concurrent_bulk = 0` raises `ConfigError`
  - Unit: `test_jobs_config_invalid_negative` — `checkpoint_interval = -1` raises `ConfigError`
  - Unit: `test_jobs_config_missing_section_uses_defaults` — TOML without `[jobs]` section uses defaults
  - Checkpoint: `uv run pytest tests/test_config_jobs.py -v --no-cov -n0`

#### Task 1.3 — `EXPORT_SCHEMA_VERSION` constant and archive utilities skeleton
- [x] **File**: `archon_search/jobs/export_archive.py` (new file)
- **Depends on**: Task 2.1
- **Note**: `ImportArchiveReader.read_manifest()` calls `validate_archive_members()` which is implemented in Task 2.1. Place the stub call (or import with `from archon_search._path_safety import validate_archive_members`) at the top of `export_archive.py`. Task 2.1 must land first.
- **Description**:
  - Define `EXPORT_SCHEMA_VERSION: int = 1` at module level
  - Add `ExportArchiveWriter` class:
    - `__init__(self, tmp_path: Path) -> None`: opens `tmp_path` for append in binary mode; sets `_tmp_path = tmp_path`, `_lines_written = 0`, `_file: IO[bytes] | None = None`; calls `tmp_path.parent.mkdir(parents=True, exist_ok=True)`; opens the file
    - `write_doc(self, doc: dict) -> None`: serializes `doc` to JSON (compact, `ensure_ascii=False`) + `\n`, writes bytes; increments `_lines_written`
    - `lines_written` property: returns `_lines_written`
    - `finalize(self, manifest: dict, archive_path: Path) -> None`: closes `_file`; builds tar.gz via `tarfile.open(archive_path, "w:gz")`; adds `manifest.json` (encoded `json.dumps(manifest, ensure_ascii=False).encode()`) and `documents.jsonl` (the tmp file, arcname `"documents.jsonl"`); closes tar; calls `self.cleanup()`
    - `cleanup(self) -> None`: closes `_file` if open; deletes `_tmp_path` if it exists
    - `__enter__`/`__exit__`: context manager; `__exit__` calls `cleanup()` on exception only
  - Add `ImportArchiveReader` class:
    - `__init__(self, archive_path: Path) -> None`: stores path; does not open yet
    - `read_manifest(self) -> dict`: opens tar via `tarfile.open(archive_path, "r:gz")`; calls `validate_archive_members(tf)` (Task 2.1 — no `filter` arg is needed on `open()` since member selection is already validated by `validate_archive_members()`); extracts `manifest.json` member content via `tf.extractfile(member)` as UTF-8 JSON; validates required keys: `schema_version`, `collection`, `exported_at`, `doc_count`, `active_embedding_model`; raises `ValueError` with descriptive message if any key missing or malformed; returns the dict
    - `iter_docs(self, skip: int = 0) -> Iterator[dict]`: opens tar, extracts `documents.jsonl` member as a text stream; skips first `skip` lines; parses each subsequent line as JSON; yields the dict; raises `ValueError(f"Corrupt line {lineno}: {exc}")` on parse failure. **Per-line schema** (`documents.jsonl`): each line is a JSON object with keys `doc_id`, `chunk_id`, `text`, `vector` (base64-encoded little-endian float32), `source_path`, `indexed_at`, `file_type`, `language`, `metadata`, `acl`, `custom_score`, `ingested_by`, and `updated_at`.
- **Releasable**: after this task, archive writer and reader are unit-testable
- **Tests (TDD)** — `tests/test_export_archive.py` (new file):
  - Unit: `test_writer_creates_valid_tar` — write 3 docs, finalize, open resulting tar, confirm exactly `manifest.json` + `documents.jsonl` members, line count = 3
  - Unit: `test_writer_lines_written_counter` — counter increments correctly
  - Unit: `test_writer_cleanup_deletes_tmp` — tmp file deleted after finalize
  - Unit: `test_writer_cleanup_on_exception` — if `finalize()` raises, `cleanup()` still deletes tmp
  - Unit: `test_reader_read_manifest_valid` — reads manifest from a valid tar correctly
  - Unit: `test_reader_missing_manifest_key` — tar with manifest missing `active_embedding_model` raises `ValueError`
  - Unit: `test_reader_iter_docs_all` — iterates 5 docs in order
  - Unit: `test_reader_iter_docs_skip` — `skip=3` yields only the last 2 of 5 docs
  - Unit: `test_reader_corrupt_line` — malformed JSON line raises `ValueError` mentioning line number
  - Checkpoint: `uv run pytest tests/test_export_archive.py -v --no-cov -n0`

#### Task 1.4 — Update `JobStore`: eviction fix, factory methods, serialization, `update_progress()`
- [x] **File**: `archon_search/jobs/store.py`
- **Depends on**: Task 1.1
- **Description**:
  - `_evict_old()` (lines 173–181): add a status guard — only evict jobs in `{JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED}`; jobs in `QUEUED`, `PENDING`, `RUNNING`, `CANCELLING` are never evicted regardless of age
  - Serialization: extend the inline serialization logic inside `_write_atomic()` (lines 157–171 of `store.py`). The existing code has an `isinstance` dispatch that maps `ReindexJob` and `DeleteJob` to their `job_type` strings. Extend this dispatch to add `elif isinstance(job, ExportJob): job_type = 'export'` (including `collection`, `output_path`, `tmp_path` in the dict) and `elif isinstance(job, ImportJob): job_type = 'import'` (including `collection`, `archive_path`, `force_overwrite`, `ignore_schema_version`, `on_error` in the dict). For ALL job types, add `progress` to the serialized dict (value may be `None`). No separate `_serialize_job()` method needs to be created — extend the existing inline dispatch.
  - `_load()` deserialization: extend the `job_type` dispatch in `_load()` to handle `"export"` → `ExportJob(**item)` and `"import"` → `ImportJob(**item)`. Handle missing `progress` key with `.get("progress")` for backward compatibility with pre-D1 persisted jobs.
  - `create_export(self, collection: str, output_path: str, tmp_path: str, namespace: str = DEFAULT_NAMESPACE) -> ExportJob`: creates an `ExportJob` with `status=JobStatus.QUEUED`, UUID job_id, ISO timestamps, and the given fields; persists via `create_job()`; returns the job
  - `create_import(self, collection: str, archive_path: str, force_overwrite: bool, ignore_schema_version: bool, on_error: str, namespace: str = DEFAULT_NAMESPACE) -> ImportJob`: same pattern with `ImportJob` fields
  - `update_progress(self, job_id: str, processed: int, total: int, phase: str) -> None`: calls `self.update(job_id, progress={"processed": processed, "total": total, "phase": phase})`
  - `list_queued_bulk(self) -> list[ExportJob | ImportJob]`: returns all jobs with `status == QUEUED` that are `isinstance(job, (ExportJob, ImportJob))`; ordered by `created_at` ascending (FIFO)
  - `_CRASH_STATUSES` (line 19): add `JobStatus.QUEUED` is NOT in crash statuses (QUEUED jobs survive crashes by design) — verify this is already correct and document in a comment
- **Releasable**: after this task, bulk jobs can be created, queried, and progressed; eviction is safe for QUEUED jobs
- **Tests (TDD)** — `tests/test_job_store_queued.py` (new file):
  - Unit: `test_create_export_job_is_queued` — `create_export(...)` returns `ExportJob` with `status=QUEUED`
  - Unit: `test_create_import_job_is_queued` — `create_import(...)` returns `ImportJob` with `status=QUEUED`
  - Unit: `test_evict_old_skips_queued` — QUEUED job older than 8 days is NOT evicted
  - Unit: `test_evict_old_skips_running` — RUNNING job older than 8 days is NOT evicted
  - Unit: `test_evict_old_removes_done` — DONE job older than 8 days IS evicted
  - Unit: `test_update_progress_sets_field` — `update_progress(job_id, 50, 100, "reading")` results in `get(job_id).progress == {"processed": 50, "total": 100, "phase": "reading"}`
  - Unit: `test_serialization_roundtrip_export_job` — `create_export()` + reload from JSON roundtrips all fields correctly
  - Unit: `test_serialization_roundtrip_import_job` — `create_import()` + reload from JSON roundtrips all fields
  - Unit: `test_load_legacy_job_missing_progress` — hand-crafted JSON without `progress` key deserializes with `progress=None`
  - Unit: `test_list_queued_bulk_ordering` — returns QUEUED export/import jobs sorted by `created_at` ascending
  - Unit: `test_list_queued_bulk_excludes_ingest` — regular `IngestJob` in QUEUED-equivalent state (PENDING) is not returned
  - Checkpoint: `uv run pytest tests/test_job_store_queued.py -v --no-cov -n0`

#### Task 1.5 — Update `job_to_dict()` and `JobResponse` to include `progress`
- [x] **Files**: `archon_search/jobs/model.py`, `archon_search/server/schemas.py`
- **Depends on**: Task 1.1
- **Description**:
  - `archon_search/jobs/model.py` — `job_to_dict(job: IngestJob) -> dict`: add `"progress": job.progress` to the returned dict (value is `None` for existing job types that don't set it)
  - `archon_search/server/schemas.py` — `JobResponse`: add `progress: dict | None = None` as an optional field with default `None`; this is backward-compatible (existing clients that don't use `progress` are unaffected; strict Pydantic clients that reject unknown fields will receive `None` for old job types)
  - Add `BREAKING.md` entry: "`JobResponse` now includes an optional `progress` field (`dict | None`); `JobStatus` now includes `QUEUED` as a valid value — clients that exhaustively switch on status must handle it"
- **Releasable**: after this task, `GET /jobs/{job_id}` returns `progress` for all job types; existing callers receive `null` for jobs that don't set it
- **Tests (TDD)** — `tests/test_job_response_progress.py` (new file):
  - Unit: `test_job_to_dict_includes_progress_none` — `job_to_dict(IngestJob(...))` dict has key `"progress"` with value `None`
  - Unit: `test_job_to_dict_includes_progress_dict` — job with `progress={"processed": 5, "total": 10, "phase": "reading"}` serializes correctly
  - Unit: `test_job_response_progress_optional` — `JobResponse(job_id="x", status="RUNNING", created_at="t", updated_at="t", namespace="default")` constructs without `progress` (defaults to `None`)
  - Unit: `test_job_response_roundtrip_with_progress` — JSON serialization roundtrip preserves progress dict
  - Checkpoint: `uv run pytest tests/test_job_response_progress.py -v --no-cov -n0`

---

### Phase 2 — Path Safety & Archive Validation
> **Releasable**: after Task 2.2; archive path checks and zip-slip protection are callable. REST endpoints (Phase 4–5) depend on these.

#### Task 2.1 — `validate_export_path()` and `validate_archive_members()` in `_path_safety.py`
- [x] **File**: `archon_search/_path_safety.py`
- **Depends on**: nothing
- **Description**:
  - `validate_export_path(raw: str, allowed_base_dirs: list[Path]) -> Path`:
    - Calls `validate_ingest_path(raw)` first (reuses NUL/dotdot/absolute checks); captures the resolved `Path`
    - Then checks: `resolved_path.resolve().is_relative_to(allowed_dir)` for at least one dir in `allowed_base_dirs`
    - Raises `PathUnsafeError(reason="outside_allowed_dirs")` if no match
    - Returns the resolved path on success
  - `validate_archive_members(tf: tarfile.TarFile) -> None`:
    - Iterates `tf.getmembers()`
    - Raises `PathUnsafeError(reason="unsafe_tar_member")` if any member:
      - has a name that is not exactly `"manifest.json"` or `"documents.jsonl"`
      - has `member.name` starting with `/` (absolute)
      - has `".."` in `member.name.split("/")`
    - Uses `tarfile.data_filter` if the tarfile was opened with `filter="data"` (Python 3.12+ enforced by project minimum)
  - Add `reason` values to `PathUnsafeError` docstring: existing reasons + `"outside_allowed_dirs"`, `"unsafe_tar_member"`
- **Releasable**: after this task, path validation helpers are callable for both export and import flows
- **Tests (TDD)** — `tests/test_path_safety_export.py` (new file):
  - Unit: `test_validate_export_path_allowed` — path within allowed dir passes
  - Unit: `test_validate_export_path_outside_raises` — path outside allowed dir raises `PathUnsafeError` with reason `"outside_allowed_dirs"`
  - Unit: `test_validate_export_path_dotdot_still_rejected` — `../outside` rejected by parent `validate_ingest_path` before allowlist check
  - Unit: `test_validate_archive_members_valid` — tar with exactly `manifest.json` + `documents.jsonl` passes
  - Unit: `test_validate_archive_members_extra_member` — tar with a third entry raises `PathUnsafeError`
  - Unit: `test_validate_archive_members_traversal_name` — `../../etc/passwd` entry raises `PathUnsafeError`
  - Unit: `test_validate_archive_members_absolute_name` — `/etc/passwd` entry raises `PathUnsafeError`
  - Checkpoint: `uv run pytest tests/test_path_safety_export.py -v --no-cov -n0`

---

### Phase 3 — Bulk Job Scheduler
> **Releasable**: Phase 3 is NOT independently releasable. Task 3.2 wires a no-op dispatch closure for testing only — the no-op transitions dispatched jobs to `FAILED` with `error='workers_not_deployed'` instead of silently leaving them in `RUNNING`. The scheduler must not be deployed to production until Phase 4 (Task 4.1) replaces the no-op with the real dispatch closure. Phase 3 + Phase 4 are releasable together after Task 4.2.

#### Task 3.1 — `JobScheduler` class
- [x] **File**: `archon_search/jobs/scheduler.py` (new file)
- **Depends on**: Task 1.1, Task 1.4
- **Description**:
  - `_SCHEDULER_TICK_SECONDS: int = 5` module constant
  - `JobScheduler` class:
    - `__init__(self, store: JobStore, max_concurrent: int, dispatch_fn: Callable[[ExportJob | ImportJob], None]) -> None`: stores args; `_active: set[asyncio.Task] = set()`
    - `active_count` property: returns `len(self._active)`
    - `async def run(self) -> None`: infinite loop with `asyncio.sleep(_SCHEDULER_TICK_SECONDS)` at the start of each iteration; on each tick, calls `_tick()`; handles `asyncio.CancelledError` cleanly (exits loop, logs at DEBUG)
    - `def _tick(self) -> None`: calls `store.list_queued_bulk()`; calculates slots = `max(0, self._max_concurrent - self._active_count_running)`; for each job in FIFO order up to `slots`: calls `store.transition(job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)`; if transition succeeds, calls `dispatch_fn(job)` inside a try/except — on exception, transition the job from RUNNING to FAILED via `store.update(job.job_id, status=JobStatus.FAILED, error=f'dispatch_failed: {exc}')` and log at ERROR level; do NOT re-raise (the scheduler must continue its tick loop even if one dispatch fails)
    - `_active_count_running`: count of non-done tasks via `sum(1 for t in self._active if not t.done())`
    - `register_task(self, task: asyncio.Task) -> None`: adds to `_active`; adds a done-callback that discards from `_active`
  - Note: `dispatch_fn` is responsible for creating the asyncio.Task and calling `register_task(task)` back on the scheduler (passed via closure in the lifespan wiring — see Task 3.2)
- **Releasable**: after this task, `JobScheduler` is unit-testable in isolation
- **Tests (TDD)** — `tests/test_scheduler.py` (new file):
  - Unit: `test_tick_promotes_queued_to_running` — two QUEUED bulk jobs, `max_concurrent=1`, first tick promotes exactly one
  - Unit: `test_tick_respects_max_concurrent` — `max_concurrent=2`, three QUEUED jobs, first tick promotes exactly two
  - Unit: `test_tick_does_nothing_when_slots_full` — active count equals `max_concurrent`, no promotion
  - Unit: `test_tick_fifo_ordering` — promotes older job first (by `created_at`)
  - Unit: `test_cancelled_error_exits_run` — `run()` handles `asyncio.CancelledError` without raising
  - Unit: `test_active_count_decrements_on_task_done` — task completion decrements active count
  - Checkpoint: `uv run pytest tests/test_scheduler.py -v --no-cov -n0`

#### Task 3.2 — Register `JobScheduler` in FastAPI lifespan
- [x] **File**: `archon_search/server/app.py`
- **Depends on**: Task 3.1, Task 1.2
- **Description**:
  - In `create_app()`, accept `scheduler: JobScheduler | None = None` parameter (constructed externally so it can be injected in tests)
  - In lifespan startup (after `app.state.search_store` is connected): if `scheduler` is not None, create `task = asyncio.create_task(scheduler.run())`; add to `app.state._background_tasks`; store `app.state.scheduler = scheduler`
  - In lifespan shutdown: the existing `_background_tasks` cancellation covers the scheduler task
  - In the app factory in `server/app.py`'s `__main__` path or `run_server()`: construct `JobScheduler` from `config.jobs` and a dispatch closure (the closure will be implemented in Task 4.1/5.1 and passed in)
  - For now: wire a no-op dispatch closure that transitions dispatched jobs to FAILED with `error='workers_not_deployed'`: `lambda job: store.update(job.job_id, status=JobStatus.FAILED, error='workers_not_deployed')` — this prevents QUEUED jobs from silently hanging in RUNNING state when no workers exist.
  - The real closure is constructed in `run_server()` after Tasks 4.1 and 5.1 land: `def dispatch(job): task = asyncio.create_task(_export_task(job, store, search_store, config) if isinstance(job, ExportJob) else _import_task(job, store, search_store, pipeline, embedder_cache, config)); scheduler.register_task(task)`. This closure captures `store`, `search_store`, `pipeline`, `embedder_cache`, and `config` from the `run_server()` context. Task 3.2 is updated as part of Task 5.1 to replace the no-op with the real closure.
  - `app.state.scheduler` is accessible from request handlers for `register_task()` calls
- **Releasable**: not independently releasable — the no-op dispatch closure transitions promoted jobs to FAILED rather than processing them. Deploy only together with Phase 4 (Task 4.1).
- **Tests (TDD)** — `tests/test_scheduler_lifespan.py` (new file):
  - Integration: `test_scheduler_starts_with_app` — `create_app(scheduler=scheduler_instance)` lifespan starts the scheduler; `scheduler.active_count == 0` initially
  - Integration: `test_scheduler_cancelled_on_shutdown` — lifespan exit cancels scheduler task cleanly
  - Checkpoint: `uv run pytest tests/test_scheduler_lifespan.py -v --no-cov -n0`

---

### Phase 4 — Export Worker & REST Endpoint
> **Releasable**: after Task 4.2; `POST /collections/{name}/export` is live. Operators can trigger an export and poll for completion.

#### Task 4.1 — `_export_task()` worker
- [x] **File**: `archon_search/server/routes_export.py` (new file)
- **Depends on**: Task 1.3, Task 1.4, Task 2.1, Task 3.1, Task 3.2
- **Description**:
  - **Prerequisite**: Add `async def list_chunks_raw(self, collection: str, namespace: str) -> AsyncIterator[dict]` to `SearchStore` in `archon_search/store.py`. This method scans the collection's LanceDB table and yields each row as a dict with all columns including the raw `vector` field (as a list of floats). Uses `await table.to_arrow()` or equivalent bulk read. Must include `doc_id`, `chunk_id`, `text`, `vector`, `source_path`, `indexed_at`, `file_type`, `language`, `metadata`, `acl`, `custom_score`, `ingested_by`, and `updated_at`. Add tests in `tests/test_store.py`: `test_list_chunks_raw_returns_all_chunks` and `test_list_chunks_raw_includes_vector`.
  - `async def _export_task(job: ExportJob, store: JobStore, search_store: SearchStore, config: SearchConfig) -> None`:
    1. Wrap all logic in try/except; on any exception: `store.update(job_id, status=FAILED, error=str(exc))`; return
    2. `archive_path = Path(job.output_path)` — already validated before job creation
    3. `tmp_path = Path(job.tmp_path)`
    4. Construct `writer = ExportArchiveWriter(tmp_path)`; open as context manager
    5. `phase = "reading"`: count total docs via `await search_store.count_chunks(job.collection, job.namespace)` → set as `total`
    6. `phase = "writing"`: async-iterate all chunks from search_store (use `search_store.list_chunks_raw(job.collection, job.namespace)` — reads all chunk rows as dicts); for each chunk dict, serialize vector field as base64 little-endian float32; the doc dict written via `writer.write_doc(doc_dict)` must include all fields: `doc_id`, `chunk_id`, `text`, `vector` (base64), `source_path`, `indexed_at`, `file_type`, `language`, `metadata`, `acl`, `custom_score`, `ingested_by`, and `updated_at`; call `writer.write_doc(doc_dict)`; every `config.jobs.checkpoint_interval` docs, call `store.update_progress(job.job_id, writer.lines_written, total, "writing")`; check `store.get(job.job_id).status` — if `CANCELLING`, mark `CANCELLED` and call `writer.cleanup()`; return
    7. `phase = "packaging"`: `store.update_progress(job.job_id, writer.lines_written, total, "packaging")`; build manifest dict; call `writer.finalize(manifest, archive_path)`
    8. On success: `store.update(job.job_id, status=DONE, result={"archive_path": str(archive_path)})`
  - Manifest dict: `{"archon_search_version": importlib.metadata.version("archon-search"), "schema_version": EXPORT_SCHEMA_VERSION, "collection": job.collection, "exported_at": <ISO UTC now>, "doc_count": writer.lines_written, "active_embedding_model": <from collection meta>, "description": <from collection meta>}`
  - On cancellation check: if status is `CANCELLING`, do NOT call `writer.finalize()`; call `writer.cleanup()` then `store.update(job_id, status=CANCELLED)`
  - The worker is registered with the scheduler via `app.state.scheduler.register_task(task)` (done by the dispatch closure in Task 3.2 update)
- **Releasable**: after this task, the export worker logic is unit-testable
- **Tests (TDD)** — `tests/test_export_worker.py` (new file):
  - Integration: `test_export_task_completes` — runs `_export_task()` against a real SearchStore with seeded data; job ends DONE; archive exists at expected path
  - Integration: `test_export_task_empty_collection` — empty collection produces valid archive with `doc_count=0`
  - Integration: `test_export_task_cancellation` — seed a large collection (>200 docs to ensure the cancellation check is reached); start `_export_task()` as a task; use an `asyncio.Event` or short sleep to allow the worker to enter its writing loop, then call `store.transition(job_id, {JobStatus.RUNNING}, JobStatus.CANCELLING)`; await the task; assert job status is `CANCELLED`, tmp file is deleted, no archive on disk. Alternative simpler approach: write a custom `SearchStore` subclass that sets `CANCELLING` after yielding 50 chunks.
  - Integration: `test_export_task_store_error` — simulate read failure; job ends FAILED with error message
  - Integration: `test_export_task_checkpoint_progress` — after 100 docs, `progress.processed == 100`
  - Checkpoint: `uv run pytest tests/test_export_worker.py -v --no-cov -n0 -m integration`

#### Task 4.2 — `POST /collections/{name}/export` REST endpoint
- [x] **File**: `archon_search/server/routes_export.py`
- **Depends on**: Task 4.1, Task 1.5
- **Description**:
  - Pydantic request model: `class ExportRequest(BaseModel): output_path: str = ""`
  - `async def export_collection(name: str, body: ExportRequest, request: Request) -> JobResponse | JSONResponse`:
    1. Resolve `output_path`: if `body.output_path` is empty, use `get_data_dir() / "exports"`; `(get_data_dir() / "exports").mkdir(parents=True, exist_ok=True)`
    2. Call `validate_export_path(body.output_path or str(get_data_dir() / "exports"), [get_data_dir()])` — raises `PathUnsafeError` on violation → return `JSONResponse({"error": "path_unsafe", ...}, status_code=400)`
    3. Verify collection exists via `request.app.state.search_store.get_collection_meta(name, namespace)` — 404 if not found
    4. Compute `tmp_path = resolved_output_path / f".export-{uuid4()}.jsonl.tmp"`; `archive_path = resolved_output_path / f"{name}-{datetime_utc_iso}.tar.gz"` (use `%Y%m%dT%H%M%SZ` format for filename safety)
    5. Create job: `job = request.app.state.job_store.create_export(collection=name, output_path=str(archive_path), tmp_path=str(tmp_path), namespace=request.state.namespace)`
    6. Schedule dispatch via `request.app.state.scheduler` (the scheduler will call `_export_task` when a slot is available; the dispatch closure was wired at startup in Task 3.2)
    7. Return `JSONResponse(job_to_dict(job), status_code=202)`
  - Register route in `archon_search/server/app.py`: `app.include_router(export_router, prefix="/collections")` with bearer auth dependency
- **Releasable**: after this task, `POST /collections/{name}/export` is live
- **Tests (TDD)** — `tests/test_routes_export.py` (new file):
  - Integration: `test_post_export_returns_202_and_job_id` — valid collection, valid path; returns 202 with job_id
  - Integration: `test_post_export_default_output_path` — empty `output_path` uses `get_data_dir() / "exports"`
  - Integration: `test_post_export_collection_not_found` — 404 for unknown collection
  - Integration: `test_post_export_path_outside_allowed` — path outside `get_data_dir()` returns 400
  - Integration: `test_post_export_unauthenticated` — no bearer token returns 401
  - Checkpoint: `uv run pytest tests/test_routes_export.py -v --no-cov -n0 -m integration`

---

### Phase 5 — Import Worker & REST Endpoint
> **Releasable**: after Task 5.2; `POST /collections/{name}/import` is live. Export + import round-trip is fully operational.

#### Task 5.1 — `_import_task()` worker
- [x] **File**: `archon_search/server/routes_export.py`
- **Depends on**: Task 1.3, Task 1.4, Task 2.1, Task 3.1, Task 3.2
- **Description**:
  - `async def _import_task(job: ImportJob, store: JobStore, search_store: SearchStore, pipeline: SearchPipeline, embedder_cache: EmbedderCache, config: SearchConfig) -> None`:
    1. Wrap all logic in try/except; on exception: `store.update(job_id, status=FAILED, error=str(exc))`; return
    2. `phase = "validating"`: open `ImportArchiveReader(Path(job.archive_path))`; call `reader.read_manifest()` → `manifest`; check `manifest["schema_version"] == EXPORT_SCHEMA_VERSION` (or `job.ignore_schema_version`); check `manifest["active_embedding_model"]` matches the importing collection's configured embedding model (or server default if collection doesn't exist yet) — raise `ValueError("embedding model mismatch: ...")` if mismatch (not bypassable); update progress with `phase="validating"`, `total=manifest["doc_count"]`
    3. If collection exists and not `job.force_overwrite`: raise `ValueError(f"collection '{job.collection}' already exists; use force_overwrite=true to overwrite")`; this should have been caught at the endpoint level, but double-check here
    4. If collection exists and `job.force_overwrite`: (a) call `await search_store.drop_collection(job.collection)` to remove the LanceDB table; (b) call `await search_store.delete_collection_meta(job.collection, job.namespace)` to remove the metadata row. Note: verify these method names against `store.py` — the actual deletion code in `routes_collections.py` lines 315–318 is the authoritative reference.
    5. `phase = "ingesting"`: `processed = 0`; call `store.get(job.job_id).progress` to get the checkpoint (`skip = progress["processed"] if progress else 0`); iterate `reader.iter_docs(skip=skip)`; for each doc: decode `doc["vector"]` from base64 to `list[float]` (using `import struct, base64; raw = base64.standard_b64decode(b64str); floats = list(struct.unpack(f"{len(raw)//4}f", raw))`); reconstruct `ChunkRecord(doc_id=doc['doc_id'], chunk_id=doc['chunk_id'], text=doc['text'], vector=floats, source_path=doc['source_path'], indexed_at=doc['indexed_at'], file_type=doc['file_type'], language=doc['language'], metadata=doc['metadata'], acl=doc.get('acl'), custom_score=doc.get('custom_score'), ingested_by=doc.get('ingested_by', 'import'), updated_at=doc.get('updated_at', ''))`; accumulate `ChunkRecord` objects in a batch of `config.jobs.checkpoint_interval`; call `await search_store.ingest_chunks(job.collection, batch, namespace=job.namespace)` (using the existing `ingest_chunks()` method — verify exact signature at `store.py:1208`; the per-collection lock is handled internally by `ingest_chunks()`); every checkpoint interval, call `store.update_progress(job.job_id, processed, manifest["doc_count"], "ingesting")`; on cancellation check, same pattern as export; handle corrupt lines per `job.on_error` ("fail" re-raises; "skip" increments `skipped` counter)
    6. `phase = "indexing"`: `await search_store.rebuild_fts_index(job.collection, language=detected_language)` where `detected_language = manifest.get('language', '')` (v1 archives may not record a collection-level language; use `''` if absent — verify the `rebuild_fts_index` signature at `store.py:1279`); resolve the embedder: `global_embedder = await embedder_cache.get_or_load(manifest['active_embedding_model'])` (uses the manifest's `active_embedding_model`, already validated to match the server's configured model in step 2); call `await pipeline.recompute_collection_meta(job.collection, global_embedder, namespace=job.namespace, force=True)` (pass `global_embedder` positionally as the second argument — the actual signature is `recompute_collection_meta(self, collection: str, global_embedder: Embedder, ...)`); write manifest collection metadata fields (description, active_embedding_model) to collection meta table
    7. On success: `store.update(job.job_id, status=DONE, result={"imported": processed, "skipped": skipped, "total_in_archive": manifest["doc_count"]})`
  - Vector decode: `import struct, base64; raw = base64.standard_b64decode(b64str); floats = list(struct.unpack(f"{len(raw)//4}f", raw))`
- **Releasable**: after this task, the import worker logic is unit-testable
- **Tests (TDD)** — `tests/test_import_worker.py` (new file):
  - Integration: `test_import_task_roundtrip` — export a seeded collection, import it into a new collection; search returns expected results
  - Integration: `test_import_task_force_overwrite` — import into existing collection with `force_overwrite=True` drops old data
  - Integration: `test_import_task_existing_collection_no_force_fails` — import into existing collection without force fails with FAILED status
  - Integration: `test_import_task_schema_version_mismatch_rejected` — archive with `schema_version=99` fails unless `ignore_schema_version=True`
  - Integration: `test_import_task_embedding_model_mismatch_always_rejected` — embedding model mismatch always fails
  - Integration: `test_import_task_on_error_skip` — archive with one corrupt line; `on_error="skip"` completes DONE with `skipped=1`
  - Integration: `test_import_task_on_error_fail` — corrupt line with `on_error="fail"` → FAILED
  - Integration: `test_import_task_resume_from_checkpoint` — create a 200-doc archive (to ensure at least one checkpoint is written at doc 100); run `_import_task()` until checkpoint fires at doc 100; simulate crash by directly calling `store.update(job_id, status=JobStatus.FAILED)`; the job's `progress` is `{'processed': 100, 'total': 200, 'phase': 'ingesting'}`; call `_import_task()` again on the same job (it reads `progress.processed = 100` and calls `reader.iter_docs(skip=100)`); assert only 100 docs are written in the second run (not 200)
  - Checkpoint: `uv run pytest tests/test_import_worker.py -v --no-cov -n0 -m integration`

#### Task 5.2 — `POST /collections/{name}/import` REST endpoint
- [x] **File**: `archon_search/server/routes_export.py`
- **Depends on**: Task 5.1, Task 1.5
- **Description**:
  - Pydantic request model: `class ImportRequest(BaseModel): path: str; force_overwrite: bool = False; ignore_schema_version: bool = False; on_error: str = "fail"`
  - Validator: `on_error` must be `"fail"` or `"skip"`; raise 422 if not
  - `async def import_collection(name: str, body: ImportRequest, request: Request) -> JobResponse | JSONResponse`:
    1. Validate `body.path` via `validate_export_path(body.path, [get_data_dir()])` — 400 on `PathUnsafeError`
    2. Verify archive exists (`Path(body.path).exists()`) — 422 if not
    3. Pre-validate archive members: open tar briefly to call `validate_archive_members(tf)` — 422 on unsafe entry
    4. Read manifest to check `active_embedding_model` mismatch early (avoid creating a job that will immediately fail): 422 if mismatch
    5. Check if collection exists: if yes and not `body.force_overwrite` → 409 `{"error": "collection_exists"}`
    6. Check if schema_version mismatch (and not `body.ignore_schema_version`) → 422 `{"error": "schema_version_mismatch"}`
    7. Create job: `job = store.create_import(collection=name, archive_path=body.path, force_overwrite=body.force_overwrite, ignore_schema_version=body.ignore_schema_version, on_error=body.on_error, namespace=namespace)`
    8. Return `JSONResponse(job_to_dict(job), status_code=202)`
- **Releasable**: after this task, `POST /collections/{name}/import` is live; export → import round-trip works end-to-end
- **Tests (TDD)** — `tests/test_routes_export.py`:
  - Integration: `test_post_import_returns_202` — valid archive, valid path → 202
  - Integration: `test_post_import_path_outside_allowed` — path outside data dir → 400
  - Integration: `test_post_import_archive_not_found` → 422
  - Integration: `test_post_import_collection_exists_no_force` → 409
  - Integration: `test_post_import_schema_version_mismatch_no_flag` → 422
  - Integration: `test_post_import_invalid_on_error` — `on_error="invalid"` → 422
  - Checkpoint: `uv run pytest tests/test_routes_export.py -v --no-cov -n0 -m integration`

---

### Phase 6 — Job List & Resume Endpoints
> **Releasable**: after Task 6.2; operators can list all jobs and resume crashed ones.

#### Task 6.1 — `GET /jobs` list endpoint with cursor pagination
- [ ] **File**: `archon_search/server/routes_jobs.py`
- **Depends on**: Task 1.4, Task 1.5
- **Description**:
  - `JobListResponse(BaseModel)`: `items: list[JobResponse]`, `next_cursor: str | None`, `total: int`
  - `async def list_jobs(request: Request, status: list[str] = Query(default=[]), kind: list[str] = Query(default=[]), limit: int = Query(default=50, ge=1, le=200), cursor: str | None = Query(default=None)) -> JobListResponse`:
    - Namespace from `request.state.namespace`
    - Fetches all jobs from `store.list()`, filters by namespace; filters by `status` (if non-empty); filters by `kind` using **exact type matching** (not `isinstance`): `type(job) is IngestJob` for `"ingest"`, `type(job) is ReindexJob` for `"reindex"`, `type(job) is DeleteJob` for `"delete"`, `type(job) is ExportJob` for `"export"`, `type(job) is ImportJob` for `"import"`. Do NOT use `isinstance()` for `IngestJob` since it is the base class of all job types and would match everything.
    - Sorts by `created_at` descending
    - Cursor is the `job_id` of the last item returned; cursor-based pagination: find the cursor job's index, return the next `limit` items
    - `total` is the count of all matching jobs before pagination
    - Returns `JobListResponse`
  - Register route: `GET /jobs` with bearer auth
  - Note: `store.list()` does an in-memory scan — acceptable for v1 since job count is bounded by eviction
- **Releasable**: after this task, `GET /jobs` is live
- **Tests (TDD)** — `tests/test_jobs_list_resume.py` (new file):
  - Integration: `test_list_jobs_empty` — returns `{"items": [], "next_cursor": null, "total": 0}`
  - Integration: `test_list_jobs_default_limit` — 60 jobs returns 50 + next_cursor
  - Integration: `test_list_jobs_filter_by_status` — `?status=RUNNING` filters correctly
  - Integration: `test_list_jobs_filter_by_kind` — `?kind=export` returns only ExportJobs
  - Integration: `test_list_jobs_namespace_isolated` — jobs from other namespaces not returned
  - Integration: `test_list_jobs_cursor_pagination` — cursor advances through full list correctly
  - Integration: `test_list_jobs_kind_ingest_excludes_export_import` — create one `IngestJob`, one `ExportJob`, one `ImportJob`; `?kind=ingest` returns only the `IngestJob` (verifies exact type matching excludes subclass instances)
  - Integration: `test_list_jobs_unauthenticated` — 401
  - Checkpoint: `uv run pytest tests/test_jobs_list_resume.py::test_list_jobs_empty tests/test_jobs_list_resume.py::test_list_jobs_default_limit tests/test_jobs_list_resume.py::test_list_jobs_filter_by_status tests/test_jobs_list_resume.py::test_list_jobs_filter_by_kind tests/test_jobs_list_resume.py::test_list_jobs_namespace_isolated tests/test_jobs_list_resume.py::test_list_jobs_cursor_pagination tests/test_jobs_list_resume.py::test_list_jobs_kind_ingest_excludes_export_import tests/test_jobs_list_resume.py::test_list_jobs_unauthenticated -v --no-cov -n0 -m integration`

#### Task 6.2 — `POST /jobs/{job_id}/resume` endpoint
- [ ] **File**: `archon_search/server/routes_jobs.py`
- **Depends on**: Task 6.1, Task 4.1, Task 5.1
- **Description**:
  - `async def resume_job(job_id: str, request: Request) -> JobResponse | JSONResponse`:
    1. `job = store.get(job_id)` — None or wrong namespace → 404 `{"error": "not_found"}`
    2. If `job.status != JobStatus.FAILED` → 409 `{"error": "job_not_failed", "current_status": job.status.value}`
    3. Validate archive/tmp still exists:
       - For `ExportJob`: check `Path(job.tmp_path).exists()` OR `job.progress is None` (no checkpoint → restart); if tmp missing AND progress exists → 422 `{"error": "source_not_found"}`
       - For `ImportJob`: check `Path(job.archive_path).exists()` → 422 if missing
    4. Transition: `store.transition(job_id, {JobStatus.FAILED}, JobStatus.QUEUED)` — returns updated job
    5. The scheduler will pick up the QUEUED job on next tick; the worker reads `job.progress` to resume from checkpoint
    6. Return `JSONResponse(job_to_dict(updated_job), status_code=202)`
  - Non-bulk jobs (IngestJob, ReindexJob, DeleteJob) return 409 with `{"error": "job_not_resumable", "reason": "only export and import jobs support resume"}`
- **Releasable**: after this task, crashed export/import jobs can be resumed
- **Tests (TDD)** — `tests/test_jobs_list_resume.py`:
  - Integration: `test_resume_failed_export_job` — transitions FAILED → QUEUED, returns 202
  - Integration: `test_resume_failed_import_job` — transitions FAILED → QUEUED
  - Integration: `test_resume_non_failed_job` — RUNNING job returns 409
  - Integration: `test_resume_missing_archive` — archive deleted between failure and resume → 422
  - Integration: `test_resume_ingest_job_not_resumable` — IngestJob returns 409 with reason
  - Integration: `test_resume_wrong_namespace` — 404
  - Checkpoint: `uv run pytest tests/test_jobs_list_resume.py -v --no-cov -n0 -m integration`

---

### Phase 7 — MCP Tools
> **Releasable**: after Task 7.1; MCP clients can export and import collections via the protocol.

#### Task 7.1 — `export_collection` and `import_collection` MCP tools
- [ ] **File**: `archon_search/server/mcp.py`
- **Depends on**: Task 4.2, Task 5.2
- **Description**:
  - Follow the exact `@app.tool()` pattern from `ingest_file` (lines 793–835):
  - `export_collection(collection: str, output_path: str = "") -> dict`:
    - Validates args; calls the export endpoint logic directly (or via internal function extracted from the route handler) — does NOT make an HTTP call; constructs the job via `store.create_export(...)` directly
    - Returns `job_to_dict(job)` on success; returns `McpErrorResponse(error=..., code=...)` on `PathUnsafeError`, collection-not-found, etc.
    - Non-blocking: returns `JobResponse` dict immediately (job is QUEUED; client polls separately)
  - `import_collection(collection: str, path: str, force_overwrite: bool = False, ignore_schema_version: bool = False, on_error: str = "fail") -> dict`:
    - Same pattern; validates `on_error` is `"fail"` or `"skip"`; pre-validates archive; checks collection/schema/model; creates import job; returns `job_to_dict(job)`
  - Both tools use `DEFAULT_NAMESPACE` for the namespace, matching the pattern of `ingest_file` and `ingest_directory` MCP tools which also use `DEFAULT_NAMESPACE` implicitly. Namespace-aware MCP is not in scope for this plan.
  - Update `BREAKING.md` to note 2 new MCP tools (additive, not breaking; but tool count is documented)
- **Releasable**: after this task, MCP clients can trigger export/import
- **Tests (TDD)** — `tests/test_mcp_export.py` (new file):
  - Integration: `test_mcp_export_collection_returns_job` — tool call returns a dict with `job_id` and `status=QUEUED`
  - Integration: `test_mcp_export_path_unsafe` — returns `McpErrorResponse` with `code="path_unsafe"`
  - Integration: `test_mcp_import_collection_returns_job` — tool call returns job dict
  - Integration: `test_mcp_import_invalid_on_error` — returns `McpErrorResponse` for `on_error="invalid"`
  - Checkpoint: `uv run pytest tests/test_mcp_export.py -v --no-cov -n0 -m integration`

---

### Phase 8 — CLI Commands
> **Releasable**: after Task 8.2; operators can use `archon-search export` and `archon-search import` from the terminal.

#### Task 8.1 — `archon-search export` CLI command
- [ ] **File**: `archon_search/cli/export_cmd.py` (new file), `archon_search/cli/main.py`
- **Depends on**: Task 4.2
- **Description**:
  - `archon_search/cli/export_cmd.py`:
    - `@click.command("export")` with arguments: `collection: str` (required), options: `--output-dir PATH` (default: empty, resolved to data dir), `--wait / --no-wait` (default: no-wait), `--api-url TEXT` (default: `http://localhost:8765`, from config or env), `--api-key TEXT` (from env `ARCHON_SEARCH_API_KEY` or key file)
    - Calls `POST /collections/{collection}/export` via `httpx` (already a dep); on 202, prints `job_id`
    - `--wait` behavior: polls `GET /jobs/{job_id}` every 2 seconds; on each poll if `progress` is set, prints `[{phase}] {processed}/{total}`; on DONE, prints archive path from `result.archive_path`; on FAILED, prints error and exits with code 1; on CTRL+C, prints "Polling stopped — job continues on server" and exits 0
  - `archon_search/cli/main.py`: add `from archon_search.cli.export_cmd import export_cmd; main.add_command(export_cmd)`
- **Releasable**: after this task, `archon-search export <collection>` is usable
- **Tests (TDD)** — `tests/test_cli_export.py` (new file):
  - Unit: `test_export_cmd_prints_job_id` — mock HTTP response; command prints job_id and exits 0
  - Unit: `test_export_cmd_wait_prints_progress` — mock poll responses with progress; verifies progress output format
  - Unit: `test_export_cmd_wait_exits_1_on_failed` — mock FAILED response; exit code 1
  - Unit: `test_export_cmd_collection_not_found` — mock 404; exits 1 with error message
  - Checkpoint: `uv run pytest tests/test_cli_export.py -v --no-cov -n0`

#### Task 8.2 — `archon-search import` CLI command
- [ ] **File**: `archon_search/cli/export_cmd.py`, `archon_search/cli/main.py`
- **Depends on**: Task 5.2, Task 8.1
- **Description**:
  - `@click.command("import")` with arguments: `collection: str`, `path: str`; options: `--force-overwrite / --no-force-overwrite`, `--ignore-schema-version / --no-ignore-schema-version`, `--on-error [fail|skip]` (default: fail), `--wait / --no-wait`, `--api-url TEXT`, `--api-key TEXT`
  - Calls `POST /collections/{collection}/import` via httpx
  - `--wait` behavior: same polling pattern as export; on DONE, prints `{"imported": N, "skipped": M, "total": T}`; if `skipped > 0`, prints a warning line
  - Add `import_cmd` to `main.py`
- **Releasable**: after this task, the full export/import CLI workflow is available
- **Tests (TDD)** — `tests/test_cli_export.py`:
  - Unit: `test_import_cmd_prints_job_id` — mock 202; exits 0
  - Unit: `test_import_cmd_wait_prints_imported_count` — mock DONE with result; prints imported/skipped/total
  - Unit: `test_import_cmd_wait_warns_on_skipped` — `skipped > 0` prints warning
  - Unit: `test_import_cmd_collection_exists_no_force` — mock 409; exits 1 with message
  - Checkpoint: `uv run pytest tests/test_cli_export.py -v --no-cov -n0`

---

### Final Phase — Verification & Documentation

#### Task 9.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to discover all documentation in the project (READMEs, ADRs, API docs, architecture docs, user guides, `CLAUDE.md`, `CHANGELOG`) and update every file whose content is affected by D1+D2:
    - `Documentation/Architecture/600_api_reference_or_public_interface.md` — add `POST /collections/{name}/export`, `POST /collections/{name}/import`, `GET /jobs`, `POST /jobs/{job_id}/resume`; add `export_collection` and `import_collection` MCP tools; add `archon-search export` and `archon-search import` CLI commands
    - `Documentation/Architecture/120_services_and_integration_architecture.md` — add scheduler as a background service; document QUEUED status and bulk job lifecycle
    - `Documentation/Architecture/200_testing_strategy.md` — note `integration` marker on export/import tests
    - `CLAUDE.md` — update MCP tool count (was 10, now 12); add `archon-search export/import` to CLI subcommands list; note `[jobs]` config section
    - `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md` — remove the "job list endpoint missing" item if present
    - `archon-search.toml.example` — add `[jobs]` section with `max_concurrent_bulk = 1` and `checkpoint_interval = 100`
    - `Documentation/Architecture/510_release_and_environment_strategy.md` — note that `BREAKING.md` has entries for `QUEUED` status and `progress` field
  - Do NOT touch unrelated docs.
  - Verify all acceptance criteria below before marking complete.
- **Releasable**: after this task, the feature is fully shipped and documented.
- **Acceptance criteria** (must all pass):
  - **AC1**: `uv run pytest -n auto` passes full suite with coverage ≥ 85%; no new failures
  - **AC2**: `uv run pytest tests/test_job_store_queued.py tests/test_export_archive.py tests/test_scheduler.py tests/test_path_safety_export.py -v --no-cov -n0` — all unit tests pass
  - **AC3**: `uv run pytest tests/test_routes_export.py tests/test_jobs_list_resume.py tests/test_mcp_export.py -v --no-cov -n0 -m integration` — all integration tests pass
  - **AC4**: `uv run pytest tests/test_cli_export.py -v --no-cov -n0` — CLI unit tests pass
  - **AC5**: `curl -X POST http://localhost:8765/collections/test/export -H "Authorization: Bearer $KEY" -d '{}' | jq .status` returns `"QUEUED"` against a running server with a seeded collection
  - **AC6**: Full export → import round-trip: export a collection, import it into a new collection name, search results match
  - **AC7**: `GET /jobs` returns paginated results with `next_cursor` when > 50 jobs exist
  - **AC8**: `POST /jobs/{id}/resume` transitions a FAILED export job to QUEUED
  - **AC9**: `BREAKING.md` has entries for `progress` field addition and `QUEUED` status
  - **AC10**: `archon-search.toml.example` includes `[jobs]` section
  - **AC11**: `GET /jobs/{job_id}` response includes `"progress": null` for an existing IngestJob (backward compat)
  - **AC12**: A path outside `get_data_dir()` in `POST /collections/{name}/export` returns `400`
  - **AC13**: An archive with a tar-traversal entry (`../../etc/passwd`) is rejected on import with `422`
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every AC1–AC13 above is checked.
