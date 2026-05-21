# A4 — Explain / Debug Endpoint (Roadmap Item 12)
**Purpose**: Ship `POST /explain` (REST) and a parallel `explain` MCP tool that return the full per-stage score breakdown for a query (vector / FTS / RRF / reranker) plus the routing decision when no collection is pinned — turning today's opaque single `score` into a debuggable provenance object without leaking telemetry-grade information.
**Audience**: archon-search contributors implementing A4 and reviewers of the resulting PRs.
**Status**: Draft (revised after code-grounded review)

> **Order**: Ships AFTER A3 (search failure semantics). Pipeline-stage failures on `/explain` return HTTP **500** (was: 503). Aligns with A3's `/search` taxonomy — 503 is reserved for meta-lookup failures only. Supersedes the earlier "intentional divergence from `/search`'s silent swallow" framing in this plan: post-A3 `/search` no longer swallows pipeline-stage failures, so `/explain` is simply consistent, not divergent.

---

## Background

The hybrid pipeline computes vector ranks, FTS ranks, RRF fusion, reranker scores, and routing centroid similarities, then discards all of them and returns a single opaque `score` (`SearchResult.score` in `_types.py`). Debugging a wrong / missing / surprising result today requires reading the source of module-level `store._hybrid_search_with_trace` (`store.py:727`), `Reranker._rerank_with_trace` (`reranker.py:69`, signature `(self, query, candidates, top_k)`), and `MultiCollectionRouter.rank` (`router.py:127`), or reaching into eval-harness private paths.

The intermediate data already exists internally — every dataclass needed is in `archon_search/_diagnostics.py` (`SearchScoreBreakdown`, `ScoredSearchCandidate`) — but `_diagnostics` is **private and mutable**. Exposing it directly would couple the wire contract to an internal type and create silent breaking changes on every internal refactor.

**Retrieval pool**: `SearchPipeline.search()` (`pipeline.py:300-301`) calls `store.hybrid_search(..., top_k=self._top_k_retrieve)`. `self._top_k_retrieve` comes from `SearchConfig.top_k_retrieve` (default `15`). Inside `store.hybrid_search` (`store.py:471`) this is further amplified to `fetch = max(top_k * 3, 20)` per search leg. **`store._hybrid_search_with_trace` (`store.py:727+`) does NOT perform this amplification** — it passes `candidate_depth` straight to `.limit(candidate_depth)`. To keep the candidate pool identical between `/search` and `/explain`, the explain orchestration must replicate the amplification at the call site by passing `candidate_depth = max(self._top_k_retrieve * 3, 20)` to `hybrid_search_with_trace`. There is no dynamic `_retrieval_pool_size(top_k)` helper to extract; the amplification is computed inline in `SearchPipeline.explain`.

**Important `/search` constraint**: `/search` does not accept a request-level `top_k`. Its return slice is fixed to `self._top_k_return` (default `5`). The "top-k equality" contract between `/search` and `/explain` therefore only holds when `/explain` is called with `top_k == config.top_k_return`. For other `top_k` values, top-k equality is not contracted and structurally cannot be.

This plan implements `Documentation/Backlog/explain-endpoint-brief.md`. The brief's deviation from the roadmap line (`03_world_class_roadmap.md:51`) is intentional and documented: v1 ships **five of seven** breakdown fields. `matched_filters` lands additively with A4.1 once A2 ships; `expansions` lands additively with A4.2 once Phase B/C ships. **No placeholder fields.**

## Goal

A caller can `POST /explain` (or invoke the MCP `explain` tool) with `{query, collection?, top_k?, rerank?}` and receive:

- The top `top_k` results with full text + per-stage score breakdown (vector rank/score/kind, FTS rank/score/kind, RRF score, reranker score).
- Up to 20 near-miss candidates with score breakdowns but **no text field** (structural — the model has no such field).
- The routing decision (`routing.candidates`, all collections, **bypassing the confidence gate**) when `collection` is omitted; `routing: null` when pinned.
- An `acl_filtered: bool` mirroring `/search`.

When `rerank=true` AND `top_k == config.top_k_return`, the top-`top_k` slice of `/explain` and the result list of `/search` are guaranteed identical (same ordered `(doc_id, chunk_id)` pairs). Telemetry emits one entry per call with **no query text** and no result doc IDs — only `endpoint=explain`, `collection`, `latency_ms`, `result_count`.

---

## Scope

### In Scope
- New public Pydantic schemas in `archon_search/server/routes_explain.py` (co-located with the route, matching the `routes_search.py` pattern): `ExplainRequest`, `ExplainResponse`, `ExplainResult`, `ExplainNearMiss` (no `text` field), `ExplainScoreBreakdown`, `RoutingExplain`, `RoutingCandidate`. Mapping from internal `ScoredSearchCandidate` lives in a classmethod `ExplainResponse.from_pipeline_result(...)` (and helper classmethods on `ExplainResult`/`ExplainNearMiss`/`ExplainScoreBreakdown`), mirroring `SearchResultSchema.from_result` in `routes_search.py`. **No dedicated `explain_mapper.py` module.**
- New orchestration method `SearchPipeline.explain(query, collection, *, top_k, rerank, namespace, query_vector=None)` returning a structured result object (top-`top_k` slice, near-miss slice, ACL-filtered flag). Internally calls `_hybrid_search_with_trace` then `_rerank_with_trace`. To match the retrieval pool size of `SearchPipeline.search()`, the explain call site computes `candidate_depth = max(self._top_k_retrieve * 3, 20)` and passes it to `hybrid_search_with_trace` — the trace function does not amplify internally, so the amplification must be replicated here. Accepts an optional precomputed `query_vector` to avoid double-embedding on the collectionless flow (the route layer embeds once for routing and passes the vector through).
- Refactor inside `archon_search/router.py`: extract a private `_score_collections(query_embedding, collections) -> list[tuple[CollectionMeta, float | None]]` consumed by both the existing `rank()` and the new `rank_with_scores()`. As part of the refactor, `_score_collections` introduces an explicit alpha tie-break (ascending `collection.name`) that propagates to `rank()`. The previous behavior of `rank()` was that the order of equal-similarity entries was undefined; tightening this to a stable alphabetical tie-break is a determinism improvement, not a breaking change.
- New `MultiCollectionRouter.rank_with_scores(query_embedding, collections) -> list[tuple[CollectionMeta, float | None]]` that bypasses the confidence-threshold gate, returns every supplied collection, and is built on top of `_score_collections`.
- Expose `LanceStore.hybrid_search_with_trace(...)` as a thin instance method on the store class that delegates to the existing module-level `_hybrid_search_with_trace(...)`. The module-level function is not renamed and remains private; the instance method is the seam pipeline code calls.
- Extend `archon_search/_diagnostics.ScoredSearchCandidate` with `acl: list[str] | None = None` (defaulted, no breaking constructor change). `_hybrid_search_with_trace` populates it from the LanceDB row in the same way `SearchStore.hybrid_search` does. This is necessary so `apply_acl_filter` can operate on the trace-path candidate list.
- Extend `archon_search/_diagnostics.ScoredSearchCandidate` with the A1/A2 metadata fields needed for `/explain` response parity with `/search`: `file_type: str = ""`, `indexed_at: str = ""`, `updated_at: str = ""`, `ingested_by: IngestedBy = "cli"`, `language: str | None = None`, `metadata: dict[str, str] = field(default_factory=dict)` (all defaulted, no breaking constructor change). `_hybrid_search_with_trace` populates them from the LanceDB row in the same way `SearchStore.hybrid_search` does (mirroring A1's row-to-`SearchResult` mapping). Without this, `ExplainResult.from_candidate(...)` cannot populate the metadata fields the public schema declares.
- New `POST /explain` REST endpoint behind the existing bearer-auth middleware (`server/middleware_auth.py`).
- New `explain` MCP tool (10th tool) in `server/mcp.py`. Implements its own pipeline-call path independently, matching how the existing `search` MCP tool is implemented (no shared helper extracted across REST and MCP — they each call the pipeline themselves).
- Extension of `archon_search/telemetry/entry.py`: add `EndpointKind.explain`, add `TelemetryEntry.from_explain_result(*, collection, result_count, latency_ms)` factory (keyword-only, no `query` parameter — structural invariant), update `DOCUMENTED_SCHEMA_FIELDS` only if the factory introduces a field not already present (current set already covers every field used; verify via a model-fields equality test).
- OpenAPI surface: `/explain` materializes automatically in `GET /openapi.json` once the FastAPI route is wired and schemas are registered. Bearer security is auto-injected by `_configure_openapi` in `app.py`.
- Documentation: `Architecture/600_api_reference_or_public_interface.md` adds `/explain` REST + `explain` MCP tool with the JSON example from the brief. `BREAKING.md` is touched only to record that A4 is purely additive (no breaks).

### Out of Scope
- **`matched_filters` field** — A4.1, additive once A2 ships.
- **`expansions` field** — A4.2, additive once Phase B/C ships.
- **Per-stage timing breakdown** — separate observability concern (A4.3).
- **Persisting explain traces** — request/response only.
- **UI / visualization** — JSON only.
- **Explaining ingestion or chunking decisions** — retrieval-side only.
- **Refactoring `_rerank_with_trace` / `_hybrid_search_with_trace`** to drop the underscore prefix — they remain private; the public contract is the new Pydantic schemas, not the internal dataclasses.
- **CLI subcommand** `archon-search explain "query"` — deferred.
- **`/explain/route` routing-only variant** — deferred until/unless collectionless `/explain` cost becomes a complaint.
- **Adding `top_k` as a request parameter to `/search`** — out of scope; the equality contract is scoped to `top_k == config.top_k_return`.

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 5.1 — Final verification & documentation update].

---

## What does NOT change
- `/search` request/response schema and ordering.
- The `self._top_k_retrieve` retrieval pool size used by `SearchPipeline.search()` — `/explain` reproduces the identical post-amplification pool by passing `max(self._top_k_retrieve * 3, 20)` as `candidate_depth`.
- The private `_diagnostics.SearchScoreBreakdown` dataclass.
- The `_diagnostics.ScoredSearchCandidate` dataclass's existing fields and field order. **Additive defaulted fields are appended**: `acl: list[str] | None = None` (ACL filtering on the trace path), and the A1/A2 metadata fields `file_type: str = ""`, `indexed_at: str = ""`, `updated_at: str = ""`, `ingested_by: IngestedBy = "cli"`, `language: str | None = None`, `metadata: dict[str, str] = field(default_factory=dict)` (response parity with `/search`). No existing field changes.
- The `_rerank_with_trace` / `_hybrid_search_with_trace` names (underscore prefix stays). The module-level `_hybrid_search_with_trace` keeps its signature; the new `LanceStore.hybrid_search_with_trace` is a thin instance-method delegate.
- The public behavior of `MultiCollectionRouter.rank()` aside from a tightened alphabetical tie-break for equal-similarity entries (a determinism improvement; previously undefined order). `rank()` still applies the confidence-threshold gate and `shortlist_size` truncation.
- The `query`-less telemetry invariant — `TelemetryEntry.from_explain_result` accepts no `query` kwarg and `TelemetryEntry` itself has no field that can carry one.
- The `Bearer` auth requirement on every endpoint except `GET /health`.
- The `--cov-fail-under=85` gate on the default pytest run.
- Telemetry's `extra="forbid"` posture in `TelemetryEntry`.

---

## Known limitations / accepted trade-offs
- **Raw FTS score availability** depends on LanceDB exposing `_score` on FTS rows. `store.py:781-782` already reads it defensively; when absent `fts_score: null` is surfaced and `fts_rank` is kept. This is the documented fallback, not a bug.
- **`vector_score_kind = "distance"`** is surfaced verbatim — it is a similarity-derived distance for LanceDB cosine indexes (lower is closer). The schema docstring documents this; the value is **not** transformed to `"cosine"`.
- **`source_path` is included in every result and near-miss** — matches `/search` today; not a new exposure.
- **Near-miss pool size depends on the underlying retrieval pool.** The pool is `max(self._top_k_retrieve * 3, 20)` (from `store.hybrid_search`). With the default `top_k_retrieve=15` the pool size is `45`. The near-miss list is `sorted_candidates[top_k : top_k + 20]`, capped at 20.
- **Telemetry on `/explain` is intentionally minimal.** No query text, no result doc IDs — `result_count` is a scalar to avoid path leakage given that `doc_id` is path-derived.
- **MCP `explain` tool namespace**: like the existing MCP `search` tool, the MCP `explain` tool operates against `DEFAULT_NAMESPACE` only. The MCP server has no namespace-routing surface in v1. AC11 (ACL parity) is therefore scoped to REST.
- **No shared `_run_explain` helper between REST and MCP**: each handler calls `SearchPipeline.explain` and `ExplainResponse.from_pipeline_result` directly, matching the existing `search` tool / `/search` divergence in the codebase. Drift is caught by the REST↔MCP parity test (AC2).
- **Direct in-process call instead of HTTP self-call for routing**: collectionless `/explain` reads `all_collections_meta` via `pipeline.get_all_collections_meta(namespace=ns)` (which namespace-filters in `pipeline.py:355`; the underlying `store.get_all_collections_meta()` does NOT filter by namespace) directly, **not** via `MultiCollectionRouter.fetch_metadata()`'s HTTP self-call. This diverges from `routes_route.py`'s approach but is cheaper and avoids thread-pool exhaustion risk. Documented divergence; if `/route` ever moves to a direct call, the two converge.

---

## Architecture

### Modules touched / added

- `archon_search/_diagnostics.py` — **change**. Append defaulted fields to `ScoredSearchCandidate`: `acl: list[str] | None = None`, `file_type: str = ""`, `indexed_at: str = ""`, `updated_at: str = ""`, `ingested_by: IngestedBy = "cli"`, `language: str | None = None`, `metadata: dict[str, str] = field(default_factory=dict)`. All defaulted, so existing constructors continue to work.
- `archon_search/store.py` — **change**. (a) Populate `ScoredSearchCandidate.acl` and the A1/A2 metadata fields (`file_type`, `indexed_at`, `updated_at`, `ingested_by`, `language`, `metadata`) from the LanceDB row inside `_hybrid_search_with_trace` (mirrors how `SearchStore.hybrid_search` reads them — including the `_normalize_ingested_by` boundary and `parse_metadata()` on the raw JSON string, per A1). (b) Add `LanceStore.hybrid_search_with_trace(self, collection, query_vector, query_text, candidate_depth)` thin instance method that delegates to the module-level `_hybrid_search_with_trace(self, ...)`.
- `archon_search/router.py` — **change**. Extract private `_score_collections(query_embedding, collections) -> list[tuple[CollectionMeta, float | None]]` shared by `rank()` and `rank_with_scores()`. The helper sorts scored entries by descending score with **ascending `collection.name` tie-break**, and appends unscored (None-centroid / mismatched-model) entries last in ascending-name order. `rank()` keeps its confidence-gate and `shortlist_size` truncation. `rank_with_scores()` adds neither.
- `archon_search/pipeline.py` — **change**. Add `ExplainPipelineResult` dataclass and `async SearchPipeline.explain(query, collection, *, top_k, rerank, namespace, query_vector=None) -> ExplainPipelineResult`.
- `archon_search/telemetry/entry.py` — **change**. Add `EndpointKind.explain`. Add classmethod `TelemetryEntry.from_explain_result(*, collection, result_count, latency_ms)`. Verify `DOCUMENTED_SCHEMA_FIELDS == set(TelemetryEntry.model_fields)` post-change.
- `archon_search/server/routes_explain.py` — **new**. Defines all explain schemas (co-located, matching the `routes_search.py` pattern), the `from_pipeline_result` classmethod, and the `POST /explain` handler.
- `archon_search/server/app.py` — **change**. Register `routes_explain.router` alongside the other routers.
- `archon_search/server/mcp.py` — **change**. Register the `explain` MCP tool (10th). Tool body imports `ExplainRequest`, `ExplainResponse`, `SearchPipeline.explain` and `MultiCollectionRouter.rank_with_scores` directly; returns `response.model_dump(mode="json", exclude_none=False)`.

### `extra="forbid"` decision

All new Explain schemas use `model_config = ConfigDict(extra="forbid")`. This diverges from existing schemas in `schemas.py` (`SearchRequest`, `RouteRequest`, etc.) which omit it. Rationale, documented in `routes_explain.py` module docstring: `/explain` is a new endpoint with no legacy clients, and `forbid` makes contract violations loud rather than silent — desirable for a debug endpoint.

### Public signatures

```python
# archon_search/server/routes_explain.py

class ExplainScoreBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vector_rank: int | None
    vector_score: float | None
    vector_score_kind: str | None  # "distance" for LanceDB cosine — see docstring
    fts_rank: int | None
    fts_score: float | None
    fts_score_kind: str | None     # "bm25" when raw score present
    rrf_score: float
    reranker_score: float | None   # null when rerank=false

    @classmethod
    def from_breakdown(cls, b: SearchScoreBreakdown) -> "ExplainScoreBreakdown": ...


class ExplainResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id: str
    chunk_id: str
    source_path: str
    text: str
    # `score` is computed as `breakdown.reranker_score` when non-None, else
    # `breakdown.rrf_score` — populated by the `from_candidate` classmethod,
    # not provided by the caller. Same convention on ExplainNearMiss.
    score: float
    breakdown: ExplainScoreBreakdown
    # Metadata parity with SearchResult (A1+A2). Without these,
    # filter-exclusion debugging via /explain is impossible.
    # Nullability mirrors A1's SearchResult exactly so a client deserializing
    # both /search and /explain sees identical types for identical field names.
    file_type: str = ""
    indexed_at: str = ""
    updated_at: str = ""
    ingested_by: IngestedBy = "cli"
    language: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_candidate(cls, c: ScoredSearchCandidate) -> "ExplainResult":
        """Populates the metadata fields (file_type, indexed_at, updated_at,
        ingested_by, language, metadata) from the candidate row in addition
        to doc_id/chunk_id/source_path/text/score/breakdown."""
        ...


class ExplainNearMiss(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id: str
    chunk_id: str
    source_path: str
    score: float
    breakdown: ExplainScoreBreakdown
    # Metadata parity with SearchResult (A1+A2) — same fields as ExplainResult
    # so filter-exclusion debugging works on near-misses too. Nullability
    # mirrors A1's SearchResult exactly (non-optional strings with empty
    # defaults; language/metadata stay optional).
    file_type: str = ""
    indexed_at: str = ""
    updated_at: str = ""
    ingested_by: IngestedBy = "cli"
    language: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    # NOTE: no `text` field. Absence is structural.

    @classmethod
    def from_candidate(cls, c: ScoredSearchCandidate) -> "ExplainNearMiss":
        """Same metadata population as ExplainResult.from_candidate; no `text`."""
        ...


class RoutingCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collection: str
    centroid_score: float | None  # null for mismatched-model / no-centroid collections


class RoutingExplain(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invoked: bool
    chosen_collection: str
    confidence_threshold: float        # the threshold rank() would have applied
    chosen_below_threshold: bool       # True when chosen.centroid_score < confidence_threshold
    candidates: list[RoutingCandidate]


class ExplainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    collection: str | None = None
    top_k: int = Field(default=5, ge=1, le=100)
    rerank: bool = True

    @field_validator("query")
    @classmethod
    def _query_nonempty(cls, v: str) -> str: ...

    @field_validator("collection")
    @classmethod
    def _collection_nonempty(cls, v: str | None) -> str | None: ...


class ExplainResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rerank: bool
    routing: RoutingExplain | None
    collection: str
    acl_filtered: bool
    results: list[ExplainResult]
    near_misses: list[ExplainNearMiss]

    @classmethod
    def from_pipeline_result(
        cls,
        *,
        rerank: bool,
        collection: str,
        routing: RoutingExplain | None,
        result: "ExplainPipelineResult",
    ) -> "ExplainResponse": ...


# archon_search/router.py
class MultiCollectionRouter:
    def _score_collections(
        self,
        query_embedding: list[float],
        collections: list[CollectionMeta],
    ) -> list[tuple[CollectionMeta, float | None]]:
        """Shared scoring + tie-break helper. Used by rank() and rank_with_scores()."""

    def rank_with_scores(
        self,
        query_embedding: list[float],
        collections: list[CollectionMeta],
    ) -> list[tuple[CollectionMeta, float | None]]: ...


# archon_search/pipeline.py
@dataclass
class ExplainPipelineResult:
    top_results: list[ScoredSearchCandidate]
    near_misses: list[ScoredSearchCandidate]
    acl_filtered: bool


class SearchPipeline:
    async def explain(
        self,
        query: str,
        collection: str,
        *,
        top_k: int = 5,
        rerank: bool = True,
        namespace: str = DEFAULT_NAMESPACE,
        query_vector: list[float] | None = None,
    ) -> ExplainPipelineResult: ...


# archon_search/store.py
class LanceStore:
    async def hybrid_search_with_trace(
        self,
        collection: str,
        query_vector: list[float],
        query_text: str,
        candidate_depth: int,
    ) -> list[ScoredSearchCandidate]:
        """Thin delegate to module-level _hybrid_search_with_trace."""


# archon_search/telemetry/entry.py
class EndpointKind(StrEnum):
    search = "search"
    search_with_context = "search_with_context"
    route = "route"
    explain = "explain"  # NEW


class TelemetryEntry:
    @classmethod
    def from_explain_result(
        cls,
        *,
        collection: str,
        result_count: int,
        latency_ms: float,
    ) -> TelemetryEntry: ...
    # No `query` parameter. Structural invariant.
```

### Why `RoutingExplain` ≠ `RouteResponse`

`/route` and `/explain` answer different questions and intentionally use different shapes:
- `/route`'s `RouteResponse` returns the **selected shortlist** after gating, plus the decomposer side-effects. It's optimised for the orchestration use case.
- `/explain`'s `RoutingExplain` returns **every candidate** with its centroid score, **including collections the confidence gate would have filtered out**, plus the threshold itself and a `chosen_below_threshold` flag. It's optimised for "why did routing pick X?".

### Data flow (collectionless request)

```
POST /explain {query, top_k, rerank}
  └─> routes_explain.explain
        ├─ all_meta = await pipeline.get_all_collections_meta(namespace=ns)
        │     (namespace-filtered in pipeline.py:355; DIRECT call — no HTTP self-call)
        ├─ if empty → 404 {"detail": "no collections available"}
        ├─ vector = await pipeline._embedder.embed_one(query)
        ├─ col_router = MultiCollectionRouter(...)            (built locally)
        │     ranked = col_router.rank_with_scores(vector, all_meta)
        │     (confidence gate BYPASSED, all collections returned)
        ├─ ranked = [(m, s) for m, s in ranked if acl_allows(principal, m)]
        │     (ACL filter — disallowed collections must not leak into routing.candidates)
        ├─ if not ranked → 404 {"detail": "no collections available"}
        ├─ chosen_collection = ranked[0][0].name              (alpha tie-break)
        ├─ pipeline.explain(query, chosen_collection, top_k, rerank,
        │                   namespace=ns, query_vector=vector)
        │     (vector passed through to avoid double-embed)
        ├─ routing = RoutingExplain(
        │     invoked=True,
        │     chosen_collection=chosen_collection,
        │     confidence_threshold=col_router._confidence_threshold,
        │     chosen_below_threshold=(ranked[0][1] is not None
        │                              and ranked[0][1] < col_router._confidence_threshold),
        │     candidates=[RoutingCandidate(collection=m.name, centroid_score=s)
        │                 for m, s in ranked],
        │   )
        ├─ response = ExplainResponse.from_pipeline_result(
        │     rerank=body.rerank, collection=chosen_collection,
        │     routing=routing, result=pipeline_result,
        │   )
        ├─ telemetry_writer.enqueue(TelemetryEntry.from_explain_result(...))
        └─ return response
```

### Determinism contract
- `results` sorted by `score` descending, tied by `(doc_id, chunk_id)` ascending.
- `near_misses` sorted by `score` descending, tied by `(doc_id, chunk_id)` ascending.
- `routing.candidates` sorted by `centroid_score` descending (None last), tied by `collection` ascending — produced by the shared `_score_collections` helper, so `rank()` and `rank_with_scores()` agree on tie-breaks.

---

## Task breakdown

### Phase 1 — Public schemas & telemetry surface
> **Releasable**: after Task 1.2 the public schemas and telemetry factory exist standalone — importable, validating, and unit-tested. Nothing user-callable yet.

#### Task 1.1 — Extend telemetry entry with `explain`
- [ ] **File**: `archon_search/telemetry/entry.py`
- **Depends on**: nothing
- **Description**:
  - Add `explain = "explain"` to `EndpointKind`.
  - Add classmethod `TelemetryEntry.from_explain_result(*, collection: str, result_count: int, latency_ms: float) -> TelemetryEntry`. Body constructs an entry with `status="ok"`, `endpoint="explain"`, no `result_doc_ids`. Keyword-only, no `*args`, no `query` parameter.
  - Verify `DOCUMENTED_SCHEMA_FIELDS == set(TelemetryEntry.model_fields)` post-change.
  - Do not modify `TelemetryEntry.model_config` (`extra="forbid"`, `frozen=True` stays).
- **Releasable**: after this task, telemetry can serialize an explain entry; the structural no-`query` invariant is preserved.
- **Tests (TDD)** — `tests/test_telemetry_entry.py` (extend existing file):
  - Unit: `test_endpoint_kind_includes_explain` — `EndpointKind("explain") == EndpointKind.explain`.
  - Unit: `test_from_explain_result_returns_valid_entry` — call with `collection="docs", result_count=3, latency_ms=12.5`; assert `endpoint == "explain"`, `status == "ok"`, `collection == "docs"`, `result_count == 3`, `result_doc_ids is None`, `query_id` is a hex string.
  - Unit: `test_from_explain_result_rejects_query_kwarg` — `with pytest.raises(TypeError): TelemetryEntry.from_explain_result(query="x", collection="docs", result_count=0, latency_ms=1.0)`.
  - Unit: `test_from_explain_result_is_keyword_only` — `inspect.signature(TelemetryEntry.from_explain_result).parameters["collection"].kind == KEYWORD_ONLY`.
  - Unit: `test_from_explain_result_signature_has_no_query_param` — `assert "query" not in inspect.signature(TelemetryEntry.from_explain_result).parameters`.
  - Unit: `test_documented_schema_fields_equals_model_fields_after_change` — `assert set(TelemetryEntry.model_fields.keys()) == DOCUMENTED_SCHEMA_FIELDS`.
  - Checkpoint: `uv run pytest tests/test_telemetry_entry.py -v`

#### Task 1.2 — Public explain schemas in `routes_explain.py`
- [ ] **File**: `archon_search/server/routes_explain.py` (new — schema-only stub at this stage; route handler added in Task 3.1)
- **Depends on**: nothing
- **Description**:
  - Create `archon_search/server/routes_explain.py` with the seven public models listed in the Architecture section, plus the `from_pipeline_result` / `from_candidate` / `from_breakdown` classmethods. All `extra="forbid"`. Co-locating schemas with the route matches the `routes_search.py` pattern.
  - `ExplainRequest.query` must reject empty / whitespace-only strings via a `field_validator` mirroring `routes_search.SearchRequest.query_nonempty` (no `min_length=1` on the `Field`; the validator is the sole guard — matches `/search`). `top_k` constrained `ge=1, le=100`.
  - `ExplainNearMiss` must NOT declare a `text` field at any point.
  - `from_candidate` on `ExplainResult` / `ExplainNearMiss` computes final `score`: `breakdown.reranker_score` when non-`None`, else `breakdown.rrf_score`.
  - `from_pipeline_result` does not re-sort — it consumes the pipeline result in given order.
  - Module docstring states: "This module is the only seam between `archon_search._diagnostics` and the public wire schema. Adding fields here is a public-contract change."
  - **Do not** register the FastAPI router here yet (the route handler arrives in Task 3.1). An empty `router = APIRouter()` is fine.
- **Releasable**: after this task, callers can `from archon_search.server.routes_explain import ExplainResponse` and build a response from an `ExplainPipelineResult`.
- **Tests (TDD)** — `tests/server/test_routes_explain_schemas.py` (new file):
  - Unit: `test_explain_request_accepts_minimal_payload` — `ExplainRequest(query="foo")` yields defaults `collection=None, top_k=5, rerank=True`.
  - Unit: `test_explain_request_rejects_empty_query` — empty / whitespace `query` raises `ValidationError`.
  - Unit: `test_explain_request_rejects_top_k_out_of_range` — `top_k=0` and `top_k=101` raise.
  - Unit: `test_explain_request_forbids_extra_fields` — unknown kwarg raises.
  - Unit: `test_explain_near_miss_has_no_text_field` — `"text" not in ExplainNearMiss.model_fields`.
  - Unit: `test_explain_near_miss_rejects_text_in_payload` — constructing with a `text=` kwarg raises.
  - Unit: `test_explain_response_serializes_routing_null_when_pinned` — build a response with `routing=None`; assert `model_dump(mode="json", exclude_none=False)["routing"] is None`.
  - Unit: `test_explain_response_does_not_include_query_field` — `"query" not in ExplainResponse.model_fields`.
  - Unit: `test_from_candidate_uses_reranker_score_when_present` — candidate with `reranker_score=0.91`, `rrf_score=0.03` → `ExplainResult.from_candidate(...).score == 0.91`.
  - Unit: `test_from_candidate_uses_rrf_score_when_reranker_none` — `reranker_score=None`, `rrf_score=0.03` → `score == 0.03`.
  - Unit: `test_from_candidate_near_miss_strips_text` — candidate with `text="leak"`; output dict has no `text` key.
  - Unit: `test_from_pipeline_result_preserves_order` — feed an `ExplainPipelineResult` whose internal lists are pre-sorted; assert response lists have identical `(doc_id, chunk_id)` order.
  - Unit: `test_from_pipeline_result_routing_passthrough` — supplied `RoutingExplain` is returned verbatim; `routing=None` → `response.routing is None`.
  - Unit: `test_from_pipeline_result_acl_filtered_flag_passthrough` — `acl_filtered=True` → response field `True`.
  - Unit: `test_from_pipeline_result_empty_results_and_near_misses` — empty inputs yield empty lists, not `None`.
  - Unit: `test_explain_response_round_trips_brief_example` — parse the JSON example from `explain-endpoint-brief.md` into `ExplainResponse` and re-dump; assert key set equality.
  - Checkpoint: `uv run pytest tests/server/test_routes_explain_schemas.py -v`

---

### Phase 2 — Diagnostics extension, router refactor & pipeline orchestration
> **Releasable**: after Task 2.3 the pipeline can compute an explain trace end-to-end against a real LanceDB collection. Still no HTTP surface.

#### Task 2.1 — Extend `ScoredSearchCandidate` with ACL and expose store delegate
- [ ] **Files**: `archon_search/_diagnostics.py`, `archon_search/store.py`
- **Depends on**: nothing
- **Description**:
  - Append `acl: list[str] | None = None` to `ScoredSearchCandidate`.
  - In module-level `_hybrid_search_with_trace`, populate `acl=row.get("acl")` on each constructed candidate (same field name LanceDB stores via `SearchStore.hybrid_search`).
  - Add `LanceStore.hybrid_search_with_trace(self, collection, query_vector, query_text, candidate_depth)` thin instance method that does `return await _hybrid_search_with_trace(self, collection, query_vector, query_text, candidate_depth)`.
- **Releasable**: after this task, `apply_acl_filter` can operate directly on the trace-path candidate list.
- **Tests (TDD)**:
  - Unit (`tests/test_diagnostics.py`, new or extended): `test_scored_search_candidate_acl_defaults_to_none` — construct without `acl`; field is `None`.
  - Unit: `test_scored_search_candidate_accepts_acl_list` — construct with `acl=["ns1"]`; field round-trips.
  - Integration (`tests/test_store_trace.py`, new or extended): `test_hybrid_search_with_trace_populates_acl_on_candidates` — fixture collection with ACL'd chunks; assert every returned candidate's `acl` matches the LanceDB row.
  - Unit (`tests/test_store_trace.py`): `test_lance_store_hybrid_search_with_trace_delegates_to_module_function` — patch the module-level function; call the instance method; assert the patch was invoked with the same arguments and the result is propagated.
  - Checkpoint: `uv run pytest tests/test_diagnostics.py tests/test_store_trace.py -v`

#### Task 2.2 — Router refactor + `rank_with_scores`
- [ ] **File**: `archon_search/router.py`
- **Depends on**: nothing
- **Description**:
  - Extract `_score_collections(self, query_embedding, collections) -> list[tuple[CollectionMeta, float | None]]`. Iterates collections; for each, computes cosine similarity if `centroid is not None and embedding_model == self._embedding_model`, else pairs with `None`. Returns scored entries sorted by descending score with ascending `meta.name` tie-break; unscored entries appended in ascending-name order.
  - Refactor `rank()` to call `_score_collections`, then apply the existing confidence-gate / `shortlist_size` truncation behavior.
  - New `rank_with_scores(self, query_embedding, collections) -> list[tuple[CollectionMeta, float | None]]` returns the full output of `_score_collections` unchanged — no confidence gate, no truncation.
  - Docstring on `rank_with_scores`: "Unlike `rank()`, this method bypasses the confidence-threshold gate and does not truncate to `shortlist_size`. Intended for `/explain` only."
  - Document the tie-break tightening on `rank()` in its docstring: "Equal-similarity entries are sorted by `meta.name` ascending (previously undefined)."
- **Releasable**: after this task, `/explain` can list every candidate collection with its centroid score regardless of routing confidence.
- **Tests (TDD)** — `tests/test_router.py` (extend existing file):
  - Unit: `test_rank_with_scores_returns_all_collections` — 5 collections with varied centroids; result length == 5.
  - Unit: `test_rank_with_scores_bypasses_confidence_gate` — `confidence_threshold=0.99` such that `rank()` returns `[]`; `rank_with_scores()` returns all collections sorted.
  - Unit: `test_rank_with_scores_handles_mismatched_model` — collection with mismatched `embedding_model` paired with `None` score, placed after scored entries.
  - Unit: `test_rank_with_scores_handles_none_centroid` — collection with `centroid=None` → `None` score, placed last.
  - Unit: `test_rank_with_scores_alphabetical_tie_break` — two collections with identical computed similarity → sorted by `name` ascending.
  - Unit: `test_rank_with_scores_does_not_truncate_to_shortlist` — `shortlist_size=2`, provide 5 collections → returns 5.
  - Unit: `test_rank_uses_alpha_tie_break` — regression / behavior-tightening: two collections with identical similarity → `rank()` returns them in ascending `name` order.
  - Unit: `test_rank_preserves_confidence_gate_after_refactor` — existing confidence-gate tests still pass against the refactored `rank()`.
  - Unit: `test_rank_preserves_shortlist_truncation_after_refactor` — `shortlist_size=2`, 5 collections → `rank()` returns 2.
  - Checkpoint: `uv run pytest tests/test_router.py -v`

#### Task 2.2.5 — Thread A1/A2 metadata fields through `ScoredSearchCandidate` and `_hybrid_search_with_trace`
- [ ] **Files**: `archon_search/_diagnostics.py`, `archon_search/store.py`
- **Depends on**: Task 2.1 (which already touches both files for the `acl` field; this task extends that change)
- **Description**:
  - In `_diagnostics.py`: append the following defaulted fields to `ScoredSearchCandidate` (after the `acl` field added by Task 2.1): `file_type: str = ""`, `indexed_at: str = ""`, `updated_at: str = ""`, `ingested_by: IngestedBy = "cli"` (import `IngestedBy` from `archon_search._types`), `language: str | None = None`, `metadata: dict[str, str] = field(default_factory=dict)` (import `field` from `dataclasses`). All defaulted — no breaking constructor change.
  - In `store.py` module-level `_hybrid_search_with_trace`: when constructing each `ScoredSearchCandidate`, populate these new fields from the LanceDB row dict using the same idioms A1 introduced in `SearchStore.hybrid_search`'s row-to-`SearchResult` mapping:
    - `file_type=row.get("file_type") or ""`
    - `indexed_at=row.get("indexed_at") or ""`
    - `updated_at=row.get("updated_at") or row.get("indexed_at") or ""` (preserves A1's fallback)
    - `ingested_by=_normalize_ingested_by(row.get("ingested_by"))` (reuse the A1 helper — never let the legacy `"archon-search-cli"` value leak into the trace path)
    - `language=row.get("language")`
    - `metadata=parse_metadata(row.get("metadata") or "{}")` (parses the stored JSON string into a `dict`)
  - Rationale: without this, `ExplainResult.from_candidate(...)` cannot populate the metadata fields the public schema declares. The whole point of A4's metadata parity with `/search` is to make filter-exclusion debugging via `/explain` possible.
  - **Do not** add any new normalization or transform; this task is a faithful mirror of A1's existing row-to-result mapping applied to the trace dataclass.
- **Releasable**: after this task, every `ScoredSearchCandidate` produced by `_hybrid_search_with_trace` carries the same metadata an A1 `SearchResult` does. The Pydantic schemas in Task 1.2 already declare matching fields; the pipeline orchestration in Task 2.3 consumes them unchanged.
- **Tests (TDD)** — extend `tests/test_store_trace.py`:
  - Integration (`@pytest.mark.integration`, real LanceDB temp dir): `test_hybrid_search_with_trace_populates_file_type` — fixture row with `file_type="py"`; assert every returned candidate's `file_type == "py"`.
  - Integration: `test_hybrid_search_with_trace_populates_indexed_at` — fixture row with a known `indexed_at` ISO string; assert it propagates verbatim.
  - Integration: `test_hybrid_search_with_trace_updated_at_falls_back_to_indexed_at` — fixture row with `updated_at=""`; assert candidate's `updated_at == indexed_at`.
  - Integration: `test_hybrid_search_with_trace_populates_ingested_by` — fixture row with `ingested_by="watcher"`; assert candidate's `ingested_by == "watcher"`.
  - Integration: `test_hybrid_search_with_trace_normalizes_legacy_ingested_by` — write a row directly with `ingested_by="archon-search-cli"`; assert returned candidate's `ingested_by == "cli"` (boundary normalization parity with A1's `hybrid_search`).
  - Integration: `test_hybrid_search_with_trace_populates_language` — fixture row with `language="en"`; assert it propagates. Also test a row with no `language` column / `None` value; assert candidate's `language is None`.
  - Integration: `test_hybrid_search_with_trace_metadata_is_parsed_dict` — fixture row with `metadata='{"k":"v"}'` (JSON string as stored); assert `candidate.metadata == {"k": "v"}` (parsed, not the raw string).
  - Unit: `test_scored_search_candidate_metadata_fields_default` — construct `ScoredSearchCandidate` with only required args; assert `file_type == ""`, `indexed_at == ""`, `updated_at == ""`, `ingested_by == "cli"`, `language is None`, `metadata is None`. Pins the defaults so direct test construction stays cheap.
  - Checkpoint: `uv run pytest tests/test_diagnostics.py tests/test_store_trace.py -v`

#### Task 2.3 — `SearchPipeline.explain` orchestration
- [ ] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 1.2 (consumer schemas exist), Task 2.1 (store delegate + ACL field), Task 2.2 (router method available; pipeline does not call it directly but the route handler in Phase 3 does), Task 2.2.5 (metadata fields populated on trace candidates so `from_candidate` has data to read)
- **Description**:
  - Add `ExplainPipelineResult` dataclass at module scope (`top_results`, `near_misses`, `acl_filtered`).
  - Add `async SearchPipeline.explain(query, collection, *, top_k=5, rerank=True, namespace=DEFAULT_NAMESPACE, query_vector=None) -> ExplainPipelineResult`.
  - Flow:
    1. `vector = query_vector if query_vector is not None else await self._embedder.embed_one(query)`.
    2. `candidate_depth = max(self._top_k_retrieve * 3, 20)`; `candidates = await self.store.hybrid_search_with_trace(collection, vector, query, candidate_depth=candidate_depth)`. The amplification is replicated **at this call site** because `_hybrid_search_with_trace` passes `candidate_depth` straight to `.limit(...)` (no internal amplification). This reproduces the post-amplification pool size of `store.hybrid_search` used by `SearchPipeline.search()`.
    3. ACL-filter the candidate pool using `apply_acl_filter(candidates, lambda c: c.acl, namespace)`; capture `acl_filtered: bool`.
    4. If `rerank`: `candidates = await self._reranker._rerank_with_trace(query, candidates, top_k=len(candidates))` — note the positional `candidates` argument matches the real signature `(self, query, candidates, top_k)`. Reranks the entire surviving pool so near-misses are ranked too. If not `rerank`: leave `reranker_score=None` on every candidate; sort by `rrf_score`.
    5. Sort full list by final score (`reranker_score` if reranked else `rrf_score`) descending, tied by `(doc_id, chunk_id)` ascending.
    6. Slice: `top_results = sorted[:top_k]`; `near_misses = sorted[top_k : top_k + 20]`.
    7. Return `ExplainPipelineResult(top_results, near_misses, acl_filtered)`.
  - **Do not** import from `archon_search.server.*`. The pipeline returns raw `ScoredSearchCandidate` lists; mapping happens in the route layer via `ExplainResponse.from_pipeline_result`.
  - **No `_retrieval_pool_size(top_k)` helper is introduced.** `SearchPipeline.search` passes `self._top_k_retrieve` to `store.hybrid_search` (which amplifies internally). `SearchPipeline.explain` passes the already-amplified `max(self._top_k_retrieve * 3, 20)` to `hybrid_search_with_trace` (which does not amplify). The post-amplification pool size matches.
- **Releasable**: after this task, a unit test can construct a `SearchPipeline` against a fixture LanceDB collection and call `.explain(...)` to get a deterministic trace.
- **Tests (TDD)** — `tests/test_pipeline_explain.py` (new file):
  - Unit: `test_explain_returns_top_results_and_near_misses` — fixture collection with ≥25 chunks; `top_k=5` → `len(top_results) == 5`, `len(near_misses) <= 20`.
  - Unit: `test_explain_top_k_matches_search_when_rerank_true_and_top_k_equals_top_k_return` — call `pipeline.search(query, coll)` (returns `_top_k_return` results) and `pipeline.explain(query, coll, top_k=pipeline._top_k_return, rerank=True)`; assert ordered `(doc_id, chunk_id)` of `top_results == [(r.doc_id, r.chunk_id) for r in search.results]`.
  - Unit: `test_explain_rerank_false_uses_rrf_ordering` — call with `rerank=False`; every candidate has `score_breakdown.reranker_score is None`; list is sorted by `rrf_score` descending with `(doc_id, chunk_id)` tie-break.
  - Unit: `test_explain_uses_amplified_retrieval_pool` — spy/monkeypatch `store.hybrid_search_with_trace` to capture the `candidate_depth` arg; assert it equals `max(pipeline._top_k_retrieve * 3, 20)` regardless of request `top_k`. This is the post-amplification pool size matching what `store.hybrid_search` produces internally for `SearchPipeline.search()`.
  - Unit: `test_explain_accepts_precomputed_query_vector_and_skips_embedding` — monkeypatch `embedder.embed_one` to raise; call `explain(query_vector=[...])`; assert no error and the supplied vector is forwarded to the store.
  - Unit: `test_explain_near_miss_pool_capped_at_20` — corpus with ≥30 surviving candidates; assert `len(near_misses) == 20` once the post-ACL pool minus top-k is ≥ 20.
  - Unit: `test_explain_small_corpus_returns_what_is_available` — corpus with 3 chunks, `top_k=5` → `len(top_results) <= 3`, `near_misses == []`.
  - Unit: `test_explain_acl_filtered_when_all_filtered` — ACL fixture that filters every chunk for the namespace → `top_results == []`, `near_misses == []`, `acl_filtered is True`.
  - Unit: `test_explain_partial_acl_filter_adjusts_near_miss_count` — ACL fixture that filters about half the pool; assert `len(near_misses) == min(20, max(0, P - top_k))` where `P` is the post-ACL surviving count.
  - Unit: `test_explain_identical_scores_tie_break_by_doc_chunk_id` — construct/mock candidates with identical final scores; assert ordering is `(doc_id, chunk_id)` ascending.
  - Integration: `test_explain_against_real_lancedb_fixture` — `tests/fixtures/explain_corpus/` LanceDB table → end-to-end deterministic trace shape (marker `integration` if needed).
  - Checkpoint: `uv run pytest tests/test_pipeline_explain.py -v`

---

### Phase 3 — REST endpoint
> **Releasable**: after Task 3.1 `POST /explain` is callable end-to-end against a running server with a real collection.

#### Task 3.1 — `POST /explain` route handler
- [ ] **Files**: `archon_search/server/routes_explain.py` (extend with handler), `archon_search/server/app.py` (register router)
- **Depends on**: Task 1.1, Task 1.2, Task 2.1, Task 2.2, Task 2.3
- **Description**:
  - Add `POST /explain` handler in `routes_explain.py`, bearer-auth-protected by the existing middleware (no per-route hook needed).
  - Request body: `ExplainRequest`.
  - Branch on `body.collection`:
    - **Pinned collection**:
      1. `meta = await pipeline.get_collection_meta(body.collection, namespace=ns)`; on `None` → `404 {"detail": "collection not found"}`.
      2. `result = await pipeline.explain(body.query, body.collection, top_k=body.top_k, rerank=body.rerank, namespace=ns)`.
      3. `routing = None`; `chosen_or_pinned = body.collection`.
    - **Collectionless** (direct in-process path, no HTTP self-call):
      1. `all_meta = await pipeline.get_all_collections_meta(namespace=ns)` (already namespace-filters in `pipeline.py:355`).
      2. If empty → `404 {"detail": "no collections available"}`.
      3. Build the router inline: `col_router = MultiCollectionRouter(search_url=..., embedder=pipeline._embedder, shortlist_size=config.routing_shortlist_size, confidence_threshold=config.routing_confidence_threshold, embedding_model=config.embedding_model)`. The `search_url` is unused by `rank_with_scores` (no `fetch_metadata` call), so it can be the configured server URL or any non-empty string.
      4. `vector = await pipeline._embedder.embed_one(body.query)`.
      5. `ranked = col_router.rank_with_scores(vector, all_meta)`.
      6. **ACL-filter `ranked`** to the caller's ACL-allowed collection set BEFORE building `RoutingCandidate` entries. Use the same ACL evaluator that gates results/near-misses so the routing block does not leak the existence of disallowed collections. Implementation: `ranked = [(m, s) for m, s in ranked if acl_allows(principal, m)]`. If the resulting list is empty, return `404 {"detail": "no collections available"}` (the caller is functionally in the same state as an empty store).
      7. `chosen_meta, chosen_score = ranked[0]`; `chosen_or_pinned = chosen_meta.name`.
      8. `routing = RoutingExplain(invoked=True, chosen_collection=chosen_or_pinned, confidence_threshold=config.routing_confidence_threshold, chosen_below_threshold=(chosen_score is not None and chosen_score < config.routing_confidence_threshold), candidates=[RoutingCandidate(collection=m.name, centroid_score=s) for m, s in ranked])` — `candidates` is the ACL-filtered list.
      9. `result = await pipeline.explain(body.query, chosen_or_pinned, top_k=body.top_k, rerank=body.rerank, namespace=ns, query_vector=vector)` — vector passed through to avoid double-embed.
  - `response = ExplainResponse.from_pipeline_result(rerank=body.rerank, collection=chosen_or_pinned, routing=routing, result=result)`.
  - Telemetry: on success, `writer.enqueue(TelemetryEntry.from_explain_result(collection=chosen_or_pinned, result_count=len(response.results), latency_ms=...))`; on error paths, `TelemetryEntry.from_error(endpoint="explain", ...)` mirroring `routes_route.py`. Wrap each telemetry enqueue in `try/except` so a writer failure cannot abort the response.
  - Error envelopes:
    - Empty query → `422` (Pydantic).
    - Pinned collection not found → `404 {"detail": "collection not found"}`.
    - Collectionless + no collections → `404 {"detail": "no collections available"}`.
    - Collectionless + router/meta-lookup exception → `503 {"detail": "<router error>"}`. **503 is reserved for meta-lookup failures only** (mirrors A3's `/search` taxonomy).
    - Store/`hybrid_search_with_trace` failure → `500 {"detail": "store error: ..."}`. Pipeline-stage failure → 500 per A3 taxonomy.
    - Reranker failure → `500 {"detail": "reranker error: <msg>"}`. Pipeline-stage failure → 500 per A3 taxonomy. Post-A3, `/search` no longer swallows reranker exceptions either — `/explain` is consistent with `/search`, not divergent.
    - Router picks a collection that then 404s during search → `500 {"detail": "router selected ..., search failed: ..."}`.
    - `top_k > 100` → `422` (Pydantic).
  - Register `routes_explain.router` in `app.py` alongside `routes_route.router`.
- **Releasable**: after this task, `curl -H "Authorization: Bearer …" -X POST http://…/explain -d '{"query":"…"}'` returns a complete explain JSON object, and `GET /openapi.json` lists `/explain` with bearer security auto-injected.
- **Tests (TDD)** — `tests/server/test_routes_explain.py` (new file):
  - Unit: `test_post_explain_without_auth_returns_401` — no `Authorization` header → 401.
  - Unit: `test_post_explain_empty_query_returns_422` — body `{"query": ""}` → 422.
  - Unit: `test_post_explain_pinned_collection_not_found_returns_404` — unknown collection → 404 with `detail == "collection not found"`.
  - Unit: `test_post_explain_collectionless_no_collections_returns_404` — empty store, no collection in body → 404 with `detail == "no collections available"`.
  - Unit: `test_post_explain_top_k_above_100_returns_422` — `top_k=101` → 422.
  - Unit: `test_post_explain_top_k_below_1_returns_422` — `top_k=0` → 422.
  - Unit: `test_post_explain_collectionless_router_failure_returns_503` — patch `rank_with_scores` to raise → 503.
  - Unit: `test_post_explain_store_failure_returns_500` — patch `hybrid_search_with_trace` to raise → 500 (per A3 taxonomy; pipeline-stage failure).
  - Unit: `test_post_explain_reranker_failure_returns_500` — patch reranker `_rerank_with_trace` to raise; assert status `500` and `detail` starts with `"reranker error:"`. Consistent with post-A3 `/search` behavior (A3 made `/search` raise 500 on pipeline-stage failures instead of returning 200+[]); `/explain` aligns with that taxonomy.
  - Unit: `test_post_explain_telemetry_writer_failure_does_not_abort_response` — patch `writer.enqueue` to raise `OSError`; assert 200 still returned with full response.
  - Unit: `test_post_explain_concurrent_collectionless_requests` — fire 3 concurrent collectionless calls via `asyncio.gather`; assert all succeed with shape-equivalent responses.
  - Integration: `test_post_explain_pinned_collection_happy_path` — fixture LanceDB, valid token → 200 with full `ExplainResponse` shape; `routing is None`; `len(results) <= top_k`.
  - Integration: `test_post_explain_collectionless_includes_routing_block` — multi-collection fixture, no collection in body → 200 with `routing.candidates` non-empty, sorted by score desc + alpha tie-break.
  - Integration: `test_post_explain_routing_covers_every_collection_no_gating` — confidence_threshold set very high; assert `set(c.collection for c in response.routing.candidates) == set(<all ACL-allowed collection names in namespace>)` and `routing.chosen_below_threshold is True`. Note: "all collections" here means all ACL-allowed collections for the caller — confidence gating is bypassed but ACL filtering still applies.
  - Integration: `test_post_explain_routing_candidates_acl_filtered` — fixture with 3 collections where the caller's principal is ACL-allowed for only 1 of them; assert (a) `set(c.collection for c in response.routing.candidates)` contains exactly the 1 allowed collection, (b) the 2 disallowed collection names are NOT present anywhere in the response body (not as `chosen_collection`, not in `candidates`). Pins the no-leak guarantee on `routing.candidates`.
  - Integration: `test_post_explain_collectionless_all_collections_acl_filtered_returns_404` — caller is ACL-denied for every collection in the namespace; collectionless `/explain` → 404 `{"detail": "no collections available"}`.
  - Integration: `test_post_explain_search_top_k_equality_at_top_k_return` — for `{collection, query}` with `top_k == config.top_k_return`, ordered `(doc_id, chunk_id)` of `/explain`'s top-`top_k` `results` equals `/search`'s `results`. Documents the equality contract scope.
  - Integration: `test_post_explain_near_miss_no_text_field` — assert `"text" not in nm` for every `nm in response.near_misses`.
  - Integration: `test_post_explain_rerank_false_orders_by_rrf` — `rerank=false` → every `result.breakdown.reranker_score is None`; list sorted by `breakdown.rrf_score` desc.
  - Integration: `test_post_explain_rerank_false_collectionless` — combine `rerank=false` with collectionless mode; assert routing block populated AND every result has `reranker_score is None`.
  - Integration: `test_post_explain_near_miss_pool_sizes` — three corpus sizes (`P >> top_k+20`, `P` mid-range, `P <= top_k`) → counts match `min(20, max(0, P - top_k))`.
  - Integration: `test_post_explain_near_miss_at_exact_boundary` — corpora sized so post-ACL pool is exactly `top_k + 20` and `top_k + 21`; assert near-miss counts are `20` and `20` respectively (cap holds).
  - Integration: `test_post_explain_acl_filtered_returns_empty_and_flag` — ACL fixture filtering all docs → `results == []`, `near_misses == []`, `acl_filtered is True`.
  - Integration: `test_post_explain_empty_collection_returns_empty_results` — pinned collection with zero chunks → 200, `results == []`, `near_misses == []`, `acl_filtered is False`.
  - Integration: `test_post_explain_pinned_collection_wrong_namespace_returns_404` — collection exists in another namespace; current-namespace request → 404.
  - Integration: `test_post_explain_telemetry_emits_no_query` — telemetry writer aimed at tmp dir; call `/explain` with a unique query string; read JSONL via `telemetry.reader`; assert (a) `"query"` not in parsed dict, (b) query string is not a substring of the raw JSONL line, (c) `endpoint == "explain"`, (d) `result_count == len(response.results)`.
  - Integration: `test_post_explain_openapi_includes_route` — `GET /openapi.json` paths include `"/explain"` with a `post` operation; `requestBody` schema is `ExplainRequest`; security scheme includes `HTTPBearer` (auto-injected by `_configure_openapi`).
  - Checkpoint: `uv run pytest tests/server/test_routes_explain.py -v`

---

### Phase 4 — MCP tool
> **Releasable**: after Task 4.1 MCP clients can invoke `explain` and get equivalent output to REST.

#### Task 4.1 — `explain` MCP tool registration
- [ ] **File**: `archon_search/server/mcp.py`
- **Depends on**: Task 3.1 (route handler defines the reference shape)
- **Description**:
  - Add a 10th `@app.tool()` function `explain(query: str, collection: str | None = None, top_k: int = 5, rerank: bool = True) -> dict[str, Any]`.
  - Tool body is **independent of the REST handler** (matches the existing `search` MCP tool pattern in `mcp.py`, which duplicates pipeline-call logic rather than sharing a helper). It builds `ExplainRequest` internally for validation, runs the same pinned-vs-collectionless branch, calls `SearchPipeline.explain`, builds `ExplainResponse.from_pipeline_result`, and returns `response.model_dump(mode="json", exclude_none=False)`.
  - Namespace: operates against `DEFAULT_NAMESPACE` only, matching the existing MCP `search` tool. AC11 (ACL parity) does not apply to MCP in v1.
  - Telemetry: emit the same `TelemetryEntry.from_explain_result(...)` on success and `from_error(endpoint="explain", ...)` on failure, mirroring how the existing MCP `search` tool emits in `mcp.py`.
  - Error envelope parity with REST: empty query, missing pinned collection, collectionless + no collections each return an MCP error response (use the existing MCP error pattern in `mcp.py`; do not silently swallow).
- **Releasable**: after this task, the MCP server exposes 10 tools.
- **Tests (TDD)** — `tests/server/test_mcp_explain.py` (new file):
  - Unit: `test_mcp_app_registers_explain_tool` — introspect the FastMCP app; `"explain"` is in the registered tool names; tool count is 10.
  - Unit: `test_mcp_explain_rejects_empty_query` — invoke tool with `query=""` → error response (matching how existing MCP search rejects empty queries).
  - Unit: `test_mcp_explain_missing_collection_returns_not_found` — invoke with unknown `collection` → error response with `not_found`-equivalent code.
  - Unit: `test_mcp_explain_collectionless_no_collections_returns_not_found` — empty store → error response.
  - Integration: `test_mcp_explain_rest_parity` — call REST `/explain` and MCP `explain` with the same inputs against the fixture LanceDB; compare `json.loads(json.dumps(rest_dict)) == json.loads(json.dumps(mcp_dict))` (deep dict equality after a JSON round trip, avoiding float-formatting drift between Pydantic's `model_dump(mode="json")` and direct dict construction).
  - Integration: `test_mcp_explain_telemetry_no_query` — telemetry writer aimed at tmp dir; invoke MCP tool; JSONL line has no `query` key and does not contain the query string substring; `endpoint == "explain"`.
  - Checkpoint: `uv run pytest tests/server/test_mcp_explain.py -v`

---

### Phase 5 — Verification & documentation

#### Task 5.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: Task 1.1, 1.2, 2.1, 2.2, 2.3, 3.1, 4.1
- **Description**:
  - Spawn a documentation agent to discover and update every file affected by A4:
    - `Documentation/Architecture/600_api_reference_or_public_interface.md` — add `POST /explain` REST entry with the JSON example from the brief; add the `explain` MCP tool to the tool list.
    - `Documentation/Architecture/100_system_architecture_overview.md` — update the route/MCP tool tables if they enumerate routes.
    - `Documentation/Architecture/110_component_catalog_and_layer_breakdown.md` — add `routes_explain.py`, the new `SearchPipeline.explain` method, `LanceStore.hybrid_search_with_trace`, the `acl` field on `ScoredSearchCandidate`, and `MultiCollectionRouter.rank_with_scores` + `_score_collections` to the catalog.
    - `Documentation/Architecture/150_security_and_privacy_architecture.md` — note that `/explain` does not echo the query in the response body and telemetry does not log it. Confirm `source_path` exposure is already documented for `/search`; if not, extend.
    - `CLAUDE.md` — bump the MCP tool count from "9 total" to "10 total" and add `explain` to the tool list.
    - `BREAKING.md` — add an entry stating A4 is purely additive (new endpoint, new MCP tool, new telemetry enum value, new optional dataclass field, tightened tie-break on `rank()` which was previously undefined).
    - `Documentation/Backlog/03_world_class_roadmap.md` — mark item 12 as A4-shipped; note the two deferred sub-fields (`matched_filters` → A4.1, `expansions` → A4.2).
  - The agent must NOT touch docs unrelated to A4.
  - Verify every acceptance criterion below before marking complete. Run the full default test suite (`uv run pytest`) and the eval-marker test (`uv run pytest -m eval`) to confirm green.
- **Releasable**: after this task, the feature is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass — see `explain-endpoint-brief.md` §"Acceptance Criteria" for full prose):
  - [ ] **AC1 — Endpoint exists and is authenticated** — `POST /explain` without bearer token returns 401; with valid token returns 200.
  - [ ] **AC2 — REST↔MCP response equivalence** — for the same inputs against the pre-built fixture LanceDB table, the REST and MCP responses are deep-equal after a JSON round trip (i.e. `json.loads(json.dumps(rest)) == json.loads(json.dumps(mcp))`). Compared this way to dodge float-formatting drift; both responses originate from `ExplainResponse.model_dump(mode="json", exclude_none=False)` so structural parity is exact.
  - [ ] **AC3 — `/search` top-k equality when `rerank=true` AND `top_k == config.top_k_return`** — ordered `(doc_id, chunk_id)` of `explain.results[:top_k]` equals `search.results` for the same `{collection, query}` when the explain request's `top_k` equals the server's `top_k_return` config. For other `top_k` values this equality is not contracted (and structurally cannot be — `/search` has no `top_k` request param).
  - [ ] **AC4 — `rerank=false` ordering** — every `result.breakdown.reranker_score is None`; list sorted by `rrf_score` desc with `(doc_id, chunk_id)` ascending tie-break.
  - [ ] **AC5 — Near-miss pool size** — let `P` = count of candidates surviving ACL filtering from the `max(self._top_k_retrieve * 3, 20)` retrieval pool. Assert `len(near_misses) == min(20, max(0, P - top_k))` for three corpus sizes (large / mid / smaller-than-`top_k`).
  - [ ] **AC6 — Near-miss `text` absent** — `"text" not in nm` for every near-miss; structurally absent on the model.
  - [ ] **AC7 — Routing block presence/absence** — pinned → `routing is None`; collectionless → `routing.candidates` non-empty, sorted by `centroid_score` desc + alpha tie-break, and `routing.confidence_threshold` / `routing.chosen_below_threshold` are populated.
  - [ ] **AC8 — Routing covers all ACL-allowed collections (no confidence gating, ACL still applies)** — `set(c.collection for c in routing.candidates) == set(<ACL-allowed collection names in namespace>)`, including collections the confidence gate would filter out. ACL-disallowed collections MUST NOT appear in `routing.candidates`. If all collections are ACL-filtered → 404 `{"detail": "no collections available"}`.
  - [ ] **AC9 — Telemetry no-query (positive)** — parsed telemetry JSONL line has no `"query"` key; query string not a substring of raw line; `endpoint == "explain"`; `result_count == len(response.results)`.
  - [ ] **AC10 — Telemetry factory rejects query (structural)** — `"query" not in inspect.signature(TelemetryEntry.from_explain_result).parameters`; calling `from_explain_result(query="x", ...)` raises `TypeError`.
  - [ ] **AC11 — ACL parity (REST only)** — `/explain` and `/search` return the same `acl_filtered: True` + empty `results` shape under a fully-filtering ACL fixture. MCP `explain` operates in `DEFAULT_NAMESPACE` only; ACL parity does not apply to MCP in v1.
  - [ ] **AC12 — Edge-case error envelopes** — empty query → 422; missing pinned collection → 404; collectionless + no collections → 404 `{"detail": "no collections available"}`; collectionless + router/meta-lookup error → 503 (meta-lookup failure path only); store error → 500 (pipeline-stage failure per A3 taxonomy); reranker error → 500 `{"detail": "reranker error: <msg>"}` (pipeline-stage failure per A3 taxonomy — consistent with post-A3 `/search`, no longer a divergence); `top_k > 100` → 422; telemetry writer failure → does not abort response.
  - [ ] **AC13 — MCP error envelope parity** — empty query, missing collection, collectionless + no collections each surface a matching MCP error (no silent swallowing).
  - [ ] **AC14 — Eval-harness consistency via public mapping** [eval marker] — single fixture query: call `_rerank_with_trace` directly, pass output through `ExplainResponse.from_pipeline_result` (wrapping it in an `ExplainPipelineResult`), assert the resulting `ExplainResponse` equals the live `/explain` response against the same corpus.
  - [ ] **AC15 — Coverage** — default `--cov-fail-under=85` gate remains green without amendment. (LOC budget removed; coverage is the only enforceable constraint.)
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked. Final command: `uv run pytest && uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py`.
