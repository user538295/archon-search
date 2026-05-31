# C1 — Per-Collection Embedding Model

**Purpose**: Allow operators to assign a distinct fastembed model to each collection, lazy-load embedders on demand via an LRU cache, and keep searches coherent during model-change transitions via an explicit reindex lifecycle.
**Audience**: archon-search contributors implementing C1; reviewers; operators who will use the new PATCH endpoint and reindex flow.
**Status**: To Do

---

## Background

All collections are locked to the server's single global embedding model. Operators who need multilingual, domain-specific, or upgraded models per corpus cannot express that configuration. Collections with a model that diverges from the global default are silently excluded from multi-collection search. C1 fixes this by introducing per-collection model state, a server-side LRU embedder cache, a new `PATCH /collections/{name}` endpoint, and an explicit reindex lifecycle that maintains query/vector space coherence during transitions.

## Goal

After C1: an operator can create a collection with a custom `embedding_model`, change it later via `PATCH /collections/{name}` (sets `pending_embedding_model`), and trigger a reindex job that promotes the pending model to `active_embedding_model` on success. Searches during the transition use `active_embedding_model` (old model). The sync/watcher path no longer spuriously reindexes non-default-model collections. The LRU cache lazy-loads models and is safe under concurrent async requests.

---

## Scope

### In Scope
- `active_embedding_model` / `pending_embedding_model` / `needs_reindex` / `reindex_job_id` on `CollectionMeta`
- `migrate_per_collection_model()` migration with 3-state crash-recovery idempotency
- `embedder_cache_size` and `eager_load_embedders` config keys; `EmbedderCache` class in `app.state`
- Pipeline IoC: `search`, `ingest_file`, `ingest_directory`, `explain`, `search_with_context`, `recompute_collection_meta` receive embedder as parameter; `self._embedder` renamed `self._global_embedder`
- `PATCH /collections/{name}` — new endpoint with full state machine (states a, a′, b, c, d)
- Dimension validation at PATCH/POST time (stored dim from LanceDB arrow schema; new model dim from `list_supported_models()` or timeout-guarded instantiation)
- `POST /collections/` gains optional `embedding_model` field
- `GET /collections/{name}` fix: returns `active_embedding_model` (was `config.embedding_model`)
- `GET /collections/` — add `active_embedding_model`, `needs_reindex` to `CollectionSummary`
- `SearchResponse` gains `embedding_model: str` field
- Router: `_ROUTING_FIELDS` and `_score_collections` updated to `active_embedding_model`
- `_reindex_task` function (separate from `_default_ingest_task`); `IngestJob.target_embedding_model` field
- Reindex endpoint: reads `pending_embedding_model`, sets `target_embedding_model`, sets `reindex_job_id`
- Sync/watcher read-side fix (compare `CollectionMeta.active_embedding_model` per collection) and write-side fix (write `active_embedding_model` not global to state store)
- `update_collection` MCP tool (11th tool)
- CLI `archon-search collection reindex {name}` fix (resolution rules, write-back, failure semantics)
- `/status` endpoint update: lists collections with `needs_reindex: true`
- `BREAKING.md` entry for the field rename

### Out of Scope
- Multi-model query fanout
- Automatic reindex on model change
- Per-collection reranker model
- Multilingual FTS changes (C2)
- TOML-level per-collection model config
- Cross-dimension model migration (reindex does not drop/recreate vector table)

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 12.1 — Final verification & documentation update].

---

## What does NOT change
- Public REST/MCP API contracts not listed in In Scope above
- `search_many` signature — continues to embed with `self._global_embedder` (cross-model fanout excluded from scope)
- Router algorithm, `_cosine_similarity`, multi-collection exclusion logic (already works; only field name changes)
- `ingest_chunks` internal logic (only the `embedding_model` argument source changes)
- `JobStatus` enum values (PENDING, RUNNING, DONE, FAILED, CANCELLED, CANCELLING)
- Telemetry no-raw-query invariant
- Test marker rules and coverage gate

---

## Known limitations / accepted trade-offs
- **Dimension mismatch blocks reindex**: if new model dimension differs from stored vectors' dimension, PATCH returns 422 and the operator must delete + recreate the collection. Cross-dimension migration is a future item.
- **LRU cache is in-process only**: no cross-process embedder sharing. Consistent with existing store-lock limitation.
- **CLI reindex has no JobStore integration**: CLI calls `pipeline.ingest_directory` directly; no `reindex_job_id` is set during CLI reindex. Operators who need job tracking must use the REST API.
- **`eager_load_embedders` does not warm the router cache** (A6's concern): newly warmed embedders are available from the LRU cache for searches but the router's description-embedding cache is unaffected.
- **`embedding_model` column rename strategy**: LanceDB does not support atomic column rename. The migration adds `active_embedding_model` (value copied from `embedding_model` via computed SQL expression) and new columns; the old `embedding_model` column is dropped using `alter_columns` if the LanceDB version supports it, otherwise left in the physical schema but never read/written by application code. The `_meta_schema` no longer includes `embedding_model`.
- **Partial reindex failure leaves mixed-model vectors**: `ingest_directory` processes files individually; a failure after partial completion leaves some chunks with new-model vectors alongside old-model vectors. `active_embedding_model` correctly reflects the old model (searches use old embedder), but the new-model chunks will produce garbage similarity scores, degrading result quality until a full reindex succeeds. Operator remediation: re-trigger the reindex (or delete and recreate the collection in the worst case). This is documented as accepted risk.
- **`_reindex_task` C1 field promotion is not atomic with concurrent description/centroid updates**: The success-path promotion in `_reindex_task` (step 5) does a read-then-write of `CollectionMeta` without holding a lock across both operations. A concurrent `update_description`, `recompute_collection_meta`, or incremental centroid write can overwrite the row between the read and write, causing `_reindex_task` to overwrite that operation's changes with stale values for `description`, `centroid`, `doc_count`, etc. The C1 fields (`active_embedding_model`, `pending_embedding_model`, `needs_reindex`, `reindex_job_id`) will be correctly promoted, but stale non-C1 fields may overwrite concurrent updates. **Mitigation**: None automatic. `recompute_collection_meta` runs INSIDE `ingest_directory` (called in step 4), BEFORE the stale read-modify-write in step 5 — it cannot heal a write that hasn't happened yet. No automatic recompute is triggered AFTER step 5. The worst case is permanently stale centroid/description data that persists until the next file-change event triggers a sync recompute (which may never happen for static collections). Operator remediation: manually trigger `GET /search?collection=... &force_recompute=true` (or wait for the next file-change event). A future improvement: add an explicit `recompute_collection_meta(force=True)` call in `_reindex_task` immediately after `update_collection_meta` in step 5. This is documented as accepted risk; a future improvement would add a partial `UPDATE` store method that applies only C1 fields under the lock.
- **PATCH revert during in-flight reindex does not cancel the reindex**: If an operator issues a PATCH revert (state c: `active=A, pending=B, request=A`) while a reindex is running with `target_model=B`, the reindex completion will still promote `active_embedding_model=B`, overriding the revert. The operator receives no notification that the revert was superseded. Operator remediation: after observing `active_embedding_model=B` post-reindex, re-issue `PATCH` to restore model A and trigger a new reindex. Cancellation of in-flight reindex jobs is a future item.
- **`PATCH /collections/{name}` handler is also subject to read-modify-write races**: The PATCH handler reads `CollectionMeta`, checks `reindex_job_id`, modifies state machine fields, and writes back — all without holding a lock across the operation. A `_reindex_task` that completes between PATCH's read and write will have its promotion (active/pending/reindex_job_id updates) overwritten by PATCH's stale write. The worst case: PATCH reads `active=A, pending=B`, the reindex completes and promotes `active=B, pending=None`, PATCH writes `active=A, pending=C` (overwriting the promotion). Operator remediation: GET the collection state and re-issue PATCH if needed. This is documented as accepted risk.

---

## Architecture

### New dataclass fields — `archon_search/collection_meta.py`

```python
@dataclasses.dataclass
class CollectionMeta:
    # --- existing fields ---
    name: str
    description: str | None = None
    centroid: list[float] | None = None
    centroid_sum: list[float] | None = None
    mutations_since_recompute: int = 0
    needs_recompute: bool = False
    doc_count: int = 0
    chunk_count: int = 0
    last_indexed: datetime | None = None
    last_described: datetime | None = None
    described_at_doc_count: int | None = None
    namespace: str = DEFAULT_NAMESPACE
    description_embedding: list[float] | None = None
    # --- C1 additions ---
    active_embedding_model: str = ""           # renamed from embedding_model
    pending_embedding_model: str | None = None
    needs_reindex: bool = False
    reindex_job_id: str | None = None
```

### New `_meta_schema` columns — `archon_search/store.py`

Replace `pa.field("embedding_model", pa.utf8())` with:
- `pa.field("active_embedding_model", pa.utf8())` — nullable=False (default `""`)
- `pa.field("pending_embedding_model", pa.utf8(), nullable=True)` — `None` → `""`
- `pa.field("needs_reindex", pa.bool_(), nullable=True)` — default `False`
- `pa.field("reindex_job_id", pa.utf8(), nullable=True)` — default `""`

### New config keys — `archon_search/config.py`

```python
embedder_cache_size: int = 3       # LRU cache max entries
eager_load_embedders: bool = False  # pre-warm all distinct models at startup
```

### New class — `archon_search/embedder_cache.py`

```python
class EmbedderCache:
    def __init__(self, max_size: int) -> None: ...
    async def get_or_load(self, model_name: str) -> Embedder:
        # 1. Check LRU cache under asyncio.Lock (O(1))
        # 2. On miss: check per-model asyncio.Event; if loading: await event
        # 3. If not loading: set event, release lock, load via asyncio.to_thread(make_embedder, ...)
        # 4. On load complete: acquire lock, store in cache (evict LRU if max_size reached), set event
        # 5. Lock is NEVER held across asyncio.to_thread boundary
```

### New IngestJob field — `archon_search/types.py`

```python
@dataclass
class ReindexJob(IngestJob):
    target_embedding_model: str | None = None  # captured at job-creation time
```

### Pipeline method signature changes — `archon_search/pipeline.py`

```python
async def search(self, query: str, collection: str, namespace: str = DEFAULT_NAMESPACE, *, embedder: Embedder, filters: SearchFilters | None = None) -> SearchPipelineResult
async def ingest_file(self, path: Path, collection: str, ..., embedder: Embedder, namespace: str = DEFAULT_NAMESPACE, ...) -> None
async def ingest_directory(self, path: Path, collection: str, ..., embedder: Embedder, namespace: str = DEFAULT_NAMESPACE, ...) -> None
async def explain(self, query: str, collection: str | None = None, ..., embedder: Embedder | None = None) -> ExplainPipelineResult
    # embedder=None → use self._global_embedder (multi-collection path)
    # embedder=<value> → use this (single-collection path)
async def search_with_context(self, query: str, collection: str, context_window: int = 1, namespace: str = DEFAULT_NAMESPACE, *, embedder: Embedder, filters: SearchFilters | None = None) -> list[dict[str, Any]]
async def recompute_collection_meta(self, collection: str, global_embedder: Embedder, namespace: str = DEFAULT_NAMESPACE, force: bool = False) -> None
    # global_embedder: used for description_embedding only
    # centroid computed from stored vectors; active_embedding_model preserved from existing row
```

### New PATCH endpoint — `archon_search/server/routes_collections.py`

```python
@router.patch("/{name}", status_code=200, response_model=CollectionDetail)
async def patch_collection(name: str, body: PatchCollectionBody, request: Request) -> CollectionDetail | JSONResponse
```

### New MCP tool — `archon_search/server/mcp.py`

```python
# Tool 11: update_collection
# Wraps PATCH /collections/{name}; enforces namespace isolation via MCP auth layer
```

---

## Task breakdown

### Phase 1 — Data Models & Migration
> **Releasable**: after Task 1.5 — new fields exist in CollectionMeta and store schema; migration runs at startup; IngestJob carries `target_embedding_model`; config keys are loaded. No behavior change yet; all fields default to safe values.

#### Task 1.1 — CollectionMeta: add `active_embedding_model`, `pending_embedding_model`, `needs_reindex`, `reindex_job_id`; rename `embedding_model`
- [x] **File**: `archon_search/collection_meta.py`
- **Depends on**: nothing
- **Description**:
  - Rename `embedding_model: str = ""` to `active_embedding_model: str = ""`.
  - Add `pending_embedding_model: str | None = None`, `needs_reindex: bool = False`, `reindex_job_id: str | None = None`.
  - Update every existing reference to `meta.embedding_model` in `collection_meta.py` itself (if any) to `meta.active_embedding_model`.
  - No behavior change — these are inert new fields until later tasks wire them up.
- **Releasable**: `CollectionMeta` carries the new fields; existing code that references `meta.embedding_model` will fail type-checking and must be updated in Task 1.2.
- **Tests (TDD)** — `tests/test_collection_meta.py`:
  - Unit: `test_active_embedding_model_defaults_to_empty_string` — fresh `CollectionMeta("foo")` has `active_embedding_model=""`.
  - Unit: `test_pending_embedding_model_defaults_to_none` — fresh instance has `pending_embedding_model=None`.
  - Unit: `test_needs_reindex_defaults_to_false` — fresh instance has `needs_reindex=False`.
  - Unit: `test_reindex_job_id_defaults_to_none` — fresh instance has `reindex_job_id=None`.
  - Checkpoint: `uv run pytest tests/test_collection_meta.py -v`

#### Task 1.2 — store.py: update `_meta_schema`, `_row_to_meta`, `update_collection_meta` for C1 fields
- [x] **File**: `archon_search/store.py`
- **Depends on**: Task 1.1
- **Description**:
  - In `_meta_schema()`: replace `pa.field("embedding_model", pa.utf8())` with `pa.field("active_embedding_model", pa.utf8())`. Add `pa.field("pending_embedding_model", pa.utf8(), nullable=True)`, `pa.field("needs_reindex", pa.bool_(), nullable=True)`, `pa.field("reindex_job_id", pa.utf8(), nullable=True)`.
  - In `_row_to_meta()`: read `active_embedding_model=row.get("active_embedding_model", "")`, `pending_embedding_model=row.get("pending_embedding_model") or None`, `needs_reindex=bool(row.get("needs_reindex") or False)`, `reindex_job_id=row.get("reindex_job_id") or None`. Remove the `embedding_model` read.
  - In `update_collection_meta()` (and `_do_write_meta_unlocked`): replace `"embedding_model": meta.embedding_model` with `"active_embedding_model": meta.active_embedding_model`. Add `"pending_embedding_model": meta.pending_embedding_model or ""`, `"needs_reindex": meta.needs_reindex`, `"reindex_job_id": meta.reindex_job_id or ""`.
  - Update every other call site in `store.py` that constructs a `CollectionMeta(...)` with `embedding_model=` to use `active_embedding_model=` instead.
  - **Critical construction sites that must carry through ALL C1 fields from `existing_meta`:**
    - `update_description()`: after reading `existing_meta`, the reconstructed `CollectionMeta` must explicitly set `pending_embedding_model=existing_meta.pending_embedding_model`, `needs_reindex=existing_meta.needs_reindex`, `reindex_job_id=existing_meta.reindex_job_id`. Failing to propagate these fields will silently clear a live reindex state.
    - `_do_update_meta_on_add()` (incremental centroid path): for EXISTING collections, `active_embedding_model` must be read from `existing.active_embedding_model`, NOT from the writer's `embedding_model` argument. The writer's model (used for chunk encoding) is NOT authoritative for `active_embedding_model`. Propagate all C1 fields from `existing`. For brand-NEW collections (no existing row), use `active_embedding_model=embedding_model` (the writer's model) with `pending_embedding_model=None`, `needs_reindex=False`, `reindex_job_id=None`.
    Note: `_do_update_meta_on_add` has THREE construction sites for existing collections (not two). The third and most critical is the incremental-update happy path (approximately line 916-931 in the current codebase). Run `grep -n "CollectionMeta(" archon_search/store.py | head -30` to enumerate ALL sites before implementation. For EACH existing-collection site: `active_embedding_model=existing.active_embedding_model` (NOT `embedding_model=embedding_model`).
    - `_do_subtract_meta_on_delete()`: replace `existing.embedding_model` with `existing.active_embedding_model`; propagate all other C1 fields from `existing`.
    - All other `CollectionMeta(...)` construction sites in `store.py`: do a `grep -n "CollectionMeta(" store.py` inventory before implementation; every site must be explicitly handled — partial updates must carry through all C1 fields from the existing row.
  - The existing `centroid_sum`, `mutations_since_recompute`, `needs_recompute` fields (B5) are unaffected.
- **Releasable**: `CollectionMeta` round-trips through `update_collection_meta` / `get_collection_meta` with all C1 fields.
- **Tests (TDD)** — `tests/test_store.py`:
  - Unit: `test_active_embedding_model_round_trips` — write meta with `active_embedding_model="model-X"`, read back; value matches.
  - Unit: `test_pending_embedding_model_round_trips_none` — write `pending_embedding_model=None`; read back as `None`.
  - Unit: `test_pending_embedding_model_round_trips_value` — write `pending_embedding_model="model-Y"`; read back as `"model-Y"`.
  - Unit: `test_needs_reindex_round_trips_true` — write `needs_reindex=True`; read back as `True`.
  - Unit: `test_reindex_job_id_round_trips` — write `reindex_job_id="job-123"`; read back as `"job-123"`.
  - Unit: `test_reindex_job_id_round_trips_none` — write `reindex_job_id=None`; read back as `None`.
  - Checkpoint: `uv run pytest tests/test_store.py -v -k "active_embedding or pending_embedding or needs_reindex or reindex_job_id"`

#### Task 1.3 — `migrate_per_collection_model()` with 3-state crash-recovery idempotency
- [x] **File**: `archon_search/store.py`
- **Depends on**: Task 1.2
- **Description**:
  - `async def migrate_per_collection_model(self) -> None:` — follows the pattern of `migrate_namespace` (lines 531–548).
  - **State detection** (read schema names before acting):
    - (a) `embedding_model` exists, `active_embedding_model` absent: copy `embedding_model` values into a new `active_embedding_model` column via `table.add_columns({"active_embedding_model": "embedding_model"})` (SQL expression copies values); then add `pending_embedding_model`, `needs_reindex`, `reindex_job_id` columns with safe defaults. Attempt to drop `embedding_model` via `alter_columns` / `drop_column` if the LanceDB version supports it; otherwise leave it in the physical schema with a log warning.
    - (b) `active_embedding_model` exists but one or more of `pending_embedding_model` / `needs_reindex` / `reindex_job_id` absent: skip rename step, add only the missing columns.
    - (c) All four columns present: no-op (return immediately).
  - Column defaults for new columns: `pending_embedding_model=""`, `needs_reindex=false`, `reindex_job_id=""`.
  - Catch `RuntimeError` with "already exists" in message for concurrent migration guard.
  - Wire into `app.py` lifespan alongside `migrate_namespace`, `migrate_description_embedding`, `migrate_acl`, `migrate_centroid_sum`: add `await app.state.search_store.migrate_per_collection_model()`.
- **Releasable**: existing pre-C1 databases gain the new columns on next startup; all three crash-recovery states are safe to re-run.
- **Tests (TDD)** — `tests/test_store.py`:
  - Unit: `test_migrate_per_collection_model_state_a` — create a meta table with `embedding_model` column having a row with value `"BAAI/bge-small-en-v1.5"`; run migration; verify (a) `active_embedding_model` column exists AND (b) the row's `active_embedding_model` value equals `"BAAI/bge-small-en-v1.5"` (NOT the literal string `"embedding_model"`). Also add fallback strategy: "If LanceDB's `add_columns` does not support column-reference expressions (verify by running the migration on a test DB and checking that values are copied, not the literal string `'embedding_model'`): use a two-step approach: (1) `table.add_columns({'active_embedding_model': "''"})` to add the column with empty default; (2) read all rows with `table.to_pandas()` (or equivalent async iterator), compute `{row.name: row.embedding_model for row in rows}` as a dict mapping, then for each row call `table.delete(f'name = \"{row.name}\"')` and re-insert with `active_embedding_model` set to the correct value. This is the pattern used by the existing `_do_write_meta_unlocked` method."
  - Unit: `test_migrate_per_collection_model_state_a_multi_row` — create meta table with 3 rows having distinct `embedding_model` values (`"model-A"`, `"model-B"`, `"model-C"`); run migration; verify each row's `active_embedding_model` matches its original `embedding_model` value (i.e., row with `embedding_model="model-A"` gets `active_embedding_model="model-A"`, NOT `"embedding_model"` or `"model-B"`). This is the key test for the literal-string copy bug: a single-row test cannot detect if `add_columns` copies the literal column name instead of values.
  - Unit: `test_migrate_per_collection_model_state_a_fallback_on_expression_failure` — mock `table.add_columns` to raise `RuntimeError` when called with a SQL expression argument (simulating a LanceDB version that doesn't support column-reference expressions); verify the fallback path (add column with empty default, then read all rows and re-insert with correct values) is taken; verify that all row values are correctly copied (not the literal string `"embedding_model"`).
  - Unit: `test_migrate_per_collection_model_state_b` — create a meta table with `active_embedding_model` but missing `needs_reindex`; run migration; verify missing columns added, no rename attempted.
  - Unit: `test_migrate_per_collection_model_state_c` — run migration on a fully-migrated table; verify no error and schema unchanged.
  - Unit: `test_migrate_per_collection_model_idempotent` — run state-(a) migration twice; second call is a no-op.
  - Unit: `test_migrate_per_collection_model_no_meta_table_noop` — no `_META_TABLE`; returns without error.
  - Unit: `test_migrate_per_collection_model_backfills_global_default` — row with empty `embedding_model`; after migration, `active_embedding_model=""` (not an error — global default is `""`; the route layer fills in the real model name for new collections).
  - Integration (`@pytest.mark.integration`): `test_migrate_per_collection_model_state_b_crash_recovery` — simulates crash recovery: insert rows with `active_embedding_model` set but `pending_embedding_model` column absent; run `migrate_per_collection_model`; verify all rows get `pending_embedding_model=None`, `needs_reindex=False`, `reindex_job_id=None`. (AC 45)
  - Checkpoint: `uv run pytest tests/test_store.py -v -k "migrate_per_collection_model"`

#### Task 1.4 — `ReindexJob.target_embedding_model` field + JSON serialization
- [x] **File**: `archon_search/types.py`, `archon_search/jobs/store.py` (or wherever `IngestJob` is serialized/deserialized)
- **Depends on**: nothing
- **Description**:
  - Add `target_embedding_model: str | None = None` to the `ReindexJob` dataclass (subclass of `IngestJob`). `ReindexJob` is already defined with `pass`; add this one field.
  - Update JSON serialization: when serializing a `ReindexJob`, include `"target_embedding_model": job.target_embedding_model`. When deserializing, treat a missing `"target_embedding_model"` key as `None` (backwards compat for in-flight jobs created before C1).
  - Verify the `JobStore` `get` / `create_job` round-trips the new field correctly.
  - **Type discriminator in JobStore serialization**: `ReindexJob` and `DeleteJob` must survive server restarts by round-tripping through `JobStore`. Add the following changes:
    - In `_write_atomic()`: after `dataclasses.asdict(job)`, inject `"job_type"` based on the concrete class: `"reindex"` for `ReindexJob`, `"delete"` for `DeleteJob`, `"ingest"` for `IngestJob`. Use `isinstance(job, ReindexJob)` then `isinstance(job, DeleteJob)` then default to `"ingest"`.
    - In `_load()`: **type dispatch MUST happen BEFORE crash-recovery `dataclasses.replace()`**. Steps: (1) read `job_type = item.pop("job_type", "ingest")`; (2) dispatch: `"reindex"` -> `ReindexJob(**item)`, `"delete"` -> `DeleteJob(**item)`, default -> `IngestJob(**item)`. (3) THEN apply crash-recovery if needed: `dataclasses.replace(job, status=JobStatus.FAILED, ...)`. Because `dataclasses.replace()` preserves the concrete subclass, `target_embedding_model` is preserved through crash-recovery.
    - `jobs_store.py` import: add `from archon_search.types import ReindexJob, DeleteJob` (or wherever they live).
    - Jobs serialized BEFORE C1 (no `job_type` field): `item.pop("job_type", "ingest")` defaults to `"ingest"` — safe backwards compat.
  - **Return type annotation note**: `JobStore.get()` and `JobStore.update()` both have `-> IngestJob | None` and `-> IngestJob` annotations respectively. At runtime, the concrete subclass (`ReindexJob`, `DeleteJob`) is preserved by `_load()` and `dataclasses.replace()`. The annotations are intentionally not changed to union types — callers that need subclass fields must use `isinstance` checks. The `_reindex_task` invariant guards (explicit isinstance check) are the enforcement mechanism.
- **Releasable**: `ReindexJob` carries `target_embedding_model`; legacy jobs without the field deserialize to `target_embedding_model=None`.
- **Tests (TDD)** — `tests/test_jobs.py` (or the job-store test file):
  - Unit: `test_reindex_job_target_embedding_model_defaults_to_none` — fresh `ReindexJob` has `target_embedding_model=None`.
  - Unit: `test_reindex_job_target_embedding_model_round_trips` — serialize `ReindexJob(target_embedding_model="model-X")`; deserialize; value preserved.
  - Unit: `test_reindex_job_missing_field_deserializes_to_none` — deserialize JSON without `target_embedding_model` key; field is `None` (no `KeyError`).
  - Unit: `test_job_store_round_trips_reindex_job_type` — call `job_store.create_job(ReindexJob(target_embedding_model="model-X", ...))` → `job = job_store.get(job_id)` → assert `isinstance(job, ReindexJob)` AND `job.target_embedding_model == "model-X"`. Verifies that `_load()` correctly dispatches on the type discriminator rather than always producing `IngestJob`.
  - Unit: `test_job_store_round_trips_delete_job_type` — `job_store.create_job(DeleteJob(deleted_ids=["id1", "id2"], ...))` → `job = job_store.get(job_id)` → assert `isinstance(job, DeleteJob)` AND `job.deleted_ids == ["id1", "id2"]`.
  - Unit: `test_job_store_crash_recovery_preserves_reindex_job_subclass` — serialize `ReindexJob(status=RUNNING, target_embedding_model="model-X")`; simulate crash (create new `JobStore` pointing to same file); load; assert `isinstance(job, ReindexJob)` AND `job.target_embedding_model == "model-X"` AND `job.status == FAILED` (crash-recovery applied). This is the critical regression test for the `type-dispatch BEFORE replace` ordering requirement.
  - Unit: `test_job_store_write_includes_job_type_field` — serialize a `ReindexJob`; read the raw JSON file; assert `"job_type"` key is present with value `"reindex"`. (Guards against `_write_atomic` omitting the discriminator.)
  - Checkpoint: `uv run pytest tests/test_jobs.py -v -k "target_embedding_model"`
  - Checkpoint: `uv run pytest tests/test_jobs.py -v -k "round_trips_reindex_job_type"`

#### Task 1.5 — Config keys: `embedder_cache_size`, `eager_load_embedders`
- [x] **File**: `archon_search/config.py`
- **Depends on**: nothing
- **Description**:
  - Add `embedder_cache_size: int = 3` and `eager_load_embedders: bool = False` to `SearchConfig`.
  - Add validation in config loading: raise `ConfigurationError` (or `ValueError`) if `embedder_cache_size < 1`. Document in `archon-search.toml.example` that the minimum value is 1.
  - Add to `archon-search.toml.example` with comments.
  - No behavior wired yet.
- **Releasable**: config keys are loaded from TOML; existing configs that don't include them get safe defaults.
- **Tests (TDD)** — `tests/test_config.py`:
  - Unit: `test_embedder_cache_size_defaults_to_3` — default config has `embedder_cache_size=3`.
  - Unit: `test_eager_load_embedders_defaults_to_false` — default config has `eager_load_embedders=False`.
  - Unit: `test_embedder_cache_size_from_toml` — TOML with `embedder_cache_size = 5` loads as `5`.
  - Unit: `test_eager_load_embedders_from_toml` — TOML with `eager_load_embedders = true` loads as `True`.
  - Unit: `test_embedder_cache_size_zero_raises` — config with `embedder_cache_size = 0`; assert `ConfigurationError` (or `ValueError`) raised.
  - Unit: `test_embedder_cache_size_negative_raises` — config with `embedder_cache_size = -1`; assert `ConfigurationError` (or `ValueError`) raised.
  - Checkpoint: `uv run pytest tests/test_config.py -v -k "embedder_cache_size or eager_load_embedders"`

---

### Phase 2 — LRU Embedder Cache
> **Releasable**: after Task 2.2 — `EmbedderCache` is in `app.state`, accessible to route handlers. First search requests will use the cache; all existing tests still pass.

#### Task 2.1 — `EmbedderCache` class
- [x] **File**: `archon_search/embedder_cache.py` (new file)
- **Depends on**: Task 1.5
- **Description**:
  - `class EmbedderCache`:
    - `__init__(self, max_size: int) -> None`: stores `max_size`; initializes `_cache: OrderedDict[str, Embedder]` (from `collections.OrderedDict` — use `move_to_end(key)` for O(1) access-order maintenance on cache hit and `popitem(last=False)` for O(1) LRU eviction), `_lock: asyncio.Lock`, `_loading: dict[str, asyncio.Event]`.
    - `async def get_or_load(self, model_name: str) -> Embedder`:
      1. Acquire `_lock`. Check `_cache` for `model_name`. On HIT: call `_cache.move_to_end(model_name)` (O(1) with `OrderedDict`), release lock, return embedder.
      2. On MISS: check `_loading` for a concurrent load event. If event exists: release lock, await event, re-enter step 1.
      3. No existing event: create `asyncio.Event()` in `_loading[model_name]`. Release lock. Load via `await asyncio.to_thread(make_embedder, model_name)`, wrapped in `try/finally`:
         ```
         try:
             embedder = await asyncio.to_thread(make_embedder, model_name)
         except Exception:
             # Cleanup: acquire lock, remove event from _loading (NOT set it yet), set it so waiters wake up and find nothing in cache, then re-raise
             async with _lock:
                 event = _loading.pop(model_name, None)
                 if event:
                     event.set()  # wake waiters so they don't deadlock
             raise
         ```
         Waiters that were waiting on this event will re-enter step 1 after waking, find a cache miss and no loading event, and will attempt to load themselves. The first one to acquire the lock will kick off a new load. This means a failed load propagates as an exception to the original caller and all waiters retry.
      4. Acquire lock. Store in `_cache`. If `len(_cache) > max_size`: evict via `lru_key, _ = _cache.popitem(last=False)` (removes least-recently-used entry; Python reference counting keeps the evicted Embedder alive in any caller already holding it). Remove `_loading[model_name]` event. Set the event (wake waiting coroutines). Release lock. Return embedder.
      5. **Lock invariant**: the lock is NEVER held during `asyncio.to_thread`. At most one `to_thread` load per model_name runs concurrently; all others await the event.
    - `async def preload(self, model_names: list[str]) -> None`: calls `asyncio.gather(*[self.get_or_load(m) for m in model_names], return_exceptions=True)`. Exceptions (unknown models) are logged as warnings and suppressed — startup must not abort. (AC 38, 55)
    - `def cached_models(self) -> list[str]`: returns list of currently cached model names (snapshot).
  - Module-level comment documenting which validation path is used for unknown models (see Task 4.2).
- **Releasable**: `EmbedderCache` is usable standalone; tests can verify concurrency properties in isolation.
- **Tests (TDD)** — `tests/test_embedder_cache.py`:
  - Unit: `test_get_or_load_returns_embedder` — mock `make_embedder`; first call returns an `Embedder`.
  - Unit: `test_get_or_load_caches_result` — second call for same model does NOT call `make_embedder` again (mock call count = 1).
  - Unit: `test_lru_eviction_removes_oldest` — `max_size=1`; load model-A, then model-B; `cached_models()` contains only model-B.
  - Unit: `test_evicted_embedder_still_usable_by_caller` — hold reference to model-A embedder; evict via model-B load; use model-A embedder's `model_name` attribute — no exception (reference counting keeps it alive). (AC 54)
  - Unit: `test_concurrent_eviction_burst` — `max_size=2`; fire 4 concurrent `get_or_load` calls for 4 distinct model names; assert all 4 return the correct embedder instance; assert `len(cache._cache) <= 2` after all complete; assert the 2 retained entries are the 2 most recently accessed. (Tests the lock-acquire-store-evict-release sequence under concurrent burst with multiple evictions.)
  - Unit: `test_concurrent_miss_deduplication` — mock `make_embedder` with a slow coroutine (asyncio.sleep); fire 3 concurrent `get_or_load` calls for the same model; assert `make_embedder` called exactly once. Also assert all 3 concurrent callers received the same embedder instance (identity check: `e1 is e2 is e3`). (AC 52)
  - Unit: `test_concurrent_eviction_safety` — `max_size=1`; two concurrent calls for different models both return correct embedders; neither raises. (AC 54)
  - Unit: `test_preload_skips_unknown_model_without_abort` — mock `make_embedder` to raise for one model; `preload(["good", "bad"])` completes without exception; "good" is cached. (AC 38)
  - Unit: `test_preload_uses_asyncio_to_thread` — verify `asyncio.to_thread` is called (not direct `make_embedder`) during load (spy or monkeypatch). (AC 55)
  - Unit: `test_get_or_load_make_embedder_raises_cleans_up_loading_event` — mock `make_embedder` to raise `ValueError`; call `get_or_load` and verify the exception propagates (not swallowed); then verify a SECOND call to `get_or_load` for the same model name succeeds (the loading event was cleaned up, not left dangling). (AC 52 error-path variant)
  - Unit: `test_concurrent_waiters_retry_after_failed_load` — fire 3 concurrent `get_or_load` calls for the same model; mock `make_embedder` to fail on the first call and succeed on the second; verify using `asyncio.wait_for` with a 2s timeout that all 3 callers complete (either with the embedder or with exactly 1 raising and the other 2 retrying successfully); assert total `make_embedder` call count is 2 (1 failed + 1 successful retry). (Deadlock guard — if cache cleanup logic is broken, the 2s timeout fires.)
  - Unit: `test_preload_failure_does_not_leave_dangling_loading_event` — call `preload(["good-model", "bad-model"])` with `make_embedder` failing for "bad-model"; after `preload` returns, call `get_or_load("bad-model")` with `make_embedder` now succeeding; use `asyncio.wait_for(get_or_load("bad-model"), timeout=2.0)` and assert it completes without hanging.
  - Checkpoint: `uv run pytest tests/test_embedder_cache.py -v`

#### Task 2.2 — App.state integration: cache init, eager loading, lifespan wiring
- [ ] **File**: `archon_search/server/app.py`, `archon_search/server/mcp.py`
- **Depends on**: Task 2.1, Task 1.3 (migration must run before eager load)
- **Description**:
  - After all migrations complete in the lifespan startup, add:
    ```python
    embedder_cache = EmbedderCache(config.embedder_cache_size)
    app.state.embedder_cache = embedder_cache
    if config.eager_load_embedders:
        metas = await app.state.search_store.get_all_collections_meta()
        distinct_models = {m.active_embedding_model for m in metas if m.active_embedding_model}
        await embedder_cache.preload(list(distinct_models))
    ```
  - Use the existing `SearchStore.get_all_collections_meta()` method (which reads all meta rows from `_META_TABLE`, all namespaces). Do NOT create a new method if this existing one covers the use case — verify by reading `store.py` for `get_all_collections_meta` before adding anything new.
  - Route handlers resolve the embedder cache via `request.app.state.embedder_cache`.
  - On shutdown: no explicit cleanup needed (Python GC handles `Embedder` objects).
  - **MCP factory signature change** (`archon_search/server/mcp.py`): `create_app(pipeline, default_collection, writer, config)` must gain `embedder_cache: EmbedderCache` as a fifth parameter. `create_mcp_http_app(pipeline, default_collection, writer, config)` must also gain `embedder_cache: EmbedderCache`. **Architectural note**: `create_mcp_http_app` is currently ONLY called from tests (`test_mcp_auth.py`) — there is no production caller in `app.py`. The FastAPI app (`app.py`) and MCP app (`mcp.py`) are completely separate Starlette apps that do NOT share `app.state`. Each must create its own `EmbedderCache` instance. The tests calling `create_mcp_http_app(...)` must be updated to pass `embedder_cache=EmbedderCache(max_size=3)`. When/if MCP is served in production, the caller (CLI command or process) creates its own `EmbedderCache` from the config. There is NO shared cache between FastAPI and MCP processes.
  - **Lifespan ordering** (in `app.py`): the `EmbedderCache` must be created AFTER all migrations complete (so `active_embedding_model` columns exist before eager loading reads them) and BEFORE `yield` (so `app.state.embedder_cache` is available to request handlers). Correct order: `migrate_namespace()` → `migrate_description_embedding()` → `migrate_acl()` → `migrate_centroid_sum()` → `migrate_per_collection_model()` [Task 1.3] → `EmbedderCache(config.embedder_cache_size)` → optional eager load [Task 2.2] → `yield`.
  - **`app.state` assignment**: explicitly set `app.state.embedder_cache = embedder_cache` in the lifespan startup block (not just a local variable — it must be accessible via `request.app.state.embedder_cache` in all route handlers).
- **Releasable**: `app.state.embedder_cache` exists at server startup; handlers can call `.get_or_load(model_name)`.
- **Tests (TDD)** — `tests/test_app.py` (or `tests/test_server_startup.py`):
  - Integration: `test_embedder_cache_in_app_state` — build a test app; lifespan completes; `app.state.embedder_cache` is an `EmbedderCache` instance.
  - Integration: `test_eager_load_embedders_false_does_not_preload` — `eager_load_embedders=False`; `EmbedderCache.cached_models()` is empty after startup.
  - Integration: `test_eager_load_embedders_true_preloads_collection_models` — create collection with `active_embedding_model="model-X"`; `eager_load_embedders=True`; after startup, "model-X" is in `cached_models()`. (AC 37)
  - Unit: `test_create_mcp_http_app_accepts_embedder_cache` — call `create_mcp_http_app(pipeline, 'default', embedder_cache=EmbedderCache(max_size=3))` and verify no `TypeError` is raised; also update existing test fixtures in `test_mcp_auth.py` to pass `embedder_cache`.
  - Checkpoint: `uv run pytest tests/test_app.py -v -k "embedder_cache"`

---

### Phase 3 — Pipeline IoC Refactor
> **Releasable**: after Task 3.6 — all pipeline methods accept an embedder parameter; `self._embedder` is fully renamed to `self._global_embedder`; all 6 modified call sites pass the embedder through. Feature is not user-visible yet (routes still call the old way until Phase 5/7 updates them), but pipeline unit tests use the new signatures.

#### Task 3.1 — Rename `self._embedder` → `self._global_embedder` in `pipeline.py`
- [ ] **File**: `archon_search/pipeline.py`
- **Depends on**: nothing (pure rename, no logic change)
- **Description**:
  - Global replace: `self._embedder` → `self._global_embedder` throughout `pipeline.py`.
  - Verify no `self._embedder` references remain (grep).
  - `self._global_embedder` retains all existing uses: `search_many` query embedding, `embedder_is_warm` probe, description embedding in `ingest_directory` (until Task 3.3 moves it), `explain` multi-collection path.
  - No call-site changes outside `pipeline.py` in this task — callers still pass the same constructor argument.
  - Update `__init__` parameter name if it was also `embedder` → `global_embedder` (check constructor; if the param is named `embedder`, rename to `global_embedder` to avoid confusion).
- **Releasable**: grep for `self._embedder` in `pipeline.py` returns zero results. **Important**: Task 1.1 renames the dataclass field. Every file that accesses `meta.embedding_model` (pipeline.py search_many, explain; router.py; routes_collections.py; sync.py) will fail at runtime until its respective update task completes. Task 3.1 is not independently releasable — the full Phase 1+3 sequence must complete before CI passes.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Unit: `test_self_embedder_does_not_exist` — AST-scan (or grep) `pipeline.py`; assert `self._embedder` does not appear as an attribute access.
  - Checkpoint: `uv run pytest tests/test_pipeline.py -v -k "global_embedder or embedder_rename"`

#### Task 3.2 — Add `embedder: Embedder` parameter to `search()`
- [ ] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 3.1
- **Description**:
  - New signature: `async def search(self, query: str, collection: str, namespace: str = DEFAULT_NAMESPACE, *, embedder: Embedder, filters: SearchFilters | None = None) -> SearchPipelineResult`.
  - Replace `vector = await self._global_embedder.embed_one(query)` with `vector = await embedder.embed_one(query)`.
  - `self._global_embedder` is NOT referenced in `search()` after this task.
  - Update all call sites of `search()` in `pipeline.py` (if any self-calls exist). Route-layer call sites are updated in Phase 5.
- **Releasable**: `search()` is embedder-agnostic; callers supply the embedder.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Unit: `test_search_uses_passed_embedder` — mock embedder-A and embedder-B; verify `embedder_A.embed_one` called when `embedder=embedder_A`, not when `embedder=embedder_B`.
  - Unit: `test_search_does_not_call_global_embedder` — mock `self._global_embedder`; call `search(..., embedder=mock_embedder)`; assert `self._global_embedder.embed_one` never called.
  - Checkpoint: `uv run pytest tests/test_pipeline.py -v -k "search_uses_passed_embedder"`

#### Task 3.3 — Add `embedder: Embedder` to `ingest_file()`; fix `ingest_chunks` write-path
- [ ] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 3.1
- **Description**:
  - New signature: `async def ingest_file(self, path: Path, collection: str, rebuild_fts: bool = True, ..., *, embedder: Embedder, namespace: str = DEFAULT_NAMESPACE, ...) -> None`.
  - Replace `self._global_embedder.embed(...)` calls inside `ingest_file` with `embedder.embed(...)`.
  - The `ingest_chunks` call within `ingest_file` must pass `embedding_model=embedder.model_name` (not `self._global_embedder.model_name`). (AC 50)
  - `self._global_embedder` must NOT be referenced in `ingest_file` after this task.
  - Update all internal call sites of `ingest_file` inside `pipeline.py` (e.g., `ingest_directory` calls it in a loop — that's updated in Task 3.4).
- **Releasable**: `ingest_file()` writes the correct per-collection `embedding_model` to the vector table.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Unit: `test_ingest_file_uses_passed_embedder` — mock embedder; verify `embedder.embed` called, not `self._global_embedder.embed`.
  - Unit: `test_ingest_file_writes_correct_model_name_to_chunks` — after `ingest_file(..., embedder=embedder_X)`, query the vector store; assert all ingested chunks have `embedding_model == embedder_X.model_name`. (AC 50)
  - Checkpoint: `uv run pytest tests/test_pipeline.py -v -k "ingest_file_uses_passed_embedder or ingest_file_writes_correct_model"`

#### Task 3.4 — Add `embedder: Embedder` to `ingest_directory()`; fix post-ingest `CollectionMeta` construction
- [ ] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 3.3
- **Description**:
  - New signature: `async def ingest_directory(self, path: Path, collection: str, ..., *, embedder: Embedder, namespace: str = DEFAULT_NAMESPACE, ...) -> None`.
  - Forward `embedder` to every `self.ingest_file(...)` call within `ingest_directory`.
  - In the post-ingest `CollectionMeta` construction (the pre-B5 / non-incremental branch at pipeline.py ~404–424):
    - `description_embedding`: must use `self._global_embedder.embed_one(description)` — NOT `embedder` (global model invariant for routing). (Brief line 76)
    - `active_embedding_model` field: for an existing collection, read `existing_meta.active_embedding_model` and write it back unchanged. For a brand-new collection (no existing row), use `embedder.model_name`. (Brief line 76)
  - `self._global_embedder` is used ONLY for `description_embedding` here; `embedder` is used for chunk encoding.
  - If the incremental B5 path (`centroid_incremental_enabled=True`) skips constructing a new `CollectionMeta` directly, verify that path also does not overwrite `active_embedding_model` with the global model.
  - In the pre-B5 `CollectionMeta(...)` construction, ALL four C1 fields must be preserved from `existing_meta`: `active_embedding_model=existing_meta.active_embedding_model`, `pending_embedding_model=existing_meta.pending_embedding_model`, `needs_reindex=existing_meta.needs_reindex`, `reindex_job_id=existing_meta.reindex_job_id`. Omitting any of these will silently destroy live reindex state when a sync event fires during a pending model change.
- **Releasable**: `ingest_directory()` correctly writes `active_embedding_model` for both new and existing collections.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Unit: `test_ingest_directory_preserves_active_embedding_model` — existing collection with `active_embedding_model="model-X"`; ingest with `embedder=embedder_Y`; after ingest, `active_embedding_model` still `"model-X"`. (AC 44 partial)
  - Unit: `test_ingest_directory_sets_active_embedding_model_for_new_collection` — new collection; ingest with `embedder=embedder_X`; `active_embedding_model` is `embedder_X.model_name`.
  - Unit: `test_ingest_directory_description_uses_global_embedder` — spy on `self._global_embedder.embed_one`; verify called exactly once (for description); `embedder.embed_one` called for chunks.
  - Unit: `test_ingest_directory_preserves_all_c1_fields` — existing collection with `pending_embedding_model="model-B"`, `needs_reindex=True`, `reindex_job_id="job-42"`; after `ingest_directory(...)`, read meta; all three fields are unchanged. (Complements the existing `test_ingest_directory_preserves_active_embedding_model`)
  - Checkpoint: `uv run pytest tests/test_pipeline.py -v -k "ingest_directory"`

#### Task 3.5 — Add `embedder: Embedder | None` to `explain()`
- [ ] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 3.1
- **Description**:
  - New signature: `async def explain(self, query: str, collection: str | None = None, ..., embedder: Embedder | None = None, ...) -> ExplainPipelineResult`.
  - Single-collection path (`collection` is not None): embed the query with `embedder` (if provided); if `embedder=None`, fall back to `self._global_embedder` (maintains current behavior for multi-collection callers who don't provide an embedder).
  - Multi-collection path (`collections` is not None): always use `self._global_embedder` (unchanged from today).
  - Route layer (Phase 6) will resolve and pass the per-collection embedder for single-collection calls. (AC 43)
  - Do NOT change the multi-collection explain path behavior.
  - AC 25 grep assertion: after this task, `self._global_embedder` appears in: `search_many`, multi-collection explain path, `embedder_is_warm`, description embedding. Single-collection explain path uses `embedder` parameter.
- **Releasable**: `explain()` accepts per-collection embedder for single-collection calls.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Unit: `test_explain_single_collection_uses_passed_embedder` — mock embedder; single-collection explain with `embedder=mock`; verify `mock.embed_one` called.
  - Unit: `test_explain_multi_collection_uses_global_embedder` — multi-collection explain with `embedder=None`; verify `self._global_embedder.embed_one` called.
  - Unit: `test_explain_single_collection_no_embedder_falls_back_to_global` — single-collection with `embedder=None`; `self._global_embedder` used (backwards compat for callers not yet updated).
  - Checkpoint: `uv run pytest tests/test_pipeline.py -v -k "explain_single_collection or explain_multi_collection"`

#### Task 3.6 — Add `embedder: Embedder` to `search_with_context()` and `global_embedder` to `recompute_collection_meta()`
- [ ] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 3.2 (`search_with_context` delegates to `search()`), Task 3.1 (`recompute_collection_meta` needs global_embedder)
- **Description**:
  - `search_with_context`: add `embedder: Embedder` keyword parameter; forward to `self.search(..., embedder=embedder)`.
    New signature: `async def search_with_context(self, query: str, collection: str, context_window: int = 1, namespace: str = DEFAULT_NAMESPACE, *, embedder: Embedder, filters: SearchFilters | None = None) -> list[dict[str, Any]]`.
  - `recompute_collection_meta`: add `global_embedder: Embedder` parameter (positional after `collection`); retain `namespace: str = DEFAULT_NAMESPACE` and `force: bool = False`.
    New signature: `async def recompute_collection_meta(self, collection: str, global_embedder: Embedder, namespace: str = DEFAULT_NAMESPACE, force: bool = False) -> None`.
    - Replace `self._global_embedder.embed_one(description)` with `global_embedder.embed_one(description)`.
    - The `CollectionMeta(...)` constructor call in BOTH branches (no-vectors branch and has-vectors branch) must propagate ALL four C1 fields from `existing_meta`: `active_embedding_model=existing_meta.active_embedding_model`, `pending_embedding_model=existing_meta.pending_embedding_model`, `needs_reindex=existing_meta.needs_reindex`, `reindex_job_id=existing_meta.reindex_job_id`. Both branches perform a full row replacement — any field not explicitly set reverts to its dataclass default.
    - **Edge case: `existing_meta is None`** (force=True on a brand-new or forcibly-recomputed collection): in this case, there are no C1 fields to preserve. Use `active_embedding_model=global_embedder.model_name` (NOT `self._global_embedder.model_name` after the Task 3.1 rename — use the `global_embedder` parameter), and set `pending_embedding_model=None`, `needs_reindex=False`, `reindex_job_id=None`. This preserves the existing behavior for new collections while correctly handling the `None` case.
    - Update call sites within `pipeline.py` itself (e.g., self-calls in `ingest_directory` if any).
- **Releasable**: all 6 modified method signatures are in place; grep for `self._embedder` in `pipeline.py` returns zero.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Unit: `test_search_with_context_uses_passed_embedder` — mock embedder; verify `mock.embed_one` called through the `search()` delegation.
  - Unit: `test_recompute_collection_meta_preserves_active_embedding_model` — collection with `active_embedding_model="model-X"`; call `recompute_collection_meta(collection, global_embedder=global_embedder_mock)`; read meta; assert `active_embedding_model` still `"model-X"`. (AC 44)
  - Unit: `test_recompute_collection_meta_preserves_all_c1_fields` — collection with `active_embedding_model="model-X"`, `pending_embedding_model="model-Y"`, `needs_reindex=True`, `reindex_job_id="job-99"`; call `recompute_collection_meta`; verify all four C1 fields unchanged after the call.
  - Unit: `test_recompute_collection_meta_uses_global_embedder_for_description` — verify `global_embedder_mock.embed_one` called for description; not for centroid computation.
  - Unit: `test_no_self_embedder_in_pipeline` — AST or grep assertion: `self._embedder` does not appear in `pipeline.py`. (AC 25)
  - Unit: `test_search_many_signature_unchanged` — verify `pipeline.search_many` signature has not changed (use `inspect.signature`); assert it does NOT accept an `embedder` parameter.
  - Unit: `test_telemetry_entry_no_query_parameter` — verify no factory method in `archon_search/telemetry/entry.py` accepts a `query` parameter (grep/AST check). This asserts the no-raw-query invariant survives C1 changes.
  - Unit: `test_job_status_enum_values_unchanged` — verify `JobStatus` has exactly: PENDING, RUNNING, DONE, FAILED, CANCELLED, CANCELLING.
  - Checkpoint: `uv run pytest tests/test_pipeline.py -v -k "search_with_context or recompute_collection_meta"`

#### Task 3.7 — Update `meta.embedding_model` references in `search_many` and explain multi-collection path
- [ ] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 1.1
- **Description**:
  - In `search_many()`: replace every `meta.embedding_model` attribute access with `meta.active_embedding_model`.
  - In `explain()` multi-collection path: replace every `meta.embedding_model` attribute access with `meta.active_embedding_model`.
  - Verify by: `grep -rn '\.embedding_model\b' archon_search/` returns zero results (excluding `archon_search/store.py`'s migration code which temporarily reads both old and new columns during Task 1.3), EXCLUDING legitimate `config.embedding_model` references (which refer to `SearchConfig.embedding_model`, not `CollectionMeta.embedding_model`). To isolate non-config references, use: `grep -rn '\.embedding_model\b' archon_search/ | grep -v 'config\.embedding_model' | grep -v 'migrate_per_collection_model'`.
  - In `routes_explain.py`: replace all `pipeline._embedder` references with `pipeline._global_embedder` (covers the multi-collection routing path where the route handler accesses the pipeline's embedder directly).
  - In `archon_search/eval/runner.py` and `archon_search/eval/_tracing.py`: replace all `pipeline._embedder` references with `pipeline._global_embedder`.
  - Run: `grep -rn '\._embedder\b' archon_search/` after this task to confirm zero results (catches both `self._embedder` and `pipeline._embedder`).
  - This is purely a rename; no logic change.
- **Releasable**: `search_many` and multi-collection `explain` no longer raise `AttributeError`.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Unit: `test_no_embedding_model_attribute_accesses` — grep/AST across `archon_search/` (excluding migration code); assert no `.embedding_model` attribute access on any object. This replaces the two `AttributeError`-absence tests which only test within one call path.
  - Unit: `test_no_underscore_embedder_anywhere` — grep/AST scan across all of `archon_search/` (not just pipeline.py); assert `\._embedder\b` appears zero times. (This replaces the earlier `test_no_self_underscore_embedder` which only checked `self._embedder`.)
  - Unit: `test_search_many_no_embedding_model_attribute_error` — call `search_many()` with a `CollectionMeta` that has `active_embedding_model` (not `embedding_model`); assert no `AttributeError`.
  - Unit: `test_explain_multi_collection_no_embedding_model_attribute_error` — call `explain()` in multi-collection mode; assert no `AttributeError`.
  - Checkpoint: `uv run pytest tests/test_pipeline.py -v -k "search_many or explain_multi_collection"`

---

### Phase 4 — Validation Helpers
> **Releasable**: after Task 4.2 — both validators callable from PATCH and POST handlers. No REST behavior change yet.

#### Task 4.1 — `_get_stored_vector_dimension(collection, db)` helper
- [ ] **File**: `archon_search/store.py`
- **Depends on**: nothing
- **Description**:
  - `async def get_stored_vector_dimension(self, collection: str, namespace: str = DEFAULT_NAMESPACE) -> int | None`: opens the collection's chunk table; reads the PyArrow schema of the `vector` column; returns the fixed-size list dimension. Returns `None` if the table does not exist or has no rows (empty collection, no schema yet).
  - Implementation: `table = await db.open_table(collection_name)`, `schema = await table.schema()`, `vector_field = schema.field("vector")`, `return vector_field.type.list_size`.
  - This reads the LanceDB arrow schema metadata — O(1), no row scan. (Brief line 87: "stored dimension: read from the LanceDB table's arrow schema")
  - `collection_name` is the resolved LanceDB table name (namespace-prefixed if applicable — follow the same table-name resolution as other store methods).
  - Also add `async def count_chunks(self, collection: str, namespace: str) -> int` to `SearchStore`: opens the collection's chunk table (using same table-name resolution as `get_stored_vector_dimension`); calls `table.count_rows()` (O(1) metadata read); returns the count. Returns `0` if the table does not exist (new collection with no ingested data). This method is used by the PATCH handler for empty-collection detection.
- **Releasable**: route handlers can call `store.get_stored_vector_dimension(collection)` at PATCH/POST time.
- **Tests (TDD)** — `tests/test_store.py`:
  - Unit: `test_get_stored_vector_dimension_returns_correct_dim` — ingest a chunk with a 384-dim vector; `get_stored_vector_dimension` returns `384`.
  - Unit: `test_get_stored_vector_dimension_returns_none_for_missing_table` — non-existent collection returns `None`.
  - Unit: `test_count_chunks_returns_correct_count` — ingest 3 chunks into a collection; `count_chunks` returns `3`.
  - Unit: `test_count_chunks_empty_collection_returns_zero` — collection exists but has no chunks; `count_chunks` returns `0`.
  - Unit: `test_count_chunks_nonexistent_collection_returns_zero` — collection does not exist; `count_chunks` returns `0` (no exception).
  - Checkpoint: `uv run pytest tests/test_store.py -v -k "get_stored_vector_dimension"`

#### Task 4.2 — `validate_embedding_model()` helper (model name + dimension validation)
- [ ] **File**: `archon_search/embedder.py` (or new `archon_search/model_validation.py`)
- **Depends on**: nothing
- **Description**:
  - `async def validate_embedding_model(model_name: str, timeout_seconds: float = 30.0) -> int`: validates `model_name` and returns its output dimension.
    - Step 1: call `fastembed.TextEmbedding.list_supported_models()` (if available). Search for a descriptor with `name == model_name`. If found and descriptor has `"dim"` key: return `descriptor["dim"]`.
    - Step 2 (fallback for custom/ONNX models not in the list): `asyncio.wait_for(asyncio.to_thread(make_embedder, model_name), timeout=timeout_seconds)`. On success: return `embedder.embedding_dim`. On `asyncio.TimeoutError`: raise `ModelValidationError("could not determine model output dimension; verify the model name and ensure it is reachable.")`. (AC 39)
    - Raises `ModelValidationError(str)` on failure.
  - `class ModelValidationError(ValueError): pass` — new exception, importable from this module.
  - If `list_supported_models()` raises `AttributeError` (old fastembed version without that API): skip step 1, go directly to step 2.
  - Module-level comment: "Dimension lookup: prefer list_supported_models() (O(1), no model download); fall back to timeout-guarded instantiation for custom/ONNX models."
- **Releasable**: `validate_embedding_model()` callable from PATCH and POST route handlers.
- **Tests (TDD)** — `tests/test_model_validation.py`:
  - Unit: `test_validate_known_model_returns_dim` — mock `list_supported_models` returning `[{"name": "model-X", "dim": 384}]`; `validate_embedding_model("model-X")` returns `384`.
  - Unit: `test_validate_unknown_model_falls_back_to_instantiation` — mock `list_supported_models` returning empty; mock `make_embedder` returning embedder with `embedding_dim=512`; `validate_embedding_model("custom-model")` returns `512`.
  - Unit: `test_validate_timeout_raises_model_validation_error` — mock `asyncio.to_thread` to raise `asyncio.TimeoutError`; assert `ModelValidationError` raised with 'could not determine model output dimension' in message. (AC 39)
  - Unit: `test_validate_list_supported_models_unavailable_falls_back` — `list_supported_models` raises `AttributeError`; `validate_embedding_model` falls back to instantiation without raising.
  - Checkpoint: `uv run pytest tests/test_model_validation.py -v`

---

### Phase 5 — PATCH /collections/{name}
> **Releasable**: after Task 5.1 — `PATCH /collections/{name}` is fully functional with the complete state machine, dimension validation, and 409 guard. (AC 4–9, 30–31, 36, 42, 48, 56)

#### Task 5.1 — `PATCH /collections/{name}` endpoint + `PatchCollectionBody` schema
- [ ] **File**: `archon_search/server/routes_collections.py`, `archon_search/server/schemas.py`
- **Depends on**: Task 4.2 (model validation), Task 4.1 (stored dimension), Task 1.2 (CollectionMeta fields), Task 2.2 (app.state.embedder_cache)
- **Description**:
  - **`PatchCollectionBody`** (Pydantic): `embedding_model: str` (required — Pydantic default; missing key → 422 automatically). Validate in `@validator`: reject empty string or None → 422 with 'embedding_model field required'. (AC 42)
  - **Route handler** `patch_collection(name: str, body: PatchCollectionBody, request: Request)`:
    1. Resolve namespace `ns` from auth.
    2. Load `CollectionMeta` for `(name, ns)`. If not found: return 404. (AC 56)
    3. Validate `body.embedding_model` via `validate_embedding_model()`. On `ModelValidationError`: return 422. (AC 6)
    4. Get `new_dim` from validator result. Get `stored_dim = await store.get_stored_vector_dimension(collection, namespace=ns)`. If `stored_dim is not None` and `stored_dim != new_dim`: return 422 with message `"model dimension mismatch: current vectors are {stored_dim}-dim, new model produces {new_dim}-dim; delete and recreate collection to change dimensions"`. (AC 7)
    5. **409 guard**: if `meta.reindex_job_id is not None`: cross-check `JobStore` for job status. If status is DONE, FAILED, or CANCELLED: clear `meta.reindex_job_id` (stale auto-clear, continue). If status is RUNNING or PENDING: return 409. (AC 5, 36)
    6. **State machine** (based on `meta.active_embedding_model` and `meta.pending_embedding_model`):
       - (a) `active=A`, `pending=None`, `request=A` → 200 no-op, return current meta unchanged.
       - (a′) `active=A`, `pending=B`, `request=B` → 200 no-op, return current meta unchanged. (AC 48)
       - (b) `active=A`, `pending=None`, `request=B` (B ≠ A) → check chunk count. If collection chunk count > 0: set `pending_embedding_model=B`, `needs_reindex=True`. If collection chunk count == 0: set `active_embedding_model=B`, `pending_embedding_model=None`, `needs_reindex=False`, `reindex_job_id=None` (empty collection — no data to reindex; clear any stale reindex_job_id from prior failed jobs). (AC 4, 32)
       - (c) `active=A`, `pending=B`, `request=A` → clear `pending_embedding_model=None`, `needs_reindex=False`. (AC 30)
       - (d) `active=A`, `pending=B`, `request=C` (C ≠ A, C ≠ B) → check chunk count. If collection chunk count > 0: set `pending_embedding_model=C`, `needs_reindex=True`. If collection chunk count == 0: set `active_embedding_model=C`, `pending_embedding_model=None`, `needs_reindex=False`, `reindex_job_id=None` (empty collection, no data to reindex; clear any stale reindex_job_id). (AC 31)
    7. Write updated `CollectionMeta` via `store.update_collection_meta(meta)`.
    8. Return 200 with `CollectionDetail` response.
  - **Empty-collection check**: Use `await store.count_chunks(collection, namespace)` (implemented in Task 4.1) for the empty-collection check in step 6(b). Do NOT open the LanceDB table directly in the route handler. Do NOT use `store.count_documents()` (O(N) full scan).
  - Namespace isolation: reject if collection's namespace ≠ `ns`. (AC 9)
- **Releasable**: PATCH endpoint is operational; all state machine transitions, validation, and 409 guard work.
- **Tests (TDD)** — `tests/test_routes_collections.py`:
  - Unit: `test_patch_returns_200_on_model_change` — PATCH with new valid model; returns 200, `needs_reindex=True`. (AC 4)
  - Unit: `test_patch_returns_409_on_active_reindex` — `reindex_job_id` set with RUNNING job; returns 409. (AC 5)
  - Unit: `test_patch_returns_422_on_unknown_model` — `validate_embedding_model` raises `ModelValidationError`; returns 422. (AC 6)
  - Unit: `test_patch_returns_422_on_dimension_mismatch` — stored_dim=384, new_dim=512; returns 422 with dimension details. (AC 7)
  - Unit: `test_patch_idempotent_same_active_model` — active=A, no pending, request=A; returns 200, `needs_reindex` unchanged. (AC 8)
  - Unit: `test_patch_namespace_isolation` — PATCH on collection in different namespace; returns 403/404. (AC 9)
  - Unit: `test_patch_state_c_revert` — active=A, pending=B, request=A; clears pending, `needs_reindex=False`. (AC 30)
  - Unit: `test_patch_state_d_replace_pending` — active=A, pending=B, request=C; sets `pending=C`, `needs_reindex=True` (was already True and must remain True). (AC 31)
  - Unit: `test_patch_empty_collection_sets_active_directly` — `count_chunks()==0`; active=new model, no reindex. (AC 32)
  - Unit: `test_patch_stale_reindex_job_id_auto_cleared` — `reindex_job_id` set with DONE job; PATCH clears stale ID and proceeds. (AC 36)
  - Unit: `test_patch_stale_cancelled_reindex_job_id_auto_cleared` — `reindex_job_id` set with CANCELLED job; PATCH clears stale ID and proceeds. (AC 36)
  - Unit: `test_patch_stale_failed_reindex_job_id_auto_cleared` — `reindex_job_id` set with FAILED job; PATCH clears stale ID and proceeds. (AC 36)
  - Unit: `test_patch_state_a_prime_same_pending` — active=A, pending=B, request=B; 200 no-op. (AC 48)
  - Unit: `test_patch_missing_embedding_model_returns_422` — body `{}`; 422 with 'embedding_model field required'. (AC 42)
  - Unit: `test_patch_null_embedding_model_returns_422` — body `{"embedding_model": null}`; 422. (AC 42)
  - Unit: `test_patch_empty_string_embedding_model_returns_422` — body `{"embedding_model": ""}`; 422. (AC 42)
  - Unit: `test_patch_nonexistent_collection_returns_404` — name not in store; 404. (AC 56)
  - Checkpoint: `uv run pytest tests/test_routes_collections.py -v -k "patch"`

---

### Phase 6 — Collection Endpoints & Search Response Schema
> **Releasable**: after Task 6.4 — `POST /collections/` accepts `embedding_model`; `GET /collections/{name}` returns C1 fields; `GET /collections/` includes `active_embedding_model` and `needs_reindex`; `SearchResponse` has `embedding_model` field.

#### Task 6.1 — `POST /collections/` gains `embedding_model` field
- [ ] **File**: `archon_search/server/routes_collections.py`, `archon_search/server/schemas.py`
- **Depends on**: Task 4.2 (validate model name), Task 1.2 (store schema)
- **Description**:
  - Add `embedding_model: str | None = None` to the collection creation request schema. `None` means "use global default".
  - In the route handler: if `embedding_model` is provided, call `validate_embedding_model()`. On failure: return 422. (AC 14)
  - Store as `active_embedding_model` in `CollectionMeta` when creating the row. (AC 12, 13)
  - `pending_embedding_model=None`, `needs_reindex=False`, `reindex_job_id=None` on creation.
- **Releasable**: operators can create collections with custom models; existing creation without `embedding_model` continues to work.
- **Tests (TDD)** — `tests/test_routes_collections.py`:
  - Unit: `test_create_collection_with_embedding_model` — POST with `embedding_model="model-X"`; `CollectionMeta.active_embedding_model == "model-X"`. (AC 12)
  - Unit: `test_create_collection_without_embedding_model_uses_global` — POST without field; `active_embedding_model == config.embedding_model`. (AC 13)
  - Unit: `test_create_collection_unknown_model_returns_422` — unknown model; 422. (AC 14)
  - Checkpoint: `uv run pytest tests/test_routes_collections.py -v -k "create_collection"`

#### Task 6.2 — `CollectionDetail` schema: add `active_embedding_model`, `pending_embedding_model`, `needs_reindex`, `reindex_job_id`; fix `GET /collections/{name}`
- [ ] **File**: `archon_search/server/schemas.py`, `archon_search/server/routes_collections.py`
- **Depends on**: Task 1.2
- **Description**:
  - Update `CollectionDetail` Pydantic schema: remove `embedding_model: str`; add `active_embedding_model: str`, `pending_embedding_model: str | None`, `needs_reindex: bool`, `reindex_job_id: str | None`.
  - Fix `GET /collections/{name}` route (`routes_collections.py:315` area): replace `config.embedding_model` (global) with `meta.active_embedding_model` from the retrieved `CollectionMeta`. (AC 15)
  - Populate all four new fields from `CollectionMeta`.
  - Document in `BREAKING.md`: "`embedding_model` renamed to `active_embedding_model` in `GET /collections/{name}` response; `pending_embedding_model` (nullable), `needs_reindex` (bool), `reindex_job_id` (nullable) added."
- **Releasable**: `GET /collections/{name}` returns correct per-collection model data. (AC 15–18)
- **Tests (TDD)** — `tests/test_routes_collections.py`:
  - Unit: `test_get_collection_returns_active_embedding_model` — collection with `active_embedding_model="model-X"`; response has `active_embedding_model="model-X"`, not global. (AC 15, 16)
  - Unit: `test_get_collection_pending_null_before_patch` — before any PATCH; `pending_embedding_model=null`. (AC 16)
  - Unit: `test_get_collection_reflects_patch_state` — after PATCH; `active` unchanged, `pending` set, `needs_reindex=true`. (AC 17)
  - Unit: `test_get_collection_reflects_reindex_completion` — after reindex completes; `active` promoted, `pending=null`, `needs_reindex=false`. (AC 18)
  - Checkpoint: `uv run pytest tests/test_routes_collections.py -v -k "get_collection"`

#### Task 6.3 — `CollectionSummary` schema + `GET /collections/` list response
- [ ] **File**: `archon_search/server/schemas.py`, `archon_search/server/routes_collections.py`
- **Depends on**: Task 6.2
- **Description**:
  - Add `active_embedding_model: str` and `needs_reindex: bool` to `CollectionSummary`.
  - Update the `GET /collections/` route to populate these fields from each `CollectionMeta`.
- **Releasable**: operators can scan for `needs_reindex=true` collections via the list endpoint. (AC 41)
- **Tests (TDD)** — `tests/test_routes_collections.py`:
  - Unit: `test_list_collections_includes_active_embedding_model` — list response; each item has `active_embedding_model`.
  - Unit: `test_list_collections_includes_needs_reindex` — collection with `needs_reindex=true`; appears in list with that field.
  - Checkpoint: `uv run pytest tests/test_routes_collections.py -v -k "list_collections"`

#### Task 6.4 — `SearchResponse.embedding_model` field
- [ ] **File**: `archon_search/server/routes_search.py` (or verify the actual location of `SearchResponse` by grepping — if it was moved to `schemas.py` it may already be there; the implementer must confirm before proceeding)
- **Depends on**: nothing (schema-only change)
- **Description**:
  - Verify location of `SearchResponse` via `grep -n 'class SearchResponse' archon_search/server/` before making changes.
  - Add `embedding_model: str` to `SearchResponse` (the top-level response envelope, NOT per-result).
  - For single-collection search: the route handler will populate this with `collection_meta.active_embedding_model` (wired in Task 7.2).
  - For multi-collection search (`search_many`): populate with `config.embedding_model` (global model). (AC 1)
  - Schema change only in this task — population logic in Task 7.2.
- **Releasable**: schema carries the field; existing tests that don't check `embedding_model` are unaffected.
- **Tests (TDD)** — `tests/test_schemas.py` (or inline):
  - Unit: `test_search_response_has_embedding_model_field` — `SearchResponse` schema has `embedding_model: str`.
  - Checkpoint: `uv run pytest tests/test_schemas.py -v -k "search_response"`

---

### Phase 7 — Search Dispatch, Explain, & Router
> **Releasable**: after Task 7.4 — single-collection search and explain use the collection's `active_embedding_model` (REST and MCP); multi-collection search correctly excludes mismatched collections; router uses the renamed field. (AC 1–3, 33, 43)

#### Task 7.1 — Router: update `_ROUTING_FIELDS` and `_score_collections` to `active_embedding_model`
- [ ] **File**: `archon_search/router.py`
- **Depends on**: Task 1.1 (field rename)
- **Description**:
  - `_ROUTING_FIELDS` (router.py:23): replace `'embedding_model'` with `'active_embedding_model'`.
  - `_score_collections` comparison (router.py:172): replace `col.embedding_model == self._embedding_model` with `col.active_embedding_model == self._embedding_model`.
  - Verify the default value of `active_embedding_model` (`""`) does not silently match any real model name. The empty string won't match `self._embedding_model` (which is the configured global model name — a non-empty string), so collections with `active_embedding_model=""` will be correctly excluded. (AC 33)
- **Releasable**: router handles the field rename; routing tests pass.
- **Tests (TDD)** — `tests/test_router.py`:
  - Unit: `test_router_excludes_mismatched_active_embedding_model` — collection with `active_embedding_model="model-X"` where global model is `"model-Y"`; routing excludes it with `reason="embedding_model_mismatch"`. (AC 3)
  - Unit: `test_router_does_not_default_to_empty_string` — collection with `active_embedding_model=""`; excluded (not silently included). (AC 33)
  - Checkpoint: `uv run pytest tests/test_router.py -v`

#### Task 7.2 — Single-collection search: per-collection embedder dispatch in `routes_search.py`
- [ ] **File**: `archon_search/server/routes_search.py`
- **Depends on**: Task 3.2 (search() signature), Task 2.2 (app.state.embedder_cache), Task 6.4 (SearchResponse.embedding_model)
- **Description**:
  - In the single-collection search route handler (`GET /search?collection=...`): after resolving the collection and namespace, call `meta = await store.get_collection_meta(collection, namespace)`. Fetch `embedder = await app.state.embedder_cache.get_or_load(meta.active_embedding_model)`. Pass `embedder=embedder` to `pipeline.search(...)`.
  - Populate `response.embedding_model = meta.active_embedding_model`. (AC 1)
  - For multi-collection search (`/search` without `collection`): populate `response.embedding_model = config.embedding_model` (global). (AC 1)
  - During `needs_reindex` window: `meta.active_embedding_model` is the OLD model — search correctly embeds with old model. (AC 2)
  - Update `routes_search.py`'s call to `pipeline.search_with_context` similarly (Task 3.6 wired the signature; this task updates the call site to pass embedder).
- **Releasable**: single-collection search uses per-collection model; response includes `embedding_model`. (AC 1, 2)
- **Tests (TDD)** — `tests/test_routes_search.py`:
  - Unit: `test_search_single_collection_uses_active_embedding_model` — collection with `active_embedding_model="model-X"`; verify `EmbedderCache.get_or_load` called with `"model-X"`. (AC 1)
  - Unit: `test_search_response_includes_embedding_model` — single-collection search response has `embedding_model="model-X"`. (AC 1)
  - Unit: `test_search_during_needs_reindex_uses_active_not_pending` — collection with `active="model-X"`, `pending="model-Y"`, `needs_reindex=true`; search uses `"model-X"`. (AC 2)
  - Unit: `test_search_multi_collection_uses_global_model` — multi-collection search; `response.embedding_model == config.embedding_model`. (AC 1)
  - Checkpoint: `uv run pytest tests/test_routes_search.py -v -k "embedding_model"`

#### Task 7.3 — Explain endpoint: per-collection embedder dispatch
- [ ] **File**: `archon_search/server/routes_explain.py`
- **Depends on**: Task 3.5 (explain() signature), Task 2.2 (embedder_cache)
- **Description**:
  - In the `/explain` route handler, for single-collection mode (`collection` is provided): resolve `meta.active_embedding_model`; fetch embedder from cache; pass `embedder=embedder` to `pipeline.explain(...)`.
  - For multi-collection mode: pass `embedder=None` (pipeline uses `self._global_embedder`).
  - The response body must identify which model was used for query embedding. If `ExplainPipelineResult` has a metadata field (or routing path output), add `embedding_model: str` populated from the per-collection model or global model accordingly. (AC 43)
  - If `ExplainPipelineResult` schema needs extension: add `embedding_model: str` to `ExplainPipelineResult` dataclass and the explain response schema.
- **Releasable**: `/explain` reflects per-collection model in single-collection mode. (AC 43)
- **Tests (TDD)** — `tests/test_routes_explain.py`:
  - Unit: `test_explain_single_collection_uses_per_collection_model` — collection with `active_embedding_model="model-X"`; explain query embedding uses `"model-X"`.
  - Unit: `test_explain_response_identifies_model_used` — response body has `embedding_model="model-X"`. (AC 43)
  - Unit: `test_explain_multi_collection_uses_global_model` — no `collection` param; `self._global_embedder` used.
  - Checkpoint: `uv run pytest tests/test_routes_explain.py -v -k "per_collection or embedding_model"`

#### Task 7.4 — MCP `search`, `search_with_context`, `explain`: per-collection embedder dispatch
- [ ] **File**: `archon_search/server/mcp.py`
- **Depends on**: Task 3.2 (`search()` signature), Task 3.3 (`ingest_file()` signature), Task 3.4 (`ingest_directory()` signature), Task 3.5 (`explain()` signature), Task 3.6 (`search_with_context()` signature), Task 2.2 (mcp.create_app() gains embedder_cache parameter)
- **Description**:
  - **Prerequisite — `mcp.create_app()` signature change**: MCP tool functions in `mcp.py` are closures that only have access to parameters passed to `create_app()`. They have NO access to FastAPI's `app.state`. Therefore, `create_app()` (`mcp.py:82`) MUST be extended to accept `embedder_cache: EmbedderCache` as an additional parameter (after `config`). Both `create_mcp_http_app()` and `create_app()` must gain `embedder_cache: EmbedderCache` as a parameter (done in Task 2.2). `create_mcp_http_app` has no production caller in `app.py` (it is served separately or test-only). Callers of `create_mcp_http_app` must pass `embedder_cache` explicitly. Update all test fixtures in `test_mcp_auth.py` that call `create_mcp_http_app(...)` to pass the new parameter. Inside MCP tool closures, access `embedder_cache` as the closure-captured parameter (NOT `app.state`).
  - In `mcp.py`, the following tools call pipeline methods **directly** (not via REST delegation): `search` (`mcp.py:196`), `search_with_context` (`mcp.py:287`), `explain` (`mcp.py:390`, `mcp.py:465`), and two ingest tools (`mcp.py:551`, `mcp.py:594`).
  - After Phase 3 changes pipeline signatures to require `embedder: Embedder`, all these calls will raise `TypeError` unless updated.
  - For the `search` tool: resolve `meta = await store.get_collection_meta(collection, namespace)`; fetch `embedder = await embedder_cache.get_or_load(meta.active_embedding_model)`; pass `embedder=embedder` to `pipeline.search(...)`.
  - **Namespace resolution**: Inspect the existing MCP `search` and `search_with_context` tool functions in `mcp.py`. If they do not currently resolve a namespace (unlike the `explain` tool which defines `ns = DEFAULT_NAMESPACE`), add `ns = DEFAULT_NAMESPACE` (or the equivalent namespace extraction from the MCP auth token, following the pattern used in the `explain` tool). Use this `ns` for `store.get_collection_meta(collection, ns)`.
  - For the `search_with_context` tool: same embedder resolution, pass to `pipeline.search_with_context(...)`.
  - For the `explain` tool (single-collection mode): same embedder resolution; pass `embedder=embedder`. For multi-collection mode: pass `embedder=None`.
  - For `ingest_file` and `ingest_directory` MCP tools: resolve `embedder = await embedder_cache.get_or_load(meta.active_embedding_model or config.embedding_model)`; pass `embedder=embedder`.
  - Also fix `mcp.py:445`: `pipeline._embedder.embed_one(...)` must become `pipeline._global_embedder.embed_one(...)` (rename from Phase 3 Task 3.1).
  - The MCP auth layer resolves namespace from the token — use the same namespace for collection lookup.
- **Releasable**: all MCP tools function correctly with per-collection model dispatch.
- **Tests (TDD)** — `tests/test_mcp.py`:
  - Unit: `test_mcp_search_uses_per_collection_embedder` — collection with `active_embedding_model="model-X"`; verify `EmbedderCache.get_or_load` called with `"model-X"` during MCP `search` call.
  - Unit: `test_mcp_search_with_context_uses_per_collection_embedder` — same as above for `search_with_context`.
  - Unit: `test_mcp_explain_single_collection_uses_per_collection_embedder` — explain with `collection` param; uses collection's `active_embedding_model`.
  - Unit: `test_mcp_ingest_file_uses_collection_embedder` — verify ingest MCP tool passes per-collection embedder.
  - Unit: `test_mcp_ingest_directory_uses_collection_embedder` — verify MCP `ingest_directory` tool resolves and passes per-collection embedder.
  - Unit: `test_mcp_explain_multi_collection_passes_none_embedder` — multi-collection MCP explain call passes `embedder=None` to `pipeline.explain(...)`.
  - Checkpoint: `uv run pytest tests/test_mcp.py -v -k "per_collection_embedder"`

---

### Phase 8 — Reindex Job Infrastructure
> **Releasable**: after Task 8.3 — the full reindex lifecycle (PATCH → pending; reindex endpoint → job created; `_reindex_task` → promotes on success) works end-to-end; `_default_ingest_task` uses the collection's `active_embedding_model` for new-collection ingest. (AC 10, 11, 28, 35, 49, 51)

#### Task 8.1 — `_reindex_task` function
- [ ] **File**: `archon_search/server/routes_jobs.py`
- **Depends on**: Task 3.4 (`ingest_directory` accepts embedder), Task 2.2 (embedder_cache), Task 1.2 (CollectionMeta fields), Task 1.4 (ReindexJob.target_embedding_model)
- **Description**:
  - **Invariant and guards** (before step 1 logic executes):
    - After `job = job_store.get(job_id)`: if `job is None` (job was pruned or store corrupted), fetch `meta = await store.get_collection_meta(collection, namespace)`, set `meta.reindex_job_id = None`, call `await store.update_collection_meta(meta)`, log an error, and return. Do NOT use `assert` here — the "never raises" contract (step 7) applies.
    - After the None check: `if not isinstance(job, ReindexJob): log error, mark job FAILED via `job_store.update()`, fetch and clear `meta.reindex_job_id`, return.`
    - After type check: `if job.status in (JobStatus.CANCELLING, JobStatus.CANCELLED): fetch meta, set `meta.reindex_job_id = None`, call `update_collection_meta`, return. Do NOT mark as FAILED — already cancelled.`
    - Only after all three guards pass should the step 1-7 logic execute.
  - `async def _reindex_task(job_id: str, store: SearchStore, job_store: JobStore, embedder_cache: EmbedderCache, pipeline: SearchPipeline, collection: str, namespace: str, collection_path: Path) -> None`:
    - `collection_path`: the filesystem path for the collection's corpus directory. Resolved in the reindex endpoint (Task 8.3) via `_all_collection_paths(config)` / `path_to_name[name]` and passed at task-creation time.
    1. Fetch `job = job_store.get(job_id)`. Read `target_model = job.target_embedding_model`. **Two branches diverge here** based on whether `target_model is None`:
       - **Model-change path** (`target_model is not None`): proceed to step 2 for embedder resolution; then step 3, 4, 5 (with full promotion).
       - **Data-only path** (`target_model is None`): this is a reindex with no model change. Fetch `meta = await store.get_collection_meta(collection, namespace)` to get `active_embedding_model`. Proceed to step 2 using `active_embedding_model` for embedder resolution. In step 5, skip the promotion step entirely — do NOT modify `active_embedding_model` or `pending_embedding_model`; only clear `reindex_job_id`. (AC 56 interaction: `None` for legacy jobs treated as "use global default" is ONLY for upgrade path; for data-only reindex via REST, `target_model` is explicitly set in Task 8.3.)
    2. Embedder resolution (both paths): 
       - Model-change path (`target_model is not None`): fetch `embedder = await embedder_cache.get_or_load(target_model)`.
       - Data-only path (`target_model is None`): fetch `meta = await store.get_collection_meta(collection, namespace)` (if not already fetched in step 1); fetch `embedder = await embedder_cache.get_or_load(meta.active_embedding_model)`.
    3. Mark job RUNNING.
    4. Call `await pipeline.ingest_directory(collection_path, collection, embedder=embedder, namespace=namespace, ...)`.
    5. **On success**:
       - Fetch current `meta = await store.get_collection_meta(collection, namespace)`.
       - **Branch by `target_model`**:
         - If `target_model is not None` (model-change reindex): always promote from `target_model` (the value captured at job-creation time in `IngestJob.target_embedding_model`), NEVER from `meta.pending_embedding_model`. Set `meta.active_embedding_model = target_model`. Set `meta.pending_embedding_model = None` only if it still equals `target_model` (if a concurrent PATCH changed `pending_embedding_model` to model C while reindex ran with target=B, set `active=B` and leave `pending=C` and `needs_reindex=True` unchanged — operator must trigger a new reindex). **Step 5 truth table** (after re-reading current `meta`):
         | Current `meta.pending_embedding_model` | Action on `pending` | Action on `needs_reindex` |
         |---|---|---|
         | == `target_model` | set to `None` | set to `False` |
         | != `target_model` AND not None | leave as-is | leave as `True` |
         | `None` (operator reverted via PATCH state-c during reindex) | leave as `None` | set to `False` |
         In all cases: set `meta.active_embedding_model = target_model` and `meta.reindex_job_id = None`.
         - If `target_model is None` (data-only reindex): do NOT modify `active_embedding_model` or `pending_embedding_model`. Only clear `meta.reindex_job_id = None`.
       - **WRITE ORDER**: `await store.update_collection_meta(meta)` FIRST, then mark job DONE. (AC 35)
       - **Note on atomicity**: The read-modify-write in step 5 is NOT atomic. Concurrent writes (description updates, centroid recomputes) may be overwritten by this step. See "Known limitations" for the full analysis — there is NO automatic self-heal after step 5. The stale non-C1 fields persist until the next file-change event triggers a sync recompute, which may never happen for static collections. This is documented accepted risk.
    6. **On failure**: fetch current `meta = await store.get_collection_meta(collection, namespace)`. Set `meta.reindex_job_id = None`. Call `await store.update_collection_meta(meta)` to persist the cleared `reindex_job_id`. Then mark job FAILED. `active_embedding_model` unchanged. (Note: `needs_reindex` and `pending_embedding_model` remain as they were — the operator must either retry the reindex or revert via PATCH.) (AC 11)
    7. Never raises — catches all exceptions, sets job to FAILED.
  - This function is SEPARATE from `_default_ingest_task`. They must not share embedder resolution logic. (AC 49)
- **Releasable**: reindex job correctly promotes `pending` → `active` on success; rolls back cleanly on failure.
- **Tests (TDD)** — `tests/test_routes_jobs.py`:
  - Unit: `test_reindex_task_promotes_active_on_success` — after task completes: `active=target_model`, `pending=None`, `needs_reindex=False`. (AC 10)
  - Unit: `test_reindex_task_preserves_active_on_failure` — mock `ingest_directory` to raise; `active_embedding_model` unchanged. (AC 11)
  - Unit: `test_reindex_task_writes_collection_meta_before_job_done` — inject a spy between the two writes; verify `CollectionMeta` write precedes job status DONE. (AC 35)
  - Unit: `test_reindex_task_uses_target_model_from_job` — `target_embedding_model="model-X"` in job; embedder_cache called with `"model-X"`. (AC 28)
  - Unit: `test_reindex_task_description_embedding_uses_global_model_dimension` — after `_reindex_task` completes with a non-global target model, fetch `CollectionMeta.description_embedding`; verify `len(description_embedding) == global_embedder.embedding_dim`, NOT `target_embedder.embedding_dim`. (AC 29)
  - Unit: `test_reindex_task_vectors_intact_after_failure` — ingest documents with model-A, then trigger `_reindex_task` with target=model-B but mock `ingest_directory` to raise partway through; verify: (a) `active_embedding_model` unchanged, (b) existing vectors still queryable (call `pipeline.search(...)` with model-A embedder and verify results). (AC 11)
  - Unit: `test_reindex_task_concurrent_patch_preserves_new_pending` — start `_reindex_task` with target=model-B; simulate a concurrent PATCH that changes `pending_embedding_model` to model-C while reindex runs; after reindex completes: verify `active_embedding_model=model-B` (promoted from target), `pending_embedding_model=model-C` (preserved from concurrent PATCH), `needs_reindex=True` (operator must trigger new reindex). (AC 10, concurrent variant)
  - Unit: `test_reindex_task_after_patch_revert_promotes_active_clears_needs_reindex` — start reindex with target=B; simulate concurrent PATCH revert (state c: set `pending=None`, `needs_reindex=False` in meta); when reindex completes, re-read meta to get reverted state; verify: `active_embedding_model=B` (promoted from target), `pending_embedding_model=None` (unchanged — revert already cleared it), `needs_reindex=False` (no pending, nothing left to reindex), `reindex_job_id=None`. (Truth table row 3 coverage)
  - Unit: `test_reindex_task_data_only_preserves_active_model` — `ReindexJob.target_embedding_model=None`; after `_reindex_task` completes: `active_embedding_model` unchanged, `pending_embedding_model` unchanged, `reindex_job_id=None`. (Data-only reindex path)
  - Unit: `test_reindex_task_data_only_failure_clears_job_id_only` — `ReindexJob.target_embedding_model=None`; mock `pipeline.ingest_directory` to raise `RuntimeError`; verify: `active_embedding_model` unchanged, `pending_embedding_model` unchanged (still `None`), `reindex_job_id=None` (cleared), `job.status == FAILED`. (Data-only reindex failure path — distinct from model-change failure because there is no pending model to preserve.)
  - Unit: `test_reindex_task_embedder_cache_failure_marks_job_failed` — mock `embedder_cache.get_or_load` to raise `RuntimeError("model not found")`; after task completes: `job.status == FAILED`, `reindex_job_id=None` in `CollectionMeta`. (Step 2 failure path — ensures job doesn't get stuck in PENDING)
  - Integration (`@pytest.mark.integration`): `test_full_lifecycle_patch_reindex_get` — using real (in-memory) stores: create collection with `active="model-A"`, PATCH to `embedding_model="model-B"`, POST to reindex, await job completion, GET collection; verify `active="model-B"`, `pending=None`, `needs_reindex=False`, `reindex_job_id=None`. (AC 10, 15-18)
  - Checkpoint: `uv run pytest tests/test_routes_jobs.py -v -k "reindex_task"`

#### Task 8.2 — `_default_ingest_task` update: read `active_embedding_model` from `CollectionMeta`
- [ ] **File**: `archon_search/server/routes_jobs.py`
- **Depends on**: Task 3.3 (`ingest_file` accepts embedder), Task 3.4 (`ingest_directory` accepts embedder), Task 2.2 (embedder_cache), Task 1.2 (CollectionMeta fields)
- **Description**:
  - **Architectural prerequisite — verify `pipeline_fn` behavior**: Before implementing this task, verify whether `app.state.ingest_pipeline` is wired to an actual callable in `app.py`. Run `grep -n "ingest_pipeline" archon_search/server/app.py` to check. If it is NOT wired (i.e., `pipeline_fn` is always `None` or a no-op), restructure `_default_ingest_task` to call pipeline methods **directly** (via `pipeline.ingest_file()` or `pipeline.ingest_directory()`) rather than delegating to `pipeline_fn`. In that case, `search_store` is `request.app.state.search_store`, `embedder_cache` is `request.app.state.embedder_cache`, and `pipeline` is `request.app.state.search_pipeline`. This is the recommended path. If `pipeline_fn` IS wired to an actual callable, the embedder resolution must happen inside `pipeline_fn`'s closure (not in `_default_ingest_task` itself). Whichever approach is used, the implementer must ensure the per-collection embedder is resolved before calling `pipeline.ingest_file()` or `pipeline.ingest_directory()`.
  - **Dependency injection required**: `_default_ingest_task` currently receives `JobStore` as `store`, not `SearchStore`. To implement this task, add `search_store: SearchStore`, `embedder_cache: EmbedderCache`, and `pipeline: SearchPipeline` parameters to `_default_ingest_task` (and `_default_ingest_task_with_lock`). **Note on `pipeline_fn`**: `app.state.ingest_pipeline` is never set in `app.py` — `pipeline_fn` is always `None` at runtime, making `_run_pipeline` a no-op stub. Task 8.2 fixes this: the restructured `_default_ingest_task` must call `pipeline.ingest_file(...)` or `pipeline.ingest_directory(...)` directly (depending on `IngestRequest.path` vs `IngestRequest.documents`), bypassing `pipeline_fn` entirely. This makes the ingest endpoint actually perform ingest for the first time. Update all call sites (3 in `routes_collections.py`, 2 in `routes_jobs.py`) to pass `pipeline=request.app.state.search_pipeline`, `search_store=request.app.state.search_store`, and `embedder_cache=request.app.state.embedder_cache` from the request context. Do NOT reach into `app.state` directly inside the task function (that creates a hidden dependency).
  - In `_default_ingest_task` (and `_default_ingest_task_with_lock`): after the collection is resolved, fetch `meta = await search_store.get_collection_meta(collection, namespace)`. Resolve `embedder = await embedder_cache.get_or_load(meta.active_embedding_model or config.embedding_model)`. Pass `embedder=embedder` to all `pipeline.ingest_file(...)` and `pipeline.ingest_directory(...)` calls.
  - **Dispatch logic within `_default_ingest_task`** (replacing the `pipeline_fn` stub):
    - If `body.path` is set: resolve `p = Path(body.path)`. If `p.is_file()`: call `pipeline.ingest_file(p, collection, embedder=embedder, namespace=namespace)`. If `p.is_dir()`: call `pipeline.ingest_directory(p, collection, embedder=embedder, namespace=namespace)`.
    - If `body.documents` is set (and `body.path` is None): This branch handles in-memory document ingest. Check whether `pipeline.ingest_documents(...)` exists (grep for it). If it does, call it with the appropriate embedder. If it does NOT exist: log a warning and skip (the document-ingest path may not be implemented yet; this is acceptable as a TODO, but must NOT silently corrupt the database).
    - `body.path` and `body.documents` are mutually exclusive per schema validation. If both are set: return an error.
  - `_default_ingest_task` does NOT read from `IngestJob.target_embedding_model` — it reads from `CollectionMeta.active_embedding_model`. (AC 49)
  - Update BOTH `_default_ingest_task` AND `_default_ingest_task_with_lock`.
  - **Behavioral note**: This task changes the `/ingest` endpoint from a no-op stub (currently does nothing when `pipeline_fn is None`) to actually performing ingest. This is a bug fix — the endpoint was always intended to perform ingest but was never correctly wired. Operators who previously relied on the endpoint doing nothing (e.g., using it as a health probe) should switch to `GET /health`.
- **Releasable**: new-collection ingest via the API uses the collection's `active_embedding_model`.
- **Tests (TDD)** — `tests/test_routes_jobs.py`:
  - Unit: `test_default_ingest_task_reads_embedder_from_collection_meta` — collection with `active_embedding_model="model-X"`; `_default_ingest_task` fetches `"model-X"` from embedder_cache. (AC 49)
  - Unit: `test_default_ingest_task_does_not_use_target_embedding_model` — `IngestJob.target_embedding_model="model-Y"` set; `_default_ingest_task` does NOT call embedder_cache with `"model-Y"`. (AC 49)
  - Unit: `test_default_ingest_task_with_lock_reads_embedder_from_collection_meta` — the `_with_lock` variant also fetches `"model-X"` from embedder_cache (not just the plain variant). (AC 49)
  - Integration (`@pytest.mark.integration`): `test_ingest_endpoint_passes_search_store_to_default_task` — POST to the ingest endpoint with a real (in-memory) test app; verify the ingest completes successfully end-to-end (confirming all call sites pass the new parameters correctly).
  - Unit: `test_default_ingest_task_empty_active_model_uses_global_fallback` — collection with `active_embedding_model=""`; verify `embedder_cache.get_or_load` called with `config.embedding_model`, NOT `""`. (Guards against the `or` fallback being accidentally removed.)
  - Unit: `test_default_ingest_task_file_path_calls_ingest_file` — `body.path` pointing to a file; verify `pipeline.ingest_file` called (not `ingest_directory`).
  - Unit: `test_default_ingest_task_dir_path_calls_ingest_directory` — `body.path` pointing to a directory; verify `pipeline.ingest_directory` called.
  - Checkpoint: `uv run pytest tests/test_routes_jobs.py -v -k "default_ingest_task"`

#### Task 8.3 — Reindex endpoint: read `pending_embedding_model`, set `target_embedding_model` + `reindex_job_id`
- [ ] **File**: `archon_search/server/routes_collections.py`, `archon_search/jobs/store.py`
- **Depends on**: Task 8.1, Task 1.4 (ReindexJob.target_embedding_model), Task 1.2 (CollectionMeta fields)
- **Description**:
  - **`JobStore.create_reindex()` method** (add to `archon_search/jobs/store.py`): `def create_reindex(self, namespace: str = DEFAULT_NAMESPACE, target_embedding_model: str | None = None) -> ReindexJob`. Constructs `ReindexJob(job_id=..., status=PENDING, namespace=namespace, target_embedding_model=target_embedding_model)` (using the same ID generation as `create()`). Include `"job_type": "reindex"` in the serialized JSON via `_write_atomic` (Task 1.4 handles this automatically via `isinstance` dispatch). Add this method's file to Task 8.3's **File** field alongside `routes_collections.py`.
  - In `reindex_collection` (`routes_collections.py:357`): replace `store.create(namespace=ns)` with `job_store.create_reindex(namespace=ns, target_embedding_model=meta.pending_embedding_model)`. This returns a `ReindexJob` instance directly. Do NOT use `store.create()` (returns `IngestJob`) followed by attribute assignment.
  - Set `meta.reindex_job_id = job.job_id`; write `CollectionMeta` via `store.update_collection_meta(meta)` BEFORE creating the asyncio task for `_reindex_task`.
  - Resolve `collection_path` from `_all_collection_paths(config)` (the same path-resolution used by the existing `reindex_collection` endpoint). Replace `asyncio.create_task(_default_ingest_task(...))` with `asyncio.create_task(_reindex_task(..., collection=name, namespace=ns, collection_path=collection_path))`.
  - **Call site clarification** (for Task 8.2 coordinators): `routes_collections.py:381` is the reindex endpoint call site that Task 8.3 REPLACES with `_reindex_task`. Task 8.2's "update all call sites" instruction applies to the OTHER 4 call sites (lines 215, 221 in `routes_collections.py` and 2 in `routes_jobs.py`), NOT to line 381 which is being replaced here. Do NOT pass new `search_store`/`embedder_cache`/`pipeline` parameters to the line 381 call — it is being removed.
  - **Step 0 — 409 guard**: Before creating a new job, load `meta = await store.get_collection_meta(name, ns)`. If `meta.reindex_job_id is not None`: cross-check `JobStore` for job status (same pattern as Task 5.1 step 5). If RUNNING or PENDING: return 409. If DONE, FAILED, or CANCELLED: clear `meta.reindex_job_id` (stale auto-clear, proceed). The reindex endpoint has the same stale-lock risk as PATCH; this guard prevents creating multiple concurrent reindex jobs.
- **Releasable**: reindex endpoint correctly captures `target_embedding_model` and sets `reindex_job_id`. (AC 51)
- **Tests (TDD)** — `tests/test_routes_collections.py`:
  - Unit: `test_reindex_endpoint_sets_reindex_job_id` — POST to `/reindex`; `GET /collections/{name}` shows `reindex_job_id` populated. (AC 51)
  - Unit: `test_reindex_endpoint_captures_pending_model` — `pending_embedding_model="model-X"` at request time; job has `target_embedding_model="model-X"`. (AC 28, 51)
  - Unit: `test_reindex_endpoint_data_only_sets_null_target` — `pending_embedding_model=None`; job has `target_embedding_model=None`.
  - Unit: `test_reindex_endpoint_returns_409_on_active_reindex` — set `meta.reindex_job_id` to a job ID with status RUNNING; POST to `/collections/{name}/reindex`; assert 409 returned and no new job created in `JobStore`. (Step 0 guard)
  - Unit: `test_reindex_endpoint_clears_stale_reindex_job_id_and_proceeds` — set `meta.reindex_job_id` to a job ID with status DONE; POST to reindex; assert stale ID is cleared and reindex proceeds (200, new `reindex_job_id` set). (Step 0 stale auto-clear)
  - Unit: `test_reindex_endpoint_creates_reindex_job_not_ingest_job` — POST to reindex, fetch job from `JobStore.get(job_id)`, assert `isinstance(job, ReindexJob)` (not `IngestJob`).
  - Checkpoint: `uv run pytest tests/test_routes_collections.py -v -k "reindex_endpoint"`

---

### Phase 9 — Sync/Watcher Fix
> **Releasable**: after Task 9.3 — no spurious reindex for non-default-model collections; `IndexingState.indexed_embedding_model` correctly reflects per-collection model after sync; sync ingest call sites pass the correct per-collection embedder. (AC 46, 47, Testing Considerations: sync non-regression)

#### Task 9.1 — Sync read-side fix: `_check_collection_changes` uses `CollectionMeta.active_embedding_model`
- [ ] **File**: `archon_search/sync.py`
- **Depends on**: Task 1.2 (CollectionMeta fields), Task 2.2 (store available in sync)
- **Description**:
  - `_check_collection_changes` (sync.py ~380) currently compares `self._embedding_model != indexed_embedding_model` (global model vs. state store). Change to: fetch `meta = await store.get_collection_meta(collection, namespace)` (or accept it as a parameter from the calling loop that pre-fetches all metas); compare `meta.active_embedding_model != indexed_embedding_model`.
  - If `_check_collection_changes` is synchronous: either convert to `async def` (cascading change) OR pre-fetch all `CollectionMeta` objects at the start of each sync cycle and pass the lookup result as a parameter. Prefer the parameter approach to minimize cascading.
  - The check must use `CollectionMeta.active_embedding_model` as authoritative. If `IndexingState.indexed_embedding_model` disagrees, still use `CollectionMeta` — no spurious reindex. (AC 47)
  - Collections with `active_embedding_model != global` must NOT trigger a spurious reindex on file-change events. (AC 33, Testing Considerations)
  - **Data-threading approach** (prefer parameter-passing to minimize cascading async changes):
    - At the start of each sync cycle (in the outer loop in `sync.py` that iterates over collections), pre-fetch all `CollectionMeta` objects: `metas = {m.name: m for m in await store.list_collection_metas(namespace=...)}`. Pass the pre-fetched `CollectionMeta` as a parameter to `_check_collection_changes(collection, meta: CollectionMeta, ...)` instead of re-fetching inside.
    - If `_check_collection_changes` is synchronous, it can accept `meta: CollectionMeta` as a plain parameter (no async needed for this approach).
    - **Store access**: `SearchCollectionSync` already accesses `self._pipeline.store` (verified at lines 127, 626, 693, 699, 701 in `sync.py`). Use `self._pipeline.store.get_collection_meta(name, namespace)` for the pre-fetch in the outer sync loop. Pass the pre-fetched `CollectionMeta` as a `meta: CollectionMeta` parameter to `_check_collection_changes` and all event handler methods that contain write sites.
    - **Constructor**: rename `self._embedding_model` to `self._global_embedding_model` in `SearchCollectionSync.__init__` (and its constructor parameter) for clarity. Update callers in `cli/sync.py` and `install.py`. This rename should be part of Task 9.1.
  - **Write sites**: Task 9.2 updates the ~9 write sites. Before implementation, run: `grep -n "indexed_embedding_model=self._embedding_model" archon_search/sync.py` to enumerate all sites. Each site is in a different event handler (initial sync, file-add, file-modify, file-delete, file-rename, etc.). The pre-fetched `CollectionMeta` must be threaded to each handler. If the pre-fetch approach is used, the `meta` parameter flows from the outer loop to each handler that writes `indexed_embedding_model`.
- **Releasable**: sync cycle no longer spuriously reindexes non-default-model collections.
- **Tests (TDD)** — `tests/test_sync.py`:
  - Unit: `test_sync_no_spurious_reindex_for_non_default_model` — collection with `active_embedding_model="model-X"` (non-global); file-change event; sync cycle; assert reindex NOT triggered. (AC, Testing Considerations)
  - Unit: `test_sync_uses_collection_meta_over_state_store` — `indexed_embedding_model` in state store is stale; `CollectionMeta.active_embedding_model` disagrees; assert no spurious reindex (CollectionMeta wins). (AC 47)
  - Unit: `test_no_self_embedding_model_in_sync` — grep/AST scan `archon_search/sync.py`; assert `self._embedding_model` appears zero times (renamed to `self._global_embedding_model`).
  - Checkpoint: `uv run pytest tests/test_sync.py -v -k "sync_no_spurious or collection_meta_authoritative"`

#### Task 9.2 — Sync write-side fix: write `active_embedding_model` to state store
- [ ] **File**: `archon_search/sync.py`
- **Depends on**: Task 9.1
- **Description**:
  - After every successful sync operation (all ~9 write sites that currently write `indexed_embedding_model=self._embedding_model`): replace `self._embedding_model` with `meta.active_embedding_model` (the per-collection value fetched in Task 9.1).
  - The state store's `indexed_embedding_model` must equal the collection's `active_embedding_model` after a successful sync, not the global model. (AC 46)
  - If the meta was pre-fetched at cycle start (Task 9.1 approach): use the same pre-fetched value for the write.
  - **Dead code cleanup**: After both Task 9.1 and Task 9.2 are complete, `self._global_embedding_model` (renamed from `self._embedding_model` in Task 9.1) is no longer read by any sync logic — all write sites now use `meta.active_embedding_model`. Remove the `_global_embedding_model` field and its constructor parameter from `SearchCollectionSync`. Update callers in `cli/sync.py` and `install.py` to no longer pass the `embedding_model` constructor argument. Also update all test call sites that pass `embedding_model=` to `SearchCollectionSync(...)` — run `grep -rn "embedding_model" tests/test_sync.py tests/test_sync_e2e.py` to enumerate them (expect 10+ sites). Also add a test: `test_no_global_embedding_model_in_sync` — verify `self._global_embedding_model` does not appear in `archon_search/sync.py` after the cleanup.
- **Releasable**: `IndexingState.indexed_embedding_model` correctly reflects per-collection model. (AC 46)
- **Tests (TDD)** — `tests/test_sync.py`:
  - Unit: `test_sync_writes_per_collection_model_to_state_store` — collection with `active_embedding_model="model-X"`; after successful sync (initial-sync path), `IndexingState.indexed_embedding_model == "model-X"`, not global. (AC 46)
  - Unit (parameterized): `test_sync_write_sites_use_per_collection_model[initial_sync|file_add|file_modify|file_delete|file_rename]` — for each of the 5 distinct sync event handler paths, trigger the event for a collection with `active_embedding_model="model-X"` and assert the written `IndexingState.indexed_embedding_model == "model-X"` (not the global model). Run `grep -n "indexed_embedding_model=self\._embedding_model" archon_search/sync.py` before implementation to enumerate all ~9 write sites; map each to an event handler path and add one parameterized case per distinct handler.
  - Checkpoint: `uv run pytest tests/test_sync.py -v -k "writes_per_collection_model"`

#### Task 9.3 — Sync engine: update `ingest_file`/`ingest_directory` call sites with per-collection embedder
- [ ] **File**: `archon_search/sync.py`
- **Depends on**: Task 9.1 (CollectionMeta pre-fetch in place), Task 3.3 (`ingest_file` requires `embedder`), Task 3.4 (`ingest_directory` requires `embedder`)
- **Description**:
  - After Phase 3, `pipeline.ingest_file()` and `pipeline.ingest_directory()` require `embedder: Embedder` as a mandatory keyword argument. The sync engine calls these methods at approximately lines 517, 644, and 671 in `sync.py`. These call sites will raise `TypeError` unless updated.
  - Run `grep -n "ingest_file\|ingest_directory" archon_search/sync.py` to enumerate all sync call sites before implementation.
  - **Embedder resolution strategy** (no `EmbedderCache` in sync — sync runs in both server and CLI contexts):
    - The sync engine already has the pre-fetched `CollectionMeta` from Task 9.1 (passed as a parameter to the event handlers). Resolve the embedder via: `embedder = make_embedder(meta.active_embedding_model or config.embedding_model)`. The `make_embedder` function is the direct instantiation path (same as `cli/collection.py`'s Task 10.2 reindex path).
    - The sync engine is short-to-medium-lived relative to the server — using `make_embedder` directly is acceptable. The server-side `EmbedderCache` is for the hot request path; sync cycles are infrequent enough that direct instantiation is correct here.
  - **Per-cycle embedder caching** (IMPORTANT — do NOT call `make_embedder()` per file event): The watcher fires one event per file; a burst of 100 file-change events for the same collection would create 100 `TextEmbedding` ONNX sessions if `make_embedder()` is called per event. Instead: cache the embedder per collection per sync cycle. In the outer loop that iterates collections (where `CollectionMeta` is pre-fetched in Task 9.1), also create one embedder per collection: `embedder = make_embedder(meta.active_embedding_model or config.embedding_model)`. Pass this single embedder instance to all file-event handlers for that collection. The embedder is re-created at the start of each new sync cycle (not per file event).
  - Update every `pipeline.ingest_file(...)` and `pipeline.ingest_directory(...)` call in `sync.py` to pass `embedder=embedder` (the per-cycle cached embedder, not a freshly-constructed one).
  - **Import change required**: add `from archon_search.embedder import make_embedder` to `sync.py`. Verify this does not create a circular import (`embedder.py` must not import from `sync.py`).
  - **`install.py`**: `install.py` constructs `SearchCollectionSync(pipeline, ..., embedding_model=cfg.embedding_model, ...)`. After Task 9.1 removes the constructor parameter (dead code cleanup), update `install.py` to no longer pass `embedding_model` to the constructor. The sync engine will resolve embedders from `CollectionMeta` instead.
  - **Note on CI window**: Tasks 9.3 must be committed in the same PR as Task 9.1/9.2 (or as close as possible). Between Phase 3 and Phase 9, `tests/test_sync.py` will fail because sync call sites pass no `embedder` to `pipeline.ingest_file/directory`. Phases 3-9 should be merged atomically if possible.
- **Releasable**: sync engine correctly invokes per-collection ingest with the right embedder. No spurious `TypeError` on file-change events.
- **Tests (TDD)** — `tests/test_sync.py`:
  - Unit (parameterized): `test_sync_ingest_calls_use_per_collection_embedder[initial_sync|file_add|file_modify]` — mock `make_embedder`; collection with `active_embedding_model="model-X"`; trigger each of the 3 distinct event paths that call `pipeline.ingest_file` or `pipeline.ingest_directory` in `sync.py`; assert `make_embedder` called with `"model-X"` (not the global model) for each path. Run `grep -n "ingest_file\|ingest_directory" archon_search/sync.py` before implementation to confirm the 3 call sites and map each to its event handler.
  - Unit: `test_sync_ingest_creates_embedder_once_per_cycle_not_per_event` — collection with `active_embedding_model="model-X"`; trigger 3 file-change events in the same sync cycle; assert `make_embedder` called exactly ONCE (not 3 times); assert all 3 `pipeline.ingest_file` calls received the SAME embedder instance (identity check: all three calls use the same object reference). This verifies that `make_embedder` is called once BEFORE the event loop (not inside it), and that the same instance is reused across all file events in the cycle.
  - Unit: `test_sync_ingest_empty_active_model_falls_back_to_global` — collection with `active_embedding_model=""`; assert `make_embedder` called with `config.embedding_model`.
  - Checkpoint: `uv run pytest tests/test_sync.py -v -k "ingest_calls_use_per_collection or empty_active_model_falls_back"`

---

### Phase 10 — MCP, CLI & Status
> **Releasable**: after Task 10.3 — `update_collection` MCP tool works; CLI reindex handles per-collection models correctly; `/status` surfaces `needs_reindex` collections. (AC 19–24, 34, 53)

#### Task 10.1 — `update_collection` MCP tool
- [ ] **File**: `archon_search/server/mcp.py`
- **Depends on**: Task 5.1 (PATCH endpoint is live)
- **Description**:
  - **Prerequisite**: Task 7.4 updates all existing MCP pipeline call sites (`search`, `search_with_context`, `explain`, `ingest_file`, `ingest_directory`) with per-collection embedder resolution. Task 10.1 adds only the new `update_collection` tool; it must NOT touch the call sites already updated in Task 7.4.
  - Add tool 11: `update_collection`. Tool function registered with `@mcp.tool()`.
  - Parameters: `collection_name: str`, `embedding_model: str`.
  - Implementation: delegate to the REST PATCH handler (or call the underlying service logic directly). Enforce namespace isolation: the authenticated namespace from the MCP auth layer restricts which collections are accessible (same enforcement as `routes_collections.py`).
  - Return: updated collection metadata (matches PATCH 200 response shape).
  - Error mapping: `ModelValidationError` → MCP error equivalent to HTTP 422; 409 → MCP error equivalent.
  - MCP tool count after this task: 11. (AC 24)
- **Releasable**: MCP clients can change a collection's model via `update_collection`. (AC 21–24)
- **Tests (TDD)** — `tests/test_mcp.py`:
  - Unit: `test_update_collection_returns_updated_meta` — valid call; returns updated metadata with `needs_reindex=true`. (AC 21)
  - Unit: `test_update_collection_unknown_model_returns_error` — unknown model; MCP error with 422-equivalent message. (AC 22)
  - Unit: `test_update_collection_running_reindex_returns_error` — active `reindex_job_id`; MCP error with 409-equivalent message. (AC 23)
  - Unit: `test_mcp_tool_count_is_11` — count registered tools; assert 11. (AC 24)
  - Unit: `test_update_collection_namespace_isolation` — attempt to update collection in a different namespace; fails. (Brief line 39)
  - Checkpoint: `uv run pytest tests/test_mcp.py -v -k "update_collection"`

#### Task 10.2 — CLI `archon-search collection reindex {name}` fix
- [ ] **File**: `archon_search/cli/collection.py`
- **Depends on**: Task 3.4 (`ingest_directory` accepts embedder), Task 1.2 (CollectionMeta fields)
- **Description**:
  - Resolution rules for the CLI reindex path (does NOT use `JobStore` or `_reindex_task`):
    - (a) If `meta.pending_embedding_model` is set: instantiate that model directly (no LRU cache — CLI is short-lived) via `make_embedder(meta.pending_embedding_model)`. Use this embedder for `pipeline.ingest_directory(...)`. On success: set `meta.active_embedding_model = meta.pending_embedding_model`, `meta.pending_embedding_model = None`, `meta.needs_reindex = False`, `meta.reindex_job_id = None`; write back via `store.update_collection_meta(meta)` before returning.
    - (b) If `meta.pending_embedding_model` is None (data-only reindex): use `meta.active_embedding_model` (or global default if empty). No promotion on success (active doesn't change).
  - **Failure path**: if `ingest_directory` raises, catch the exception, log it, and do NOT write any state changes. `active_embedding_model` unchanged, `pending_embedding_model` remains set, `needs_reindex` remains true. (AC 53)
  - If `meta.active_embedding_model` differs from config model: log a warning ("using per-collection model {model} for {collection}"). (Brief line 83)
  - (AC 34, 53)
- **Releasable**: CLI reindex handles per-collection and model-change reindexes correctly. (AC 34, 53)
- **Tests (TDD)** — `tests/test_cli_collection.py`:
  - Unit: `test_cli_reindex_uses_pending_model_for_model_change` — `pending="model-X"`; CLI reindex; embedder instantiated with `"model-X"`; `active` promoted on success. (AC 34)
  - Unit: `test_cli_reindex_uses_active_model_for_data_only` — `pending=None`; uses `active_embedding_model`. (AC 34)
  - Unit: `test_cli_reindex_failure_leaves_state_unchanged` — mock `ingest_directory` to raise; `active_embedding_model` unchanged, `pending` still set, `needs_reindex` still true. (AC 53)
  - Checkpoint: `uv run pytest tests/test_cli_collection.py -v -k "reindex"`

#### Task 10.3 — `/status` endpoint: include collections with `needs_reindex: true`
- [ ] **File**: `archon_search/server/routes_status.py`
- **Depends on**: Task 1.2 (CollectionMeta fields), Task 6.3 (CollectionSummary schema)
- **Description**:
  - In the `/status` response, add a list of collection names (or `CollectionSummary` objects) where `needs_reindex=true`. (AC 19)
  - A collection with `needs_reindex=true` does NOT make the overall status "degraded" — server operates normally. (AC 20)
  - If the `/status` response schema (`schemas_telemetry.py` or `routes_status.py`) has a top-level health indicator: verify it is not set to "degraded" based on `needs_reindex`.
- **Releasable**: operators can see which collections need reindex from `/status`. (AC 19, 20)
- **Tests (TDD)** — `tests/test_routes_status.py`:
  - Unit: `test_status_lists_needs_reindex_collections` — collection with `needs_reindex=true`; GET /status; name appears in the list. (AC 19)
  - Unit: `test_status_health_not_degraded_by_needs_reindex` — `needs_reindex=true` collection; overall status is not "degraded". (AC 20)
  - Checkpoint: `uv run pytest tests/test_routes_status.py -v -k "needs_reindex"`

---

### Phase 11 — Eval Harness
> **Releasable**: after Task 11.1 — eval suite passes with mixed-model fixtures. (AC 40)

#### Task 11.1 — Mixed-model eval fixtures
- [ ] **File**: `tests/eval/documents.jsonl`, `tests/eval/queries.jsonl`, `tests/eval/labels.jsonl`, `tests/eval/corpus/`, `tests/eval/routing/`, `archon_search/eval/backends.py`
- **Depends on**: Phase 2 (embedder cache), Phase 5 (PATCH), Phase 7 (search dispatch)
- **Description**:
  - Add eval fixtures covering at least one collection with the global model and one with a non-global model. Follow the fixture schemas documented in `tests/eval/README.md`.
  - Extend `archon_search/eval/backends.py` to support multi-model deterministic backends — the second collection's backend uses a deterministic stub for the non-global model (consistent with the harness's "deterministic, corpus-aware but label-blind" design).
  - Thresholds in `thresholds.toml`: the new fixtures should not lower existing thresholds. If the new collection's metrics are lower, add a separate threshold key for the mixed-model fixture set.
  - Run `uv run pytest -m eval tests/eval/test_eval_suite.py` and ensure it passes.
  - **Per-collection dispatch verification**: the eval fixture for the non-global-model collection must include an assertion that the search response's `embedding_model` field equals the non-global model name (not the global model). This verifies C1 dispatch is actually exercised and not silently bypassed by falling back to the global model. Add `test_eval_exercises_per_collection_dispatch` — run search against both the global-model collection and the non-global-model collection; assert `response.embedding_model` differs between the two collections.
- **Releasable**: eval suite verifies C1 does not regress retrieval or routing quality. (AC 40)
- **Tests**: the eval suite itself is the verification: `uv run pytest -m eval tests/eval/test_eval_suite.py`.
  - Checkpoint: `uv run pytest -m eval tests/eval/test_eval_suite.py`

---

### Phase 12 — Verification & Documentation

#### Task 12.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to discover all documentation in the project (`Documentation/`, `BREAKING.md`, `archon-search.toml.example`, `CLAUDE.md` references, `README`, ADRs, API reference, architecture docs, user guides) and update every file whose content is affected by C1. The agent must not update docs that are unrelated.
  - **BREAKING.md**: add entry — "The `embedding_model` field in `GET /collections/{name}` and `GET /collections/` responses is renamed to `active_embedding_model`. A new `pending_embedding_model` field (nullable) is added. Clients must update field references."
  - **Architecture docs**: update `Architecture/130_data_architecture_and_persistence.md` (new `CollectionMeta` columns), `Architecture/110_component_catalog_and_layer_breakdown.md` (`EmbedderCache` component), `Architecture/120_services_and_integration_architecture.md` (LRU cache in lifespan), `Architecture/600_api_reference_or_public_interface.md` (PATCH endpoint, new response fields, `update_collection` MCP tool).
  - **UserManual**: add a section on per-collection model configuration, the reindex lifecycle, and the `needs_reindex` visibility pattern.
  - Verify all 56 acceptance criteria below are met before marking Task 12.1 complete.
- **Releasable**: feature is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - AC 1: Single-collection search response includes `embedding_model` field with the collection's `active_embedding_model`. Multi-collection search response includes `embedding_model` with the global model.
  - AC 2: During `needs_reindex` window, search uses `active_embedding_model`, not `pending_embedding_model`.
  - AC 3: Collections with `active_embedding_model != global` appear in `excluded_collections` with `reason="embedding_model_mismatch"`.
  - AC 4: PATCH returns 200 with `needs_reindex=true` and `pending_embedding_model` set on success.
  - AC 5: PATCH returns 409 if `reindex_job_id` is set and job is active.
  - AC 6: PATCH returns 422 for unrecognised model name.
  - AC 7: PATCH returns 422 for dimension mismatch, with message identifying current and requested dimensions.
  - AC 8: PATCH with `active_embedding_model` (no pending in flight) returns 200 without setting `needs_reindex`.
  - AC 9: PATCH respects namespace isolation.
  - AC 10: After successful reindex: `needs_reindex=false`, `active=target_model` (value captured at job creation, NOT read from current `meta.pending_embedding_model` which may have changed), `pending=None` (only if still matches `target_model`; see concurrent-PATCH case), `reindex_job_id=None`.
  - AC 11: After failed model-change reindex (`target_model is not None`): `needs_reindex=true`, `active` unchanged, vectors intact, `reindex_job_id=None`. After failed data-only reindex (`target_model is None`): `active` unchanged, `pending` unchanged (was already `None` in the typical case; preserved if a concurrent PATCH set a new pending model during the reindex), `reindex_job_id=None`; `needs_reindex` remains as it was before the reindex was triggered.
  - AC 12: POST with `embedding_model` stores it as `active_embedding_model`; `pending=None`, `needs_reindex=false`.
  - AC 13: POST without `embedding_model` stores global default as `active_embedding_model`.
  - AC 14: POST with unknown model returns 422.
  - AC 15: `GET /collections/{name}` includes `active_embedding_model`, `pending_embedding_model`, `needs_reindex`.
  - AC 16: Before any PATCH: `active=creation model`, `pending=null`, `needs_reindex=false`.
  - AC 17: After PATCH (before reindex): `active` unchanged, `pending` set, `needs_reindex=true`.
  - AC 18: After reindex: `active=former pending`, `pending=null`, `needs_reindex=false`.
  - AC 19: `/status` lists collections with `needs_reindex=true`.
  - AC 20: `needs_reindex=true` does not make server "degraded".
  - AC 21: `update_collection` MCP tool returns updated metadata with `needs_reindex=true`.
  - AC 22: `update_collection` with unknown model returns 422-equivalent error.
  - AC 23: `update_collection` with running reindex returns 409-equivalent error.
  - AC 24: MCP tool count is 11.
  - AC 25: grep for `self._embedder` in `pipeline.py` returns zero; `self._global_embedder` appears only in global-model paths; `explain` has dual behavior.
  - AC 26: `migrate_per_collection_model()` is idempotent — twice yields same result.
  - AC 27: After migration, every pre-existing row has `active_embedding_model=prior embedding_model`, `pending=None`, `needs_reindex=false`, `reindex_job_id=None`.
  - AC 28: Reindex job uses `target_embedding_model` captured at job-creation time.
  - AC 29: After reindex to non-global model, `description_embedding` dimension matches global model dimension.
  - AC 30: PATCH revert (state c): `pending=null`, `needs_reindex=false`.
  - AC 31: PATCH replace-pending (state d): `pending=new_model`.
  - AC 32: PATCH on empty collection sets `active` directly; `pending=null`, `needs_reindex=false`.
  - AC 33: After `_ROUTING_FIELDS` and `_score_collections` updated, routing correctly excludes mismatched collections; no silent empty-string default.
  - AC 34: CLI reindex uses `pending_embedding_model` if set (promotes on success); uses `active_embedding_model` for data-only reindex.
  - AC 35: Reindex completion writes `CollectionMeta` before marking job DONE.
  - AC 36: Stale `reindex_job_id` (DONE/FAILED/CANCELLED) auto-cleared on next PATCH.
  - AC 37: `eager_load_embedders=true` pre-loads all distinct models; first search has no load latency.
  - AC 38: `eager_load_embedders=true` with unrecognized model: server starts, logs warning, `/health` healthy.
  - AC 39: Model validation timeout (>30s) returns 422 with 'could not determine model output dimension'; request completes within 31s.
  - AC 40: Mixed-model eval fixtures exist; eval suite passes.
  - AC 41: `GET /collections/` includes `needs_reindex` and `active_embedding_model` per collection.
  - AC 42: PATCH with missing/null/empty `embedding_model` returns 422.
  - AC 43: Single-collection `/explain` reflects collection's `active_embedding_model` in response.
  - AC 44: `recompute_collection_meta()` preserves `active_embedding_model` for non-global-model collection.
  - AC 45: Migration state (b) crash-recovery: adds missing columns, backfills defaults, is a no-op on second run.
  - AC 46: After sync of non-default-model collection, `IndexingState.indexed_embedding_model == active_embedding_model` (not global).
  - AC 47: When state store and `CollectionMeta` disagree, sync uses `CollectionMeta` (no spurious reindex).
  - AC 48: PATCH state (a′): same-pending no-op returns 200; state unchanged.
  - AC 49: `_default_ingest_task` reads from `CollectionMeta.active_embedding_model`; `_reindex_task` reads from `IngestJob.target_embedding_model`.
  - AC 50: After ingest, stored model name per chunk equals collection's `active_embedding_model`.
  - AC 51: Reindex endpoint sets `IngestJob.target_embedding_model` from `pending_embedding_model` and populates `CollectionMeta.reindex_job_id`.
  - AC 52: Three concurrent requests for same uncached model result in exactly one `TextEmbedding()` instantiation.
  - AC 53: CLI reindex failure leaves state unchanged: `active` unchanged, `pending` set, `needs_reindex=true`.
  - AC 54: `embedder_cache_size=1`, two concurrent different-model requests both complete correctly; evicted embedder not GC'd mid-encode.
  - AC 55: `eager_load_embedders=true` uses `asyncio.to_thread()` for each model load; event loop not blocked.
  - AC 56: PATCH on nonexistent collection returns 404.
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: `uv run pytest` (full suite, all markers default) passes with ≥85% coverage.
