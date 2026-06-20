# BREAKING CHANGES

## Compatibility Policy

`archon-search` uses CalVer (`YY.M.<commit-count>`). CalVer segments encode **time only** — they do not signal compatibility. This file IS the compatibility contract.

**Rule**: every release that removes or changes an existing API contract MUST add an entry here describing: what changed, the migration path, and from which release the deprecated form was announced. Consumers should subscribe to changes in this file, not interpret CalVer segments.

## Changelog

### [next release] — D4: `centroid_incremental_enabled` config field removed

**Surface**: `archon-search.toml` `[database]` section.

- The `centroid_incremental_enabled` field has been removed from `SearchConfig`. The B5 incremental centroid path is now unconditional.
- If present in an existing TOML config, the value is discarded and a WARNING is logged at startup.
- The per-operation full-recompute gate (previously activated by `centroid_incremental_enabled = false`) has been removed. The full-recompute path itself (`recompute_collection_meta`) still exists and fires on `needs_recompute = True` or periodic drift-reset checkpoints.

**Migration**: remove `centroid_incremental_enabled` from your `archon-search.toml`. For operators who never set this flag (or had it set to `true`), there is no functional change — the incremental path was already the default since B5. If you had explicitly set it to `false`, your deployment will now use the B5 incremental centroid path; run `archon-search collection reindex <name>` if you observe centroid drift.

---

### [next release] — D2 job contract: `JobResponse` gains bulk-job subclass fields

**Surface**: `GET /jobs/{job_id}` and `GET /jobs` REST responses (`JobResponse`).

- `JobResponse` now includes four optional nullable fields: `source` (`"user" | "backup" | null`), `collection` (`str | null`), `output_path` (`str | null`), `archive_path` (`str | null`).
- These fields are populated for `ExportJob` and `ImportJob` records; base `IngestJob`, `ReindexJob`, and `DeleteJob` records return them as `null`.
- Additive change. Strict-schema clients will see four new keys; permissive clients are unaffected.

**Migration**: no action required for read-only callers. Add the four keys to client schema definitions if strict validation is in use.

---

### [next release] — D2 status contract: `StatusResponse` will gain a `backup` object

**Surface**: `GET /status` REST response (`StatusResponse`).

- `StatusResponse` will gain an optional `backup: BackupStatusDetail | null` field describing scheduled-backup state (enabled flag, interval, last/next tick, per-collection backup status). Added in a later D2 task; documented here to give clients a heads-up.
- Additive change. Existing callers that ignore unknown fields are unaffected.

**Migration**: no action required for read-only callers. Add the `backup` key to client schema definitions if strict validation is in use, once the field ships.

---

### [next release] — D1/D2 MCP tools: `export_collection` and `import_collection` added (tool count 11 → 13)

**Surface**: MCP tool registry (`create_app`).

- Two new tools registered: `export_collection(collection, output_path="")` and `import_collection(collection, path, force_overwrite=False, ignore_schema_version=False, on_error="fail")`.
- Both tools are **additive** — existing MCP tool consumers are unaffected.
- MCP clients that enumerate the tool list will now see 13 tools instead of 11.

**Migration**: none required.

---

### [next release] — D1 job contract: `progress` field on `JobResponse` and `QUEUED` status

**Surface**: `GET /jobs/{job_id}` REST response (`JobResponse`), `JobStatus` enum.

- `JobResponse` now includes an optional `progress` field (`dict | None`, default `null`). Existing callers that don't reference this field are unaffected; callers that use strict schema validation will now see `progress: null` for job types that don't set it.
- `JobStatus` now includes `QUEUED` as a valid value (between `PENDING` and `RUNNING`). Clients that exhaustively switch on status strings must add a `QUEUED` case.

**Migration**: no action needed for read-only callers. Add `QUEUED` to any exhaustive status switch/match.

---

### [next release] — C7 MCP Pydantic responses: field-narrowing on collection and context tools

**Surface**: MCP tools `list_collections`, `get_collections_meta`, `get_collection_meta`, `update_collection`, `search_with_context`.

All five tools previously returned raw `dataclasses.asdict()` payloads. They now validate return values through explicit Pydantic schemas before serialization. This removes internal/transient fields that were never part of the public contract and renames one field for consistency.

---

**`list_collections` — removed fields + field rename**:
- Removed from every item in the returned list: `centroid_sum`, `mutations_since_recompute`, `needs_recompute`, `described_at_doc_count`, `needs_reindex`, `reindex_job_id`, `namespace`.
- Field **renamed**: `active_embedding_model` → `embedding_model`. Clients that read `active_embedding_model` will receive `undefined`/`null` after this change — update to read `embedding_model`.
- Fields `centroid` and `description_embedding` were already stripped before C7; that behaviour is unchanged.

**Migration**: remove any references to the seven removed fields from client code. Replace `active_embedding_model` with `embedding_model`.

---

**`get_collections_meta` — removed fields + field rename + `description_embedding` is always present**:
- Same removals as `list_collections` above (`centroid_sum`, `mutations_since_recompute`, `needs_recompute`, `described_at_doc_count`, `needs_reindex`, `reindex_job_id`, `namespace`).
- Field **renamed**: `active_embedding_model` → `embedding_model`.
- `description_embedding` was previously absent when `include_description_embedding=false` (the default). It is now always present in the schema, serialized as `null` when not included. Strict-schema clients that reject unknown or null-valued fields must add `description_embedding: list[float] | null` to their type stubs.

**Migration**: replace `active_embedding_model` with `embedding_model`. Tolerate `description_embedding: null` in the default case, or explicitly request it with `include_description_embedding: true`.

---

**`get_collection_meta` — removed fields + field rename**:
- Removed: `centroid`, `centroid_sum`, `mutations_since_recompute`, `needs_recompute`, `described_at_doc_count`, `needs_reindex`, `reindex_job_id`, `namespace`.
- Field **renamed**: `active_embedding_model` → `embedding_model`.
- Previously `centroid` (a raw float vector) was included in the response. It is now absent entirely.
- `description_embedding` was added in B4 to this tool's response. It is now **removed**: `get_collection_meta` uses `CollectionDetailSchema`, which does not include this field. Clients that were reading `description_embedding` from `get_collection_meta` must switch to `get_collections_meta` with `include_description_embedding: true`.

**Migration**: replace `active_embedding_model` with `embedding_model`. Remove references to `centroid` and the internal fields listed above. If you need `description_embedding`, use `get_collections_meta` instead.

---

**`update_collection` — removed fields + field rename**:
- Same removals and rename as `get_collection_meta` above.

**Migration**: same as `get_collection_meta`.

---

**`search_with_context` — context chunk fields removed**:
- Context chunks in `context_before` and `context_after` previously included `start_offset`, `end_offset`, and `custom_score` (only `vector` was stripped in A1). These three transient fields are now excluded from all context chunk items.
- `language` is retained — it was present before C7 and remains in the schema. This is NOT a breaking change.

**Migration**: remove any client-side reads of `start_offset`, `end_offset`, and `custom_score` from context chunk items.

---

**New error code for schema drift**:
- All 11 MCP tools now return `{"error": "<message>", "code": "schema_validation_error"}` when a Pydantic schema validation error is raised during response construction (e.g., a domain dataclass field was added without updating the MCP schema). This replaces a potential silent shape change or unhandled exception.

**Migration**: add handling for `code == "schema_validation_error"` if your client distinguishes error codes.

**Announced in**: this release.

### [next release] — C5 RAG Fusion: additive fields on MCP tool return dicts

**Surface**: MCP tools `search`, `search_with_context`, and `explain`.

**Additive fields** (backward-compatible for tolerant consumers; breaking for strict schema validators):
- `search` and `search_with_context` return dicts gain `rag_fusion_applied: bool` (default `false`), `rag_fusion_queries_used: int` (default `0`), and `rag_fusion_attempted: bool` (default `false`).
- `explain` return dict gains `rag_fusion_applied: bool` (default `false`), `rag_fusion_queries_used: int` (default `0`), `rag_fusion_attempted: bool` (default `false`), `rag_fusion_failure_reason: str | null` (default `null`), and `rag_fusion_sub_queries: list[{variant_index, result_count, top_doc_ids}] | null` (default `null`).
- `search`, `search_with_context`, and `explain` tools each gain a new `rag_fusion: bool = false` parameter.

**Migration**: no action required for consumers that ignore unknown fields. Strict schema validators should add these fields to their tool return type stubs.

**Announced in**: this release.

### [next release] — C5 RAG Fusion: additive fields on ExplainResponse

**Surface**: `POST /explain` response (`ExplainResponse`); `POST /explain` request (`ExplainRequest`).

**Additive fields** (backward-compatible for tolerant consumers; breaking for strict schema validators):
- `ExplainRequest` gains `rag_fusion: bool` (default `false`). Clients that validate the request schema strictly must allow this new optional field.
- `ExplainResponse` gains `rag_fusion_applied: bool` (default `false`). Indicates whether the explain used RAG Fusion multi-query decomposition and result fusion.
- `ExplainResponse` gains `rag_fusion_queries_used: int` (default `0`). Number of successful LLM-generated variant searches (not counting the original query).
- `ExplainResponse` gains `rag_fusion_attempted: bool` (default `false`). Indicates whether RAG Fusion generation was attempted (true even if fallback to single-query occurred).
- `ExplainResponse` gains `rag_fusion_failure_reason: str | null` (default `null`). Populated with the error type when RAG Fusion was attempted but fell back.
- `ExplainResponse` gains `rag_fusion_sub_queries: list[RagFusionSubQueryResult] | null` (default `null`). Per-variant result summaries when RAG Fusion succeeded; each entry has `variant_index`, `result_count`, and `top_doc_ids`.

**Migration**: no action required for consumers that ignore unknown fields. Strict schema validators should add all six fields to their `ExplainResponse` and `ExplainRequest` type stubs.

**Announced in**: this release.

### [next release] — C5 RAG Fusion: additive fields on SearchResponse

**Surface**: `POST /search` response (`SearchResponse`); `POST /search` request (`SearchRequest`).

**Additive fields** (backward-compatible for tolerant consumers; breaking for strict schema validators):
- `SearchRequest` gains `rag_fusion: bool` (default `false`). Clients that validate the request schema strictly must allow this new optional field.
- `SearchResponse` gains `rag_fusion_applied: bool` (default `false`). Indicates whether the search used RAG Fusion multi-query decomposition and result fusion.
- `SearchResponse` gains `rag_fusion_queries_used: int` (default `0`). Number of successful LLM-generated variant searches (not counting the original query).
- `SearchResponse` gains `rag_fusion_attempted: bool` (default `false`). Indicates whether RAG Fusion generation was attempted (true even if fallback to single-query occurred).

**Migration**: no action required for consumers that ignore unknown fields. Strict schema validators should add these fields to their `SearchResponse` and `SearchRequest` type stubs.

**Announced in**: this release.

### [next release] — C4 HyDE query expansion: additive fields on SearchResponse and ExplainResponse

**Surface**: `POST /search` response (`SearchResponse`); `POST /search` request (`SearchRequest`); `POST /explain` response (`ExplainResponse`); `POST /explain` request (`ExplainRequest`).

**Additive fields** (backward-compatible for tolerant consumers; breaking for strict schema validators):
- `SearchRequest` gains `hyde: bool` (default `false`). Clients that validate the request schema strictly must allow this new optional field.
- `SearchResponse` gains `hyde_applied: bool` (default `false`). Indicates whether the ANN lookup was driven by a HyDE-generated hypothesis embedding. Clients that validate the response schema strictly must accept this new field.
- `ExplainRequest` gains `hyde: bool` (default `false`). Clients that validate the request schema strictly must allow this new optional field.
- `ExplainResponse` gains `hyde_applied: bool` (default `false`). Indicates whether the ANN lookup was driven by a HyDE-generated hypothesis embedding. Clients that validate the response schema strictly must accept this new field.

**Migration**: no action required for consumers that ignore unknown fields. Strict schema validators should add `hyde_applied: bool` to their `SearchResponse` and `ExplainResponse` type stubs, and `hyde: bool` to their `SearchRequest` and `ExplainRequest` type stubs.

**Announced in**: this release.

### [next release] — C4 HyDE query expansion: MCP `search_with_context` return type change

**Surface**: MCP `search_with_context` tool.

**Breaking change**: The `search_with_context` tool previously returned a bare `list[dict]` (a list of `{result, context_before, context_after}` dicts). It now returns a wrapper object `{"results": list[dict], "hyde_applied": bool}` where `results` carries the same list. This is a **true breaking change** for any MCP client that iterates the return value directly or accesses items by index.

**Migration**: Update consumers to access `response["results"]` instead of iterating the response directly. `response["hyde_applied"]` is the new HyDE status field.

**Announced in**: this release.

### [next release] — C2 language field type change (SearchResult, ScoredSearchCandidate, ExplainResult, ExplainNearMiss)

**Python**: `SearchResult.language`, `ScoredSearchCandidate.language`, `ExplainResult.language`, and `ExplainNearMiss.language` now return `""` (empty string) for legacy/untagged chunks instead of `None`. Update `if result.language is None` guards to `if result.language == ""`.

**REST/JSON**: The `language` field in search and explain responses now serializes as `""` (empty string) instead of `null`. OpenAPI clients must update their type stubs (`nullable: false`, `type: string`).

**Migration**: Replace `if result.language is None` with `if result.language == ""` in any code that checks for an untagged language. REST consumers should expect `""` instead of `null` for the `language` field in `SearchResultSchema`, `ExplainResult`, and `ExplainNearMiss`.

**Announced in**: this release.

### [next release] — C1 per-collection embedding model (schema changes)

**Surface**: REST `GET /collections/{name}` response (breaking rename); `GET /collections/` response (additive); `POST /collections/` request (additive); `PATCH /collections/{name}` (new endpoint); `POST /search` response (additive); MCP (new `update_collection` tool).

**`GET /collections/{name}` — breaking rename + additive fields**:
- `embedding_model` field **renamed** to `active_embedding_model`. Clients that read `embedding_model` will receive `null`/`undefined` — update to read `active_embedding_model`.
- Three new fields added: `pending_embedding_model` (nullable string), `needs_reindex` (bool), `reindex_job_id` (nullable string).

**`GET /collections/` — additive fields**:
- Each `CollectionSummary` entry gains `active_embedding_model: str` and `needs_reindex: bool`. Non-breaking for tolerant consumers.

**`POST /collections/` — additive optional request field**:
- Request body gains optional `embedding_model: str | null`. When provided, the collection is initialized with that model as `active_embedding_model`; when omitted, the global `config.embedding_model` is used. Unknown models return 422.

**`PATCH /collections/{name}` — new endpoint**:
- Updates the embedding model for a collection. Implements the per-collection model state machine: clearing `pending_embedding_model` (when setting the same model as active), setting `pending_embedding_model` (model change requiring reindex), or directly updating `active_embedding_model` (when no data exists yet). Returns `CollectionDetail` (same shape as `GET /collections/{name}`). Dimension validation runs before any state mutation. Returns 404 for unknown collections, 409 when a reindex job is already in progress, 422 for unknown models or dimension mismatches.

**`POST /search` — additive field**:
- `SearchResponse` gains `embedding_model: str`. For single-collection searches this is the collection's `active_embedding_model`; for multi-collection fan-out it is `config.embedding_model` (the global default).

**`PATCH /collections/{name}` — 422 conditions**:
- Returns `422` for unknown models **and** for dimension mismatches (when the requested model produces vectors of a different dimension than the existing index).

**MCP — new `update_collection` tool** (11th tool):
- Accepts `collection_name: str` and `embedding_model: str`. Implements the same state machine as `PATCH /collections/{name}`. Returns the updated `CollectionMeta` dict or `{error, code}` on failure.

**Migration**:
- `GET /collections/{name}` consumers: replace `embedding_model` with `active_embedding_model` in client code. Add handling for the three new fields (`pending_embedding_model`, `needs_reindex`, `reindex_job_id`).
- `GET /collections/` consumers: tolerate the new `active_embedding_model` and `needs_reindex` keys.
- `POST /search` consumers: tolerate the new `embedding_model` key.

**Announced in**: this release.

### [next release] — B5 incremental centroid maintenance (additive internal columns)

**Surface**: `_archon_collection_meta` LanceDB table (internal schema). No public REST or MCP contract change.

**Additive internal columns**:
- `centroid_sum_json` (`utf8`) — JSON-encoded element-wise sum of all chunk vectors; combined with `chunk_count` satisfies `centroid = centroid_sum / chunk_count`. Empty string when unset.
- `mutations_since_recompute` (`int64`) — incremented on every ingest/delete; reset to `0` after a full recompute. `-1` sentinel = pre-B5 row.
- `needs_recompute` (`bool`) — set `True` when incremental maintenance cannot proceed (model mismatch, NaN/Inf in vectors, missing seed sum). Cleared on successful `recompute_collection_meta`.

These columns are **not exposed** via any REST endpoint, MCP tool, or CLI command. They are internal routing metadata. Adding them is **non-breaking** for any public API consumer.

**Mixed-version deployment caveat**: an older binary's `update_collection_meta` uses its own schema (without the three new columns). If that binary performs a delete-then-insert upsert on a table that already carries the new columns, LanceDB's upsert semantics may null out the new columns on the affected row, which the B5 runtime treats identically to `needs_recompute = True` and recovers via a full recompute on next access. Do not run mixed-version deployments without first verifying your LanceDB version's actual upsert behavior. The safe upgrade path is to restart all instances to the same binary version before relying on incremental centroid accuracy.

**Migration**: none required for operators. The three columns are populated lazily on first ingest or reindex after upgrade. Existing collections are treated as `needs_recompute = True` until their first post-upgrade recompute.

**Announced in**: this release.

### [next release] — B4 hybrid collection routing

**Surface**: MCP `get_collection_meta`, `get_collections_meta`, and `list_collections` tools.

**`get_collection_meta` — additive output key (breaking for strict-validating clients)**:
- The single-collection `get_collection_meta` tool now returns `description_embedding: list[float] | null` on the `CollectionMeta` dict. The field is `null` when no embedding has been computed (e.g., immediately after upgrade before the startup migration runs). For **tolerant JSON consumers**: non-breaking additive key. For **strict-validating MCP clients** (`extra="forbid"` schemas): the new key is a true contract change — relax the client schema or handle the new field.

**`get_collections_meta` — additive optional INPUT parameter**:
- Gains an optional `include_description_embedding: bool = False` parameter. When `false` (the default), `description_embedding` is stripped from every `CollectionMeta` in the returned list. When `true`, the field is included. The `description_embedding` field was never present pre-B4, so stripping it by default is safe for existing consumers. Clients that reject unknown MCP tool input parameters may need to be updated to tolerate the new parameter.

**`list_collections` — no change for existing clients**:
- `list_collections` has always stripped `centroid` from the returned dict; it likewise strips `description_embedding`. No behavior change for existing clients — the field was never present.

**REST `/route` shape unchanged**: the routing strategy and description-weight blending are internal to `MultiCollectionRouter`; the `/route` response schema (`{pre_context, pinned_names, routable_names, decomposer_invoked}`) is unchanged.

**Migration**:
- `get_collection_meta` consumers using strict schema validation: add `description_embedding: list[float] | null` to your client-side schema, or relax to tolerate unknown fields.
- `get_collections_meta` consumers: no action needed — `description_embedding` is absent by default. To include it, pass `include_description_embedding: true`.
- To enable hybrid routing: set `[routing] routing_strategy = "hybrid"` and optionally tune `routing_description_weight` (default `0.3`) in `archon-search.toml`.

**Announced in**: this release.

### [next release] — B3 multi-collection search

**Surface**: REST `POST /search` and `POST /explain` (additive responses + request-schema change); MCP `search`/`explain` tools (additive response-shape change for strict clients).

**Request — additive/optional on both surfaces**:
- `POST /search` and the MCP `search` tool gain an optional `collections: list[str]` field for fanning out a single query across multiple collections in one request. `POST /explain` and the MCP `explain` tool gain the same field.

**Request-schema change (REST `/search`)**:
- `SearchRequest.collection` changes from required (`str`) to optional (`str | None = None`). Exactly one of `collection` / `collections` must be supplied. Existing clients that omit `collection` now receive a different 422 message (`"supply either collection or collections"` instead of Pydantic's `"field required"`); clients that explicitly send `collection: null` get the same new error. This is a request-schema behavioral change for clients that relied on the old validation message. The MCP `search` tool preserves its existing `default_collection` fallback when neither field is supplied.

**Response — additive keys**:
- Every result in `POST /search` / `POST /explain` (REST) and the `search`/`explain` MCP tools gains a `collection` key naming its origin collection. Each response gains an `excluded_collections` list (entries `{"name", "reason"}`) reporting collections dropped from a multi-collection request (e.g. embedding-model mismatch).
- For tolerant JSON consumers (REST), these are non-breaking additive keys. For **strict-validating MCP clients** (schemas with `extra="forbid"`), the new `collection` and `excluded_collections` keys are a true contract change — the same class as A1's additive-key break. Relax the client schema to tolerate the new keys.

**`/explain` multi-collection constraint**:
- `POST /explain` (and MCP `explain`) with `rerank=false` AND more than one collection returns 422 / a `validation_error` with message `"reranking cannot be disabled for multi-collection search in v1"`. Single-collection `rerank=false` is unchanged.

**Announced in**: this release.

### [next release] — B2 (additive): `GET /ready` endpoint and `readiness` field on `GET /status`

**Surface**: REST (additive — new endpoint and new field on existing endpoint).

**Changes**:
- **New `GET /ready` endpoint** (unauthenticated) — returns `{"ready": bool, "checks": {"storage": "ok"|"fail"}}`. HTTP 200 when the storage layer is connected; HTTP 503 with the same `ReadinessResponse` body when not. This is a readiness probe, not a liveness probe — `/health` (`{status, version}`) remains the liveness signal. Both endpoints are unauthenticated and listed in `_EXEMPT_PATHS`.
- **New `readiness` field on `GET /status` response** — `StatusResponse` gains an optional `readiness: ReadinessDetail | None` sub-object containing `storage_connected`, `embedder_warm`, `reranker_warm`, `jobs` (pending/running counts), `collections_indexing`, `collections_failed`, and `watcher.running`. This field is absent (`null`) when the server cannot populate it; tolerant consumers require no migration.

Both changes are **additive and backward-compatible**. Existing consumers of `GET /health`, `GET /status`, or any other endpoint are unaffected.

**Announced in**: this release.

### [next release] — B1 observability: `stage_timings_ms` on `POST /explain` and MCP `explain`

**Surface**: REST `POST /explain` (additive new field), MCP `explain` tool (additive new field in returned dict).

**Change**: When `[observability].stage_timings_enabled = true` (the default), the `ExplainResponse` returned by `POST /explain` and the `explain` MCP tool gains a `stage_timings_ms: dict[str, float]` field containing per-stage blocked-coroutine wall times in milliseconds. When `stage_timings_enabled = false`, the field is absent entirely (not `null`).

This is a **new optional field**; it does not appear on any other endpoint or MCP tool.

**Affected clients**:
- Tolerant JSON consumers (e.g., `response.json()["stage_timings_ms"]` with a `.get()` fallback): no migration needed.
- Pydantic models or other strict-schema validators with `extra="forbid"` on the `ExplainResponse` shape will **reject** responses when timings are enabled. Either relax the model or set `stage_timings_enabled = false` in `archon-search.toml`.

**Migration**:
- To suppress the field: set `[observability] stage_timings_enabled = false` in `~/.archon-search/archon-search.toml`.
- To tolerate it in strict validators: add `stage_timings_ms: dict[str, float] | None = None` to your client-side schema.

**Announced in**: this release.

### [next release] — A2 query-side filters

**Surface**: REST (additive, non-breaking for tolerant JSON consumers); MCP (additive, non-breaking — all new params are optional with sensible defaults).

**REST — additive**:
- `POST /search` request body gains an optional `filters` field (`SearchFilters` object, `null` = no filter). Existing clients that omit `filters` are unaffected.
- `SearchFilters` fields: `file_type`, `source_path_prefix`, `source_path_glob`, `indexed_after`, `indexed_before`, `language`, `include_metadata`.
- **C2 update**: `language` is now an active filter (ISO 639-1/639-3 code or `"unknown"`). Previously rejected with 422 for any non-empty value. Invalid values (not matching `[a-z]{2,3}` or `"unknown"`, or used with multi-collection fan-out) still return 422.

**MCP — additive**:
- `search` tool gains optional kwargs: `include_metadata` (bool, default `false`), `file_type`, `source_path_prefix`, `source_path_glob`, `indexed_after`, `indexed_before`, `language`. All default to `None`/`false`; existing callers passing only `query` and `collection` are unaffected.
- `search_with_context` tool gains the same optional kwargs.
- **C2 update**: `language` is now an active filter; valid ISO codes and `"unknown"` are accepted. Invalid values still return `{error: ..., code: "validation_error"}`.

**Operational — datetime normalization**:
- Date-range filters (`indexed_after`, `indexed_before`) compare lexicographically against the `indexed_at` column using fixed-width UTC format (`YYYY-MM-DDTHH:MM:SS.ffffffZ`). Collections indexed before A2 may contain variable-precision timestamps that produce incorrect date-range results. Run `archon-search collection reindex-metadata <name> --normalize-timestamps` (introduced in this release) to rewrite all rows to the fixed-width format. This command is offline-friendly and blocks only concurrent ingest to the same collection.

**Migration**:
- REST: no migration needed for existing clients — `filters` is optional.
- MCP: no migration needed — new kwargs are optional with defaults.
- Operators using date-range filters on pre-A2 collections: run `reindex-metadata --normalize-timestamps` before relying on date-range filter results.

**Announced in**: this release.

### [next release] — A4 explain endpoint (purely additive)

**Surface**: REST (new endpoint), MCP (new tool), telemetry enum, internal diagnostics, routing.

**Change**: entirely additive — no existing API surface is modified.

- **New `POST /explain` REST endpoint** — returns per-stage retrieval/reranking score breakdown plus optional routing decision. Authenticated identically to all other REST endpoints. All schemas use `extra="forbid"`; unknown request fields produce `422`.
- **New `explain` MCP tool** (10th tool) — same response structure as REST; returns `ExplainResponse.model_dump(mode="json", exclude_none=False)`. When `config` is absent from `create_app`, collectionless calls fall back to `default_collection`.
- **New `EndpointKind.explain` enum value** in `telemetry/entry.py` — additive to the `StrEnum`; no existing `EndpointKind` value is changed or removed.
- **New optional fields on `ScoredSearchCandidate`** (`_diagnostics.py`) — `acl: list[str] | None = None` plus A1/A2 metadata fields (`file_type`, `indexed_at`, `updated_at`, `ingested_by`, `language`, `metadata`). All defaulted; existing construction sites are unaffected.
- **Determinism improvement on `MultiCollectionRouter.rank()`** — equal-similarity entries now have a stable ascending-`name` tie-break. Previously the order was undefined for tied scores. This is a determinism fix, not a breaking change; callers that relied on undefined ordering may observe a different (now stable) ordering.

**Migration**: none required. All changes are purely additive. Existing `/search`, `/route`, and MCP tool consumers are unaffected.

### [next release] — A1 metadata schema v1

**Surface**: MCP (breaking for strict-validating clients), REST (additive, non-breaking for tolerant JSON consumers).

**MCP — truly breaking**:
- `search` tool: response result items gain `file_type`, `indexed_at`, `updated_at`, `ingested_by`, `metadata`, `acl`.
- `search_with_context` tool: result items gain the same six keys; `context_before` / `context_after` items gain `file_type`, `updated_at`, `ingested_by`, `metadata`, and **no longer include `vector`** (raw embeddings were never useful at the MCP boundary and inflated payload size by `~dim*4` bytes per neighbor).
- `list_documents`: items gain the same six keys when sourced from rows that carry them.

A1 is the **last** untyped MCP shape break before C7 wraps responses in Pydantic models.

**REST — additive (non-breaking)**:
- `/search` result items gain the same six keys. Tolerant JSON consumers see new fields appear; strict-schema consumers (e.g., generated clients pinned to the older OpenAPI snapshot) must regenerate. (There is no `/search/context` REST endpoint; the equivalent capability is the MCP `search_with_context` tool, listed separately in the MCP section above.)

**New 503 contract on `/ingest` and `/collections` (A5c)**:
- During an active reindex of the same collection, the store may raise `StoreBusyError` after a 30s lock-acquisition timeout. HTTP `POST /ingest` and `POST /collections` now return HTTP 503 with `Retry-After: 30` and `{"error": "store_busy", ...}` synchronously; ingest into a different collection succeeds normally. MCP `ingest_file` and `ingest_directory` surface `StoreBusyError` synchronously as `McpErrorResponse(code="store_busy")`. (A5c closes A1's deferred 503 surface.)

**`X-Ingested-By` header normalization**:
- Missing/empty → `"http"`.
- Canonical values (`cli`, `http`, `watcher`, `reindex`) pass through.
- Legacy `"archon-search-cli"` is normalized to `"cli"` at the boundary — clients that pass legacy and inspect the stored value will now see `"cli"`.
- Unknown values are coerced to `"http"` with a WARNING log (value truncated to 32 chars).

**Migration**:
- MCP consumers: regenerate types or relax strict-mode validation; the new keys are additive on every response item, never replace existing ones.
- REST consumers using tolerant JSON parsing: nothing to do. Strict-typed clients: regenerate from the updated OpenAPI.
- Existing collections: pre-A1 rows continue to read as-is via the read-boundary normalizer (`ingested_by` legacy → `"cli"`, empty `file_type` → `""`, `updated_at` falls back to `indexed_at`). To populate real values on pre-A1 rows, run `archon-search collection reindex-metadata <name>` (offline-friendly, blocks only `/ingest` to the same collection).

**Announced in**: this release. No prior deprecation — the impacted MCP shape was never documented as stable.

### [next release] — `POST /search` pipeline-exception behavior (CON-5 / A3)

**`POST /search` pipeline-exception behavior** (`routes_search.py`, CON-5 / A3):
- Pipeline stage exceptions (embedder, store, reranker) now return **HTTP 500** with a **plain-text** body `Internal Server Error` (Content-Type `text/plain`) instead of HTTP 200 with `{results: [], acl_filtered: false}`. The route bare-re-raises the exception; Starlette's `ServerErrorMiddleware` produces the default text response — this is **not** a JSON envelope. Callers MUST NOT call `.json()` on the 500 response body; doing so will raise a JSON decode error.
- A hung pipeline call now returns **HTTP 504** with `{"detail": "Search timed out"}` after ~30 s (matching the `/route` timeout contract).
- Migration: callers that treated an empty-results 200 as a pipeline-error signal must now handle HTTP 5xx. Callers that already treat 5xx as errors are unaffected.
- The `503` meta-lookup path (`get_collection_meta` raises) is unchanged.
- MCP `search` / `search_with_context` tools are unchanged.

### [next release] — A5a ingest path safety

**Surface**: MCP (behaviour change), REST (additive).

- MCP `ingest_file` and `ingest_directory` previously accepted paths containing `..` segments, empty strings, whitespace-only strings, NUL bytes, and non-absolute paths, and silently followed/resolved them. They now reject those inputs and return `McpErrorResponse(error=..., code="path_unsafe")` with an LLM-readable reason.
- HTTP `POST /collections` and `POST /jobs/ingest` gain a new `400` response (`ErrorDetail`, `detail` prefixed `"path is unsafe:"`) for the same input classes — additive (the `400` was not previously in the OpenAPI schema).

**Migration**: callers must pass absolute paths without `..` traversal. `path: null` (documents-only) ingest on `POST /jobs/ingest` is unaffected. Symlinks and absolute-path scope are intentionally NOT validated (deferred to a future `allowed_dirs` feature).

**Announced in**: this release. No prior deprecation — the silent-acceptance behaviour was never documented as stable.

### [next release] — MCP `search` tool response shape

**Surface**: MCP (`mcp.py` `search` tool)
**Change**: `search` tool now returns `{"results": [...], "acl_filtered": bool}` instead of `[{...}, {...}]` (bare list of result dicts).
**Migration**: Update consumers to access `response["results"]` instead of iterating the response directly. `response["acl_filtered"]` provides the ACL filter flag previously unavailable on the MCP surface.
**Announced in**: this release (no prior deprecation period — the old shape was never documented as stable).

### [next release] — REST `/search` per-request `top_k` no longer honored

**Surface**: REST (`/search` POST)
**Change**: The `top_k` field in `SearchRequest` is now ignored at the route level; the pipeline uses `config.top_k_return` instead. Previously, each request could specify its own `top_k`.
**Migration**: Configure `[search] top_k_return` in `archon-search.toml` to set the desired result count.
**Announced in**: this release (the behavior was supported but never documented as stable).

### [next release] — D3 migration tooling: new REST endpoints and `STORE_SCHEMA_VERSION` policy

**Surface**: two new REST endpoints on `routes_collections.py`; `GET /status` response (`StatusResponse`).

**New endpoints**:
- `GET /collections/{name}/migrations/pending` — returns `{collection, pending: [MigrationSpec], schema_version}`. Each `MigrationSpec` has `name`, `kind` (`"in_place"`, `"rewrite"`, or `"export_rebuild"`), `description`, and `introduced_at`. Returns `404` for unknown or cross-namespace collections.
- `POST /collections/{name}/migrate` — accepts `{backup_confirmed: bool, dry_run: bool}`. In-place-only migrations return `200` with `{migrations_applied: [name…]}` synchronously; no `MigrationJob` is created. Rewrite migrations require `backup_confirmed: true` (returns `422` without it) and return `202` with a `MigrationJob` job ID. `export_rebuild` migrations always return `422` (execution deferred to D5). `409` when a `ReindexJob` is active for the collection.

**`StatusResponse` additions**:
- `store_schema_version: int` — current `STORE_SCHEMA_VERSION` constant.
- `collections_schema_behind: int` — count of collections whose `schema_version < STORE_SCHEMA_VERSION`.

All changes are additive. Strict-schema clients will see new fields; tolerant clients are unaffected.

**`STORE_SCHEMA_VERSION` bump policy**: increment this constant whenever a structural change to `_schema()` (the shared chunk-table schema) or `_meta_schema()` (the collection-metadata schema) requires existing rows to be migrated. **Exception:** per-collection chunk-table-only changes (e.g. `migrate_acl`) do NOT require a version bump — only changes to the shared `_schema()` or `_meta_schema()` require it. Every bump must also add a corresponding `MigrationSpec` entry to `SearchStore._all_migrations()`. `STORE_SCHEMA_VERSION = 0` for D3 (all five formalised startup migrations have `introduced_at = 0`).

**Migration**: no action required for existing callers. Add the new endpoint paths to client schemas if strict validation is in use. Add `store_schema_version` and `collections_schema_behind` to `StatusResponse` type stubs.

---

### [next release] — D3 migration tooling: new nullable fields on `JobResponse` (BE-11)

**Surface**: REST (all endpoints that return `JobResponse`: `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/ingest`, `POST /jobs/export`, `POST /jobs/import`, `DELETE /jobs/{job_id}`, `POST /jobs/{id}/resume`)

**Change**: `JobResponse` gains three new nullable fields (default `null`):
- `kind: string | null` — migration sub-kind (`"in_place"`, `"rewrite"`, `"export_rebuild"`); `null` for all non-migration jobs
- `migrations_applied: string[] | null` — list of migration names applied by a `MigrationJob`; `null` for all non-migration jobs
- `backup_confirmed: boolean | null` — whether the operator confirmed a backup before a rewrite migration; `null` for all non-migration jobs

For tolerant JSON consumers: fully additive — no client changes required. For strict-validating REST clients (`extra="forbid"` schemas): the three new keys are a true contract change — relax the client schema or regenerate from the updated OpenAPI snapshot.

**Migration**: regenerate client types from `GET /openapi.json`. No behavior changes to existing job kinds.

**Announced in**: this release.
