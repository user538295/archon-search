# A1 — Metadata Schema v1 (minimum end-to-end slice)

> **Supersedes brief on**: IngestedBy Literal (4 members + read normalization), lock granularity (per-collection), types.py audit-only.

**Purpose**: Ship a typed, populated, end-to-end metadata schema slice that unblocks A2 (filters) and C1 (per-collection embedding model). Wire `file_type`, `updated_at`, `ingested_by` from parser through chunker into the store; surface them in REST + MCP responses; add an opt-in `reindex-metadata` backfill CLI; audit type import paths; document the partition map in code.
**Audience**: archon-search contributors implementing A1; reviewers of the resulting PRs.
**Status**: Draft

---

## Background

The LanceDB chunk schema and `ChunkRecord` dataclass already declare metadata fields (`file_type`, `language`, `updated_at`, `ingested_by`, `custom_score`, `acl`, free-form `metadata`) but the ingest pipeline does not populate most of them: `file_type` is never set by `DocumentChunker.chunk()`, `updated_at` defaults to `""` and falls back to `indexed_at` in `store.py`, and `ingested_by` is hard-coded to `"archon-search-cli"` regardless of source. The public search response surface (`SearchResult`, `SearchResultSchema`, MCP `asdict()` payloads) omits these fields entirely. There is also a known drift bug in `SearchResultSchema.from_result()` that silently drops the existing `acl` field, and `search_with_context` MCP responses leak the raw `vector` (large float list) via `asdict(ChunkRecord)`.

A1 = real extraction end-to-end, not documentation-only. The brief at `Documentation/Backlog/A1-metadata-schema-v1-brief.md` resolves all major design questions; this plan is the implementation decomposition following the brief's 7-commit stacking order.

## Goal

After A1 ships:

1. A fresh ingest of `foo.md` produces a chunk row with `file_type == "md"`, non-empty `updated_at`, and `ingested_by` reflecting the call site (`"cli"`, `"http"`, `"watcher"`, or `"reindex"`).
2. The `/search` REST JSON response and the MCP `search` tool response both include `file_type`, `indexed_at`, `updated_at`, `ingested_by`, `metadata`, and `acl` for every result.
3. `archon-search collection reindex-metadata <name>` populates the same fields on a pre-A1 collection (opt-in, with `--dry-run`, with progress logging, with concurrent ingest blocked for the duration).
4. The eval harness still passes with unchanged thresholds; the 85% coverage gate still holds on the default pytest run.
5. `Documentation/Backlog/03_world_class_roadmap.md` is amended (drop `language` from A2's filter dimensions; forward-reference C2).
6. `BREAKING.md` documents the MCP shape changes (truly breaking for strict-validating MCP clients) and the additive REST shape changes (non-breaking for tolerant JSON consumers).

---

## Scope

### In Scope
- Threading file extension (lowercased, no leading dot) through `parser.py` → `chunker.py` → `store.py`.
- Populating `updated_at` from `path.stat().st_mtime` at ingest, with fallback to `indexed_at` when `stat()` fails (logged at DEBUG).
- Populating `ingested_by` with the actual call-site identity (`cli` / `http` / `watcher` / `reindex`).
- Growing `SearchResult` to include `file_type`, `indexed_at`, `updated_at`, `ingested_by`, `metadata`.
- Updating `SearchResultSchema` to mirror `SearchResult` **including the existing `acl` drift fix**.
- Wiring `parse_metadata()` into `SearchStore.hybrid_search`'s row-to-`SearchResult` mapping.
- Stripping `vector` from `search_with_context` MCP context payloads.
- Consolidating `types.py` ↔ `_types.py` `ChunkRecord` duplication.
- Adding explicit `nullable=True` to the `custom_score` PyArrow field for readability + `None` round-trip test.
- New `archon-search collection reindex-metadata <name>` CLI (with `--dry-run` and progress logging).
- New in-process **per-collection** `asyncio.Lock` map on `SearchStore` to serialize ingest vs reindex within a collection (ingest uses `asyncio.wait_for` on lock acquisition only, hardcoded 30s; on timeout REST `/ingest` returns HTTP 503 with `Retry-After`; reindex never times out).
- Partition-map documentation as docstring blocks on `_types.ChunkRecord`; one-paragraph pointer in `Documentation/Architecture/130_data_architecture_and_persistence.md`.
- `BREAKING.md` entry distinguishing MCP (breaking) from REST (additive).
- Roadmap amendment.

### Out of Scope
- Language detection — deferred to C2; `language` stays storage-only on `ChunkRecord`, never reaches `SearchResult`.
- `custom_score` population logic — schema-reserved only.
- Centroid incremental update — deferred to B5.
- Free-form `metadata` JSON validation changes — existing bounds unchanged.
- `doc_id` hashing for telemetry — deferred to D8.
- Query-side filter wiring (LanceDB `where()`, REST filter params) — A2's scope.
- Auto-reindex on startup.
- Cross-process locking for reindex — documented limitation.
- MCP Pydantic-model wrapping (C7/API-4) — A1 is the **last** untyped MCP shape break before C7.
- Filter-result-quality signaling for partial results — A2's problem.
- **`CollectionMeta.schema_version` field** — the brief lists this as an open question; A1 defers it entirely to a follow-up brief. Adding it now would touch ~94 `CollectionMeta(...)` call sites (verified count) for no behavior benefit in A1. A follow-up brief will resolve the strictness question (default-valued vs no-default) before implementing.

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 8.1 — Final verification & documentation update].

---

## What does NOT change
- LanceDB on-disk row format for existing chunks (legacy `ingested_by == "archon-search-cli"` and empty `file_type` / `updated_at` are preserved verbatim until `reindex-metadata` runs).
- The per-collection write locks in `sync.py:_collection_locks` (untouched; the new `SearchStore._collection_locks` is a separate, independent lock map — see "Known limitations / accepted trade-offs" for the explicit non-hierarchy statement).
- Eval harness thresholds in `tests/eval/thresholds.toml`.
- The existing `_META_MAX_FIELDS`, `_META_MAX_KEY_LEN`, `_META_MAX_VAL_LEN` bounds on free-form `metadata`.
- The `X-Ingested-By` REST header is still accepted for callers that send `"archon-search-cli"`; new behavior is that the value is normalized to `"cli"` at the boundary (the request still succeeds — no hard fail).
- `GET /health`'s no-auth contract.
- The `query`-less telemetry invariant (no raw queries logged).

---

## Known limitations / accepted trade-offs
- **No auto-migration.** Existing collections return empty `file_type` / fallback `updated_at` / legacy `ingested_by` until operators run `reindex-metadata`. A2 must address partial-result signaling.
- **Cross-process reindex/ingest safety is not guaranteed.** The new lock is `asyncio.Lock` (single-process only). Multi-process deployments must coordinate externally; documented in the persistence doc and `BREAKING.md`.
- **Lock families are independent, not strictly hierarchical.** `SearchStore`'s per-collection lock map and `SearchCollectionSync._collection_locks` are two separate structures with the same key space (collection name). The plan does **not** claim a strict outer/inner relationship between them, because `SearchStore.ingest_chunks` is called from sites (e.g., direct REST `/ingest`) that do not hold the sync lock. The store lock serializes ingest vs reindex; the sync lock serializes watcher/sync re-ingest; they coincidentally protect the same writes from different call paths.
- **`language` is intentionally storage-only.** Not exposed in responses until C2; this is enforced by the field-parity snapshot test (Task 4.2).
- **`X-Ingested-By` unknown values are silently coerced to `"http"`** with a WARNING log (not hard-failed). The unknown value is truncated to 32 characters in the log line. Rate-limiting the warning is a follow-up if abuse becomes a concern.
- **MCP shape break.** Strict-validating MCP clients see new keys; A1 is the last such break before C7 wraps responses in Pydantic models.
- **`file_type` is the last suffix only.** Compound extensions like `.tar.gz` and `.d.ts` register as `gz` / `ts` — the leading suffix is lost. Acceptable for v1 because A2 filters on the stored value directly; callers wanting compound filtering must wait for a future iteration that exposes a multi-segment field.
- **`updated_at` reflects `Path.stat().st_mtime` semantics.** Clock skew, timezone changes on the filesystem (e.g., DST transitions on FAT32, NFS clients with skewed clocks) may produce non-monotonic `updated_at` values across re-ingests. A1 does not try to normalize this; consumers should treat `updated_at` as advisory.
- **Symlinks follow target mtime.** `Path.stat()` follows symlinks by default, so `updated_at` reflects the target's mtime; `file_type` is derived from the link's own name. This is acceptable but documented.
- **Future `updated_at` values are accepted as-is.** If a file's mtime is in the future (clock skew, deliberate `touch -d`), `updated_at` carries that value verbatim. No clamping.
- **`Retry-After: 30` reflects the lock-acquisition timeout, not the remaining reindex duration.** A long reindex (e.g., a large collection taking several minutes) may produce repeated 503s for clients that retry exactly at the advertised interval. Acceptable for v1 because `reindex-metadata` is an opt-in operator command, not a hot-path operation; clients should treat `Retry-After` as a lower bound and apply their own backoff for sustained 503s.
- **`merge_insert` / `table.update` performance on large collections is unverified.** These LanceDB APIs are new to this codebase as part of A1; performance on collections with >10K rows has not been measured. The `reindex-metadata` CLI is operator-triggered and offline-friendly, so latency regressions are acceptable as long as the operation completes. Task 6.2 includes a non-blocking benchmark TODO to capture wall-clock timings during integration testing; if the numbers are unacceptable, the follow-up is a batching/pagination tweak rather than a re-design.

---

## Architecture

### Modules touched
- `archon_search/parser.py` — already lowercases suffix; expose `file_type` (suffix without dot) and `updated_at` (mtime ISO 8601) from `ParseResult` / parser output.
- `archon_search/chunker.py` — `DocumentChunker.chunk()` signature gains required `file_type: str`, `updated_at: str`, `ingested_by: Literal[...]` parameters.
- `archon_search/_types.py` — `ChunkRecord.ingested_by` becomes `IngestedBy = Literal["cli", "http", "watcher", "reindex"]` (4 members only — legacy is **not** in the Literal). `ChunkRecord` gets partition-map docstrings. `SearchResult` grows `file_type`, `indexed_at`, `updated_at`, `ingested_by`, `metadata`. The `language` and `custom_score` fields stay on `ChunkRecord` only.
- `archon_search/types.py` — `types.py` and `_types.py` contain **distinct** types (verified: `types.py` has `Chunk`, `Collection`, `CollectionDetail`, `IngestJob`, `JobStatus`, `DeleteJob`, `ReindexJob`, `Query`, `RouteResponse`; `_types.py` has `ChunkRecord`, `SearchResult`, `DocumentInfo`, `CollectionInfo`, `IngestResult`). They are **not** duplicated. Phase 1 is scoped down to "audit imports and pick the canonical import path going forward"; no relocation is required unless the audit finds an actual duplication.
- `archon_search/store.py` — `SearchStore` gains `self._collection_locks: dict[str, asyncio.Lock]` (mirrors `sync._collection_locks` pattern); `ingest_chunks` acquires the per-collection lock via `asyncio.wait_for(lock.acquire(), timeout=30.0)`; `hybrid_search` does **not** acquire any lock (read path stays lock-free). `hybrid_search`'s row-to-`SearchResult` block runs `parse_metadata()`. The existing write-path coercion at `store.py` (`c.ingested_by or "archon-search-cli"`) is **removed** — empty / falsy `ingested_by` is no longer silently rewritten to legacy. The read-path (`r.get("ingested_by") or "archon-search-cli"`) is changed to normalize legacy → `"cli"` so callers and response schemas never see the legacy string; reindex still rewrites stored legacy values to `"reindex"`. `custom_score` PyArrow field gets explicit `nullable=True`. New `reindex_metadata(collection: str, *, dry_run: bool, progress_cb)` method.
- `archon_search/server/routes_search.py` — `SearchResultSchema` gains `file_type`, `indexed_at`, `updated_at`, `ingested_by`, `metadata`, and the previously-dropped `acl`; `from_result()` populates all of them.
- `archon_search/server/routes_search.py` and `routes_collections.py` (ingest path) — populate `ingested_by` from `X-Ingested-By` header with validator; on `TimeoutError` from store lock acquisition, return HTTP 503 with `Retry-After: 30`.
- `archon_search/server/mcp.py` — `search_with_context` strips `vector` from `context_before` / `context_after` chunks before `asdict()` serialization.
- `archon_search/cli/collection.py` (or wherever the `collection` subcommand lives) — new `reindex-metadata <name> [--dry-run]` subcommand calling `SearchStore.reindex_metadata`.
- `archon_search/sync.py`, `archon_search/watcher.py` — call sites pass `ingested_by="watcher"`.
- `archon_search/cli/ingest.py` — call sites pass `ingested_by="cli"`.
- `archon_search/jobs/*` — async ingest jobs pass `ingested_by="http"` (or whatever the REST handler resolved from the header).

### New config keys
- None. Ingest-lock timeout is hardcoded to `30.0` seconds in `SearchStore` (the only externally-visible knob is the 503 `Retry-After` header, which derives from this value). A config key can be added later if operators report needing to tune it.

### New constants
- `archon_search/constants.py` — `LEGACY_INGESTED_BY = "archon-search-cli"` (used at the read-normalize and header-parse boundaries to translate legacy → `"cli"`), `INGESTED_BY_VALUES: tuple[str, ...] = ("cli", "http", "watcher", "reindex")` (4 canonical values; legacy is **not** included), `INGEST_LOCK_TIMEOUT_S: Final[float] = 30.0`.

### Type signatures (selected)
```python
# _types.py
IngestedBy = Literal["cli", "http", "watcher", "reindex"]

@dataclass
class ChunkRecord:
    # ... existing fields ...
    file_type: str = ""           # PARTITION: filterable
    language: str | None = None   # PARTITION: filterable (reserved; C2)
    metadata: dict[str, str] = field(default_factory=dict)  # PARTITION: filterable
    custom_score: float | None = None  # PARTITION: ranking (reserved)
    ingested_by: IngestedBy = "cli"     # PARTITION: audit (4 canonical values; legacy normalized at boundaries)
    updated_at: str = ""           # PARTITION: filterable
    acl: list[str] | None = None  # PARTITION: system

@dataclass
class SearchResult:
    doc_id: str
    chunk_id: str
    text: str
    score: float
    source_path: str
    file_type: str = ""
    indexed_at: str = ""
    updated_at: str = ""
    ingested_by: IngestedBy = "cli"   # never carries the legacy "archon-search-cli" value
    metadata: dict[str, str] = field(default_factory=dict)
    acl: list[str] | None = None
```

```python
# chunker.py
def chunk(
    self,
    text: str,
    doc_id: str,
    source_path: str,
    *,
    file_type: str,
    updated_at: str,
    ingested_by: IngestedBy,
) -> list[ChunkRecord]: ...
```

```python
# store.py
class SearchStore:
    def __init__(self, ...):
        self._collection_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, collection: str) -> asyncio.Lock:
        if collection not in self._collection_locks:
            self._collection_locks[collection] = asyncio.Lock()
        return self._collection_locks[collection]

    async def ingest_chunks(self, collection: str, ...):
        lock = self._lock_for(collection)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=INGEST_LOCK_TIMEOUT_S)
        except asyncio.TimeoutError as e:
            raise StoreBusyError(timeout_s=INGEST_LOCK_TIMEOUT_S) from e
        try:
            ...  # actual write
        finally:
            lock.release()

    async def reindex_metadata(
        self,
        collection: str,
        *,
        dry_run: bool = False,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> ReindexResult: ...
```

### Lock structure
`SearchStore` holds a per-collection `asyncio.Lock` map keyed by collection name. Ingest acquires the lock for **its** collection only — ingest into collection A does **not** serialize against ingest into collection B. Reindex of collection A holds A's lock for its full duration, blocking only A's ingest path. The existing `SearchCollectionSync._collection_locks` is a separate structure with the same key space; the two lock families are **independent** (see "Known limitations / accepted trade-offs"). The timeout applies only to lock acquisition; once acquired, the write itself is not interrupted.

### REST 503 contract
On `asyncio.TimeoutError` while waiting for the per-collection store lock during `POST /ingest`, the handler returns:
```
HTTP/1.1 503 Service Unavailable
Retry-After: 30
Content-Type: application/json

{"error": "store_busy", "detail": "reindex in progress; retry after Retry-After seconds"}
```
The `Retry-After` value is `str(math.ceil(timeout_s))` — non-integer timeouts (e.g., `12.7`) are rounded up to integer seconds per RFC 7231.

---

## Task breakdown

### Phase 1 — Prep: import-path audit (no relocation)
> **Releasable**: when Task 1.1 lands; no behavior change. Phase 1 was originally framed as "consolidation" but verification against source shows `types.py` and `_types.py` hold **distinct** types, not duplicates — there is nothing structural to merge. The task is reduced to an import-path audit that records the canonical home for each public name; actual relocation is deferred (and may not be needed at all).

#### Task 1.1 — Audit `types.py` vs `_types.py` external imports
- [x] **File**: N/A (audit task; output is a one-paragraph note appended to this plan)
- **Depends on**: nothing
- **Description**:
  - Verified upfront against source: `types.py` declares `JobStatus`, `IngestJob`, `ReindexJob`, `DeleteJob`, `Query`, `RouteResponse`, `Collection`, `CollectionDetail`, `Chunk`. `_types.py` declares `ChunkRecord`, `SearchResult`, `DocumentInfo`, `CollectionInfo`, `IngestResult`. No name collision; no `ChunkRecord` duplication.
  - One small drift exists: `types.py:Chunk` has `file_type: str` as a required positional arg (no default), while `_types.py:ChunkRecord.file_type` is `str = ""`. They are different dataclasses with overlapping fields, not duplicates; A1 does **not** unify them. If a downstream task finds `Chunk` is unused, mention it but do not delete it in A1.
  - Grep the repo (`archon_search/`, `tests/`, `Documentation/`) for every import from `archon_search.types` and from `archon_search._types`. Record the canonical home for each public name as a short paragraph appended to this plan.
  - **No code changes** in this task — purely a recorded audit. If the audit finds actual duplication, schedule a follow-up; do not expand A1's scope.
- **Releasable**: nothing user-visible.
- **Tests (TDD)** — N/A (audit only).
- **Checkpoint**: `grep -rn "from archon_search.types\|from archon_search._types" archon_search tests Documentation | wc -l` matches the catalogued count.

---

### Phase 2 — Schema readability: explicit `nullable=True` on `custom_score`
> **Releasable**: when Task 2.1 lands; no data migration needed.

#### Task 2.1 — Make `custom_score` schema nullability explicit + pin round-trip
- [x] **File**: `archon_search/store.py` (PyArrow schema definition for chunk table)
- **Depends on**: nothing (PyArrow schema edit is independent of the type audit)
- **Description**:
  - Locate the `pa.schema([...])` (or `pa.field(...)`) block defining the chunk table; on the `custom_score` field, add the explicit `nullable=True` keyword.
  - No data migration: PyArrow defaults `nullable=True`, so existing collections already accept `None`. This change is for readability + invariant pinning only.
  - Verify the row-to-`SearchResult` and row-to-`ChunkRecord` paths do not coerce `None` → `0.0` anywhere. If a coercion exists, remove it (call out in PR description).
- **Releasable**: `custom_score = None` round-trips through write + read without becoming `0.0`.
- **Tests (TDD)** — `tests/test_store_custom_score.py`:
  - Integration (`@pytest.mark.integration`, real LanceDB temp dir): `test_custom_score_none_round_trip` — ingest a chunk with `custom_score=None`, read it back via `_read_all_chunks` (or equivalent), assert `read.custom_score is None`.
  - Integration: `test_custom_score_value_round_trip` — ingest with `custom_score=0.42`, read back equals `0.42` (sanity).
  - Unit: `test_custom_score_field_nullable_kwarg_present` — introspect the PyArrow schema, assert the `custom_score` field has `nullable=True` explicitly set (guards against future "tidy-up" PRs flipping it).
- **Checkpoint**: `uv run pytest tests/test_store_custom_score.py -v -m "integration or not integration"`.

---

### Phase 3 — Field wiring: parser → chunker → store
> **Releasable**: when Task 3.4 lands; new ingests carry real metadata, but responses do not yet expose it (Phase 4 closes that loop). Internally observable via `_read_all_chunks`.

#### Task 3.1 — Add `IngestedBy` Literal + `INGESTED_BY_VALUES` constant
- [x] **File**: `archon_search/_types.py`, `archon_search/constants.py`
- **Depends on**: nothing (independent type alias addition; Task 4.1 ordering chosen for review clarity)
- **Description**:
  - In `_types.py`: define `IngestedBy = Literal["cli", "http", "watcher", "reindex"]` (**four** members; legacy is **not** in the Literal). Annotate `ChunkRecord.ingested_by: IngestedBy = "cli"`. Legacy `"archon-search-cli"` values stored in pre-A1 rows are normalized to `"cli"` at the read boundary in `store.hybrid_search` and `_read_all_chunks` (Task 4.3), and at the header-parse boundary (Task 3.3). Reindex (Task 6.2) still rewrites stored legacy values to `"reindex"`.
  - In `constants.py`: add `LEGACY_INGESTED_BY: Final = "archon-search-cli"` (used only at boundaries for normalization) and `INGESTED_BY_VALUES: Final[tuple[str, ...]] = ("cli", "http", "watcher", "reindex")` (4 canonical values; legacy is **not** included).
- **Releasable**: type-check passes; runtime values from any of the four members are accepted. Type-only; no call sites switched yet.
- **Tests (TDD)** — `tests/test_types_ingested_by.py`:
  - Unit: `test_ingested_by_values_constant_matches_literal` — parse the `IngestedBy` `Literal` args at runtime via `typing.get_args(IngestedBy)`, assert tuple-equal with `INGESTED_BY_VALUES` (4 members). Pins drift between the constant and the type alias.
  - Unit: `test_chunk_record_accepts_each_ingested_by_value` — parametrize over `INGESTED_BY_VALUES`, construct `ChunkRecord(...)` with each, assert no error.
  - Unit: `test_legacy_value_not_in_literal` — assert `LEGACY_INGESTED_BY not in typing.get_args(IngestedBy)` and `LEGACY_INGESTED_BY not in INGESTED_BY_VALUES`. Pins the boundary-normalization design.
- **Checkpoint**: `uv run pytest tests/test_types_ingested_by.py -v`.

#### Task 3.2 — Extend `DocumentChunker.chunk()` signature
- [x] **File**: `archon_search/chunker.py`
- **Depends on**: Task 3.1
- **Description**:
  - Change signature to `def chunk(self, text: str, doc_id: str, source_path: str, *, file_type: str, updated_at: str, ingested_by: IngestedBy) -> list[ChunkRecord]`. All three new parameters are **keyword-only and required** (no defaults — forces every call site to make a deliberate choice).
  - Populate the returned `ChunkRecord(...)` constructor with the three new values verbatim. `indexed_at` continues to be set by the chunker as today.
- **Releasable**: chunker writes the new fields onto every emitted `ChunkRecord`. Compile-time errors at every caller until Task 3.3 updates them.
- **Tests (TDD)** — `tests/test_chunker.py` (extend existing):
  - Unit: `test_chunk_propagates_file_type` — call with `file_type="md"`, assert every returned record has `record.file_type == "md"`.
  - Unit: `test_chunk_propagates_updated_at` — call with a known ISO timestamp, assert it appears on every record verbatim.
  - Unit: `test_chunk_propagates_ingested_by` — parametrize over `("cli", "http", "watcher", "reindex")`, assert each appears on every record.
  - Unit: `test_chunk_file_type_lowercase_md` — pass `file_type="md"`, assert `file_type == "md"` (not normalized to `"markdown"`).
  - Unit: `test_chunk_file_type_empty_for_no_extension` — pass `file_type=""`, assert empty string preserved.
  - Unit: `test_chunk_requires_keyword_args` — assert `TypeError` on positional call (`chunker.chunk(text, doc_id, source_path, "md", ...)`).
- **Checkpoint**: `uv run pytest tests/test_chunker.py -v`.

#### Task 3.3 — Update all chunker call sites (`pipeline`, `sync`, `watcher`, CLI ingest, REST ingest, jobs)
- [x] **File**: `archon_search/pipeline.py`, `archon_search/sync.py`, `archon_search/watcher.py`, `archon_search/cli/ingest.py` (or equivalent), `archon_search/server/routes_collections.py` (or wherever the REST `/ingest` handler builds chunks), `archon_search/jobs/` (the async ingest job runner)
- **Depends on**: Task 3.2
- **Description**:
  - Derive `file_type` from `Path(source_path).suffix.lower().lstrip(".")` at the call site (or thread it from `DocumentParser.parse()` output if the parser already exposes it).
  - Derive `updated_at` from `path.stat().st_mtime` converted to UTC ISO 8601 (`datetime.fromtimestamp(mtime, tz=UTC).isoformat()`). On `OSError` (vanished file, permission, stdin), set `updated_at = ""` and log at DEBUG (`store.py`'s existing fallback `updated_at or indexed_at` will fill it).
  - Pass `ingested_by` matching the call site: `"cli"` for the CLI ingest command, `"http"` for REST ingest (resolved from `X-Ingested-By` validator below; default `"http"` when header missing), `"watcher"` for watcher/sync re-ingest, `"reindex"` only inside the `reindex-metadata` path (Task 6.2).
  - REST handler: add `X-Ingested-By` header validator. If header missing → `"http"`. If present and value ∈ `INGESTED_BY_VALUES` (the 4 canonical values) → use as-is. If value equals `LEGACY_INGESTED_BY` (`"archon-search-cli"`) → normalize to `"cli"` (accept the old header, translate at the boundary; do not propagate legacy into new rows). Unknown values → `"http"` + WARNING log including the unknown value **truncated to 32 chars** and the requesting peer if available.
  - Remove the hard-coded `"archon-search-cli"` default in any chunker / ingest call site; the dataclass default in `_types.py` stays `"cli"` for direct construction safety in tests.
- **Releasable**: a fresh `archon-search ingest foo.md` produces chunks with `file_type="md"`, `updated_at` set from mtime, `ingested_by="cli"`. REST `/ingest` produces chunks with `ingested_by="http"` (or header-overridden value). Watcher re-ingest produces `ingested_by="watcher"`.
- **Tests (TDD)** — split across:
  - `tests/test_pipeline_metadata.py`:
    - Unit: `test_cli_ingest_sets_ingested_by_cli` — invoke pipeline via the CLI code path with a temp file, inspect emitted chunks, assert `ingested_by == "cli"`, `file_type` matches extension, `updated_at` non-empty.
    - Unit: `test_pipeline_falls_back_when_stat_fails` — patch `Path.stat` to raise `OSError`, assert `updated_at` becomes empty (or falls through to `indexed_at` in store) and a DEBUG log was emitted.
    - Unit: `test_file_type_lowercased_MD` — ingest `foo.MD`, assert `file_type == "md"`.
    - Unit: `test_file_type_empty_for_no_extension` — ingest `Makefile`, assert `file_type == ""`.
  - `tests/test_routes_ingest_header.py`:
    - Unit: `test_x_ingested_by_default_http` — POST `/ingest` without header → chunks have `ingested_by == "http"`.
    - Unit: `test_x_ingested_by_legacy_normalized_to_cli` — POST with `X-Ingested-By: archon-search-cli` → chunks have `ingested_by == "cli"` (legacy normalized at the boundary; the stored row never contains the legacy string for fresh ingest).
    - Unit: `test_x_ingested_by_unknown_coerced_to_http` — POST with `X-Ingested-By: rogue-script` → chunks have `ingested_by == "http"` + WARNING log captured (use `caplog`).
    - Unit: `test_x_ingested_by_unknown_value_truncated_in_log` — POST with `X-Ingested-By: <a 200-character string>` → WARNING log line contains the value truncated to 32 chars (assert via `caplog.text`).
    - Unit (parametrized): `test_x_ingested_by_accepts_each_known_value` — for value in `INGESTED_BY_VALUES` (4 canonical), header is accepted as-is.
    - Unit: `test_future_mtime_is_accepted_as_is` — ingest a file whose mtime is set 1 hour in the future (`os.utime`); assert `updated_at` carries that future timestamp verbatim (no clamping).
  - `tests/test_watcher_ingested_by.py`:
    - Unit: `test_watcher_reingest_uses_watcher` — trigger watcher re-ingest via the watcher's pipeline call, assert chunks have `ingested_by == "watcher"`.
- **Checkpoint**: `uv run pytest tests/test_pipeline_metadata.py tests/test_routes_ingest_header.py tests/test_watcher_ingested_by.py -v`.

#### Task 3.4 — Verify `SearchStore.ingest_chunks` writes the new fields as-is
- [x] **File**: `archon_search/store.py` (small audit + test; code changes only if a coercion is found)
- **Depends on**: Task 3.3
- **Description**:
  - Audit `SearchStore.ingest_chunks`: confirm that `file_type`, `updated_at`, `ingested_by` flow from `ChunkRecord` into the LanceDB row dict without modification. The existing fallback `updated_at = updated_at or indexed_at` stays — that's the desired behavior for `stat()` failures.
  - **Remove** the existing write-path coercion `"ingested_by": c.ingested_by or "archon-search-cli"` in `store.py` (the `or "archon-search-cli"` fallback). With the new `IngestedBy` Literal, `ingested_by` is always one of the 4 canonical values — falsy / empty values are a programmer error, not legacy data, and must not be silently rewritten to a legacy string.
  - If any other coercion is found (e.g., dropping unknown `ingested_by` values silently), surface it and remove.
  - No new public API in this task; the value of the task is the integration test below pinning end-to-end behavior.
- **Releasable**: after this task, the new fields land in LanceDB on every fresh ingest path.
- **Tests (TDD)** — `tests/test_store_ingest_metadata.py`:
  - Integration (`@pytest.mark.integration`, real LanceDB temp dir): `test_ingest_writes_file_type_and_ingested_by` — call `ingest_chunks` with a `ChunkRecord` carrying `file_type="md"`, `ingested_by="cli"`, `updated_at="2026-05-21T10:00:00+00:00"`, then read via `_read_all_chunks`; assert all three fields match exactly.
  - Integration: `test_ingest_updated_at_fallback_to_indexed_at` — ingest with `updated_at=""`, assert read-back `updated_at == indexed_at` (existing fallback preserved).
  - Integration: `test_ingest_does_not_silently_rewrite_to_legacy` — construct a `ChunkRecord` and bypass the dataclass to force `ingested_by=""` (e.g., via `dataclasses.replace` or direct attribute assignment); call `ingest_chunks`; assert the read-back row has `ingested_by == ""` (empty, **not** `"archon-search-cli"`). Pins the removal of the `or "archon-search-cli"` write coercion.
  - Unit: `test_updated_at_is_utc_iso8601_with_offset` — ingest a file with a known mtime; assert the produced `updated_at` string ends in `+00:00` and parses cleanly via `datetime.fromisoformat()`.
- **Checkpoint**: `uv run pytest tests/test_store_ingest_metadata.py -m "integration or not integration" -v`.

---

### Phase 4 — Response surface: grow `SearchResult` + `SearchResultSchema`, wire `parse_metadata()`
> **Releasable**: when Task 4.3 lands; REST `/search` and MCP `search` both expose the new fields. This is the user-facing milestone of A1.

#### Task 4.1 — Grow `SearchResult` dataclass
- [x] **File**: `archon_search/_types.py`
- **Depends on**: Task 3.1
- **Description**:
  - Add to `SearchResult` (after `source_path`, before `acl`): `file_type: str = ""`, `indexed_at: str = ""`, `updated_at: str = ""`, `ingested_by: IngestedBy = "cli"`, `metadata: dict[str, str] = field(default_factory=dict)`. Keep existing `acl: list[str] | None = None` as the last field.
  - Do **not** add `language` or `custom_score` — they remain storage-only.
- **Releasable**: `SearchResult(...)` accepts and stores the new fields. Direct construction works; the mapping in `hybrid_search` is updated in Task 4.3.
- **Tests (TDD)** — `tests/test_search_result_shape.py`:
  - Unit: `test_search_result_field_set` — `dataclasses.fields(SearchResult)` names match exactly `{doc_id, chunk_id, text, score, source_path, file_type, indexed_at, updated_at, ingested_by, metadata, acl}`. This is the canonical field-set guard.
  - Unit: `test_search_result_does_not_have_language` — `"language"` not in field names (pins the "storage-only" decision).
  - Unit: `test_search_result_does_not_have_custom_score` — `"custom_score"` not in field names.
  - Unit: `test_search_result_does_not_have_vector` — `"vector"` not in field names (defense in depth against the `search_with_context` leak).
- **Checkpoint**: `uv run pytest tests/test_search_result_shape.py -v`.

#### Task 4.2 — Mirror new fields in `SearchResultSchema`, fix the `acl` drift, add field-parity snapshot
- [ ] **File**: `archon_search/server/routes_search.py`
- **Depends on**: Task 4.1
- **Description**:
  - Add to `SearchResultSchema` Pydantic model: `file_type: str = ""`, `indexed_at: str = ""`, `updated_at: str = ""`, `ingested_by: str = "cli"`, `metadata: dict[str, str] = Field(default_factory=dict)`, and **`acl: list[str] | None = None`** (closes the existing drift bug where `from_result()` silently dropped it).
  - Update `SearchResultSchema.from_result(cls, r: SearchResult)` to populate every new field plus `acl=r.acl`.
  - Delete the existing test `test_search_result_schema_no_acl_field` (it pins the bug being fixed). Replace with `test_search_result_schema_includes_acl` below.
- **Releasable**: REST `/search` response payloads now include the five new fields + `acl` for every result.
- **Tests (TDD)** — `tests/server/test_search_result_schema.py`:
  - Unit: `test_search_result_schema_contains_every_search_result_field` — assert `{f.name for f in dataclasses.fields(SearchResult)} <= set(SearchResultSchema.model_fields.keys())`. Loosened to **one direction** (subset, not equality): every `SearchResult` field must appear on the schema, but the schema may carry additional REST-only fields if future work adds them. This still catches the original drift bug (adding a field to `SearchResult` without mirroring on the schema fails the test) without forbidding REST-side extensions.
  - Unit: `test_search_result_schema_from_result_preserves_acl_none_and_empty_list` — parametrize `acl=None`, `acl=[]`, `acl=["team-a"]`; assert each round-trips through `SearchResultSchema.from_result(SearchResult(..., acl=acl))` unchanged. Replaces the deleted `test_search_result_schema_no_acl_field` and pins both directions: ACL is now populated AND its falsy variants are preserved (not collapsed to `None`).
  - Unit: `test_search_result_schema_includes_metadata` — `SearchResult(metadata={"k":"v"})` → schema `metadata == {"k":"v"}`.
  - Unit: `test_search_result_schema_serializes_new_fields_to_json` — `model_dump_json()` output contains every new key.
  - Unit: `test_search_result_schema_no_acl_field` is **deleted** (it pinned the bug being fixed). Replaced by `test_search_result_schema_from_result_preserves_acl_none_and_empty_list` above.
- **Checkpoint**: `uv run pytest tests/server/test_search_result_schema.py -v && uv run pytest tests/ -k "search_result_schema_no_acl" || echo "old test correctly removed"`.

#### Task 4.3 — Wire `parse_metadata()` into `SearchStore.hybrid_search`'s row-to-`SearchResult` mapping
- [ ] **File**: `archon_search/store.py` (the `SearchResult(...)` construction inside `hybrid_search`)
- **Depends on**: Task 4.1, Task 3.4
- **Description**:
  - At the `SearchResult(...)` construction site inside `hybrid_search`, populate the five new fields from the row dict: `file_type=r.get("file_type") or ""`, `indexed_at=r.get("indexed_at") or ""`, `updated_at=r.get("updated_at") or r.get("indexed_at") or ""` (preserves the fallback), `ingested_by=_normalize_ingested_by(r.get("ingested_by"))`, `metadata=parse_metadata(r.get("metadata") or "{}")`.
  - Define a small helper `_normalize_ingested_by(value: str | None) -> IngestedBy` in `store.py` (or `constants.py`): returns `"cli"` if value is `None`, empty, or equals `LEGACY_INGESTED_BY`; returns the value as-is if it is one of the 4 canonical members; defensively returns `"cli"` otherwise (with a DEBUG log noting the unexpected value). This guarantees the response surface never carries the legacy string — pre-A1 rows are transparently normalized to `"cli"`.
  - Apply the same `_normalize_ingested_by` call in `_read_all_chunks` for consistency between code paths.
  - Keep `acl=r.get("acl")` as today.
  - The `parse_metadata` helper already exists in `store.py`; `_read_all_chunks` already uses it. This task closes the gap for `hybrid_search`.
- **Releasable**: REST `/search` and MCP `search` both return populated metadata for fresh chunks; pre-A1 chunks return safe defaults with `ingested_by == "cli"`. Reindex (Task 6.2) is what actually rewrites the stored legacy values to `"reindex"`.
- **Tests (TDD)** — `tests/test_store_hybrid_search_metadata.py`:
  - Integration (`@pytest.mark.integration`, real LanceDB): `test_hybrid_search_returns_file_type` — ingest a chunk with `file_type="py"`, search, assert results carry `file_type == "py"`.
  - Integration: `test_hybrid_search_metadata_is_dict_not_string` — ingest a chunk with `metadata={"k": "v"}`, search, assert `isinstance(result.metadata, dict)` and `result.metadata == {"k": "v"}` (not the raw JSON string).
  - Integration: `test_hybrid_search_updated_at_falls_back_to_indexed_at` — ingest with `updated_at=""`, search, assert `result.updated_at == result.indexed_at`.
  - Integration: `test_hybrid_search_normalizes_legacy_ingested_by_to_cli` — write a row directly with `ingested_by="archon-search-cli"` (simulating pre-A1 data), search, assert returned `ingested_by == "cli"`. Pins the boundary-normalization design.
  - Contract: `tests/contract/test_search_response_shape.py::test_rest_search_response_includes_new_keys` — call the FastAPI `/search` endpoint via `TestClient`, assert response JSON contains the five new keys + `acl`. Snapshot the response shape (key set, not values).
  - Contract: `tests/contract/test_mcp_search_response_shape.py::test_mcp_search_tool_includes_new_keys` — invoke the **registered MCP tool via the FastMCP test client** (e.g., `async with Client(mcp_app) as client: result = await client.call_tool("search", {...})`) so the FastMCP serialization layer is exercised end-to-end — not the inner Python function. Mirror the pattern used by existing MCP tests in this repo (check `tests/server/` for the canonical FastMCP test-client invocation; if no precedent exists, document the chosen pattern at the top of the test file). Assert the returned payload contains the five new keys + `acl`.
- **Checkpoint**: `uv run pytest tests/test_store_hybrid_search_metadata.py tests/contract/ -v -m "integration or not integration"`.

---

### Phase 5 — MCP audit: strip `vector` from `search_with_context` context payloads
> **Releasable**: when Task 5.1 lands; MCP `search_with_context` no longer leaks raw embeddings; payload size shrinks by `~dim * 4` bytes per neighbor.

#### Task 5.1 — Strip `vector` from `context_before` / `context_after` chunks
- [ ] **File**: `archon_search/server/mcp.py`
- **Depends on**: Task 4.3
- **Description**:
  - Locate the `search_with_context` MCP tool handler. Where it returns `asdict(chunk)` (or equivalent) for each neighbor in `context_before` / `context_after`, replace with a helper `_chunk_to_context_dict(chunk: ChunkRecord) -> dict` that runs `asdict(chunk)` then pops `"vector"`.
  - Apply the same stripping to any other place in `mcp.py` that serializes a full `ChunkRecord` as a context neighbor (audit while there).
  - The matched primary result (the chunk that actually hit the query) continues to be a `SearchResult`, which has no `vector` field — no change needed there.
- **Releasable**: MCP `search_with_context` responses do not contain `vector` keys anywhere in `context_before` / `context_after`.
- **Tests (TDD)** — `tests/server/test_mcp_search_with_context.py`:
  - Unit: `test_search_with_context_strips_vector_from_neighbors` — build an input where the neighbor `ChunkRecord` has a non-empty `vector=[0.1, 0.2, 0.3, ...]` (must be non-empty so the test exercises the stripping path, per the brief). Call the MCP handler. Assert `"vector" not in neighbor_dict` for every neighbor in both `context_before` and `context_after`.
  - Unit: `test_search_with_context_preserves_other_chunk_fields_in_neighbors` — same scenario, assert `text`, `chunk_id`, `doc_id`, `source_path`, `file_type`, `updated_at`, `ingested_by`, `metadata` all survive on the neighbor dict.
  - (The originally-planned `test_search_with_context_payload_smaller_without_vector` is **deleted** — it duplicates the assertion in `test_search_with_context_strips_vector_from_neighbors` and used a magic byte-count bound. Direction-without-size adds no signal over the absence-of-key assertion.)
- **Checkpoint**: `uv run pytest tests/server/test_mcp_search_with_context.py -v`.

---

### Phase 6 — Lock + backfill: `asyncio.Lock`, `reindex-metadata` CLI, `--dry-run`
> **Releasable**: when Task 6.4 lands; operators can backfill pre-A1 collections without taking the service down.

#### Task 6.1 — Add per-collection `asyncio.Lock` map to `SearchStore`; wire ingest with acquire-timeout and 503
- [ ] **File**: `archon_search/store.py`, `archon_search/constants.py`, `archon_search/server/routes_collections.py` (or wherever REST ingest lives)
- **Depends on**: Task 3.4
- **Description**:
  - In `SearchStore.__init__`: `self._collection_locks: dict[str, asyncio.Lock] = {}` (mirrors the structure in `SearchCollectionSync._collection_locks`). Add a `_lock_for(collection)` helper that lazily creates the lock.
  - Add `INGEST_LOCK_TIMEOUT_S: Final[float] = 30.0` to `constants.py`. **No config key** — value is hardcoded for v1. (See "New config keys" in Architecture.)
  - In `ingest_chunks`: acquire the per-collection lock with **acquisition-only timeout**:
    ```python
    lock = self._lock_for(collection)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=INGEST_LOCK_TIMEOUT_S)
    except asyncio.TimeoutError as e:
        raise StoreBusyError(timeout_s=INGEST_LOCK_TIMEOUT_S) from e
    try:
        # ... actual write; NOT wrapped in asyncio.timeout — once we hold the lock,
        # we run to completion rather than risking cancellation mid-write
        ...
    finally:
        lock.release()
    ```
    The timeout applies **only** to acquisition. Wrapping the whole body in `async with asyncio.timeout(...)` was rejected because it can cancel mid-write, leaving Lance in an undefined state.
  - In `hybrid_search`: read paths do **not** acquire any lock (LanceDB handles read concurrency). Document this on the `_collection_locks` attribute's docstring. The lock map exists only for ingest/reindex serialization within a single collection.
  - REST ingest handler: catch `StoreBusyError`, return HTTP 503 with `Retry-After: str(math.ceil(e.timeout_s))` (RFC 7231 — Retry-After is integer seconds) and JSON body `{"error": "store_busy", "detail": "reindex in progress; retry after Retry-After seconds"}`.
  - Per-collection lock granularity: ingesting into collection A does **not** block ingest into collection B. Reindex of A blocks only A's ingest path. Independence from `SearchCollectionSync._collection_locks` is documented in "Known limitations / accepted trade-offs".
  - **Lock map cleanup on drop**: `SearchStore.drop_collection` (verified to exist in `store.py:174`) must `self._collection_locks.pop(name, None)` after the underlying drop completes, otherwise dropping and recreating collections leaks lock entries indefinitely. Do this inside `drop_collection` itself (no separate hook), guarded so a missing entry is a no-op.
- **Releasable**: ingest still works when uncontended; under contention with a reindex holder of the **same** collection, ingest awaits and times out cleanly to 503. Read path unaffected; other-collection ingests unaffected.
- **Tests (TDD)** — `tests/test_store_lock.py`:
  - Unit (deterministic): `test_two_ingests_serialize` — uses `asyncio.Event` rather than an in-flight counter:
    ```python
    started = asyncio.Event()
    holder_release = asyncio.Event()
    async def holder():
        lock = store._lock_for("col"); await lock.acquire()
        started.set()
        await holder_release.wait()
        lock.release()
    async def waiter():
        await started.wait()
        await asyncio.wait_for(store.ingest_chunks("col", ...), timeout=2.0)
    # Start both; holder releases after a controlled delay; assert waiter completes
    ```
    Asserts the second ingest blocks until the holder releases. No timing race, no in-flight counter.
  - Unit: `test_ingest_times_out_when_lock_held` — acquire the per-collection lock manually, monkey-patch `INGEST_LOCK_TIMEOUT_S` to `0.1` (or pass via test seam), kick off `ingest_chunks`, assert `StoreBusyError` raised within ~0.15s.
  - Unit: `test_ingest_other_collection_not_blocked` — hold collection A's lock; ingest into collection B succeeds without blocking. Pins per-collection granularity.
  - Unit: `test_ingest_succeeds_after_holder_releases` — short-lived holder; assert the waiting ingest completes once the holder releases.
  - Unit (route-level, `TestClient`): `test_rest_ingest_returns_503_on_store_busy` — patch `SearchStore.ingest_chunks` to raise `StoreBusyError(30.0)`, POST `/ingest`, assert HTTP 503, `Retry-After: 30` header, JSON body matches the contract.
  - Unit (parametrized): `test_rest_ingest_503_retry_after_ceils_timeout` — parametrize `(timeout_s, expected_header)`: `(12.7, "13")`, `(30.0, "30")`, `(0.5, "1")`, `(60.0, "60")`. Patch `ingest_chunks` to raise `StoreBusyError(timeout_s)`; assert `response.headers["Retry-After"] == expected_header`.
  - Unit: `test_hybrid_search_does_not_acquire_store_lock` — wrap each lock in `store._collection_locks` with a counting/observable proxy (e.g., subclass `asyncio.Lock` with an `acquire_count` attribute, install before calling `hybrid_search`), run `hybrid_search`, assert `acquire_count == 0` on every observed lock. **Do not** patch `asyncio.Lock.acquire` globally — that breaks unrelated locks (e.g., `sync._collection_locks`) and produces false negatives. Alternative acceptable form: assert `store._lock_for(coll).locked() is False` sampled at multiple points during search.
  - Unit: `test_drop_collection_removes_lock_entry` — call `store._lock_for("col")` to materialize the entry, assert `"col" in store._collection_locks`, call `await store.drop_collection("col")`, assert `"col" not in store._collection_locks`. Also assert dropping an already-dropped collection (no lock entry present) does not raise.
- **Checkpoint**: `uv run pytest tests/test_store_lock.py -v`.

#### Task 6.2 — Implement `SearchStore.reindex_metadata`
- [ ] **File**: `archon_search/store.py`
- **Depends on**: Task 6.1, Task 4.3
- **Description**:
  - New method: `async def reindex_metadata(self, collection: str, *, dry_run: bool = False, progress_cb: Callable[[int, int], None] | None = None) -> ReindexResult` where `ReindexResult = dataclass(processed: int, updated: int, skipped: int, warnings: list[str])`.
  - Behavior:
    1. Acquire the per-collection lock via `self._lock_for(collection).acquire()` (no timeout — reindex is the holder).
    2. Open the collection table; iterate rows. **Read path: reindex must read RAW LanceDB rows directly — NOT via `_normalize_ingested_by` or any helper that applies it.** If the existing `_read_all_chunks` is the normalized boundary (Task 4.3 wires the `_normalize_ingested_by` call into it), introduce a parallel `_read_all_chunks_raw` for reindex's use that returns rows with the stored `ingested_by` string verbatim. Otherwise reindex sees pre-normalized `"cli"` for legacy rows, detects no diff, and silently skips the legacy → `"reindex"` rewrite. If memory is a concern for huge collections, page in batches of 1000.
    3. For each row, compute:
       - `new_file_type = Path(row.source_path).suffix.lower().lstrip(".")` (empty if no extension; empty if `source_path` is itself empty).
       - `new_updated_at` = file mtime as ISO 8601 if the file still exists; else preserve existing `row.updated_at` (or `row.indexed_at` if empty), and append a `f"missing-source: {row.source_path}"` to warnings.
       - `new_ingested_by` = `"reindex"` if `row.ingested_by == LEGACY_INGESTED_BY`; else preserve the existing value (do not retroactively rewrite `cli`/`http`/`watcher`).
    4. If any field differs from the row's current value, mark for update. Increment `processed` for every row, `updated` only for rows that changed.
    5. If `dry_run`: do not write. Just log + return the result with `updated == 0` regardless.
    6. Else: write back via an **in-place update** path — e.g., LanceDB `table.update(where=f"chunk_id = '{chunk_id}'", values={...})` or `table.merge_insert(...)` keyed on `chunk_id`. **Do not** use `delete_document` + re-add: a delete-then-add cycle exposes a window in which `hybrid_search` (which holds no lock) sees the row missing entirely, producing transient empty results. If the existing LanceDB version in this codebase does not support in-place row updates for chunk rows, batch the update at the table level (one `merge_insert` per batch), still avoiding any window of row absence. The plan does not accept a delete-then-add fallback.
    7. Call `progress_cb(processed, total)` every K rows (K=200) and at end. If `progress_cb is None`, no-op.
    8. After all batches complete (non-dry-run), call `await self.rebuild_fts_index(collection)` (verified to exist in `store.py:445`). LanceDB `merge_insert` / `table.update` may invalidate or stale the FTS index for rewritten rows; rebuilding once at the end of reindex restores hybrid-search behavior. If a future LanceDB version is verified to keep FTS coherent across in-place updates (with a code-comment source reference), this step may be removed — until then, treat the rebuild as required.
  - **Benchmark TODO (non-blocking)**: capture wall-clock timings while developing/integration-testing reindex on a representative collection (target: at least one >10K-row collection). Note the result in the PR description or as a follow-up backlog item. This is observational — not a blocking benchmark gate. See "Known limitations / accepted trade-offs" for the rationale.
  - **Crash recovery / idempotency**: a partial reindex (process killed mid-loop) is safe to resume by re-running the command. Reindex only transitions `LEGACY_INGESTED_BY` → `"reindex"` (monotonic), and the other source-derived fields (`file_type` from `source_path`, `updated_at` from current mtime) are pure functions of the source. A re-run produces the same target state on rows that were already updated and converges the rest. The in-place update from step 6 guarantees no row ever enters a "deleted" intermediate state, so a crash cannot leave the collection short of rows.
  - Concurrency: ingest into the **same** collection is blocked by the per-collection lock for the full duration; ingest into other collections is unaffected. Reads continue across all collections (they don't take any lock).
- **Releasable**: `await store.reindex_metadata("my-col")` populates new metadata fields on every pre-A1 row in the collection; `dry_run=True` returns the counts without writing.
- **Tests (TDD)** — `tests/test_store_reindex_metadata.py`:
  - Integration (`@pytest.mark.integration`, real LanceDB): `test_reindex_empty_collection_is_noop` — empty collection → `processed == 0`, `updated == 0`.
  - Integration: `test_reindex_populates_file_type_and_ingested_by` — seed a collection with rows whose `file_type == ""` and `ingested_by == "archon-search-cli"`; after reindex, every row has `file_type` matching its `source_path` extension and `ingested_by == "reindex"`.
  - Integration: `test_reindex_preserves_non_legacy_ingested_by` — seed with `ingested_by == "http"`; after reindex, value is **still** `"http"` (no retroactive rewrite).
  - Integration: `test_reindex_idempotent_logical_equality` — run reindex twice; assert all metadata fields match field-by-field on every row across the two runs. Byte-level identity NOT asserted.
  - Integration: `test_reindex_missing_source_file` — seed with a row pointing at a `source_path` that doesn't exist; after reindex, the chunk is preserved (not deleted), `updated_at` is unchanged, `ingested_by` normalized to `"reindex"`, a warning containing the path is in `result.warnings`.
  - Integration: `test_reindex_dry_run_writes_nothing` — patch the underlying Lance write methods to fail loudly if called; reindex with `dry_run=True` succeeds without invoking them.
  - Integration: `test_reindex_blocks_concurrent_ingest_same_collection` — start a long-running reindex of collection A (inject a slow row-processor), kick off an `ingest_chunks` against A with a short timeout, assert it raises `StoreBusyError`.
  - Integration: `test_reindex_does_not_block_ingest_other_collection` — reindex of A in progress; ingest into B completes normally. Pins per-collection granularity.
  - Integration: `test_reindex_empty_source_path_does_not_crash` — seed a row with `source_path=""` (defensive — shouldn't occur in practice but the loop must be robust); reindex completes, the row's `file_type` ends up `""`, a warning is logged, `ingested_by` normalized to `"reindex"`.
  - Integration: `test_reindex_progress_cb_invoked` — seed N=500 rows, pass a callback that records every `(processed, total)` pair, assert at least 2 calls and the last one is `(500, 500)`.
  - Integration: `test_reindex_reads_raw_legacy_value_and_rewrites_it` — seed a row whose **stored** `ingested_by` is `"archon-search-cli"` (write directly via the underlying Lance table to bypass any normalization), run `reindex_metadata`, then read the row back via the **raw** read path (not the normalized one). Assert the stored value is now `"reindex"`. Pins the requirement that reindex reads raw rows; would fail if reindex routed through the normalized boundary and skipped the diff.
  - Integration: `test_reindex_fts_index_still_usable_after_run` — seed a collection with rows whose text contains a distinctive token, run `reindex_metadata` (which rewrites `ingested_by` on legacy rows), then run a hybrid search query for that token. Assert FTS hits still surface (result list non-empty and includes the seeded chunk). Pins the post-reindex FTS rebuild from step 8.
- **Checkpoint**: `uv run pytest tests/test_store_reindex_metadata.py -v -m "integration or not integration"`.

#### Task 6.3 — `archon-search collection reindex-metadata <name>` CLI subcommand
- [ ] **File**: `archon_search/cli/` (whichever module owns the `collection` group; likely `archon_search/cli/main.py` or `archon_search/cli/collection.py`)
- **Depends on**: Task 6.2
- **Description**:
  - Add Click subcommand: `archon-search collection reindex-metadata <name> [--dry-run]`.
  - Implementation: open the `SearchStore` against `~/.archon-search/`, call `await store.reindex_metadata(name, dry_run=dry_run, progress_cb=<stdout progress>)`. The progress callback writes one line per K rows: `"reindex-metadata: <name> – {processed}/{total}"` (use `click.echo` with `nl=True`).
  - On completion print summary: `"reindex-metadata: <name> – done. processed={processed}, updated={updated}, warnings={len(warnings)}"`. If warnings exist, dump them one per line under `"warnings:"`.
  - Exit code: 0 on success even with warnings (warnings are advisory). 1 if the collection doesn't exist.
- **Releasable**: operators can run `archon-search collection reindex-metadata my-col` and `archon-search collection reindex-metadata my-col --dry-run`.
- **Tests (TDD)** — `tests/cli/test_reindex_metadata_cli.py`:
  - Unit (`CliRunner`): `test_reindex_metadata_invokes_store` — patch `SearchStore.reindex_metadata` to return a known `ReindexResult`, invoke CLI, assert it was called with `dry_run=False` and the right collection name, exit code 0.
  - Unit: `test_reindex_metadata_dry_run_flag` — invoke with `--dry-run`, assert `reindex_metadata` called with `dry_run=True`.
  - Unit: `test_reindex_metadata_unknown_collection_exits_1` — patch store to raise `CollectionNotFoundError`, assert exit code 1 and a clear error message.
  - Unit: `test_reindex_metadata_prints_progress` — patch store to invoke `progress_cb(50, 100)` then `progress_cb(100, 100)`; capture stdout, assert both lines printed.
  - Unit: `test_reindex_metadata_prints_warnings` — patch store to return a `ReindexResult` with a non-empty `warnings` list; assert each warning appears in stdout.
- **Checkpoint**: `uv run pytest tests/cli/test_reindex_metadata_cli.py -v`.

#### Task 6.4 — Pre-A1 fixture collection integration test (end-to-end backfill)
- [ ] **File**: `tests/integration/test_reindex_backfill_e2e.py`
- **Depends on**: Task 6.3
- **Description**:
  - Build a pytest fixture that creates a LanceDB collection with chunks shaped like pre-A1 data: `file_type=""`, `updated_at=""`, `ingested_by="archon-search-cli"`, populated `source_path` pointing at real temp files. **Note**: this is a Python-constructed fixture (the pre-A1 code path no longer exists in the current branch), so it cannot reproduce every storage-layer quirk of historical writes (e.g., NULL vs empty-string nullability, exact LanceDB row-format details from older versions). Coverage for those quirks lives in Task 3.4's integration tests, which exercise the real write path.
  - Run the `reindex-metadata` CLI (via `CliRunner`) against this fixture.
  - Assert post-reindex: every chunk has `file_type` matching the temp file's extension, `updated_at` matching the file's mtime, `ingested_by == "reindex"`.
  - This test exists as a single end-to-end pin for the operator-facing migration path the brief explicitly calls out.
- **Releasable**: confidence that an operator running A1's CLI against pre-A1 data gets the expected result.
- **Tests (TDD)** — `tests/integration/test_reindex_backfill_e2e.py`:
  - Integration (`@pytest.mark.integration`): `test_pre_a1_collection_after_reindex` — the scenario above.
  - Integration: `test_pre_a1_collection_dry_run_changes_nothing` — same fixture, run with `--dry-run`, assert all rows still have legacy values.
- **Checkpoint**: `uv run pytest tests/integration/test_reindex_backfill_e2e.py -v -m integration`.

---

### Phase 7 — watcher-replace pin, eval-harness fixture sweep
> **Releasable**: when Task 7.2 lands; the invariant set is complete and the eval harness still passes.
>
> **Note**: the originally-planned `CollectionMeta.schema_version` task is **dropped from A1** (the brief explicitly defers it as an open question). A follow-up brief will resolve strictness and implementation. The ~94 `CollectionMeta(...)` call sites are unchanged in A1.

#### Task 7.1 — Watcher delete-then-re-ingest integration pin
- [ ] **File**: `tests/integration/test_watcher_replace.py`
- **Depends on**: Task 3.3, Task 4.3
- **Description**:
  - Integration test pinning the brief's edge case: watcher re-ingest on a changed file must delete old chunks and emit new ones with the new `updated_at` and `file_type`, with no stale duplicates.
  - Set up a watched directory with a file. Ingest it. Mutate the file (touch + change contents). **Invoke the watcher's event-handler method directly** (e.g., `await watcher._handle_event(...)` or the equivalent public seam) rather than relying on `watchdog` to propagate the filesystem change. This trades full e2e fidelity for determinism — the watchdog-driven path has flaky timing on macOS/Linux CI and is not what's being tested here (the brief's invariant is "the re-ingest produces correct fields", not "watchdog fires within X ms").
  - Assert:
    - Old chunks for the doc are gone (query by `doc_id`, expect only new chunks).
    - New chunks have `updated_at` greater than the original (or at least non-empty and matching the new mtime).
    - `ingested_by == "watcher"` on the new chunks.
- **Releasable**: confidence the brief's `replace=True` semantics are preserved end-to-end with the new fields.
- **Tests (TDD)** — `tests/integration/test_watcher_replace.py`:
  - Integration (`@pytest.mark.integration`): `test_watcher_replace_no_stale_duplicates` — the scenario above.
- **Checkpoint**: `uv run pytest tests/integration/test_watcher_replace.py -v -m integration`.

#### Task 7.2 — Eval-harness fixture sweep
- [ ] **File**: `tests/eval/test_metrics.py`, `tests/eval/test_types.py` (the two files referencing `SearchResult`, enumerated upfront via `grep -l SearchResult tests/eval/`). If a future eval file is added that constructs `SearchResult`, extend the sweep.
- **Depends on**: Task 4.3
- **Description**:
  - Audit the two enumerated files (and any new arrivals) for fixtures or assertions that construct a `SearchResult` directly or assert on `asdict(SearchResult(...))`. Update to populate the new fields (use safe defaults: `file_type=""`, `updated_at=""`, `indexed_at=""`, `ingested_by="cli"`, `metadata={}`).
  - Run the eval suite end-to-end (`uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py`). Assert thresholds unchanged.
  - **Do not** modify `tests/eval/thresholds.toml`. If thresholds regress, the change is wrong — investigate root cause; don't lower bars.
- **Releasable**: eval harness is green with the new shape.
- **Tests (TDD)** — N/A (this task fixes existing tests; success criterion is the eval suite running clean).
- **Checkpoint**: `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py`.

---

### Phase 8 — Docs, BREAKING.md, partition map, roadmap amendment, final verification
> **Releasable**: when Task 8.3 lands; A1 is shipped.
>
> Phase 8 is split into three tasks (originally one) so each has a single concern: code-side docstrings (8.1), repository docs (8.2), final audit + acceptance verification (8.3).

#### Task 8.1 — Partition-map docstrings on `ChunkRecord`; persistence-doc pointer
- [ ] **File**: `archon_search/_types.py`, `Documentation/Architecture/130_data_architecture_and_persistence.md`
- **Depends on**: Task 4.3
- **Description**:
  1. Add partition-map docstring blocks on every field of `ChunkRecord` in `archon_search/_types.py`. Format per field (one-line docstring above the field, or a single `"""..."""` block at the top of the dataclass enumerating each field's partition). The partitions are: **system** (`doc_id`, `chunk_id`, `text`, `vector`, `source_path`, `indexed_at`, `acl`), **filterable** (`file_type`, `language`, `updated_at`, `metadata`), **ranking** (`custom_score`), **audit** (`ingested_by`).
  2. Add a one-paragraph pointer to `Documentation/Architecture/130_data_architecture_and_persistence.md`: *"The per-field partition map (system / filterable / ranking / audit) for `ChunkRecord` lives in `archon_search/_types.py` as docstrings on the dataclass — see the source for the authoritative breakdown."* Do **not** duplicate the table here.
- **Releasable**: code is self-describing for the partition map; the persistence doc points at the code.
- **Tests (TDD)**: N/A — docstring/doc task.
- **Checkpoint**: visual inspection; grep `_types.py` for the partition labels.

#### Task 8.2 — `BREAKING.md` entry, roadmap amendment
- [ ] **File**: `BREAKING.md`, `Documentation/Backlog/03_world_class_roadmap.md`
- **Depends on**: Task 8.1
- **Description**:
  1. Add a `BREAKING.md` entry under a new section heading for the A1 release. Distinguish:
     - **MCP (truly breaking for strict-validating clients)**: `search`, `search_with_context`, `list_documents` response shapes gain keys `file_type`, `indexed_at`, `updated_at`, `ingested_by`, `metadata`, `acl`. `search_with_context` no longer includes `vector` in context chunks.
     - **REST (additive, non-breaking for tolerant JSON consumers)**: `/search` and `/search/context` response items gain the same keys.
     - Note A1 is the **last** untyped MCP shape break before C7 (Pydantic-wrapped MCP responses).
     - Note `POST /ingest` may return HTTP 503 with `Retry-After` while reindex is in progress (new contract).
     - Note the `X-Ingested-By` header accepts the legacy value `"archon-search-cli"` but normalizes it to `"cli"` at the boundary — clients that pass legacy and inspect the stored value will see `"cli"`.
  2. Amend `Documentation/Backlog/03_world_class_roadmap.md`: in A2's listed filter dimensions, **remove `language`** and add a forward reference to **C2** (real language detection). Add one line under A2: *"A1 ships filterable fields populated; A2 only adds query-side wiring."*
  - **Note**: the originally-planned tech-debt sunset entry for the `"archon-search-cli"` `Literal` member is **dropped** — under the revised design, legacy is **not** in the `Literal` (it is normalized at boundaries), so there is no Literal member to sunset.
- **Releasable**: the public contract change is documented; the roadmap matches reality.
- **Tests (TDD)**: N/A.
- **Checkpoint**: visual review.

#### Task 8.3 — Documentation tree audit + acceptance verification
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks (1.1 through 8.2)
- **Description**:
  1. Run an agent over the whole `Documentation/` tree to find any other doc that mentions `SearchResult`, `SearchResultSchema`, the chunk schema, MCP response shape, or the `ingest_by` legacy value, and update them. The agent must **not** edit docs that aren't affected.
  2. Verify every acceptance criterion below.
- **Releasable**: A1 is fully verified, documented, and the roadmap is reconciled.
- **Acceptance criteria** (must all pass):
  - [ ] A fresh `archon-search ingest <path-to-foo.md>` produces a chunk row where `file_type == "md"`, `updated_at` is a non-empty ISO 8601 string matching the file's mtime, and `ingested_by == "cli"`.
  - [ ] REST `POST /search` JSON response items include `file_type`, `indexed_at`, `updated_at`, `ingested_by`, `metadata`, and `acl` for every result.
  - [ ] MCP `search` tool response items include the same six keys.
  - [ ] MCP `search_with_context` response: `context_before` and `context_after` entries do **not** contain a `vector` key, but **do** contain `file_type`, `updated_at`, `ingested_by`, `metadata`.
  - [ ] `archon-search collection reindex-metadata <pre-A1-collection>` populates `file_type` from extension, refreshes `updated_at` from mtime, and rewrites `ingested_by` from `"archon-search-cli"` → `"reindex"`. `--dry-run` reports the same counts without writing.
  - [ ] During an active reindex of collection A, `POST /ingest` to collection A returns HTTP 503 with `Retry-After: 30` and JSON body `{"error": "store_busy", ...}`. Ingest to a **different** collection succeeds normally.
  - [ ] `custom_score = None` round-trips through ingest and read-back without coercion to `0.0`.
  - [ ] `SearchResult` dataclass fields are a subset of `SearchResultSchema.model_fields` keys (field-parity snapshot test green).
  - [ ] `SearchResult` does **not** contain `language`, `custom_score`, or `vector`.
  - [ ] `IngestedBy` literal has exactly 4 members (`"cli"`, `"http"`, `"watcher"`, `"reindex"`); legacy `"archon-search-cli"` is normalized at boundaries and never appears in `SearchResult` payloads for any input.
  - [ ] Watcher re-ingest of a changed file replaces old chunks (no stale duplicates) and new chunks carry the new `updated_at`, `file_type`, and `ingested_by == "watcher"`.
  - [ ] The eval harness (`uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py`) passes with **unchanged** thresholds.
  - [ ] The default pytest run (`uv run pytest`) passes with `--cov-fail-under=85` enforced.
  - [ ] `BREAKING.md` contains the A1 entry distinguishing MCP-breaking from REST-additive changes and documenting the new 503/`Retry-After` ingest contract.
  - [ ] `Documentation/Architecture/130_data_architecture_and_persistence.md` contains the one-paragraph pointer to `_types.ChunkRecord` for the partition map.
  - [ ] `Documentation/Backlog/03_world_class_roadmap.md` no longer lists `language` under A2's filter dimensions; C2 is forward-referenced.
  - [ ] No version is hardcoded anywhere; `hatch-vcs` continues to derive the version from git tags.
- **Tests (TDD)**: N/A — verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked, then run the full default suite: `uv run pytest`.

---

## Open Questions (recorded; not blocking)

1. **`CollectionMeta.schema_version`**: deferred from A1 entirely. A follow-up brief will resolve (a) whether to add the field, (b) defaulted-required vs strict-required strictness, and (c) the migration path for the ~94 existing call sites.
2. **`BREAKING.md` exhaustiveness**: the brief leans toward exhaustive (every MCP tool gaining keys). Task 8.2 implements exhaustive — enumerate `search`, `search_with_context`, `list_documents`. Tighten if reviewers prefer the short form.
3. **`ingested_by` sub-source for HTTP** (e.g., `http:<api-key-id>`): out of scope for A1. Revisit if audit needs grow (separate brief).

---

## Appendix A — Task 1.1 import-path audit (2026-05-21)

`grep -rn "from archon_search.types\|from archon_search._types" archon_search tests Documentation | wc -l` → **78** lines (77 source imports + 1 in this plan's checkpoint command itself).

**`archon_search.types`** — 3 import sites, all narrow to job/REST types:
- `archon_search/jobs/model.py` → `IngestJob, JobStatus`
- `tests/test_routes_jobs.py` → `IngestJob`
- `tests/test_types.py` → multi-name import (`JobStatus, IngestJob, ReindexJob, DeleteJob, Query, RouteResponse, Collection, CollectionDetail, Chunk`)

**`archon_search._types`** — ~74 import sites across `archon_search/` (store, reranker, chunker, pipeline, server/routes_search) and `tests/`, all referencing `ChunkRecord`, `SearchResult`, `DocumentInfo`, `CollectionInfo`, `IngestResult`.

**Canonical homes** (no relocation needed; A1 keeps these stable):
- `ChunkRecord`, `SearchResult`, `DocumentInfo`, `CollectionInfo`, `IngestResult` → `archon_search._types`
- `JobStatus`, `IngestJob`, `ReindexJob`, `DeleteJob`, `Query`, `RouteResponse`, `Collection`, `CollectionDetail`, `Chunk` → `archon_search.types`

**No name collision** between the two modules. **No `ChunkRecord` duplication.** The only overlap is `types.py:Chunk` having a `file_type: str` required positional arg vs `_types.py:ChunkRecord.file_type = ""` — different dataclasses with overlapping field names, not duplicates. `types.py:Chunk` appears unused outside `tests/test_types.py`; surfaced here per the plan's instruction, but **not removed in A1**. No code changes follow from this audit.
