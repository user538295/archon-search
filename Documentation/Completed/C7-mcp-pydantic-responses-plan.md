# C7 — MCP Responses Behind Pydantic Models
**Purpose**: Replace all `asdict()` serialization in MCP tools with Pydantic-validated schemas, making dataclass shape changes fail loudly instead of silently drifting the MCP contract.
**Audience**: Backend developer implementing the feature.
**Status**: Done

---

## Background

Every MCP tool in `archon_search/server/mcp.py` currently returns raw `dataclasses.asdict()` payloads with no schema validation. When a domain dataclass field is renamed, removed, or added, REST routes fail at the `response_model` boundary — but MCP tools silently absorb the drift because there is no schema gate at the serialization boundary. This was acceptable during early development; it is not acceptable now that internal and external clients depend on the MCP contract.

The fix is mechanical and low-risk: introduce `server/mcp_schemas.py` with explicit Pydantic models for every tool return shape, wire each tool to validate through its schema before returning, and add a `schema_validation_error` error code so future drift surfaces as a clear, actionable error rather than a silent shape change or crash.

---

## Goal

Every MCP tool return value is validated by a Pydantic model before serialization. All new schemas use `extra='forbid'` so field additions also fail loudly. Internal and transient fields (`vector`, `start_offset`, `end_offset`, `custom_score`, `centroid`, `centroid_sum`, `needs_recompute`, `needs_reindex`, `reindex_job_id`, `namespace`, `mutations_since_recompute`, `described_at_doc_count`) are excluded via explicit `from_result()` classmethods. A new `schema_validation_error` code makes developer-visible drift distinguishable from client-input errors. Five tools narrow their current response shapes — `BREAKING.md` entries are added for all narrowing tools.

---

## Scope

### In Scope
- New `archon_search/server/mcp_schemas.py` with all MCP-specific Pydantic schemas
- `from_result()` classmethods for all types with transient or internal fields (Pattern A: direct constructor)
- Migration of all 11 MCP tools to validate through their schemas before returning
- `_ERR_SCHEMA = 'schema_validation_error'` constant in `mcp.py`
- `ValidationError` catch paths in every tool returning this code on schema drift
- All tool return annotations stay `dict[str, Any]` / `list[dict[str, Any]]`; Pydantic validation is done internally via schema construction + `.model_dump(mode="json")` before returning (see Architecture for rationale)
- `BREAKING.md` entries for all five field-narrowing tools

### Out of Scope
- Changing the MCP protocol wire format or adding new fields
- REST schema changes — REST is already Pydantic-gated
- Moving `ExplainResponse` from `routes_explain.py` to `schemas.py`
- Converting `McpErrorResponse` from TypedDict to Pydantic model
- Autogeneration of MCP schemas from domain dataclasses

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task F.1 — Final verification & documentation update].

---

## What does NOT change
- `McpErrorResponse` TypedDict structure (`error: str`, `code: str`)
- The import of `ExplainResponse` and nested types from `routes_explain.py`
- REST route schemas in `schemas.py` or any `routes_*.py` file
- The MCP protocol wire format or field names (except where narrowing is intentional — see BREAKING.md)
- `IngestedBy` literal validation — schemas declare `ingested_by: str` to avoid importing the Literal type cross-module

---

## Known limitations / accepted trade-offs
- `explain` retains `-> dict[str, Any]` and continues to call `model_dump(mode="json")` internally. The conditional `stage_timings_ms` removal requires post-processing that prevents returning the Pydantic model directly. A `ValidationError` catch is added for consistency even though `ExplainResponse.from_pipeline_result()` construction is unlikely to fail in practice.
- `McpSearchResultSchema.from_result()` uses Pattern A (direct constructor). `SearchResult` has no `vector` field, so the existing `d.pop("vector", None)` in `search` is a no-op safety check and is removed after migration.
- All tool return annotations stay `dict[str, Any]` (or `list[dict[str, Any]]` for list-returning tools). Pydantic validation happens INSIDE the tool by constructing the schema and calling `.model_dump(mode="json")` before returning. This avoids fighting FastMCP's type machinery — `@app.tool()` with a `-> dict[str, Any]` annotation does NOT call `model_validate` on the return value, so `McpErrorResponse` dict returns on error paths are safe.
- The field rename `active_embedding_model` → `embedding_model` in all `CollectionMeta`-derived MCP schemas is a BREAKING change documented in `BREAKING.md`.
- `CollectionListItemSchema` omits `namespace` — it was exposed via the old `asdict()` path but is not part of the public MCP contract.
- `CollectionListItemSchema`, `CollectionDetailSchema`, and `CollectionMetaMcpSchema` share 8 identical public fields with no shared base class. This duplication is accepted: the three schemas may diverge independently in future versions, and shallow Pydantic inheritance introduces its own complexity. When `CollectionMeta` gains a new public field, all three `from_result()` classmethods must be updated — this is a known maintenance obligation.

---

## Architecture

### FastMCP Pydantic return-type handling

FastMCP's `func_metadata.py` uses `issubclass(type_annotation, BaseModel)` to detect whether to auto-serialize return values. This check fails for `list[SomeModel]` because `list` is not a `BaseModel` subclass. Additionally, when a return annotation is a `BaseModel` subclass, FastMCP calls `model_validate(result)` on the return value — a plain `McpErrorResponse` dict returned on the error path would then fail validation against the schema.

**Resolution**: All tool return annotations stay `-> dict[str, Any]` (or `-> list[dict[str, Any]]` for list-returning tools). `@app.tool()` with these annotations does NOT call `model_validate` on the return value. Validation is performed INSIDE the tool: construct the Pydantic schema (which is the validation gate), then return `schema.model_dump(mode="json")`. On error paths, return the plain `McpErrorResponse` TypedDict directly — it is already a dict and passes through unmodified.

This approach sidesteps both the list-detection issue and the `McpErrorResponse` incompatibility entirely.

### New module: `archon_search/server/mcp_schemas.py`

All new schemas live here. Imports `ConfigDict` and `BaseModel` from Pydantic. Must include `from __future__ import annotations` at the top of the file — this enables PEP 563 deferred evaluation so `TYPE_CHECKING`-only imports work as runtime type hints in function signatures. Does NOT import from any `routes_*.py` file. Domain dataclass imports are type-checking-only (`TYPE_CHECKING`).

### Test helper: constructing a fake `ValidationError` for mocks

To simulate schema drift in tests, construct a real `ValidationError` using a minimal invalid input and use it as `side_effect`:

```python
try:
    McpSearchResultSchema.model_validate({"bad": 1})
except ValidationError as e:
    _fake_err = e
mock_from_result.side_effect = _fake_err
```

This produces a real `ValidationError` instance. Use this pattern in all `test_*_schema_drift_*` tests rather than constructing `ValidationError` manually via `from_exception_data`.

**Schemas introduced:**

| Schema | Used by | `from_result()` | Notes |
|--------|---------|----------------|-------|
| `ExcludedCollectionMcpSchema` | `search` | no — flat, constructed directly | |
| `McpSearchResultSchema` | `search`, `search_with_context` | yes — `from_result(r: SearchResult)` | |
| `McpSearchResponse` | `search` | no — constructed from sub-schemas | fields: `results`, `acl_filtered`, `excluded_collections`, `hyde_applied: bool = False` |
| `ContextChunkSchema` | `search_with_context` | yes — excludes `vector`, `start_offset`, `end_offset`, `custom_score` | |
| `SearchWithContextItemSchema` | `search_with_context` | no — assembled from sub-schemas | |
| `SearchWithContextResponse` | `search_with_context` | no — top-level wrapper | fields: `results: list[SearchWithContextItemSchema]`, `hyde_applied: bool` |
| `CollectionListItemSchema` | `list_collections` | yes — renames `active_embedding_model` → `embedding_model`, excludes all internals | |
| `CollectionDetailSchema` | `get_collection_meta`, `update_collection` | yes — same exclusions as above | |
| `CollectionMetaMcpSchema` | `get_collections_meta` | yes — same + `description_embedding: list[float] \| None = None` | |
| `IngestResultSchema` | `ingest_file`, `ingest_directory` | yes — excludes `needs_recompute` | |
| `DocumentInfoSchema` | `list_documents` | yes — `from_result(r: DocumentInfo)` | |
| `DeleteDocumentSchema` | `delete_document` | no — `DeleteDocumentSchema(deleted=count)` | |

**CollectionMeta public contract** (all three collection schemas):
- Included: `name`, `description`, `doc_count`, `chunk_count`, `last_indexed`, `last_described`, `embedding_model` (← `active_embedding_model`), `pending_embedding_model`
- Always excluded: `centroid`, `centroid_sum`, `description_embedding` (except `CollectionMetaMcpSchema`), `mutations_since_recompute`, `needs_recompute`, `described_at_doc_count`, `needs_reindex`, `reindex_job_id`, `namespace`

### Error code constant in `mcp.py`

```python
_ERR_SCHEMA = "schema_validation_error"
```

Defined at module level, alongside the existing error-code usage. Tests import and assert this constant rather than matching bare strings.

Tools that already have a `ValidationError` catch for `SearchFilters` (`search`, `search_with_context`) require a separate inner try/except for the schema construction block to avoid shadowing the existing catch. See Task 2.1 and Task 2.2 for the required structure.

### Per-tool return type changes

All annotations keep their `dict[str, Any]` / `list[dict[str, Any]]` form (see FastMCP section above). Pydantic validation happens inside each tool via schema construction + `.model_dump(mode="json")`. The "New annotation" column shows the actual return annotation in code; the "Validates through" column names the schema used internally.

| Tool | Old annotation | New annotation | Validates through |
|------|---------------|----------------|-------------------|
| `search` | `dict[str, Any]` | `dict[str, Any]` | `McpSearchResponse` |
| `search_with_context` | `dict[str, Any]` | `dict[str, Any]` | `SearchWithContextResponse` |
| `explain` | `dict[str, Any]` | `dict[str, Any]` (kept) | `ExplainResponse` (existing) |
| `ingest_file` | `dict[str, Any]` | `dict[str, Any]` | `IngestResultSchema` |
| `ingest_directory` | `list[dict[str, Any]]` | `list[dict[str, Any]]` | `IngestResultSchema` (per item) |
| `list_collections` | `list[dict[str, Any]]` | `list[dict[str, Any]]` | `CollectionListItemSchema` (per item) |
| `get_collections_meta` | `list[dict[str, Any]]` | `list[dict[str, Any]]` | `CollectionMetaMcpSchema` (per item) |
| `get_collection_meta` | `dict[str, Any]` | `dict[str, Any]` | `CollectionDetailSchema` |
| `list_documents` | `list[dict[str, Any]]` | `list[dict[str, Any]]` | `DocumentInfoSchema` (per item) |
| `delete_document` | `dict[str, Any]` | `dict[str, Any]` | `DeleteDocumentSchema` |
| `update_collection` | `dict[str, Any]` | `dict[str, Any]` | `CollectionDetailSchema` |

---

## Task breakdown

### Phase 1 — Schema definitions (`server/mcp_schemas.py`)
> **Releasable**: after this phase, all schemas are importable and tested in isolation; no tools are yet migrated

#### Task 1.1 — Search response schemas
- [x] **File**: `archon_search/server/mcp_schemas.py` (create)
- **Depends on**: nothing
- **Description**:
  - Create `mcp_schemas.py` with `from __future__ import annotations` as the very first line, then a module-level docstring explaining it holds MCP-specific schemas
  - `ExcludedCollectionMcpSchema(name: str, reason: str)` — `extra='forbid'`
  - `McpSearchResultSchema` — all `SearchResult` fields except `vector` (which `SearchResult` does not have — confirm no pop needed); `extra='forbid'`; fields: `doc_id: str`, `chunk_id: str`, `text: str`, `score: float`, `source_path: str`, `file_type: str = ""`, `language: str = ""`, `indexed_at: str = ""`, `updated_at: str = ""`, `ingested_by: str = "cli"`, `metadata: dict[str, str] = {}`, `acl: list[str] | None = None`, `collection: str = ""`; `from_result(cls, r: SearchResult) -> McpSearchResultSchema` using Pattern A
  - `McpSearchResponse(results: list[McpSearchResultSchema], acl_filtered: bool, excluded_collections: list[ExcludedCollectionMcpSchema], hyde_applied: bool = False)` — `extra='forbid'`
  - All schemas: `model_config = ConfigDict(extra='forbid')`
  - Domain imports under `TYPE_CHECKING` only
- **Releasable**: `McpSearchResponse`, `McpSearchResultSchema`, `ExcludedCollectionMcpSchema` importable and validated
- **Tests (TDD)** — `tests/test_mcp_schemas.py` (create):
  - Unit: `test_mcp_search_result_schema_fields` — `set(McpSearchResultSchema.model_fields.keys()) == {expected_set}` where expected_set is the exact public field set; fails if field is added or removed
  - Unit: `test_mcp_search_result_schema_from_result` — construct a `SearchResult`, call `from_result()`, assert all field values map correctly
  - Unit: `test_mcp_search_result_schema_extra_forbid` — `McpSearchResultSchema.model_validate({...valid..., 'surprise': 1})` raises `ValidationError`
  - Unit: `test_mcp_search_response_fields` — `set(McpSearchResponse.model_fields.keys()) == {'results', 'acl_filtered', 'excluded_collections', 'hyde_applied'}`
  - Unit: `test_mcp_search_response_hyde_applied_defaults_false` — construct `McpSearchResponse(results=[], acl_filtered=False, excluded_collections=[])` without passing `hyde_applied`; assert `response.hyde_applied is False`
  - Unit: `test_mcp_search_response_extra_forbid` — passing extra field raises `ValidationError`
  - Unit: `test_excluded_collection_schema_extra_forbid` — extra field raises `ValidationError`
  - Unit: `test_mcp_schemas_has_future_annotations` — `import inspect, archon_search.server.mcp_schemas as m; src = inspect.getsource(m); assert src.startswith("from __future__ import annotations")` — verifies the import is present at the top of the file at runtime
  - Checkpoint: `uv run pytest tests/test_mcp_schemas.py -x --no-cov`

#### Task 1.2 — Context search schemas
- [x] **File**: `archon_search/server/mcp_schemas.py`
- **Depends on**: Task 1.1
- **Description**:
  - `ContextChunkSchema` — `ChunkRecord` with `vector`, `start_offset`, `end_offset`, `custom_score` excluded; `extra='forbid'`; fields: `doc_id: str`, `chunk_id: str`, `text: str`, `source_path: str`, `indexed_at: str`, `file_type: str = ""`, `language: str = ""`, `metadata: dict[str, str] = {}`, `ingested_by: str = "cli"`, `updated_at: str = ""`, `acl: list[str] | None = None`; `from_result(cls, chunk: ChunkRecord) -> ContextChunkSchema` using Pattern A
  - `SearchWithContextItemSchema(result: McpSearchResultSchema, context_before: list[ContextChunkSchema], context_after: list[ContextChunkSchema])` — `extra='forbid'`
  - `SearchWithContextResponse(results: list[SearchWithContextItemSchema], hyde_applied: bool)` — `extra='forbid'`; top-level wrapper that mirrors the current `search_with_context` return shape `{"results": [...], "hyde_applied": bool}`
- **Releasable**: `ContextChunkSchema`, `SearchWithContextItemSchema`, and `SearchWithContextResponse` importable
- **Tests (TDD)** — `tests/test_mcp_schemas.py`:
  - Unit: `test_context_chunk_schema_fields` — `set(ContextChunkSchema.model_fields.keys()) == {'doc_id', 'chunk_id', 'text', 'source_path', 'indexed_at', 'file_type', 'language', 'metadata', 'ingested_by', 'updated_at', 'acl'}` — must contain `language` and must NOT contain `vector`, `start_offset`, `end_offset`, `custom_score`
  - Unit: `test_context_chunk_schema_from_result_excludes_transient` — build a `ChunkRecord` with `vector=[0.1]`, `start_offset=5`, `end_offset=10`, `custom_score=0.9`; call `from_result()`; assert `ContextChunkSchema` instance has none of those fields
  - Unit: `test_context_chunk_schema_extra_forbid` — extra field raises `ValidationError`
  - Unit: `test_search_with_context_item_schema_fields` — `set(SearchWithContextItemSchema.model_fields.keys()) == {'result', 'context_before', 'context_after'}`
  - Unit: `test_search_with_context_item_schema_extra_forbid` — extra field raises `ValidationError`
  - Unit: `test_search_with_context_response_fields` — `set(SearchWithContextResponse.model_fields.keys()) == {'results', 'hyde_applied'}`
  - Unit: `test_search_with_context_response_extra_forbid` — extra field raises `ValidationError`
  - Checkpoint: `uv run pytest tests/test_mcp_schemas.py::test_context_chunk_schema_fields tests/test_mcp_schemas.py::test_context_chunk_schema_from_result_excludes_transient tests/test_mcp_schemas.py::test_search_with_context_item_schema_fields -x --no-cov`

#### Task 1.3 — Collection meta schemas
- [x] **File**: `archon_search/server/mcp_schemas.py`
- **Depends on**: Task 1.1
- **Description**:
  - `CollectionListItemSchema` — public `CollectionMeta` fields for `list_collections`; `extra='forbid'`; fields: `name: str`, `description: str | None = None`, `doc_count: int = 0`, `chunk_count: int = 0`, `last_indexed: datetime | None = None`, `last_described: datetime | None = None`, `embedding_model: str = ""`, `pending_embedding_model: str | None = None`; `from_result(cls, meta: CollectionMeta) -> CollectionListItemSchema` mapping `meta.active_embedding_model` → `embedding_model`
  - `CollectionDetailSchema` — same fields as `CollectionListItemSchema`, same exclusions; separate class for `get_collection_meta` and `update_collection`; `from_result(cls, meta: CollectionMeta) -> CollectionDetailSchema`
  - `CollectionMetaMcpSchema` — same as above + `description_embedding: list[float] | None = None`; `from_result(cls, meta: CollectionMeta, *, include_description_embedding: bool = False) -> CollectionMetaMcpSchema`; when `include_description_embedding=False`, field is `None`
  - All three schemas: `model_config = ConfigDict(extra='forbid')`
  - `datetime` fields declared as `datetime | None` — FastMCP serializes via `model_dump(mode="json")` internally, producing ISO 8601 strings
- **Releasable**: all three collection schemas importable with tested `from_result()` classmethods
- **Tests (TDD)** — `tests/test_mcp_schemas.py`:
  - Unit: `test_collection_list_item_schema_fields` — `set(CollectionListItemSchema.model_fields.keys()) == {'name', 'description', 'doc_count', 'chunk_count', 'last_indexed', 'last_described', 'embedding_model', 'pending_embedding_model'}`; must NOT contain `centroid`, `centroid_sum`, `namespace`, `needs_recompute`, `needs_reindex`, `reindex_job_id`, `mutations_since_recompute`, `described_at_doc_count`, `active_embedding_model`
  - Unit: `test_collection_list_item_schema_from_result_field_mapping` — build a `CollectionMeta` with `active_embedding_model="model-x"`; call `from_result()`; assert `schema.embedding_model == "model-x"`
  - Unit: `test_collection_list_item_schema_extra_forbid` — extra field raises `ValidationError`
  - Unit: `test_collection_detail_schema_fields` — same field-coverage check as `CollectionListItemSchema`
  - Unit: `test_collection_detail_schema_from_result` — asserts correct field mapping
  - Unit: `test_collection_detail_schema_extra_forbid` — extra field raises `ValidationError`
  - Unit: `test_collection_meta_mcp_schema_fields` — `set(CollectionMetaMcpSchema.model_fields.keys()) == {'name', 'description', 'doc_count', 'chunk_count', 'last_indexed', 'last_described', 'embedding_model', 'pending_embedding_model', 'description_embedding'}`
  - Unit: `test_collection_meta_mcp_schema_description_embedding_excluded_by_default` — `from_result(meta, include_description_embedding=False)` → `schema.description_embedding is None`
  - Unit: `test_collection_meta_mcp_schema_description_embedding_included` — `from_result(meta, include_description_embedding=True)` with a meta that has `description_embedding=[0.1, 0.2]` → schema contains the vector
  - Unit: `test_collection_meta_mcp_schema_extra_forbid` — extra field raises `ValidationError`
  - Checkpoint: `uv run pytest tests/test_mcp_schemas.py -k "collection" -x --no-cov`

#### Task 1.4 — Ingest, document, and delete schemas
- [x] **File**: `archon_search/server/mcp_schemas.py`
- **Depends on**: Task 1.1
- **Description**:
  - `IngestResultSchema` — `IngestResult` with `needs_recompute` excluded; `extra='forbid'`; fields: `doc_id: str`, `chunks_created: int`, `status: str`, `error: str | None = None`; `from_result(cls, r: IngestResult) -> IngestResultSchema` using Pattern A
  - `DocumentInfoSchema` — all `DocumentInfo` fields (no internal fields in this dataclass); `extra='forbid'`; fields: `doc_id: str`, `source_path: str`, `chunk_count: int`, `indexed_at: str`; `from_result(cls, r: DocumentInfo) -> DocumentInfoSchema` using Pattern A
  - `DeleteDocumentSchema(deleted: int)` — `extra='forbid'`; no `from_result()` needed, constructed directly
- **Releasable**: `IngestResultSchema`, `DocumentInfoSchema`, `DeleteDocumentSchema` importable
- **Tests (TDD)** — `tests/test_mcp_schemas.py`:
  - Unit: `test_ingest_result_schema_fields` — `set(IngestResultSchema.model_fields.keys()) == {'doc_id', 'chunks_created', 'status', 'error'}`; must NOT contain `needs_recompute`
  - Unit: `test_ingest_result_schema_from_result_excludes_needs_recompute` — build `IngestResult(needs_recompute=True, ...)`; call `from_result()`; assert schema has no `needs_recompute` attribute accessible as a model field
  - Unit: `test_ingest_result_schema_extra_forbid` — extra field raises `ValidationError`
  - Unit: `test_document_info_schema_fields` — `set(DocumentInfoSchema.model_fields.keys()) == {'doc_id', 'source_path', 'chunk_count', 'indexed_at'}`
  - Unit: `test_document_info_schema_from_result` — field values match the input `DocumentInfo`
  - Unit: `test_document_info_schema_extra_forbid` — extra field raises `ValidationError`
  - Unit: `test_delete_document_schema_fields` — `set(DeleteDocumentSchema.model_fields.keys()) == {'deleted'}`
  - Unit: `test_delete_document_schema_extra_forbid` — extra field raises `ValidationError`
  - Checkpoint: `uv run pytest tests/test_mcp_schemas.py -k "ingest or document or delete" -x --no-cov`

---

### Phase 2 — Tool migrations (`server/mcp.py`)
> **Releasable**: each task below is independently releasable after Phase 1 is complete; all Phase 1 tasks must be done first

#### Task 2.1 — `_ERR_SCHEMA` constant + migrate `search` tool
- [x] **File**: `archon_search/server/mcp.py`
- **Depends on**: Task 1.1
- **Description**:
  - Add `_ERR_SCHEMA = "schema_validation_error"` at module level, near the existing error-code usage pattern
  - Import `McpSearchResponse`, `McpSearchResultSchema`, `ExcludedCollectionMcpSchema` from `mcp_schemas`
  - Remove the existing `asdict(r)` / `d.pop("vector", None)` pattern from both the single-collection and multi-collection paths of `search`
  - Replace with `McpSearchResultSchema.from_result(r)` for each result
  - Build `McpSearchResponse(results=..., acl_filtered=..., excluded_collections=[ExcludedCollectionMcpSchema(name=e.name, reason=e.reason) for e in ...], hyde_applied=<bool from pipeline result>)`
  - Return `response.model_dump(mode="json")` — do NOT return the `McpSearchResponse` instance directly; this keeps the annotation as `-> dict[str, Any]` and prevents FastMCP from calling `model_validate` on the return value
  - **Important**: `search` already has `except ValidationError as exc: return McpErrorResponse(error=str(exc), code="validation_error")` in each path to catch `SearchFilters` validation errors. The schema drift catch must NOT be a second bare `except ValidationError` in the same try block — it would shadow the `SearchFilters` catch. Instead, use a separate INNER try/except that wraps ONLY the schema construction and serialization lines (after the pipeline call succeeds):
    ```python
    # inner try wrapping schema construction only
    try:
        response = McpSearchResponse(
            results=[McpSearchResultSchema.from_result(r) for r in ...],
            ...
        )
        return response.model_dump(mode="json")
    except ValidationError as exc:
        return McpErrorResponse(error=str(exc), code=_ERR_SCHEMA)
    ```
    This leaves the outer `except ValidationError` for `SearchFilters` intact and operating normally.
  - `include_metadata=False` path: after `McpSearchResultSchema.from_result(r)` constructs the schema, apply `result_schema.metadata = {}` if `include_metadata` is False before adding to the results list — same mutation-after-construction pattern as Task 2.2.
  - Return annotation stays `-> dict[str, Any]`
- **Releasable**: `search` tool validates results through `McpSearchResponse` before returning
- **Tests (TDD)** — `tests/test_mcp.py`:
  - Unit: `test_search_include_metadata_false_clears_metadata` — mock pipeline returning a `SearchResult` with metadata populated; call `search()` with `include_metadata=False`; assert all result items in the returned dict have empty metadata dicts
  - Unit: `test_search_returns_mcp_search_response_shape` — mock pipeline returning a valid `SearchResult`; call `search()`; assert return value is a dict with keys `results`, `acl_filtered`, `excluded_collections`, `hyde_applied` and no `embedding_model`
  - Unit: `test_search_multi_collection_returns_mcp_search_response_shape` — multi-collection fan-out path; assert return shape matches `McpSearchResponse` (no `embedding_model`)
  - Unit: `test_search_acl_filtered_with_excluded_collections` — mock pipeline result with `acl_filtered=True` and at least one excluded collection; assert return dict contains `acl_filtered: True` and a non-empty `excluded_collections` list with expected `name`/`reason` fields
  - Integration: `test_search_schema_drift_returns_schema_validation_error` — use the test helper pattern (`try: McpSearchResultSchema.model_validate({"bad": 1}); except ValidationError as e: _err = e`) to get a real `ValidationError`; patch `McpSearchResultSchema.from_result` with `side_effect=_err`; call `search()`; assert return value is a dict with `code == _ERR_SCHEMA`
  - Checkpoint: `uv run pytest tests/test_mcp.py -k "search" -x --no-cov`

#### Task 2.2 — Migrate `search_with_context` tool
- [x] **File**: `archon_search/server/mcp.py`
- **Depends on**: Task 1.2
- **Description**:
  - Import `SearchWithContextItemSchema`, `ContextChunkSchema`, `SearchWithContextResponse` from `mcp_schemas`
  - Remove `_chunk_to_context_dict()` call pattern and `asdict(r["result"])` / `d.pop("vector", None)` pattern
  - Replace with: `McpSearchResultSchema.from_result(r["result"])` for the inner result; `ContextChunkSchema.from_result(c)` for each context chunk; assemble `SearchWithContextItemSchema(result=..., context_before=..., context_after=...)` per item
  - Assemble the top-level response: `SearchWithContextResponse(results=items, hyde_applied=<bool from pipeline>)` then return `response.model_dump(mode="json")` — this preserves the existing `{"results": [...], "hyde_applied": bool}` wire format; return annotation stays `-> dict[str, Any]`
  - `include_metadata=False` path: for `McpSearchResultSchema.from_result()` and `ContextChunkSchema.from_result()`, clear metadata field by setting `schema.metadata = {}` before assembling (or handle in `from_result()` by accepting an `include_metadata` parameter — prefer: apply the clear after construction to avoid schema coupling)
  - **Important**: `search_with_context` already has `except ValidationError as exc: return McpErrorResponse(error=str(exc), code="validation_error")` catching `SearchFilters` validation errors (around line 394). The schema drift catch must NOT be a second bare `except ValidationError` in the same try block — it would shadow the `SearchFilters` catch. Use a separate INNER try/except wrapping only the schema construction + `model_dump` call, identical to the pattern described in Task 2.1.
  - Return annotation stays `-> dict[str, Any]`
  - Remove `_chunk_to_context_dict()` helper function from `mcp.py` after migration (it is now unused)
- **Releasable**: `search_with_context` validates all nested shapes through schemas; wire format preserved
- **Tests (TDD)** — `tests/test_mcp.py`:
  - Unit: `test_search_with_context_result_shape` — mock pipeline returning a single result with context chunks; assert return is a dict with `results` list and `hyde_applied` bool; assert no `start_offset`, `end_offset`, `custom_score`, `vector` in context chunks
  - Unit: `test_search_with_context_hyde_applied_propagated` — mock pipeline returning `hyde_applied=True`; assert `return_value["hyde_applied"] is True`
  - Unit: `test_search_with_context_hyde_applied_false` — mock pipeline returning `hyde_applied=False`; assert `return_value["hyde_applied"] is False`
  - Unit: `test_search_with_context_include_metadata_false_clears_metadata` — assert `metadata == {}` in result and context chunks when `include_metadata=False`
  - Unit: `test_chunk_to_context_dict_removed` — `import archon_search.server.mcp as mcp_module; assert not hasattr(mcp_module, '_chunk_to_context_dict')` — guards against dead-code retention
  - Integration: `test_search_with_context_schema_drift_returns_schema_validation_error` — patch schema construction to raise `ValidationError`; assert `code == _ERR_SCHEMA`
  - Checkpoint: `uv run pytest tests/test_mcp.py -k "search_with_context" -x --no-cov`

#### Task 2.3 — Migrate `explain` tool (ValidationError catch)
- [x] **File**: `archon_search/server/mcp.py`
- **Depends on**: Task 2.1 (for `_ERR_SCHEMA`)
- **Description**:
  - `explain` already uses `ExplainResponse.model_dump(mode="json")` and is the reference implementation; return annotation stays `dict[str, Any]` (post-processing conditional pop of `stage_timings_ms` prevents returning the model directly)
  - Add `except ValidationError as exc: return McpErrorResponse(error=str(exc), code=_ERR_SCHEMA)` in both the single-collection and multi-collection paths, wrapping `ExplainResponse.from_pipeline_result(result, include_stage_timings=stage_timings)` specifically (approximately lines ~538 in current `mcp.py`)
  - Placement differs by path:
    - Multi-collection path: `from_pipeline_result()` is called after the pipeline error-handler try/except concludes — wrap it in its own try/except (it is already logically 'outside' the handler block).
    - Single-collection path: `from_pipeline_result()` is called INSIDE the main try block (which catches `ExplainStageError`, `Exception`, etc.). Add an INNER try/except around the `from_pipeline_result()` call + `model_dump` + conditional pop + return — identical to the inner-try pattern from Tasks 2.1/2.2 — so the `ValidationError` catch is specific to schema construction, not the broader pipeline.
  - This is a defensive guard: `ExplainResponse` construction is unlikely to raise in practice, but the catch makes the error path explicit and consistent with all other tools
- **Releasable**: `explain` has a `schema_validation_error` catch path
- **Tests (TDD)** — `tests/test_mcp.py`:
  - Integration: `test_explain_schema_drift_returns_schema_validation_error` — patch target is `archon_search.server.mcp.ExplainResponse.from_pipeline_result` (since `mcp.py` imports `ExplainResponse` at the top, this is the correct patch location); use the test helper pattern to produce a real `ValidationError` as `side_effect`; assert result is a dict with `code == _ERR_SCHEMA`
  - Checkpoint: `uv run pytest tests/test_mcp.py -k "explain" -x --no-cov`

#### Task 2.4 — Migrate `ingest_file` and `ingest_directory` tools
- [x] **File**: `archon_search/server/mcp.py`
- **Depends on**: Task 1.4
- **Description**:
  - Import `IngestResultSchema` from `mcp_schemas`
  - `ingest_file`: replace `return asdict(result)` with `schema = IngestResultSchema.from_result(result); return schema.model_dump(mode="json")`; add `except ValidationError as exc: return McpErrorResponse(error=str(exc), code=_ERR_SCHEMA)` before general catch; return annotation stays `-> dict[str, Any]`
  - `ingest_directory`: replace `[asdict(r) for r in results]` with `[IngestResultSchema.from_result(r).model_dump(mode="json") for r in results]`; add `ValidationError` catch; return annotation stays `-> list[dict[str, Any]]`
- **Releasable**: both ingest tools exclude `needs_recompute` from responses
- **Tests (TDD)** — `tests/test_mcp.py`:
  - [x] Unit: `test_ingest_file_result_excludes_needs_recompute` — mock pipeline returning `IngestResult(needs_recompute=True, ...)`; call `ingest_file()`; assert return value has no `needs_recompute` field
  - [x] Unit: `test_ingest_directory_result_excludes_needs_recompute` — same for directory ingest returning a list
  - [x] Integration: `test_ingest_file_schema_drift_returns_schema_validation_error` — patch `IngestResultSchema.from_result` to raise `ValidationError`; assert `code == _ERR_SCHEMA`
  - [x] Integration: `test_ingest_directory_schema_drift_returns_schema_validation_error` — same for directory ingest
  - Checkpoint: `uv run pytest tests/test_mcp.py -k "ingest" -x --no-cov`

#### Task 2.5 — Migrate `list_collections` and `get_collections_meta` tools
- [x] **File**: `archon_search/server/mcp.py`
- **Depends on**: Task 1.3
- **Description**:
  - Import `CollectionListItemSchema`, `CollectionMetaMcpSchema` from `mcp_schemas`
  - `list_collections`: replace the `asdict(r)` / `d.pop("centroid")` / `d.pop("description_embedding")` pattern with `[CollectionListItemSchema.from_result(r).model_dump(mode="json") for r in results]`; add `ValidationError` catch; return annotation stays `-> list[dict[str, Any]]`
  - `get_collections_meta`: replace the `asdict(r)` / conditional `d.pop("description_embedding")` pattern with `[CollectionMetaMcpSchema.from_result(r, include_description_embedding=include_description_embedding).model_dump(mode="json") for r in results]`; add `ValidationError` catch; return annotation stays `-> list[dict[str, Any]]`
  - The old inline pop logic is deleted entirely — field exclusion is now handled by `from_result()`
- **Releasable**: both collection listing tools exclude all internal `CollectionMeta` fields
- **Tests (TDD)** — `tests/test_mcp.py`:
  - Unit: `test_list_collections_excludes_internal_fields` — mock pipeline returning a `CollectionMeta` with all internal fields populated; assert return items have none of `centroid`, `centroid_sum`, `namespace`, `needs_recompute`, `needs_reindex`, `reindex_job_id`, `mutations_since_recompute`, `described_at_doc_count`
  - Unit: `test_list_collections_renames_active_embedding_model` — assert return item has `embedding_model` key, not `active_embedding_model`
  - Unit: `test_get_collections_meta_without_description_embedding` — `description_embedding` is `None` when `include_description_embedding=False`
  - Unit: `test_get_collections_meta_with_description_embedding` — `description_embedding` is present when `include_description_embedding=True`
  - Integration: `test_list_collections_schema_drift_returns_schema_validation_error` — patch `from_result` to raise `ValidationError`; assert `code == _ERR_SCHEMA`
  - Integration: `test_get_collections_meta_schema_drift_returns_schema_validation_error` — same
  - Checkpoint: `uv run pytest tests/test_mcp.py -k "list_collections or get_collections_meta" -x --no-cov`

#### Task 2.6 — Migrate `get_collection_meta` and `update_collection` tools
- [x] **File**: `archon_search/server/mcp.py`
- **Depends on**: Task 1.3
- **Description**:
  - Import `CollectionDetailSchema` from `mcp_schemas`
  - `get_collection_meta`: replace `return asdict(meta)` with `schema = CollectionDetailSchema.from_result(meta); return schema.model_dump(mode="json")`; add `except ValidationError as exc: return McpErrorResponse(error=str(exc), code=_ERR_SCHEMA)` before general catch; return annotation stays `-> dict[str, Any]`
  - `update_collection`: replace `return asdict(meta)` at the end of the state machine with `schema = CollectionDetailSchema.from_result(meta); return schema.model_dump(mode="json")`; add `ValidationError` catch; return annotation stays `-> dict[str, Any]`
- **Releasable**: both tools return narrowed `CollectionDetailSchema` (internal fields stripped)
- **Tests (TDD)** — `tests/test_mcp.py`:
  - Unit: `test_get_collection_meta_excludes_internal_fields` — assert return dict excludes `centroid`, `centroid_sum`, `namespace`, `needs_recompute`, `needs_reindex`, `reindex_job_id`, `mutations_since_recompute`, `described_at_doc_count`
  - Unit: `test_get_collection_meta_renames_active_embedding_model` — assert `embedding_model` key present, no `active_embedding_model`
  - Unit: `test_update_collection_excludes_internal_fields` — same assertions for `update_collection` success path
  - Unit: `test_update_collection_no_op_same_model` — mock a pipeline call where the model is unchanged so no reindex is triggered; assert the return shape matches `CollectionDetailSchema` fields (i.e. returns a valid dict with `name`, `embedding_model`, etc. but no internal fields)
  - Unit: `test_update_collection_pending_model_path` — mock a pipeline call that triggers pending state (new model set, reindex scheduled); assert the return shape still matches `CollectionDetailSchema` (contains `pending_embedding_model`) and no internal fields leak
  - Integration: `test_get_collection_meta_schema_drift_returns_schema_validation_error` — patch `from_result` to raise `ValidationError`; assert `code == _ERR_SCHEMA`
  - Integration: `test_update_collection_schema_drift_returns_schema_validation_error` — same
  - Checkpoint: `uv run pytest tests/test_mcp.py -k "get_collection_meta or update_collection" -x --no-cov`

#### Task 2.7 — Migrate `list_documents` and `delete_document` tools
- [x] **File**: `archon_search/server/mcp.py`
- **Depends on**: Task 1.4
- **Description**:
  - Import `DocumentInfoSchema`, `DeleteDocumentSchema` from `mcp_schemas`
  - `list_documents`: replace `[asdict(r) for r in results]` with `[DocumentInfoSchema.from_result(r).model_dump(mode="json") for r in results]`; add `ValidationError` catch; return annotation stays `-> list[dict[str, Any]]`
  - `delete_document`: replace `return {"deleted": count}` with `schema = DeleteDocumentSchema(deleted=count); return schema.model_dump(mode="json")`; add `ValidationError` catch; return annotation stays `-> dict[str, Any]`
- **Releasable**: both tools validate return shapes through schemas
- **Tests (TDD)** — `tests/test_mcp.py`:
  - Unit: `test_list_documents_returns_document_info_schema_shape` — mock pipeline returning a `DocumentInfo`; assert return list item has `doc_id`, `source_path`, `chunk_count`, `indexed_at`
  - Unit: `test_delete_document_returns_delete_schema_shape` — assert return value has `deleted` key
  - Integration: `test_list_documents_schema_drift_returns_schema_validation_error` — patch `from_result` to raise `ValidationError`; assert `code == _ERR_SCHEMA`
  - Integration: `test_delete_document_schema_drift_returns_schema_validation_error` — patch `archon_search.server.mcp.DeleteDocumentSchema` (the class reference in `mcp.py`'s namespace, NOT `__init__`) with `side_effect=_fake_err` (a real `ValidationError` obtained via the test helper pattern — e.g. `try: DeleteDocumentSchema.model_validate({"bad": 1}); except ValidationError as e: _fake_err = e`); this makes `DeleteDocumentSchema(deleted=count)` raise when called; assert return value is a dict with `code == _ERR_SCHEMA`. Note: Pydantic v2 uses a Rust-generated `__init__` that cannot be patched with `mock.patch.object(DeleteDocumentSchema, '__init__')` — always patch the class reference in the module namespace instead
  - Checkpoint: `uv run pytest tests/test_mcp.py -k "list_documents or delete_document" -x --no-cov`

---

### Phase 3 — Contract documentation
> **Releasable**: after this phase, the narrowed MCP contracts are recorded and clients are informed

#### Task 3.1 — `BREAKING.md` entries for field-narrowing tools
- [x] **File**: `BREAKING.md`
- **Depends on**: Tasks 2.5, 2.6, 2.7
- **Description**:
  - Append entries for the five tools that narrow their response shapes relative to the current `asdict()` output. Format: same as existing BREAKING.md entries (date, tool name, change description, migration path).
  - **`list_collections`**: previously returned all `CollectionMeta` fields except `centroid` and `description_embedding`; now returns only the public contract fields. Removed fields: `centroid_sum`, `mutations_since_recompute`, `needs_recompute`, `described_at_doc_count`, `needs_reindex`, `reindex_job_id`, `namespace`. Field `active_embedding_model` renamed to `embedding_model`.
  - **`get_collections_meta`**: same removals as `list_collections`. Additionally, when `include_description_embedding=False` (default), `description_embedding` is now `null` rather than absent (field is always present in the schema).
  - **`get_collection_meta`**: previously returned all `CollectionMeta` fields including `centroid`; now returns only the public contract (no `centroid`, no internal fields). Field `active_embedding_model` renamed to `embedding_model`.
  - **`update_collection`**: same removals as `get_collection_meta`.
  - **`search_with_context`**: context chunks previously included `start_offset`, `end_offset`, and `custom_score` (only `vector` was stripped); now all transient/internal fields are excluded. Note: `language` is preserved in the schema (it was included in the old `asdict()` output and remains in `ContextChunkSchema`) — it is NOT a breaking change.
- **Releasable**: breaking changes are documented and discoverable
- **Tests (TDD)**: N/A — documentation task
- **Checkpoint**: review `BREAKING.md` for completeness and verify all five tools are described with their migration paths

---

### Final Phase — Verification & Documentation

#### Task F.1 — Final verification & documentation update
- [x] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to discover all documentation in the project (READMEs, ADRs, API docs, architecture docs, user guides, CHANGELOG) and update every file whose content is affected by the changes delivered in this plan. The agent must not update docs that are unrelated.
  - Key docs to check: `Documentation/Architecture/100_system_architecture_overview.md`, `Documentation/Architecture/110_component_catalog_and_layer_breakdown.md`, `Documentation/Architecture/600_api_reference_or_public_interface.md`, `Documentation/Architecture/150_security_and_privacy_architecture.md`, `Documentation/Architecture/520_api_design_and_contracts.md`, `Documentation/UserManual/`, any doc describing MCP tool response shapes.
  - Check `tests/contract/test_mcp_search_response_shape.py` (if present) — update its assertions to expect the new `McpSearchResponse` shape (including `hyde_applied: bool`) instead of raw dict assertions that predate the Pydantic migration.
  - Verify all acceptance criteria below are met before marking this task complete.
- **Releasable**: after this task, the feature is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - All 11 MCP tool return values are validated through a Pydantic schema before leaving `mcp.py`
  - All new schemas in `mcp_schemas.py` have `model_config = ConfigDict(extra='forbid')`
  - All `from_result()` classmethods use Pattern A (direct constructor, no `asdict()` intermediary)
  - `_ERR_SCHEMA = "schema_validation_error"` constant is defined at module level in `mcp.py`
  - Every tool has a `ValidationError` catch path returning `McpErrorResponse(error=str(exc), code=_ERR_SCHEMA)`
  - `search` return value has no `embedding_model` field
  - `search_with_context` context chunks have no `vector`, `start_offset`, `end_offset`, `custom_score`
  - All `CollectionMeta`-derived tool responses have no `centroid`, `centroid_sum`, `namespace`, `needs_recompute`, `needs_reindex`, `reindex_job_id`, `mutations_since_recompute`, `described_at_doc_count`; and use `embedding_model` not `active_embedding_model`
  - `ingest_file` and `ingest_directory` responses have no `needs_recompute` field
  - `BREAKING.md` has entries for all five narrowing tools
  - `uv run pytest` passes with coverage ≥ 85%
  - `uv run pytest tests/test_mcp_schemas.py` all pass
  - `uv run pytest tests/test_mcp.py` all pass
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked; run `uv run pytest --no-cov` and `uv run pytest -m 'not live and not eval and not benchmark and not integration'` to confirm full suite passes.
