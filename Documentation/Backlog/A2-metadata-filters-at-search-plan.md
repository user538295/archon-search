# A2 — Metadata Filters at Search (minimum slice)
**Purpose**: Push metadata filters (`file_type`, `source_path_prefix`, `source_path_glob`, `indexed_after`, `indexed_before`, `language`-reserved, `include_metadata`) into `/search` (REST) and `search` / `search_with_context` (MCP). Filters compile to a single safe-quoted SQL predicate string, are pushed into LanceDB via `.where(pred)` on both vector and FTS branches (async API's default prefiltering behavior), then `fnmatch.fnmatchcase` post-RRF applies the glob branch. A shared `SearchFilters` Pydantic model lives in `archon_search/filters.py` (kept out of core `_types.py` to avoid leaking Pydantic into every core consumer) and prevents REST↔MCP drift.
**Audience**: archon-search contributors implementing A2 and reviewers of the resulting PRs.
**Status**: Draft

> **Depends on A1**: `SearchResult` already carries `file_type, indexed_at, updated_at, ingested_by: IngestedBy, metadata`. A2 adds `language` + all filter logic. A2 must NOT re-declare A1's fields.

---

## Prerequisites
- **A1 hard prerequisite for Phase 5 only.** Task 5.1 (`reindex-metadata --normalize-timestamps`) extends A1's `reindex-metadata` CLI; A1 must have merged that command before A2 Phase 5 can start. Phases 1–4 and 6 do not depend on any A1 deliverable beyond the schema and storage that A1 has already shipped (`ChunkRecord` fields and the LanceDB schema columns).
- **LanceDB async `.where()` shape — verified prior to design.** `lancedb.query.AsyncStandardQuery.where()` takes `predicate: str | Expr` only; there is NO parameterized `?`-placeholder API and NO `prefilter` kwarg in the async path. Prefiltering is the default; the async opt-out is the separate `.postfilter()` method. The plan is built on this verified surface — do not attempt to pass `(predicate, values)` tuples or a `prefilter=True` kwarg.

---

## Background

A1 extended `ChunkRecord` and the LanceDB schema with `file_type`, `language`, `indexed_at`, `updated_at`, `ingested_by`, `metadata` (`archon_search/_types.py`, `archon_search/store.py`) AND widened `SearchResult` with `file_type, indexed_at, updated_at, ingested_by: IngestedBy, metadata` and populated those fields in `LanceStore.hybrid_search`. A2 adds only the `language: str | None = None` field to `SearchResult` (deferred from A1 because A1 stores `""`) and otherwise does NOT re-declare A1's fields. As collections grow past a few thousand chunks, top-k is diluted by irrelevant file types or stale documents and there is no escape hatch short of building a new collection.

This plan implements the brief at `Documentation/Backlog/metadata-filters-at-search-brief.md`. The brief resolves all major design questions; this plan is the implementation decomposition. The user settled three open points before planning, and a devil's-advocate cycle adjusted them further:

1. **A1 overlap (response widening)** — A1 ships the response widening for `file_type, indexed_at, updated_at, ingested_by: IngestedBy, metadata`. A2 adds only the `language` field to `SearchResult` and wires it end-to-end (extractor stays out-of-scope until C2; A2 plumbs it through the store, response, and MCP shape).
2. **`SearchFilters` location** — `archon_search/filters.py` (NEW). Keeping Pydantic out of `archon_search/_types.py` preserves the dataclass-only import graph that 40+ test files and every core module rely on. The pydantic dependency lives at the API/predicate seam, not in the core types module.
3. **Timestamp backfill UX** — extend A1's `reindex-metadata` CLI to also normalize legacy variable-precision ISO timestamps to the new fixed-width form. One operational primitive.
4. **`include_metadata` placement (accepted ergonomic trade-off)** — `include_metadata` is technically a response-shaping flag, not a filter predicate. The plan keeps it inside `SearchFilters` (matching the brief) rather than promoting it to a top-level `SearchRequest.include_metadata`. Rationale: (a) callers pass a single optional object instead of two correlated fields; (b) MCP tool signatures stay symmetric with REST; (c) when `filters` is omitted entirely, `include_metadata` correctly defaults to `False` without a separate code path. The contract test in Task 2.3 explicitly excludes `include_metadata` from the predicate-builder field check, so no SQL is emitted for it.

## Goal

A caller can `POST /search` (or invoke MCP `search` / `search_with_context`) with any combination of `file_type`, `source_path_prefix`, `source_path_glob`, `indexed_after`, `indexed_before`, `language` (reserved → 422), and `include_metadata`. Invalid filters are rejected at the API boundary with HTTP 422. Valid filters are compiled to a single safely-quoted SQL string (via centralized escape helpers; never f-string SQL inline) and pushed into LanceDB via `.where(pred)` on **both** vector and FTS branches (async `.where()` is prefilter-by-default; `.postfilter()` is NEVER called). Returned hits include the surfaced metadata fields, RRF + ACL + reranker continue to work in the expected order, the no-raw-query telemetry invariant is preserved, and `tests/eval/thresholds.toml` gains a `[search_filtered]` p95 ceiling that the benchmark suite enforces. After A2: filter correctness is the success measure, not UI polish.

---

## Scope

### In Scope
- Response widening (A2 delta only): add `language: str | None = None` to `SearchResult`; populate it in `hybrid_search` row projection; extend `SearchResultSchema` for REST and the MCP tool schemas to surface `language`. A1's fields (`file_type, indexed_at, updated_at, ingested_by: IngestedBy, metadata`) are already present and must not be re-declared.
- `archon_search/filters.py` (NEW): `SearchFilters` Pydantic model — shared by REST `SearchRequest` and MCP `search` / `search_with_context`.
- `archon_search/store_filters.py` (NEW): pure predicate builder returning a complete SQL predicate STRING (no parameterization API exists in LanceDB async). Centralized escape helpers `_sql_quote_str` (single-quotes + doubles internal `'`) and `escape_like` (escapes `%`, `_`, `\`) — every user value flows through them. SQL form is `LIKE '<escaped>%' ESCAPE '\\'`. Architectural rationale shifts from "parameterization prevents injection" to "centralized escape helpers + restrictive input validation prevent injection."
- Datetime normalization helper `normalize_iso_utc` in `archon_search/_types.py` (pure stdlib, no pydantic) — fixed-width `YYYY-MM-DDTHH:MM:SS.ffffffZ`. Used by store ingestion and by predicate builder before SQL emission.
- `LanceStore.hybrid_search(filters=...)`: `.where(pred)` on both vector and FTS branches (default prefiltering); `.postfilter()` is NEVER called.
- `_compute_fetch(top_k, *, has_glob: bool) -> int` helper consolidating the two over-fetch multipliers in one place: `max(top_k * GLOB_OVERFETCH_FACTOR, 60)` when `has_glob`, else `max(top_k * 3, 20)`. Replaces the inline `max(top_k * 3, 20)` literal in `hybrid_search`.
- `fnmatch.fnmatchcase` post-RRF glob filter with `GLOB_OVERFETCH_FACTOR = 5`; under-delivery emits one `WARNING` log line.
- `SearchPipeline.search(filters=...)`: filters apply before ACL, before reranker. Pipeline-level WARNING when combined filter+ACL attrition leaves the reranker with fewer than `top_k` candidates.
- REST `SearchRequest.filters: SearchFilters | None`; MCP `search` / `search_with_context` accept the same fields as typed kwargs that hydrate `SearchFilters`. MCP tools strip `metadata` from each result dict when `include_metadata=False` before `asdict()` serialization.
- `TelemetryEntry.filter_flags: FilterFlags` — typed Pydantic submodel with `extra="forbid"` and explicit boolean fields (no `dict[str, bool]` bag). No filter values logged.
- Extend A1's `reindex-metadata` CLI with `--normalize-timestamps / --no-normalize-timestamps` (default ON when A2 ships). Legacy-format detection regex catches anything not matching `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$` (including `+00:00` offsets, missing-tz, variable-precision).
- Documentation: `Architecture/600_api_reference_or_public_interface.md`, `Architecture/130_data_architecture_and_persistence.md`, `Architecture/520_api_design_and_contracts.md`, regenerated OpenAPI (with `language` description containing literal "reserved" + "C2"), `BREAKING.md` entry **scoped to MCP only** + operational note on datetime normalization.
- Tests: unit (default suite, coverage-gated), integration (real LanceDB), Hypothesis property test for `LIKE` escape with the LIKE simulator anchored to hand-verified cases, contract test for REST↔MCP filter parity, no-result 200 test, **12-cell** ACL × filter matrix (explicit owning task), benchmark-marker test with `[search_filtered]` thresholds anchored to an empirical baseline (not a cargo-culted 250ms).

### Out of Scope
- `language` filter actually matching (A1 stores `""`; full detection is C2). A2 reserves the parameter and rejects non-empty values with HTTP 422.
- A4 explain endpoint — separate task.
- Custom `metadata.<key>` filtering.
- OR / NOT / nested boolean predicates (A2 is implicit AND across filters).
- Cross-collection / router-aware filters (router still sends a filtered query to all selected collections).
- Scalar indexes on `file_type` / `indexed_at` — full-scan `.where()` accepted at current scale; benchmark guards latency.
- `include_metadata` as field-projection list (boolean only).
- CLI surface for filtered search.
- Case-insensitive glob matching (see Known limitations).

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 7.1 — Final verification & documentation update].

---

## What does NOT change
- LanceDB on-disk row format for existing chunks (legacy variable-precision and `+00:00`-suffixed timestamps preserved until `reindex-metadata --normalize-timestamps` runs).
- The RRF fusion logic (`store.py` `_rrf_score`) and the `top_k * 3` over-fetch factor when no glob is active (now reached via `_compute_fetch`).
- ACL filter ordering relative to reranker (`apply_acl_filter` runs before `_reranker.rerank` in `pipeline.py:302-303`).
- The `query`-less telemetry invariant (`TelemetryEntry` factory still rejects a `query` kwarg).
- `GET /health`'s no-auth contract; the `Bearer` requirement on every other endpoint.
- The `--cov-fail-under=85` gate on the default pytest run.
- Coverage rule: split / matrix CI runs MUST `coverage combine` before applying the threshold; **never** bake `--no-cov` into `addopts`.

---

## Known limitations / accepted trade-offs
- **Routed multi-collection search** does not benefit from filter-aware collection elimination — `file_type=md` is still sent to all router-selected collections, including those with zero `.md` chunks. Captured in Future Iterations.
- **No scalar indexes** on `file_type` / `indexed_at`. Full-scan `.where()` is acceptable at few-thousand-chunk scale; the `[search_filtered]` benchmark is the regression guard.
- **`fnmatch.fnmatchcase` has no path/directory semantics.** `*` matches `/` (so `*.md` matches `docs/api/foo.md`); `**` is identical to `*` — not because of directory-boundary crossing but because `fnmatch` has no path concept at all. Users wanting path-aware globbing combine `source_path_prefix` (anchors the parent) with `source_path_glob` (matches the suffix). Documented; not silently accepted.
- **`source_path_glob` is case-sensitive** (`fnmatch.fnmatchcase`). On case-insensitive filesystems (macOS default) users may expect `*.MD` to match `readme.md`; it does not. Documented; case-insensitive option deferred to Future Iterations.
- **Legacy mixed-format timestamps** (variable precision, `+00:00` offsets, missing tz) may produce silently incorrect boundary results until `reindex-metadata --normalize-timestamps` runs. Mitigated by an operational WARNING log naming the legacy-format row count when a date-range query hits a mixed collection.
- **`GLOB_OVERFETCH_FACTOR = 5`** is tuned for typical (low-ACL-attrition) deployments. Operators see a WARNING when the post-filter pool shrinks below `top_k`, and a second WARNING when filter+ACL combined attrition leaves the reranker short; tuning is operational, not hardcoded.
- **`include_metadata` defaults to `false`** — custom metadata can reach ~200KB per chunk; always-on is pathological at high `top_k`.
- **No parameterized SQL.** LanceDB async `.where()` accepts only a string predicate or an `Expr` tree. A2 chooses the string path via centralized escape helpers. Defense-in-depth comes from input validation (`SearchFilters` rejects empty strings and bounds `language`) plus the unconditional `_sql_quote_str` / `escape_like` calls. There is no SQL constructed via f-strings or `.format()` outside `store_filters.build_where`.
- **`AsyncFTSQuery.where()` reliance is verified by integration test.** If LanceDB's FTS query builder drops `.where()` support in a future version, the integration test in Task 3.1 fails loudly. Documented as a known risk; the plan does not fall back to post-filtering the FTS branch in Python.

### Coverage strategy
The default pytest run excludes `integration`, `benchmark`, `live`, `eval` markers — these tests do **not** contribute to the `--cov-fail-under=85` gate. To keep the coverage gate green every branch in new code (`hybrid_search` with filters, the glob post-filter loop, the under-delivery warning, the pipeline filter+ACL attrition warning, the legacy-format regex check, `_compute_fetch`, `build_where`) is exercised by at least one **unit** test that uses a thin in-memory fake or mock — not only by integration tests. Tasks 3.1, 3.2, 3.3, 6.1 all carry explicit unit tests alongside their integration tests for this reason.

---

## Architecture

### Modules touched / added
- `archon_search/_types.py` — **add** `language: str | None = None` to `SearchResult` (A1's `file_type, indexed_at, updated_at, ingested_by: IngestedBy, metadata` are already declared — do NOT re-add). **Add** `normalize_iso_utc(dt: datetime | str) -> str` helper (pure stdlib). **No Pydantic imports added here** — `_types.py` stays dataclass-only.
- `archon_search/filters.py` (**NEW**) — `SearchFilters(BaseModel)`. Pydantic lives at the API/predicate seam, not in core types.
- `archon_search/store_filters.py` (**NEW**) — pure functions:
  - `build_where(filters: SearchFilters) -> str` — emits a complete SQL predicate string (empty string when no filter is set). Implicit AND across set filters. Every user value flows through `_sql_quote_str` and (for LIKE patterns) `escape_like`. No f-string SQL inline.
  - `escape_like(s: str) -> str` — escapes `%`, `_`, `\` with a literal `\`.
  - `_sql_quote_str(s: str) -> str` — wraps in single quotes, doubles any embedded `'`.
  - `_compute_fetch(top_k: int, *, has_glob: bool) -> int` — single source of truth for the over-fetch multipliers.
  - `GLOB_OVERFETCH_FACTOR: int = 5` — module-level constant with a comment block explaining the attrition stack (glob × ACL).
- `archon_search/store.py` — **change** `LanceStore.hybrid_search` to accept `filters: SearchFilters | None = None`; call `build_where(filters)` and apply `.where(pred)` on both the vector and FTS search builders when `pred` is non-empty (async default = prefilter). Use `_compute_fetch(top_k, has_glob=...)` for the fetch size. After RRF, if `filters and filters.source_path_glob`, apply `fnmatch.fnmatchcase` and re-truncate; warn on under-delivery. If `filters and (filters.indexed_after or filters.indexed_before)`, run the legacy-format regex over fetched rows and emit the mixed-storage WARNING when non-fixed-width rows are present. Row projection populates the new `SearchResult` fields. `.postfilter()` is NEVER called on either builder.
- `archon_search/pipeline.py` — **change** `SearchPipeline.search(filters=...)` and `SearchPipeline.search_with_context(filters=...)` to forward filters into the store. ACL + reranker order unchanged. After ACL filtering, if any filter was set and the surviving pool is below `top_k`, emit a WARNING describing combined filter+ACL attrition.
- `archon_search/server/schemas.py` — **change** `SearchRequest` to embed `filters: SearchFilters | None = None`. `SearchResultSchema` grows to mirror `SearchResult`.
- `archon_search/server/routes_search.py` — **change** `POST /search` to pass `request.filters` into the pipeline; strip `metadata` from each result when `include_metadata` is false.
- `archon_search/server/mcp.py` — **change** `search` and `search_with_context` tools to accept the SearchFilters fields as kwargs (typed); hydrate `SearchFilters` and pass through. Strip `metadata` from each result dict when `include_metadata=False` BEFORE the `asdict()`-shaped dict is returned. The MCP tool's published input schema is the contract test target.
- `archon_search/telemetry/entry.py` — **add** typed `FilterFlags(BaseModel)` submodel with explicit boolean fields; `TelemetryEntry.filter_flags: FilterFlags = Field(default_factory=FilterFlags)`. `extra="forbid"` preserved on both models. No filter values logged.
- `archon_search/cli/collection.py` — **change** A1's `reindex-metadata` to also accept `--normalize-timestamps / --no-normalize-timestamps` (default ON when A2 ships). Reuses A1's lock and progress callback.
- Documentation: `Architecture/600`, `Architecture/130`, `Architecture/520`, regenerated `openapi.json`, `BREAKING.md`.

### Public signatures
```python
# archon_search/_types.py  (no Pydantic — stays pure stdlib + dataclasses)
def normalize_iso_utc(dt: datetime | str) -> str: ...
    # → "YYYY-MM-DDTHH:MM:SS.ffffffZ"

# SearchResult ALREADY carries the A1 fields:
#   file_type: str, indexed_at: str, updated_at: str,
#   ingested_by: IngestedBy, metadata: dict[str, str]
# A2 adds ONLY the `language` field below. Do NOT re-declare A1's fields.
@dataclass
class SearchResult:
    # ... existing fields from A1 ...
    language: str | None = None  # A2 addition (extractor in C2; A1/A2 store empty)

# archon_search/filters.py  (NEW — pydantic lives here, not in _types)
class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_type: str | None = None
    source_path_prefix: str | None = None
    source_path_glob: str | None = None
    indexed_after: datetime | None = None
    indexed_before: datetime | None = None
    language: str | None = None  # reserved; non-empty → 422
    include_metadata: bool = False

# archon_search/store_filters.py  (NEW)
def escape_like(s: str) -> str: ...
def _sql_quote_str(s: str) -> str: ...
def build_where(filters: SearchFilters) -> str: ...
def _compute_fetch(top_k: int, *, has_glob: bool) -> int: ...
GLOB_OVERFETCH_FACTOR: int = 5

# archon_search/store.py
async def hybrid_search(
    self,
    collection: str,
    query_vector: list[float],
    query_text: str,
    top_k: int,
    filters: SearchFilters | None = None,
) -> list[SearchResult]: ...

# archon_search/pipeline.py
async def search(
    self,
    query: str,
    collection: str,
    *,
    filters: SearchFilters | None = None,
) -> SearchPipelineResult: ...

# archon_search/telemetry/entry.py
class FilterFlags(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    file_type: bool = False
    source_path_prefix: bool = False
    source_path_glob: bool = False
    indexed_after: bool = False
    indexed_before: bool = False
    include_metadata: bool = False
    # `language` deliberately omitted: rejected at validation, never reaches telemetry.
```

### Pipeline order
1. Pydantic validation (`SearchFilters`).
2. `SearchPipeline.search` forwards filters to `LanceStore.hybrid_search`.
3. Store compiles `build_where(filters)` and applies `.where(pred)` on **both** vector + FTS branches (async default = prefilter). `.postfilter()` is NEVER called.
4. RRF fuses filtered candidates.
5. If `source_path_glob`, store applies `fnmatch.fnmatchcase`, re-truncates to `top_k`, warns on under-delivery. If a date filter is set, store runs the legacy-format regex and warns on mixed-storage rows.
6. ACL filter (`apply_acl_filter`); pipeline warns on combined filter+ACL attrition.
7. Reranker.
8. Response carries the surfaced metadata fields (`metadata` dict suppressed when `include_metadata` is false — REST strips at schema layer, MCP strips before `asdict()`).

### Backwards-Compatibility
- REST: new optional fields on request + response are **additive and non-breaking**. No `BREAKING.md` entry for REST.
- MCP: tool schemas change (new optional input params + new output fields). For strict MCP clients this is a schema change → `BREAKING.md` entry **scoped to MCP only**.
- Datetime normalization: writes change format; reads compatible with old format only when explicit reindex runs. Documented in `BREAKING.md` as an operational note.

---

## Task breakdown

### Phase 1 — Response widening (A2 delta: `language` only)
> **Internal milestone**: A1 already widened `SearchResult` with `file_type, indexed_at, updated_at, ingested_by: IngestedBy, metadata`. Phase 1 of A2 adds ONLY the `language` field and wires it through the store row projection, REST schema, and MCP tool schema. Tasks are grouped here for clean commit boundaries, not for staged release.

#### Task 1.1 — Add `language` to `SearchResult` dataclass
- [x] **File**: `archon_search/_types.py`
- **Depends on**: nothing (A1 has already shipped the other metadata fields)
- **Description**:
  - Add a single field to `SearchResult` (A1's fields stay as A1 declared them — do NOT re-declare):
    ```python
    language: str | None = None  # A2 addition (extractor lands in C2)
    ```
  - Defaults to `None` to preserve backwards-compatible construction in unrelated tests. No Pydantic import added to this file.
  - Do NOT touch `ingested_by` — A1 typed it as the `IngestedBy` Literal; A2 must not change the type or default.
- **Releasable**: after this task, the `SearchResult` dataclass carries `language` alongside A1's metadata fields.
- **Tests (TDD)** — `tests/test_types.py`:
  - Unit: `test_search_result_language_defaults_to_none` — constructing `SearchResult` without `language` works and produces `None`.
  - Unit: `test_search_result_language_carried_when_set` — explicit construction with `language="en"` round-trips.
  - Unit: `test_search_result_ingested_by_remains_ingested_by_literal` — A1's `ingested_by: IngestedBy` is untouched by A2 (regression guard against accidental retyping).
  - Checkpoint: `uv run pytest tests/test_types.py -x`

#### Task 1.2 — Populate `language` in `hybrid_search` row projection
- [x] **File**: `archon_search/store.py`
- **Depends on**: Task 1.1
- **Description**:
  - A1 already populates `file_type, indexed_at, updated_at, ingested_by, metadata` in the row projection — do NOT re-add that code.
  - In the same projection block, populate `language` from the raw row using `row.get("language") or None` so missing/legacy columns degrade gracefully to `None`.
- **Releasable**: after this task, `/search` responses carry `language` alongside A1's metadata fields.
- **Tests (TDD)** — `tests/test_store.py`:
  - Unit: `test_hybrid_search_row_projection_populates_language` — synthetic rows fed through a thin in-memory fake table; assert `language` on `SearchResult` mirrors the row.
  - Unit: `test_hybrid_search_row_projection_language_missing_yields_none` — row without the `language` column produces `SearchResult.language is None`, no exception.
  - Integration (`-m integration`): `test_hybrid_search_returns_language_field` — ingest a chunk with `language="en"`; assert it is present on the returned `SearchResult`.
  - Checkpoint: `uv run pytest tests/test_store.py -k hybrid_search -x`

#### Task 1.3 — Extend `SearchResultSchema` + MCP tool-schema export with `language`
- [x] **File**: `archon_search/server/schemas.py`, `archon_search/server/routes_search.py`, `archon_search/server/mcp.py`
- **Depends on**: Task 1.2
- **Description**:
  - A1 already extended `SearchResultSchema` with `file_type, indexed_at, updated_at, ingested_by, metadata`. A2 adds ONLY `language: str | None = None`. Update `from_result` to forward `language` from the dataclass.
  - REST: when `request.filters is None or not request.filters.include_metadata`, remove `metadata` from each result before returning. Implementation choice: strip after `from_result` produces the schema, or set `metadata=None` and serialize with `exclude_none=True`. Either is fine; choose one and stick with it.
  - MCP: in `mcp.py`, the tool body currently returns `{"results": [asdict(r) for r in result_obj.results], ...}`. After `asdict`, when the caller did NOT pass `include_metadata=True`, pop `"metadata"` from each result dict. The published MCP tool schema must surface the new return fields (FastMCP derives this from the return type; verify in the contract test in Task 4.2).
- **Releasable**: after this task, REST and MCP responses surface the metadata fields and respect `include_metadata`.
- **Tests (TDD)** — `tests/test_routes_search.py`, `tests/server/test_mcp_search.py`:
  - Unit: `test_search_response_includes_language_field` — mocked pipeline returns a `SearchResult` with `language="en"`; REST response JSON contains it. (A1 already covers the other metadata fields; this test only locks the A2 delta.)
  - Unit: `test_search_response_omits_custom_metadata_when_include_metadata_false`.
  - Unit: `test_search_response_includes_custom_metadata_when_include_metadata_true`.
  - Unit: `test_mcp_search_strips_metadata_when_include_metadata_false` — call the MCP tool body with `include_metadata` unset and a fake pipeline returning a non-empty `metadata` dict; assert the returned dict has no `"metadata"` key.
  - Contract: `test_mcp_search_tool_schema_advertises_metadata_fields` — inspect the MCP tool's published output schema and assert it lists the new fields.
  - Checkpoint: `uv run pytest tests/test_routes_search.py tests/server/test_mcp_search.py -x`

---

### Phase 2 — `SearchFilters` model + predicate builder
> **Releasable**: when Task 2.3 is complete, filters can be validated, normalized, and compiled to a safe-quoted SQL predicate string — but no caller uses them yet.

#### Task 2.1 — `SearchFilters` Pydantic model
- [x] **File**: `archon_search/filters.py` (NEW)
- **Depends on**: nothing
- **Description**:
  - Add the `SearchFilters` model with the signature in the Architecture section, in its own module `filters.py`. Do NOT add this to `_types.py` — keeping Pydantic out of the core types module preserves the dataclass-only import graph for `store.py`, `pipeline.py`, and all consumers that depend transitively on `_types.py`.
  - `model_config = ConfigDict(extra="forbid")`.
  - `@field_validator` rules:
    - `file_type`: strip leading dot, lowercase, reject `""` with `ValueError` (FastAPI → 422).
    - `source_path_prefix`: reject `""`. Otherwise free-form (escape applied at predicate-build time, not here).
    - `source_path_glob`: reject `""`. Defensive: `re.compile(fnmatch.translate(value))` to confirm compilability. Note: `fnmatch.translate` accepts any string, so this validator is effectively defense-in-depth, not a meaningful rejection path. Documented; no negative-input test required.
    - `language`: any non-empty value → `ValueError("language filtering not yet supported (see C2)")`.
  - `@model_validator(mode="after")`:
    - Treat naive `indexed_after` / `indexed_before` as UTC.
    - Coerce date-only inputs: `indexed_after=DATE` → start-of-day, `indexed_before=DATE` → end-of-day (inclusive bounds).
    - Enforce `indexed_after <= indexed_before` → `ValueError`.
- **Releasable**: after this task, callers can construct `SearchFilters` and rely on validation.
- **Tests (TDD)** — `tests/test_search_filters.py`:
  - Unit: `test_file_type_strip_leading_dot_and_lowercase` — `".MD"` → `"md"`.
  - Unit: `test_file_type_empty_string_rejected`.
  - Unit: `test_source_path_prefix_empty_rejected`.
  - Unit: `test_source_path_glob_empty_rejected`.
  - Unit: `test_indexed_after_naive_treated_as_utc`.
  - Unit: `test_indexed_after_date_only_coerced_to_start_of_day`.
  - Unit: `test_indexed_before_date_only_coerced_to_end_of_day`.
  - Unit: `test_indexed_after_greater_than_indexed_before_rejected`.
  - Unit: `test_language_non_empty_rejected_references_c2` — error message mentions C2.
  - Unit: `test_extra_field_rejected` — `extra="forbid"` proven.
  - Unit: `test_defaults_all_none_or_false`.
  - Checkpoint: `uv run pytest tests/test_search_filters.py -x`

#### Task 2.2 — Datetime normalization helper
- [x] **File**: `archon_search/_types.py`
- **Depends on**: nothing (independent from 2.1)
- **Description**:
  - `normalize_iso_utc(dt: datetime | str) -> str` returns `YYYY-MM-DDTHH:MM:SS.ffffffZ` (6-digit microseconds, always `Z`).
  - Accepts `datetime` (naive treated as UTC; aware converted to UTC) and ISO-8601 string (including `+00:00`, missing tz, and variable-precision forms).
  - Used both by the store ingestion path (going forward) and by the predicate builder to normalize `indexed_after` / `indexed_before` before SQL quoting.
  - Lives in `_types.py` (pure stdlib — no Pydantic dependency added).
- **Releasable**: after this task, all new timestamps share a fixed-width form safe for lexicographic comparison.
- **Tests (TDD)** — `tests/test_types.py`:
  - Unit: `test_normalize_iso_utc_naive_datetime` — naive treated as UTC, 6-digit microseconds.
  - Unit: `test_normalize_iso_utc_aware_datetime` — non-UTC tz converted to UTC.
  - Unit: `test_normalize_iso_utc_string_round_trip` — fixed-width input returns itself.
  - Unit: `test_normalize_iso_utc_variable_precision_string` — `"2026-05-21T10:00:00Z"` and `"2026-05-21T10:00:00.123456Z"` both produce fixed-width.
  - Unit: `test_normalize_iso_utc_plus_zero_offset_string` — `"2026-05-21T10:00:00+00:00"` produces the same fixed-width as `"...Z"`.
  - Unit: `test_lexicographic_order_preserved` — chronological pairs sort correctly as strings after normalization.
  - Checkpoint: `uv run pytest tests/test_types.py -k normalize_iso_utc -x`

#### Task 2.3 — Predicate builder, escape helpers, fetch helper
- [x] **File**: `archon_search/store_filters.py` (NEW)
- **Depends on**: Task 2.1, Task 2.2
- **Description**:
  - `_sql_quote_str(s: str) -> str` — returns `'<s with internal ' doubled>'`. The standard SQL string-literal escape. Every user-supplied string flows through this before reaching the SQL string. No f-string or `.format()` SQL anywhere outside this module.
  - **Note**: A5 (ingest hardening) will reuse this helper instead of introducing its own quoting in `store.py`. Keep `_sql_quote_str` import-safe (no `SearchFilters` dependency) so `store.py` writes can call it directly.
  - `escape_like(s: str) -> str` — escapes `%`, `_`, `\` with a literal `\`. SQL form uses `LIKE <quoted> ESCAPE '\\'`.
  - `build_where(filters: SearchFilters) -> str`:
    - Returns the empty string when no filter is set (caller skips `.where()`).
    - Implicit AND across set filters.
    - `file_type` → `file_type = <_sql_quote_str(file_type)>`.
    - `source_path_prefix` → `source_path LIKE <_sql_quote_str(escape_like(prefix) + '%')> ESCAPE '\\'`.
    - `indexed_after` → `indexed_at >= <_sql_quote_str(normalize_iso_utc(indexed_after))>`.
    - `indexed_before` → `indexed_at <= <_sql_quote_str(normalize_iso_utc(indexed_before))>`.
    - `language` is never reachable (rejected at validation); assert not set.
    - `source_path_glob` and `include_metadata` are NOT in the SQL.
  - `_compute_fetch(top_k: int, *, has_glob: bool) -> int` — single source of truth: `max(top_k * GLOB_OVERFETCH_FACTOR, 60)` when `has_glob`, else `max(top_k * 3, 20)`.
  - `GLOB_OVERFETCH_FACTOR: int = 5` exported at module level with a comment block explaining the attrition stack (glob × ACL).
- **Releasable**: after this task, the predicate compiler is callable and the over-fetch policy is centralized.
- **Tests (TDD)** — `tests/test_store_filters.py`:
  - Unit: `test_sql_quote_str_doubles_internal_single_quotes` — `O'Reilly` → `'O''Reilly'`.
  - Unit: `test_sql_quote_str_wraps_plain_string` — `"abc"` → `"'abc'"`.
  - Unit: `test_escape_like_percent_underscore_backslash` — hand-picked cases for each metacharacter.
  - Unit: `test_like_simulator_hand_verified_cases` — explicit assertions over a small Python LIKE simulator: `(pattern="a%b", input="acb", expected=True)`, `(pattern="a\\%b", input="a%b", expected=True)`, `(pattern="a_b", input="acb", expected=True)`, `(pattern="a\\_b", input="a_b", expected=True)`, `(pattern="a\\\\b", input="a\\b", expected=True)`. The simulator is the oracle for the Hypothesis test below; these cases prove the oracle is itself correct before the property test runs.
  - Property (Hypothesis): `test_escape_like_round_trip` — `forall s, escape_like(s)` produces a LIKE pattern that matches only `s` under the verified simulator's semantics (escape char `\\`).
  - Unit: `test_build_where_empty_filters_returns_empty_string`.
  - Unit: `test_build_where_file_type_only` — predicate is `file_type = 'md'`; uses `_sql_quote_str`.
  - Unit: `test_build_where_source_path_prefix_uses_escape_clause` — predicate contains `LIKE '<escaped>%' ESCAPE '\\'`.
  - Unit: `test_build_where_source_path_prefix_with_special_chars` — prefix containing `%`, `_`, `\`, `'` is correctly quoted and escaped; final SQL is syntactically valid (parseable by sqlglot or equivalent if available; otherwise a regex sanity check).
  - Unit: `test_build_where_indexed_after_normalized_to_fixed_width`.
  - Unit: `test_build_where_combined_filters_anded` — every set filter appears as a separate `AND` clause.
  - Unit: `test_build_where_glob_not_emitted_as_sql`.
  - Unit: `test_build_where_handles_every_search_filters_field` — introspect `SearchFilters.model_fields`; remove `include_metadata` (response-only), `source_path_glob` (post-RRF), `language` (rejected); assert every remaining field appears in the output of `build_where` when set. Locks the model↔builder contract.
  - Unit: `test_compute_fetch_branches` — `has_glob=False` returns `max(top_k * 3, 20)`; `has_glob=True` returns `max(top_k * 5, 60)`.
  - Checkpoint: `uv run pytest tests/test_store_filters.py -x`

---

### Phase 3 — Store + pipeline wiring
> **Releasable**: when Task 3.4 is complete, calling `LanceStore.hybrid_search(filters=...)` or `SearchPipeline.search(filters=...)` returns correctly filtered results end-to-end. REST/MCP surface still not wired.

#### Task 3.1 — `hybrid_search` accepts filters; `.where()` on both branches; `.postfilter()` never called
- [x] **File**: `archon_search/store.py`
- **Depends on**: Task 1.2, Task 2.3
- **Description**:
  - Add `filters: SearchFilters | None = None` to `LanceStore.hybrid_search`.
  - Compute `pred = build_where(filters)` and `fetch = _compute_fetch(top_k, has_glob=bool(filters and filters.source_path_glob))`. Replace the inline `max(top_k * 3, 20)` literal.
  - When `pred` is non-empty, apply `.where(pred)` to **both** the vector search builder and the FTS search builder. The async `.where()` API takes a string only (no `prefilter` kwarg). Prefiltering is the default in the async API. `.postfilter()` is NEVER called.
  - The plan does NOT depend on any `?`-placeholder parameterization API. Centralized escape helpers + restrictive input validation are the injection-prevention story.
- **Releasable**: after this task, `hybrid_search` filters at the LanceDB layer on both legs.
- **Tests (TDD)** — `tests/test_store.py`:
  - Unit: `test_hybrid_search_filter_calls_where_on_both_branches` — fake table builder records `.where()` calls; assert exactly one `.where(pred)` call on the vector builder and one on the FTS builder, both with the same string.
  - Unit: `test_hybrid_search_no_filters_no_where_called` — `filters=None` → `.where()` never called.
  - Unit: `test_hybrid_search_never_calls_postfilter` — structural assertion across all branches (no filter / filter / glob / FTS-failure-with-filter / FTS-failure-no-filter) that `.postfilter()` is NEVER called on either builder.
  - Unit: `test_hybrid_search_fetch_uses_compute_fetch_helper` — `_compute_fetch` is called (patch and assert call arguments).
  - Unit: `test_hybrid_search_fts_failure_with_filter_falls_back_to_vector_only` — FTS raises an "index not available" error; vector branch still receives the `.where()` call; warning is logged.
  - Integration (`-m integration`): `test_hybrid_search_file_type_filter` — ingest `.md` and `.py` chunks; `file_type="md"` returns only `.md` rows.
  - Integration: `test_hybrid_search_source_path_prefix_filter` — including a chunk with `source_path="/docs/100%_complete/README.md"` and `source_path_prefix="/docs/100%"` — proves the `LIKE ... ESCAPE '\\'` form survives the round-trip against real LanceDB.
  - Integration: `test_hybrid_search_indexed_after_filter` — fixed-width timestamps; assert boundary inclusivity.
  - Integration: `test_hybrid_search_prefilter_returns_full_top_k_from_matching_subset` — filter that matches a small fraction still returns up to `top_k` from the matching subset (behavioral check that prefiltering is in effect; the structural check is `test_hybrid_search_never_calls_postfilter` above).
  - Integration: `test_hybrid_search_filter_applies_to_fts_branch_via_where` — chains `.where(pred).limit(fetch).to_list()` on the FTS query builder against a real LanceDB collection and asserts FTS-only hits are excluded by the filter. If LanceDB's `AsyncFTSQuery` does not support `.where()`, this test fails loudly; documented in Known limitations.
  - Checkpoint: `uv run pytest tests/test_store.py -k filter -x`

#### Task 3.2 — `fnmatch` glob post-RRF + over-fetch policy + warn log + mixed-storage WARNING
- [x] **File**: `archon_search/store.py`
- **Depends on**: Task 3.1
- **Description**:
  - After RRF fusion, if `filters and filters.source_path_glob`, apply `fnmatch.fnmatchcase(row["source_path"], filters.source_path_glob)` and keep only matches.
  - Re-truncate the result list to `top_k`.
  - If the post-filter pool has fewer than `top_k` items, emit one `WARNING` log line: `"glob post-filter shrank pool below top_k: <N>/<top_k>"`.
  - If `filters and (filters.indexed_after or filters.indexed_before)`, scan the returned rows' `indexed_at` values with the strict regex `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$`. If any row fails the regex, emit one WARNING: `"date filter applied to <N> legacy-format rows in collection <C>; run reindex-metadata --normalize-timestamps to silence this and avoid silent boundary errors"`. This catches `+00:00`, variable-precision, missing-tz, etc.
  - Document on the function (one-line comment): `fnmatch` has no path semantics — `*` matches `/` and `**` is identical to `*`. Combine `source_path_prefix` (parent) with `source_path_glob` (suffix) for path-aware globbing.
- **Releasable**: after this task, glob filters work end-to-end at the store layer; mixed-storage corpora emit an operational signal on the first date-filtered query.
- **Tests (TDD)** — `tests/test_store.py`:
  - Unit: `test_glob_post_filter_keeps_matching_rows` — feed fake RRF output into the post-filter loop; assert only matching rows remain.
  - Unit: `test_glob_under_delivery_warns` — fake RRF output shrinks below `top_k`; `caplog` asserts the WARNING line.
  - Unit: `test_star_matches_across_slashes` — `*.md` against `docs/api/foo.md` matches; documents the actual `fnmatch` semantics (no path awareness).
  - Unit: `test_double_star_equivalent_to_single_star` — `**` and `*` produce identical match sets; documents the redundancy.
  - Unit: `test_mixed_format_indexed_at_triggers_warning` — fake rows with mixed `indexed_at` formats (`Z`-no-microseconds, `+00:00`, fixed-width); date filter set; `caplog` asserts the legacy-format WARNING with a row count matching the non-fixed-width rows.
  - Unit: `test_normalized_indexed_at_does_not_trigger_warning` — all rows fixed-width; no warning emitted.
  - Integration (`-m integration`): `test_hybrid_search_source_path_glob_matches`.
  - Integration: `test_hybrid_search_source_path_glob_character_class` — `"docs/[ab]/*"`.
  - Integration: `test_hybrid_search_glob_overfetch_replaces_default_multiplier` — assert via spy that `_compute_fetch` is called with `has_glob=True` when glob is set.
  - Checkpoint: `uv run pytest tests/test_store.py -k "glob or mixed_format" -x`

#### Task 3.3 — `SearchPipeline.search` / `search_with_context` forward filters + attrition WARNING
- [x] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 3.2
- **Description**:
  - Add `filters: SearchFilters | None = None` to `SearchPipeline.search` and `SearchPipeline.search_with_context` (keyword-only).
  - Forward to `store.hybrid_search(..., filters=filters)`.
  - Preserve existing order: filters apply at the store layer (pre-RRF prefilter + post-RRF glob) → `apply_acl_filter` → reranker.
  - After `apply_acl_filter`, if `filters is not None` and any field on `filters` was set (excluding `include_metadata`) AND `len(candidates_after_acl) < top_k_return`, emit a single WARNING: `"filter+ACL combined attrition: only <N>/<top_k> candidates reached reranker (filter_flags=<flags>, acl_denied=<count>)"`. This is the operator's signal that `GLOB_OVERFETCH_FACTOR` is too low for an ACL-heavy deployment.
- **Releasable**: after this task, programmatic callers of `SearchPipeline` can filter end-to-end and observe combined attrition.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Unit: `test_pipeline_search_forwards_filters_to_store` — stub store; assert filters reach `hybrid_search` kwargs.
  - Unit: `test_pipeline_warns_on_filter_plus_acl_under_delivery` — stub store returns `top_k` candidates, stub ACL denies enough to drop below `top_k`; `caplog` asserts the WARNING; filter_flags subset is correct in the message.
  - Unit: `test_pipeline_no_warning_when_no_filter_set` — same under-delivery scenario with `filters=None`; no WARNING emitted (this is just ACL doing its job).
  - Unit: `test_pipeline_no_warning_when_pool_above_top_k` — sufficient survivors; no WARNING.
  - Integration (`-m integration`): `test_pipeline_search_filter_then_acl_order` — a row excluded by filter is never seen by `apply_acl_filter` (use a spy on `apply_acl_filter` to count inputs).
  - Integration: `test_pipeline_search_filter_then_reranker_order` — reranker sees only filter+ACL survivors.
  - Checkpoint: `uv run pytest tests/test_pipeline.py -k filter -x`

#### Task 3.4 — 12-cell ACL × filter integration matrix
- [x] **File**: `tests/test_pipeline_acl_filter_matrix.py` (new)
- **Depends on**: Task 3.3
- **Description**:
  - Enumerate all 12 cells: `{no_filter, source_path_prefix, source_path_glob, date_range} × {no_acl, acl_match, acl_deny}`. Each cell is its own `@pytest.mark.integration` test function.
  - Each test seeds a real LanceDB collection with a fixed corpus, applies the filter (or none) plus the ACL configuration (or none), then asserts:
    - The result set contains exactly the expected `chunk_id`s for that cell.
    - `SearchPipelineResult.acl_filtered` flag matches expectations (True iff ACL denied at least one row).
    - For the `acl_deny + glob` and `acl_deny + date_range` cells specifically, also assert the combined-attrition WARNING from Task 3.3 fires if attrition drops below `top_k`.
- **Releasable**: after this task, the high-risk filter×ACL ordering bugs are locked behind a regression test.
- **Tests (TDD)** — `tests/test_pipeline_acl_filter_matrix.py`:
  - Integration (`-m integration`): `test_no_filter_no_acl`, `test_no_filter_acl_match`, `test_no_filter_acl_deny`.
  - Integration: `test_prefix_no_acl`, `test_prefix_acl_match`, `test_prefix_acl_deny`.
  - Integration: `test_glob_no_acl`, `test_glob_acl_match`, `test_glob_acl_deny`.
  - Integration: `test_date_range_no_acl`, `test_date_range_acl_match`, `test_date_range_acl_deny`.
  - Checkpoint: `uv run pytest -m integration tests/test_pipeline_acl_filter_matrix.py -x`

---

### Phase 4 — API surface (REST + MCP + telemetry)
> **Releasable**: after Task 4.3, callers can issue filtered queries over REST and MCP. Telemetry captures filter usage without leaking values.

#### Task 4.1 — REST `SearchRequest` embeds `SearchFilters`
- [x] **File**: `archon_search/server/schemas.py`, `archon_search/server/routes_search.py`
- **Depends on**: Task 3.3, Task 2.1
- **Description**:
  - Add `filters: SearchFilters | None = None` to `SearchRequest` (imported from `archon_search.filters`).
  - `POST /search` passes `request.filters` to `pipeline.search`.
  - Pydantic validation errors at the request boundary surface as HTTP 422 with the validator messages.
  - Suppress `metadata` from each `SearchResultSchema` in the response when `request.filters is None or not request.filters.include_metadata`.
- **Releasable**: after this task, REST callers can filter and the no-result-200 contract holds.
- **Tests (TDD)** — `tests/test_routes_search.py`:
  - Unit: `test_post_search_with_file_type_filter_returns_filtered_results` — mocked pipeline; assert filters reach it.
  - Unit: `test_post_search_invalid_filter_returns_422_with_validator_message` — covers each rejection path (empty `file_type`, `indexed_after > indexed_before`, non-empty `language`).
  - Unit: `test_post_search_no_filter_unchanged_behavior` — backwards-compat smoke.
  - Unit: `test_post_search_unknown_collection_returns_404_not_422` — distinguishes 404 vs validation error.
  - Unit: `test_openapi_schema_language_description_says_reserved_c2` — load the FastAPI-generated OpenAPI schema; assert the `language` field's `description` contains both `"reserved"` and `"C2"`.
  - Integration (`-m integration`): `test_search_filter_excludes_everything_returns_200_empty` — real `/search` against a real LanceDB collection where filters exclude all rows; assert `status==200`, `results==[]`.
  - Integration: `test_include_metadata_false_suppresses_metadata_end_to_end` — real `/search` over HTTP; assert `metadata` key absent in the response JSON when the flag is not set; present when set.
  - Checkpoint: `uv run pytest tests/test_routes_search.py -k filter -x`

#### Task 4.2 — MCP `search` / `search_with_context` accept filter kwargs + parity contract + metadata suppression
- [x] **File**: `archon_search/server/mcp.py`
- **Depends on**: Task 4.1
- **Description**:
  - Extend both MCP tool signatures with typed kwargs covering every `SearchFilters` field. Hydrate `SearchFilters(...)` inside the tool body (Pydantic validation raises → returned as a structured tool error).
  - Pass through to `pipeline.search(..., filters=...)`.
  - Honor `include_metadata`: when the caller did NOT pass `include_metadata=True`, pop the `"metadata"` key from each result dict BEFORE the dict is returned. (The current code path serializes via `asdict()`-shaped output; strip after that.)
- **Releasable**: after this task, MCP clients can filter; REST and MCP share the same field set.
- **Tests (TDD)** — `tests/server/test_mcp_search.py`:
  - Unit: `test_mcp_search_forwards_filters_to_pipeline` — mock pipeline; assert kwargs hydrate a `SearchFilters` with the expected fields.
  - Unit: `test_mcp_search_invalid_filter_surfaces_validator_error`.
  - Unit: `test_mcp_search_suppresses_metadata_when_include_metadata_false` — fake pipeline returns a non-empty `metadata` dict; MCP returns results without the `metadata` key.
  - Unit: `test_mcp_search_includes_metadata_when_include_metadata_true`.
  - **Contract**: `test_mcp_search_tool_input_schema_is_superset_of_search_filters` — inspect the FastMCP-published tool input schema and assert every field name on `SearchFilters` appears in the tool's input schema. Same assertion for `search_with_context`. The only guarantee against silent REST↔MCP drift.
  - Checkpoint: `uv run pytest tests/server/test_mcp_search.py -k filter -x`

#### Task 4.3 — Telemetry `FilterFlags` (typed submodel)
- [x] **File**: `archon_search/telemetry/entry.py`, callers in `routes_search.py` / `mcp.py`
- **Depends on**: Task 4.2
- **Description**:
  - Add a typed `FilterFlags(BaseModel)` submodel with `model_config = ConfigDict(extra="forbid", frozen=True)` and explicit boolean fields (`file_type`, `source_path_prefix`, `source_path_glob`, `indexed_after`, `indexed_before`, `include_metadata`). `language` is deliberately omitted — rejected at validation, never reaches telemetry.
  - Add `filter_flags: FilterFlags = Field(default_factory=FilterFlags)` to `TelemetryEntry`. `extra="forbid"` preserved on both models.
  - Callers compute a `FilterFlags(...)` from a `SearchFilters` instance (every field is `filters.field is not None` except `include_metadata` which is the boolean value) and pass it into the entry factory.
  - **No raw filter values logged.** `source_path_prefix` may reveal filesystem layout; never written to telemetry.
  - The factory continues to reject a `query` kwarg.
- **Releasable**: after this task, operators can answer "what fraction of queries used each filter?" without leaking values.
- **Tests (TDD)** — `tests/telemetry/test_entry.py`:
  - Unit: `test_filter_flags_default_all_false` — `FilterFlags()` has every field `False`.
  - Unit: `test_filter_flags_rejects_unknown_field` — `FilterFlags(unknown=True)` raises (`extra="forbid"` on the typed submodel — this is enforceable BECAUSE the submodel is typed, unlike a `dict[str, bool]`).
  - Unit: `test_filter_flags_rejects_non_bool_value` — `FilterFlags(file_type="yes")` raises Pydantic ValidationError (the field is typed `bool`).
  - Unit: `test_telemetry_entry_filter_flags_default_factory` — a fresh `TelemetryEntry` has a default `FilterFlags()`.
  - Unit: `test_telemetry_entry_rejects_query_kwarg` — the existing no-raw-query invariant survives.
  - Unit: `test_telemetry_entry_rejects_raw_filter_values_as_kwargs` — passing `source_path_prefix="/secret"` as a `TelemetryEntry` kwarg raises (`extra="forbid"`).
  - Integration (`-m integration`): `test_search_writes_filter_flags_to_jsonl` — REST `/search` with a filter; assert the written JSONL line has `filter_flags` populated with the expected booleans and contains no values from the original filter.
  - Checkpoint: `uv run pytest tests/telemetry/test_entry.py -x`

---

### Phase 5 — Backfill: extend `reindex-metadata` for timestamps
> **Hard dependency on A1.** Task 5.1 requires A1's `reindex-metadata` CLI to be merged. If A1 ships after Phases 1–4 of A2, Phase 5 waits.
> **Releasable**: when Task 5.1 is complete, operators have a single CLI primitive to migrate legacy timestamps to the fixed-width form.

#### Task 5.1 — `reindex-metadata --normalize-timestamps`
- [x] **File**: `archon_search/cli/collection.py` (and the underlying `SearchStore.reindex_metadata` from A1)
- **Depends on**: Task 2.2 (normalization helper); **A1 must have shipped `reindex-metadata` CLI and `SearchStore.reindex_metadata`**.
- **Description**:
  - Add `--normalize-timestamps / --no-normalize-timestamps` flag. Default ON when A2 ships.
  - When enabled, the existing reindex pass also rewrites `indexed_at` and `updated_at` via `normalize_iso_utc(...)` for any row whose stored value does NOT match the strict fixed-width regex `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$`. The strict regex catches variable-precision, `+00:00` offsets, missing-tz, and any other non-fixed-width form.
  - Reuses A1's lock and progress callback. `--dry-run` reports the number of rows that would be normalized without writing.
- **Releasable**: after this task, datetime filters work uniformly across legacy + new corpora once operators run the backfill.
- **Tests (TDD)** — `tests/test_cli_collection.py`:
  - Unit: `test_legacy_format_regex_rejects_known_legacy_shapes` — `"2026-05-21T10:00:00Z"`, `"2026-05-21T10:00:00+00:00"`, `"2026-05-21T10:00:00.123Z"`, `"2026-05-21T10:00:00"` all detected as non-fixed-width; the canonical fixed-width form is accepted.
  - Unit: `test_reindex_metadata_normalize_timestamps_dry_run_reports_count` — fake store; `--dry-run` reports the right count, writes nothing.
  - Integration (`-m integration`): `test_reindex_metadata_normalize_timestamps_rewrites_legacy_rows` — seed a collection with multiple legacy formats; run the command; assert all rows are fixed-width afterwards.
  - Integration: `test_reindex_metadata_normalize_timestamps_idempotent` — running twice is a no-op on the second run.
  - Integration: `test_reindex_metadata_normalize_timestamps_progress_logged` — progress callback fires.
  - Checkpoint: `uv run pytest tests/test_cli_collection.py -k normalize_timestamps -x`

---

### Phase 6 — Search-path backfill verification + benchmark thresholds + eval baseline
> **Releasable**: when Task 6.3 is complete, the deploy/backfill window has a search-path regression test, the filtered-search latency ceiling is enforced, and the eval baseline-unchanged invariant is automated.

#### Task 6.1 — Search-path backfill regression test (fail-then-pass)
- [x] **File**: `tests/test_search_backfill_regression.py`
- **Depends on**: Task 5.1, Task 3.2
- **Description**:
  - Integration test that demonstrates the SEARCH path (not just the CLI) transitions from wrong to correct after backfill:
    1. Seed a collection with mixed-format `indexed_at` rows, including a row whose raw-string-compared value sorts INCORRECTLY relative to the fixed-width comparison boundary. Document the exact row contents in the test for reviewability.
    2. Run `/search` with a date-range filter. Assert results either include a row that should be excluded OR exclude a row that should be included — whichever bug the format mismatch produces for that boundary. This documents the broken pre-backfill state.
    3. Run `reindex-metadata --normalize-timestamps`.
    4. Re-run the same `/search` request. Assert the result set is now exactly the expected set.
  - The test must be `@pytest.mark.integration`. Required because it exercises the full pipeline against real LanceDB.
- **Releasable**: after this task, the brief's "hardest part" (datetime safety) has a failing-then-passing regression test.
- **Tests (TDD)** — `tests/test_store.py`:
  - Integration (`-m integration`): `test_date_filter_returns_wrong_results_before_backfill_then_correct_after`.
  - Integration: `test_hybrid_search_date_filter_on_mixed_format_collection_warns` — date-range query emits the mixed-storage WARNING (caplog).
  - Integration: `test_hybrid_search_date_filter_on_normalized_collection_does_not_warn` — same collection after backfill; no warning.
  - Checkpoint: `uv run pytest -m integration tests/test_store.py -k "backfill or mixed_format" -x`

#### Task 6.2 — Benchmark threshold + baseline comparison
- [x] **File**: `tests/eval/thresholds.toml`, `tests/test_search_filtered_benchmark.py` (new, `-m benchmark`)
- **Depends on**: Task 4.1
- **Description**:
  - Two-step threshold setting (no cargo-culted numbers):
    1. Run the benchmark once unfiltered on the synthetic 10K-chunk corpus to record empirical p50/p95. Record both values as a comment block in `tests/eval/thresholds.toml` under a `[search_baseline]` header.
    2. Set `p95_ms_glob_filtered = ceil(baseline_p95 * 1.67)` capped at `250`. Document that `250` is the ABSOLUTE CEILING (the brief's value) but the recorded threshold is whichever is lower: the empirical-anchored value, or the ceiling.
  - Add `[search_filtered]` section:
    ```toml
    [search_filtered]
    p95_ms_glob_filtered = <empirical-anchored value, cap 250>
    p95_regression_pct_prefix_vs_unfiltered = 10
    ```
  - Write `test_search_filtered_benchmark.py` marked `@pytest.mark.benchmark`:
    - Synthesize a 10K-chunk collection (in-process LanceDB).
    - Bench 1 — glob filter that matches ~20%, 100 iterations, top_k=10, over HTTP localhost: assert p95 ≤ `p95_ms_glob_filtered`.
    - Bench 2 — `source_path_prefix` only (no glob): assert p95 has not regressed by more than `p95_regression_pct_prefix_vs_unfiltered`% vs the unfiltered baseline (recorded in step 1). Catches `LIKE ESCAPE` overhead and any prefiltering-default regression.
- **Releasable**: after this task, a regression on filtered-search latency fails CI when `-m benchmark` runs, and the threshold is anchored to a real number.
- **Tests (TDD)** — `tests/test_search_filtered_benchmark.py`:
  - Benchmark (`-m benchmark`): `test_glob_filtered_search_p95_under_threshold`.
  - Benchmark: `test_prefix_filtered_search_p95_regression_under_threshold`.
  - Checkpoint: `uv run pytest -m benchmark tests/test_search_filtered_benchmark.py`

#### Task 6.3 — Eval baseline-unchanged regression
- [ ] **File**: `tests/eval/test_eval_baseline_unchanged.py` (or extend the existing eval suite)
- **Depends on**: Task 3.3
- **Description**:
  - Run the existing eval suite with `filters=None` on every query and assert metrics are identical to `baselines/baseline.json`. Filters are additive; the default code path must not regress.
  - Marker `@pytest.mark.eval`.
- **Releasable**: after this task, an unfiltered eval run is automated against the baseline.
- **Tests (TDD)** — `tests/eval/test_eval_baseline_unchanged.py`:
  - Eval (`-m eval`): `test_unfiltered_eval_matches_baseline_metrics`.
  - Checkpoint: `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_baseline_unchanged.py`

---

### Final Phase — Verification & Documentation

#### Task 7.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: Tasks 1.1, 1.2, 1.3, 2.1, 2.2, 2.3, 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 4.3, 5.1, 6.1, 6.2, 6.3
- **Description**:
  - Spawn an agent to discover all documentation in the project (READMEs, ADRs, API docs, architecture docs, user guides, `BREAKING.md`) and update every file whose content is affected by A2. The agent must not update docs unrelated to A2.
  - Specifically required updates:
    - `Documentation/Architecture/600_api_reference_or_public_interface.md` — REST `SearchRequest.filters` + MCP `search` / `search_with_context` kwargs.
    - `Documentation/Architecture/130_data_architecture_and_persistence.md` — fixed-width timestamp format, mixed-storage transition window, glob post-filter, `fnmatch`'s lack of path semantics.
    - `Documentation/Architecture/520_api_design_and_contracts.md` — `SearchFilters` as the shared model (in `filters.py`, not `_types.py`); OpenAPI as authoritative for REST; MCP parity contract test.
    - Regenerated `openapi.json` reflects the new request/response shape; the `language` field description contains `"reserved"` and `"C2"`.
    - `BREAKING.md` — MCP-scoped entry (new optional input params + new output fields) and the operational note on datetime normalization.
    - `tests/eval/README.md` — note that `language` filter is reserved/rejected in A2 so future eval fixtures don't accidentally use it.
  - Verify every acceptance criterion below is met before marking this task complete.
- **Releasable**: after this task, the feature is fully verified and documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - A `POST /search` request with `filters.file_type="md"` returns only `.md`-typed chunks; verified by integration test.
  - A `POST /search` request with `filters.source_path_prefix="/docs/api/"` returns only chunks with matching `source_path`; integration test includes a path containing SQL metacharacters (`%`, `_`, `'`, `\`) to prove `_sql_quote_str` + `escape_like` round-trip correctly against real LanceDB.
  - A `POST /search` request with `filters.source_path_glob="*.md"` returns only matching chunks; with no match, response is `200` and `results=[]`.
  - `filters.indexed_after` and `filters.indexed_before` accept ISO-8601 strings, `+00:00`-offset strings, and date-only inputs; date-only `indexed_before` is end-of-day inclusive; `indexed_after > indexed_before` → 422.
  - `filters.language="en"` → HTTP 422 with message naming C2.
  - `filters.include_metadata=false` omits the custom `metadata` dict from each result in BOTH REST and MCP responses; `=true` includes it. System metadata fields (`file_type`, `indexed_at`, `updated_at`, `ingested_by`, `language`) are always present in the response.
  - Both vector and FTS branches receive a single `.where(pred)` call (string predicate, no `prefilter` kwarg) — proven structurally by a unit test asserting `.where()` is called on both builders, behaviorally by an integration test surfacing an FTS-only hit excluded by the filter, and defensively by `test_hybrid_search_never_calls_postfilter`.
  - Glob post-filter under-delivery emits one WARNING log line per query.
  - Pipeline-level WARNING fires when combined filter+ACL attrition leaves fewer than `top_k` candidates for the reranker.
  - 12-cell ACL × filter matrix (Task 3.4) passes.
  - REST↔MCP parity contract test passes: every `SearchFilters` field name appears in both `search` and `search_with_context` MCP tool input schemas.
  - `TelemetryEntry.filter_flags` is a typed `FilterFlags` submodel; raw filter values never appear in `~/.archon-search/search-logs/`. Negative tests assert the factory rejects a `query` kwarg AND `FilterFlags` rejects unknown fields and non-bool values.
  - `reindex-metadata --normalize-timestamps` migrates legacy variable-precision AND `+00:00`-offset timestamps to fixed-width; idempotent on second run; `--dry-run` reports counts without writing.
  - Search-path backfill regression test (Task 6.1) passes: a date-range query that returned incorrect results pre-backfill returns correct results post-backfill, and the mixed-storage WARNING fires pre-backfill and goes silent post-backfill.
  - `tests/eval/thresholds.toml` has a `[search_filtered]` section with `p95_ms_glob_filtered` anchored to the empirical baseline (≤ 250) and `p95_regression_pct_prefix_vs_unfiltered = 10`. `-m benchmark` enforces both.
  - Eval baseline-unchanged regression (Task 6.3) passes under `-m eval`.
  - Hypothesis property test for `escape_like` passes; the underlying LIKE simulator is itself anchored to hand-verified cases (Task 2.3).
  - `build_where`↔`SearchFilters` contract test (Task 2.3) passes: every non-response, non-rejected, non-post-RRF field on `SearchFilters` appears in the predicate when set.
  - OpenAPI schema `language` field description contains `"reserved"` and `"C2"` (Task 4.1).
  - Default-marker `uv run pytest` passes with `--cov-fail-under=85` intact; no `--no-cov` baked into `addopts`. Unit-level tests in `store.py`, `pipeline.py`, `store_filters.py`, `filters.py`, `telemetry/entry.py` carry the coverage gate — integration-only coverage is NOT relied upon.
  - `BREAKING.md` has an MCP-scoped entry and the datetime normalization operational note.
  - `Documentation/Architecture/600`, `/130`, `/520` reflect the implemented behavior; OpenAPI is regenerated and committed.
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked.
