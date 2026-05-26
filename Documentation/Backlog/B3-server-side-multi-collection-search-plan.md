# B3 — Server-Side Multi-Collection Search Primitive

**Purpose**: Ship a server-side primitive that embeds a query once, fans out hybrid retrieval across an explicit set of collections in parallel, merges the candidate pools with full provenance, and runs one global rerank pass — returning a single unified result list in which every result is tagged with its source collection.
**Audience**: archon-search contributors implementing B3 and reviewers of the resulting PRs.
**Status**: To Do

> **Order**: Ships AFTER A1 (metadata schema), A3 (search-failure semantics), A4 (explain endpoint). Lands in Phase B after B1 (per-stage timings). Gated by the deterministic eval harness (`tests/eval/`); B6 is NOT a prerequisite.

---

## Background

The only search primitive today is single-collection: `SearchPipeline.search()` embeds once, calls `store.hybrid_search()` against one collection, and rernanks one pool. REST `/search` and MCP `search` both expose only `collection: str`. The `/route` endpoint tells callers which collections are relevant but does not search them.

Multi-collection queries are therefore assembled client-side, paying N embeddings for one query, per-collection reranking that produces incomparable RRF scores (local rank spaces), and no debuggable routing path through `/explain`.

The cross-encoder reranker already scores `(query, candidate.text)` pairs independent of collection (`reranker.py:55-56`). `ScoredSearchCandidate` already carries a `collection` field (`_diagnostics.py:75`). The missing piece is a production fan-out that unifies the two diverging candidate/rerank type systems: the production path returns plain `SearchResult` (no rank provenance) while the provenance-carrying `ScoredSearchCandidate` only flows through the eval/debug-only `_hybrid_search_with_trace` (`store.py:1033-1033`, marked private at `store.py:1042-1044`).

## Goal

When this plan is complete, a REST or MCP caller can supply `collections: ["a", "b", "c"]` in a single request and receive one merged, globally reranked result list in which every result carries a `collection` field naming its origin. The query embedding is computed exactly once. Reranking is one pass over the merged pool. The fan-out is observable through B1 per-stage timings and through A4's `/explain` surface. The single-collection path is observably identical to today (same result set and ordering, only the additive `collection` field changes). The eval harness still passes.

---

## Scope

### In Scope

- `SearchResult.collection: str` field and mirroring in `SearchResultSchema`
- Type-unification: promote the trace-style `ScoredSearchCandidate` path to production; unify `Reranker.rerank` (takes `list[SearchResult]`) and `Reranker._rerank_with_trace` (takes `list[ScoredSearchCandidate]`) into one production-grade candidate rerank surface
- Reconcile retrieval sort tie-break to a single explicit `(-score, chunk_id)` rule across production and trace paths
- Migrate already-shipped A4 `explain()` call sites and `eval/_tracing.py` to the unified surface in the same commit
- New `SearchPipeline.search_many(query, collections, namespace)` — embed once, `asyncio.TaskGroup` fan-out, per-leg RRF trim, deterministic merge (legs in ascending collection-name order, within-leg candidates in reconciled retrieval order), single unconditional rerank via the unified candidate surface, convert survivors to `SearchResult`
- Whole-fan-out `asyncio.wait_for` timeout → HTTP 504; any leg failure cancels siblings → HTTP 500
- New config keys: `max_fanout` (int, default 8), `fanout_leg_trim` (int, default 40), `fanout_timeout_seconds` (float, default 30.0)
- `SearchPipelineResult` gains `excluded_collections: list[ExcludedCollection]`; `ExcludedCollection(name: str, reason: str)`
- `SearchResponse` gains `excluded_collections: list[ExcludedCollectionSchema]`
- REST `SearchRequest` gains `collections: list[str] | None` with an exactly-one-of validator; per-item nonempty + dedup; length 1–`max_fanout`; 422 on both/neither/0-length/over-limit/whitespace
- Namespace scoping: missing or out-of-namespace requested collections → identical 404 (no cross-namespace existence leak); model-mismatch collections → excluded + reported
- Resolve+scope metadata lookup failure → HTTP 503
- MCP `search` tool gains `collections` parameter with identical exclusivity; `asdict()` tagging
- `/explain` (A4) extended to accept `collections`; `rerank=false`+multi → 422 on `/explain` only; A4 explain result/near-miss models gain `collection` field
- `fanout_collections` count in search telemetry entry (no query text); per-leg + merge/rerank timings into B1's stage breakdown
- ACL: single `apply_acl_filter` pass over merged pool pre-rerank; `acl_filtered` is pool-wide boolean
- Provenance field populated at the single row-to-`SearchResult` site in `store.py` so `search_with_context` inherits it for free
- `BREAKING.md` entry: additive REST (`collection` key per result, `excluded_collections` per response, `collections` request field) vs. shape-change MCP (same additive keys — strict MCP clients see a contract change)
- Docs: `Architecture/120_`, `600_`, `210_` updated; B4 forward-reference added
- Multi-collection merge fixture under `tests/eval/` (fixed collection set, merge correctness — not routing selection)

### Out of Scope

- Collection selection intelligence — that is B4
- Cross-model multi-collection search — that is C1
- Server-side auto-routing when no collection is given
- Streaming / partial-results delivery — E1
- HyDE / query expansion (C4), RAG Fusion / multi-query (C5)
- B5 (incremental centroids), B6 (production-model eval)
- New auth / ACL model — E4
- `MAX_FANOUT` as a per-request override
- Cross-collection content dedup
- A `rerank` toggle on `/search` or MCP `search`
- Lenient skip mode for missing/out-of-namespace collections
- Per-collection `acl_filtered` signal

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 8.1 — Final verification & documentation update].

---

## What does NOT change

- `SearchPipeline.search()` observable contract: same result set and ordering as today. Additive changes only: `SearchResult` gains `collection: str = ""`, `SearchPipelineResult` gains `excluded_collections: list[ExcludedCollection] = []` and `fanout_timings: FanoutTimings | None = None`. All pre-B3 fields are unchanged (field-subset equality, not byte-for-byte).
- Single-collection eval baseline (`tests/eval/baselines/baseline.json`) must not move
- `Reranker` stable-sort contract (`reranker.py:99-100`; pinned by `test_P14_6_reranker_stable_order_on_equal_scores`) — no new rerank tie-break key
- No-raw-query structural invariant: no `query` parameter added to any telemetry entry factory
- `_hybrid_search_with_trace` module-level function name stays; the instance method `store.hybrid_search_with_trace` (added by A4) is the external seam
- Bearer auth, ACL policy, and namespace semantics unchanged
- `/route` endpoint unchanged
- `BREAKING.md` append-only

---

## Known limitations / accepted trade-offs

- **Recall ceiling**: per-leg RRF trim is a hard recall ceiling; the reranker cannot recover candidates dropped before merge. `fanout_leg_trim` must be set generously. Relevance-aware leg budgeting is a Future Iteration.
- **Single reranker thread**: the single `Reranker` instance reranks on one thread (`asyncio.to_thread`), so concurrent multi-collection requests serialize at that instance. B1 timings make the cost measurable.
- **`acl_filtered` is pool-wide**: tells the caller something was ACL-dropped but not which collection. Per-collection `acl_filtered` is a Future Iteration.
- **`embedding_model` name-equality only**: `CollectionMeta` carries `embedding_model` but no `embedding_dim`; name-equality implies dimension-equality under the single-`Embedder` invariant. C1 will add stored `embedding_dim`.
- **Strict 404 on missing/out-of-namespace**: v1 never skips silently. A lenient skip-with-report mode is a deferred enhancement.
- **Promoting trace path to production** adds a per-candidate `SearchScoreBreakdown` allocation on every production search. Mitigated by keeping breakdown capture lazy/optional on the single-collection path; B1 timings quantify residual cost.
- **`candidate_depth` is 3× `top_k_retrieve` per leg**: `search_many` retrieves `max(top_k_retrieve * 3, 20)` candidates per leg (vs `top_k_retrieve` for single-collection `search()`). This widens the per-leg net to compensate for merge loss but is 3× more expensive per leg at the retrieval stage. Relevance-aware leg budgeting is a Future Iteration.
- **`MAX_FANOUT` hardcoded at Pydantic validation layer**: `SearchRequest` and MCP validation use a module-level constant (default 8) for the length upper bound check. If an operator sets `max_fanout` to a higher value in `archon-search.toml`, requests with more than 8 collections will still be rejected at the Pydantic/MCP layer before the pipeline enforces the runtime config. Per-request fanout cap override is Out of Scope (see Scope). Operators who raise `max_fanout` beyond 8 must be aware that the HTTP/MCP validation constant must also be updated in `routes_search.py`.
- **asyncio.timeout() for fan-out timeout (not asyncio.wait_for)**: In Python 3.11+, `asyncio.wait_for` cancels the coroutine but `asyncio.TaskGroup.__aexit__` will then wait for all child task cancellations to complete before returning, making the outer timeout non-binding. `search_many` uses `asyncio.timeout()` context manager (Python 3.11+) to wrap the `TaskGroup`, which propagates `TimeoutError` immediately and allows the TaskGroup to cancel children cleanly.
- **All collections model-mismatched → empty result set (HTTP 200)**: if every requested collection is excluded due to model mismatch, `search_many` performs zero fan-out legs and returns an empty `SearchPipelineResult` (results=[], excluded_collections=[all]). This is a valid HTTP 200, not a 422 or 404.
- **`fanout_leg_trim` and `max_fanout` must be ≥ 1**: values ≤ 0 are rejected by config validation with `ConfigError`. See Task 3.1.

---

## Architecture

### New types

- `ExcludedCollection(name: str, reason: str)` — dataclass in `archon_search/_types.py`; reason values: `"embedding_model_mismatch"`
- `SearchPipelineResult.excluded_collections: list[ExcludedCollection]` — added field (default empty list)
- `SearchResult.collection: str` — new field, populated at the row-to-`SearchResult` site (`store.py` around line 694–708)

### Modified types

- `Reranker`: `rerank(query, candidates: list[SearchResult], top_k)` is kept for backward compat; `rerank_candidates(query, candidates: list[ScoredSearchCandidate], top_k) -> list[ScoredSearchCandidate]` is the new unified production-grade surface (promoted from `_rerank_with_trace`, which becomes a thin alias calling `rerank_candidates`)
- `_hybrid_search_with_trace` signature unchanged; the sort order reconciled to `(-rrf_score, chunk_id)` (was already that in the trace path; the production `hybrid_search` retrieval sort at `store.py:687` gains an explicit `chunk_id` tie-break)

### New pipeline primitive

```python
# archon_search/pipeline.py
async def search_many(
    self,
    query: str,
    collections: list[str],
    namespace: str = DEFAULT_NAMESPACE,
) -> SearchPipelineResult:
    ...
```

Internal flow:
1. `embed_one(query)` → shared vector; also initializes `embedder.embedding_dim`
2. Load all collection metas via `self.get_all_collections_meta(namespace)` (the pipeline's own namespace-filtered method, not the store directly — avoids duplicating namespace-scoping logic). Build a dict `{name: meta}`. Validate each name in `collections` is in the dict; if any are missing, raise `CollectionNotFoundError(names: list[str])`. Exclude collections where `meta.embedding_model != self._embedder.model_name`; add to `excluded_collections`. If all requested collections are excluded (empty fan-out after exclusion), return `SearchPipelineResult(results=[], acl_filtered=False, excluded_collections=excluded_collections)` immediately — no error.
3. Fan-out: use `asyncio.timeout(self._config.fanout_timeout_seconds)` context manager (Python 3.11+, not `asyncio.wait_for`) wrapping an `asyncio.TaskGroup`. Each leg's coroutine captures its own `monotonic()` start/end time internally and returns `(collection_name, list[ScoredSearchCandidate], leg_time_ms)`. On `TimeoutError` raised by `asyncio.timeout()`, convert to `FanoutTimeoutError`. On `ExceptionGroup` from `TaskGroup`: unwrap with `except* Exception as eg` and re-raise the first exception from `eg.exceptions` as a plain exception so the route handler's existing 500 mapping fires without needing to handle `ExceptionGroup`.
4. Per-leg trim to top-`fanout_leg_trim` by local RRF score (`max(self._config.fanout_leg_trim, 1)` to guard against zero). Concatenate legs in ascending collection-name order; within each leg candidates are in `(-rrf_score, chunk_id)` order. `ScoredSearchCandidate` must carry an `acl` field (confirmed present or added in Task 1.1/2.1 — see note below).
5. `apply_acl_filter(merged, lambda c: c.acl, namespace)` — `ScoredSearchCandidate.acl` must exist. If it is not currently present, Task 2.1 must add it when promoting the trace path (or confirm it is already there by reading `_diagnostics.py` before implementation).
6. `reranker.rerank_candidates(query, merged, top_k=top_k_return)` → unified top-k
7. Convert `ScoredSearchCandidate` → `SearchResult` via `_candidate_to_search_result(c)` private helper. `score` is set to `c.score_breakdown.reranker_score`; implementer must assert this is non-None since rerank is unconditional in `search_many`. `collection` is copied from `c.collection`.

> **Note on `ScoredSearchCandidate.acl`**: before implementing Task 3.2, verify that `_diagnostics.py:ScoredSearchCandidate` carries an `acl: list[str] | None` field. If absent, Task 2.1 must add it as part of the unification work (the field already flows through `store.py`'s `_hybrid_search_with_trace` path as it constructs `ScoredSearchCandidate`).

### New config keys (added to `SearchConfig`)

| Key | Type | Default | Section |
|---|---|---|---|
| `max_fanout` | `int` | `8` | `[database]` |
| `fanout_leg_trim` | `int` | `40` | `[database]` |
| `fanout_timeout_seconds` | `float` | `30.0` | `[database]` |

### REST surface changes

`SearchRequest`:
```python
collection: str | None = None          # was required; now optional
collections: list[str] | None = None   # new
# validator: exactly one of {collection, collections} must be set
# collections: 1–max_fanout entries, nonempty after strip, deduplicated
```

`SearchResponse`:
```python
excluded_collections: list[ExcludedCollectionSchema] = []  # new additive field
```

`SearchResultSchema`:
```python
collection: str = ""  # new additive field
```

### MCP surface changes

`search` tool gains optional `collections: list[str] | None` with identical exactly-one-of semantics. Result `asdict()` output gains `collection` and `excluded_collections` automatically.

### Explain surface changes

`ExplainRequest` gains `collections: list[str] | None`; exactly-one-of validator (same as `SearchRequest`). `/explain` with `rerank=false` + multiple `collections` → 422 with message `"reranking cannot be disabled for multi-collection search in v1"`. `ExplainResult` and `ExplainNearMiss` gain `collection: str`.

> **Boundary: `collections=["x"]` + `rerank=False`**: a single-item `collections` list with `rerank=False` is treated identically to the existing single-collection `explain` path with `rerank=False` (valid, no error). The 422 guard fires only when `len(collections) > 1` AND `rerank=False`. This must be enforced in the Pydantic model validator (not the route handler) so that the schema contract is consistent with `SearchRequest`.

### Telemetry

`TelemetryEntry.from_search_multi_result(*, collections: list[str], fanout_count: int, result_count: int, latency_ms: float, excluded_count: int) -> TelemetryEntry` — new factory (keyword-only, no `query` parameter). `EndpointKind` gains `"search_multi"`.

---

## Task breakdown

### Phase 1 — Provenance field on SearchResult

> **Releasable**: after Task 1.1 — single-collection `/search` responses carry a `collection` field; `search_with_context` inherits it. No multi-collection behavior yet.

#### Task 1.1 — Add `SearchResult.collection` and populate it at the store row-to-result site

- [ ] **File**: `archon_search/_types.py`, `archon_search/store.py`, `archon_search/server/routes_search.py`
- **Depends on**: nothing
- **Description**:
  - Add `collection: str = ""` field to `SearchResult` dataclass (`_types.py:86`). Default empty string ensures backward compat at call sites that construct `SearchResult` directly (tests, eval fixtures).
  - In `store.py`, `hybrid_search` populates the new field at the row-to-`SearchResult` mapping site (around line 694–708). The `collection` parameter is already available in `hybrid_search(self, collection, ...)` — pass it through: `SearchResult(..., collection=collection)`.
  - In `SearchResultSchema` (`routes_search.py:47`), add `collection: str = ""` and extend `from_result` to include `collection=r.collection`.
  - Add `excluded_collections: list[ExcludedCollectionSchema] = Field(default_factory=list)` to `SearchResponse` (`routes_search.py:79`) so the field exists from day one; it is always empty for single-collection requests.
  - Add `ExcludedCollectionSchema(name: str, reason: str)` Pydantic model to `routes_search.py`.
  - Add `ExcludedCollection(name: str, reason: str)` dataclass to `_types.py`.
  - Add `excluded_collections: list[ExcludedCollection] = field(default_factory=list)` to `SearchPipelineResult`.
  - The `/search` handler (`routes_search.py:84`) maps `result.excluded_collections` into `SearchResponse.excluded_collections` using: `excluded_collections=[ExcludedCollectionSchema(name=e.name, reason=e.reason) for e in result.excluded_collections]`. (Empty list for now on single-collection path.) No behavior change.
  - **Releasable**: after this task, every `SearchResult` carries a `collection` field, `SearchResultSchema` mirrors it, and the response schema includes the `excluded_collections` envelope (empty).
- **Tests (TDD)** — `tests/test_routes_search.py`, `tests/test_types.py`:
  - Unit: `test_search_result_has_collection_field` — construct `SearchResult(doc_id=..., chunk_id=..., text=..., score=..., source_path=..., collection="col_a")` and assert `r.collection == "col_a"`
  - Unit: `test_search_result_schema_from_result_includes_collection` — `SearchResultSchema.from_result(r).collection == r.collection`
  - Unit: `test_search_response_includes_excluded_collections_field` — `SearchResponse(results=[], acl_filtered=False).excluded_collections == []`
  - Unit: `test_field_parity_search_result_vs_schema` — extend A1's field-parity snapshot to assert `collection` is present in both `SearchResult.__dataclass_fields__` and `SearchResultSchema.model_fields`
  - Unit: `test_hybrid_search_populates_collection` — mock `store.hybrid_search` at the store level; assert the returned `SearchResult` has `collection` set to the value passed to `hybrid_search`
  - Checkpoint: `uv run pytest tests/test_routes_search.py tests/test_types.py -v`

---

### Phase 2 — Type-unification: unified candidate rerank surface

> **Releasable**: after Task 2.1 — `Reranker.rerank_candidates` is the production-grade candidate rerank surface; A4's `explain()` and `eval/_tracing.py` use the unified path. Single-collection behavior is preserved.

#### Task 2.1 — Promote `_rerank_with_trace` to `rerank_candidates` on `Reranker`

- [ ] **File**: `archon_search/reranker.py`
- **Depends on**: Task 1.1
- **Description**:
  - Rename `_rerank_with_trace` to `rerank_candidates` (public production method). Signature stays identical: `async def rerank_candidates(self, query: str, candidates: list[ScoredSearchCandidate], top_k: int) -> list[ScoredSearchCandidate]`.
  - Keep `_rerank_with_trace` as a thin alias: `async def _rerank_with_trace(self, query, candidates, top_k): return await self.rerank_candidates(query, candidates, top_k)`. This preserves backward compat for any direct call sites that are not migrated in this task (A4 call sites and eval migrated in Task 2.2).
  - No behavior change. Stable-sort contract (`reranker.py:99-100`) unchanged; `test_P14_6_reranker_stable_order_on_equal_scores` must still pass.
  - **Pre-condition check**: before implementing, read `archon_search/_diagnostics.py` and confirm `ScoredSearchCandidate` carries `acl: list[str] | None`. If absent, add it in this task (it already flows through the `_hybrid_search_with_trace` path in `store.py`). This field is required by `apply_acl_filter` in `search_many` Step 6.
  - **Releasable**: `Reranker.rerank_candidates` is callable from production code; `ScoredSearchCandidate.acl` is confirmed present.
- **Tests (TDD)** — `tests/test_reranker.py`:
  - Unit: `test_rerank_candidates_is_public` — assert `hasattr(Reranker, "rerank_candidates")` and it is not prefixed with underscore
  - Unit: `test_rerank_candidates_returns_scored_candidates` — pass a list of two `ScoredSearchCandidate` objects with stub backend; assert output is sorted by `reranker_score` descending
  - Unit: `test_rerank_candidates_stable_sort_on_equal_scores` — equal reranker scores preserve input order (extends existing `test_P14_6_*` contract)
  - Unit: `test_rerank_with_trace_alias_delegates` — `_rerank_with_trace` call returns same result as `rerank_candidates`
  - Checkpoint: `uv run pytest tests/test_reranker.py -v`

#### Task 2.2 — Migrate A4 `explain()` and `eval/_tracing.py` to `rerank_candidates`

- [ ] **File**: `archon_search/pipeline.py`, `archon_search/eval/_tracing.py`
- **Depends on**: Task 2.1
- **Description**:
  - In `SearchPipeline.explain()` (`pipeline.py:401`): replace `self._reranker._rerank_with_trace(...)` with `self._reranker.rerank_candidates(...)`. Signature is identical; no behavior change.
  - In `archon_search/eval/_tracing.py` (around lines 102, 107): replace any direct calls to `reranker._rerank_with_trace` with `reranker.rerank_candidates`. If `_tracing.py` calls `_hybrid_search_with_trace` as the module-level function (not the instance method), leave it — that is a separate path. Only the reranker call site is changed here.
  - The A4 `explain()` behavior is identical; no test changes needed beyond confirming existing A4 tests still pass.
  - **Releasable**: the unified `rerank_candidates` is the sole reranker surface used by both search and explain paths.
- **Tests (TDD)** — `tests/test_pipeline.py`, `tests/eval/test_eval_suite.py` (run without `-m eval` just to confirm no import errors):
  - Unit: `test_explain_uses_rerank_candidates` — spy on `reranker.rerank_candidates`; call `pipeline.explain(...)`; assert spy called once
  - Unit: `test_explain_does_not_call_private_rerank_with_trace` — assert `reranker._rerank_with_trace` spy NOT called directly by `explain()` (it may be called as alias, but `rerank_candidates` must be the one that does the work)
  - Checkpoint: `uv run pytest tests/test_pipeline.py -v`

#### Task 2.3 — Reconcile retrieval sort tie-break to `(-rrf_score, chunk_id)`

- [ ] **File**: `archon_search/store.py`
- **Depends on**: Task 2.2
- **Description**:
  - In `hybrid_search` production path, the candidates dict is converted to a list and sorted. Locate the sort at around `store.py:687`. Currently it sorts by score descending with no explicit tie-break. Add `.sort(key=lambda r: (-r.score, r.chunk_id))` (or equivalent) — identical to the trace path's `(-rrf_score, chunk_id)` sort at `store.py:1027`.
  - In `_hybrid_search_with_trace` (module-level, around line 1027), confirm the sort already uses `(-c.score_breakdown.rrf_score, c.chunk_id)`. If there is any deviation, align it.
  - This is a retrieval-stage sort only. The rerank sort is not touched.
  - The single-collection eval baseline must not move (retrieval tie-break change may reorder candidates only on exact score ties, which are uncommon on real corpora).
  - **Releasable**: both production and trace retrieval paths now use the same deterministic sort order.
- **Tests (TDD)** — `tests/test_store.py`:
  - Unit: `test_hybrid_search_sort_is_deterministic_on_score_ties` — inject two candidates with identical RRF score into a mocked store; assert `chunk_id` ascending tie-break produces the same order on repeated calls
  - Unit: `test_hybrid_search_with_trace_sort_matches_production_sort` — construct equal-score candidates; assert both sorts produce the same `chunk_id` ordering
  - Checkpoint: `uv run pytest tests/test_store.py -v`

---

### Phase 3 — Pipeline primitive: `search_many`

> **Releasable**: after Task 3.2 — `SearchPipeline.search_many` is callable from tests; no API surface yet.

#### Task 3.1 — New config keys for fan-out

- [ ] **File**: `archon_search/config.py`
- **Depends on**: nothing (can run in parallel with Phase 2)
- **Description**:
  - Add to `SearchConfig`:
    - `max_fanout: int = 8` — cap on number of collections in `collections` list; 422 if exceeded. Must be ≥ 1; `load_config` raises `ConfigError` if `max_fanout < 1`.
    - `fanout_leg_trim: int = 40` — per-leg candidates kept by local RRF before merge. Must be ≥ 1; `load_config` raises `ConfigError` if `fanout_leg_trim < 1`.
    - `fanout_timeout_seconds: float = 30.0` — whole-fan-out timeout using `asyncio.timeout()`. Must be > 0; `load_config` raises `ConfigError` if `fanout_timeout_seconds <= 0`.
  - Add TOML loading in `load_config` for the new keys under `[database]` section (following the existing pattern for `top_k_retrieve`, etc.). Note: these are placed in `[database]` as they govern retrieval-pipeline execution bounds — add a comment in `archon-search.toml.example` clarifying they control the fan-out execution window, not the database schema.
  - Add `archon-search.toml.example` entries for the three keys with comments.
  - **Releasable**: config keys are readable by the pipeline.
- **Tests (TDD)** — `tests/test_config.py`:
  - Unit: `test_max_fanout_default` — `SearchConfig().max_fanout == 8`
  - Unit: `test_fanout_leg_trim_default` — `SearchConfig().fanout_leg_trim == 40`
  - Unit: `test_fanout_timeout_seconds_default` — `SearchConfig().fanout_timeout_seconds == 30.0`
  - Unit: `test_max_fanout_loaded_from_toml` — parse a TOML string with `[database]\nmax_fanout = 4`; assert `config.max_fanout == 4`
  - Unit: `test_max_fanout_zero_raises_config_error` — parse TOML with `max_fanout = 0`; assert `ConfigError` raised
  - Unit: `test_max_fanout_negative_raises_config_error` — parse TOML with `max_fanout = -1`; assert `ConfigError` raised
  - Unit: `test_fanout_leg_trim_zero_raises_config_error` — parse TOML with `fanout_leg_trim = 0`; assert `ConfigError` raised
  - Unit: `test_fanout_timeout_zero_raises_config_error` — parse TOML with `fanout_timeout_seconds = 0.0`; assert `ConfigError` raised
  - Checkpoint: `uv run pytest tests/test_config.py -v`

#### Task 3.2 — Implement `SearchPipeline.search_many`

- [ ] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 2.3, Task 3.1
- **Description**:
  - Add `search_many(self, query: str, collections: list[str], namespace: str = DEFAULT_NAMESPACE) -> SearchPipelineResult` to `SearchPipeline`.
  - Step 1: `vector = await self._embedder.embed_one(query)` — exactly once.
  - Step 2: `all_meta = await self.get_all_collections_meta(namespace)` — use the pipeline's own namespace-filtered method (not `self.store.get_all_collections_meta()` directly); if this raises, propagate as-is (caller maps to 503). Build a dict `{name: meta}`. Validate each name in `collections` is in the dict; if any are missing, raise `CollectionNotFoundError(names: list[str])` (new exception in `pipeline.py`). Exclude collections where `meta.embedding_model != self._embedder.model_name`; add to `excluded_collections`. If all requested collections are excluded (in-scope list empty), return `SearchPipelineResult(results=[], acl_filtered=False, excluded_collections=excluded_collections)` immediately — no fan-out, no error.
  - Step 3: Fan out. Use `async with asyncio.timeout(self._config.fanout_timeout_seconds):` (Python 3.11+ context manager) wrapping `async with asyncio.TaskGroup() as tg:`. Each leg is started as a task calling an inner coroutine that: (a) records its own `monotonic()` start time, (b) calls `self.store.hybrid_search_with_trace(coll, vector, query, candidate_depth=max(self._top_k_retrieve * 3, 20))`, (c) records its own `monotonic()` end time, and (d) returns `(coll_name, candidates, leg_time_ms)`. Using `asyncio.timeout()` ensures that on expiry the `TimeoutError` propagates immediately from the context manager without waiting for TaskGroup teardown. Catch `TimeoutError` (from `asyncio.timeout`) → raise `FanoutTimeoutError`. For leg failures: `ExceptionGroup` from `TaskGroup` is caught with `except* Exception as eg:` and the first exception from `eg.exceptions[0]` is re-raised as a plain exception, so the route handler's existing HTTP 500 mapping fires without needing to handle `ExceptionGroup`.
  - Step 4: Per-leg trim. For each leg's `list[ScoredSearchCandidate]`, sort by `(-score_breakdown.rrf_score, chunk_id)` and keep top `max(self._config.fanout_leg_trim, 1)`.
  - Step 5: Merge. Concatenate legs in ascending collection-name order (sorted). Each candidate retains `collection` provenance from `ScoredSearchCandidate.collection`.
  - Step 6: ACL. `merged, acl_filtered = apply_acl_filter(merged, lambda c: c.acl, namespace)`. Requires `ScoredSearchCandidate.acl: list[str] | None` — verify or add in Task 2.1.
  - Step 7: Rerank. `ranked = await self._reranker.rerank_candidates(query, merged, top_k=self._top_k_return)`.
  - Step 8: Convert. `results = [_candidate_to_search_result(c) for c in ranked]` where `_candidate_to_search_result` is a private helper that maps `ScoredSearchCandidate` → `SearchResult` (including `collection`). The `score` field of `SearchResult` is set to `c.score_breakdown.reranker_score`; this must be asserted non-None (e.g., `assert c.score_breakdown.reranker_score is not None`) since rerank is unconditional in `search_many`.
  - Collect per-leg `leg_time_ms` values from Step 3 into `FanoutTimings(leg_times: dict[str, float], rerank_time_ms: float)` (rerank time measured around Step 7). Store as `SearchPipelineResult.fanout_timings`.
  - Return `SearchPipelineResult(results=results, acl_filtered=acl_filtered, excluded_collections=excluded_collections, fanout_timings=fanout_timings)`.
  - Add `CollectionNotFoundError(names: list[str])` and `FanoutTimeoutError` exceptions to `pipeline.py`.
  - **Releasable**: `pipeline.search_many` is callable and tested at unit level; no API surface yet.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Unit: `test_search_many_embeds_once` — spy on `embedder.embed_one`; call `search_many` with 3 collections; assert spy called exactly once
  - Unit: `test_search_many_reranks_once` — spy on `reranker.rerank_candidates`; call `search_many` with 2 collections; assert spy called exactly once and `len(spy.call_args.args[1]) == sum_of_trimmed_per_leg_pool`
  - Unit: `test_search_many_result_carries_collection_provenance` — use two mock collections each returning distinct `ScoredSearchCandidate` objects with distinct `collection` values; assert each returned `SearchResult.collection` matches its origin
  - Unit: `test_search_many_merge_order_deterministic` — two collections with identical stub candidates; assert output ordering is stable across calls
  - Unit: `test_search_many_namespace_scope_excludes_out_of_namespace` — mock metadata returns one collection in namespace A and one in namespace B; call with `namespace="A"`; assert collection B not searched (its leg never runs)
  - Unit: `test_search_many_missing_collection_raises_collection_not_found` — requested name absent from metadata → `CollectionNotFoundError`
  - Unit: `test_search_many_model_mismatch_excludes_and_reports` — collection with `embedding_model="other-model"` → excluded, present in `SearchPipelineResult.excluded_collections`; its `hybrid_search_with_trace` never called
  - Unit: `test_search_many_leg_failure_cancels_siblings_and_raises` — first leg's task raises a plain `RuntimeError("leg failed")`; assert `search_many` raises a plain `RuntimeError` (NOT an `ExceptionGroup`); confirm `hybrid_search_with_trace` for the other leg is eventually cancelled (use a mock that records cancellation)
  - Unit: `test_search_many_timeout_raises_fanout_timeout_error` — inject a slow leg by mocking `hybrid_search_with_trace` with a never-resolving coroutine; set `fanout_timeout_seconds=0.001`; assert `FanoutTimeoutError` raised
  - Unit: `test_search_many_single_collection_matches_search` — with identical mock backends for both `hybrid_search` and `hybrid_search_with_trace` (same stub returning same candidates), call `search_many(query, ["col"])` and `search(query, "col")` and compare result sets (field-subset equality minus `collection`). Note: this test requires matching mock behavior between both retrieval paths; it is a contract test over mocked backends, not a real pipeline comparison.
  - Unit: `test_same_chunk_id_in_two_collections_both_survive` — same `chunk_id` in collections A and B; assert both appear in merged pool (no cross-collection dedup)
  - Unit: `test_search_many_all_collections_model_mismatched_returns_empty` — all requested collections have `embedding_model` mismatching embedder; assert `search_many` returns `SearchPipelineResult(results=[], excluded_collections=[all_requested])`; assert `hybrid_search_with_trace` never called
  - Checkpoint: `uv run pytest tests/test_pipeline.py -v`

---

### Phase 4 — REST surface

> **Releasable**: after Task 4.1 — `POST /search` accepts `collections` and returns merged results with `collection` provenance and `excluded_collections`.

#### Task 4.1 — `SearchRequest` validator and handler update

- [ ] **File**: `archon_search/server/routes_search.py`
- **Depends on**: Task 3.2
- **Description**:
  - Change `SearchRequest.collection: str` to `collection: str | None = None`. Keep `collection_nonempty` validator but guard it with `if v is not None`.
  - Add `collections: list[str] | None = None`.
  - Add a `model_validator(mode="after")` that enforces exactly-one-of `{collection, collections}`:
    - Both set → `ValueError("supply either collection or collections, not both")`
    - Neither set → `ValueError("supply either collection or collections")`
    - `collections` empty list → `ValueError("collections must not be empty")`
    - `collections` length > `_FANOUT_VALIDATION_LIMIT` (a module-level constant in `routes_search.py`, default 8, matching the default `max_fanout` config value) → `ValueError(f"collections length exceeds maximum of {_FANOUT_VALIDATION_LIMIT}")`. **Important**: this constant is a Pydantic-layer guard and intentionally matches the default `max_fanout=8`. If an operator increases `max_fanout` in `archon-search.toml` beyond 8, they must also update `_FANOUT_VALIDATION_LIMIT` in the source. This divergence is a known limitation (see Known Limitations). Do NOT read config at Pydantic model construction time — Pydantic validators must not have external dependencies.
    - Per-item: strip whitespace; if blank → `ValueError("collection names must not be empty or whitespace")`
    - Deduplicate while preserving first-occurrence order.
  - Update the `/search` handler:
    - If `body.collection` is set: follow the existing single-collection path (unchanged).
    - If `body.collections` is set: call `pipeline.search_many(body.query, body.collections, namespace=ns)` inside the existing `asyncio.wait_for` wrapper (reuse `_SEARCH_TIMEOUT_SECONDS` as the outer guard; `search_many` has its own inner fan-out timeout — the outer one is a belt-and-suspenders guard).
    - Map `CollectionNotFoundError` → `JSONResponse({"detail": "collection not found"}, status_code=404)`.
    - Map `FanoutTimeoutError` → `HTTPException(status_code=504, detail="Search timed out")`.
    - Map metadata-lookup `Exception` → `JSONResponse({"detail": "service unavailable"}, status_code=503)`.
    - Populate `SearchResponse.excluded_collections` from `result.excluded_collections` using: `[ExcludedCollectionSchema(name=e.name, reason=e.reason) for e in result.excluded_collections]` — explicit conversion from `ExcludedCollection` dataclass to `ExcludedCollectionSchema` Pydantic model.
  - **Releasable**: `POST /search` with `collections` returns merged, reranked results with provenance.
- **Tests (TDD)** — `tests/test_routes_search.py`:
  - Unit: `test_search_request_both_fields_is_422` — `SearchRequest(collection="x", collections=["y"], query="q")` raises `ValidationError`
  - Unit: `test_search_request_neither_field_is_422` — `SearchRequest(query="q")` raises `ValidationError`
  - Unit: `test_search_request_empty_collections_is_422` — `collections=[]` raises `ValidationError`
  - Unit: `test_search_request_over_max_fanout_is_422` — `collections=["c"]*9` (more than 8) raises `ValidationError`
  - Unit: `test_search_request_whitespace_entry_is_422` — `collections=["  "]` raises `ValidationError`
  - Unit: `test_search_request_deduplicates` — `collections=["a","a","b"]` → `["a","b"]`
  - Unit: `test_search_request_exactly_max_fanout_is_valid` — `collections=["c"]*8` (exactly 8) → valid (boundary inclusive)
  - Unit: `test_search_request_single_collection_still_valid` — `collection="x"` without `collections` → valid
  - Unit: `test_search_handler_multi_collection_calls_search_many` — mock `pipeline.search_many`; POST `/search` with `collections`; assert `search_many` called
  - Unit: `test_search_handler_missing_collection_returns_404` — `search_many` raises `CollectionNotFoundError`; assert HTTP 404
  - Unit: `test_search_handler_fanout_timeout_returns_504` — `search_many` raises `FanoutTimeoutError`; assert HTTP 504
  - Unit: `test_search_handler_meta_lookup_failure_returns_503` — `get_all_collections_meta` raises; assert HTTP 503
  - Unit: `test_search_response_includes_excluded_collections` — `search_many` returns excluded collection; assert schema field populated
  - Contract: `test_search_response_json_includes_collection_key` — deterministic mock; assert `results[0]["collection"]` present in JSON response
  - Checkpoint: `uv run pytest tests/test_routes_search.py -v`

---

### Phase 5 — MCP surface

> **Releasable**: after Task 5.1 — MCP `search` tool accepts `collections` and returns results with `collection` and `excluded_collections`.

#### Task 5.1 — MCP `search` tool: `collections` parameter and `BREAKING.md`

- [ ] **File**: `archon_search/server/mcp.py`, `BREAKING.md`
- **Depends on**: Task 4.1
- **Description**:
  - In the MCP `search` tool function (`mcp.py:86`), add `collections: list[str] | None = None` parameter alongside the existing `collection: str | None = None`.
  - Add the same exactly-one-of validation logic as `SearchRequest` (the MCP layer validates manually because it doesn't use Pydantic model validators):
    - Both → return `McpErrorResponse(error="supply either collection or collections, not both", code="validation_error")`
    - Neither → return `McpErrorResponse(error="supply either collection or collections", code="validation_error")`
    - `collections` empty → `McpErrorResponse(error="collections must not be empty", code="validation_error")`
    - Length > `MAX_FANOUT` → `McpErrorResponse(error=f"collections length exceeds {MAX_FANOUT}", code="validation_error")`
    - Per-item whitespace → `McpErrorResponse(error="collection names must not be whitespace", code="validation_error")`
    - Deduplicate.
  - If `collections` set: call `pipeline.search_many(query, collections, ...)`. Map `CollectionNotFoundError` → `McpErrorResponse(code="not_found")`. Map `FanoutTimeoutError` → `McpErrorResponse(code="timeout")`.
  - `asdict()` on `SearchResult` already picks up `collection` (Task 1.1). `excluded_collections` is added to the MCP result dict manually (list of `{"name": ..., "reason": ...}` dicts).
  - Add a `BREAKING.md` entry under today's date: REST `/search` and `/explain` responses gain additive `collection` key per result and `excluded_collections` key per response (non-breaking for tolerant JSON clients). MCP `search`/`explain` response shapes gain `collection` and `excluded_collections` (true contract change for strict-validating MCP clients — same class as A1's additive-key break). The new `collections` request field is additive/optional on both surfaces.
  - **Releasable**: MCP `search` tool supports multi-collection requests; `BREAKING.md` documents the contract change.
- **Tests (TDD)** — `tests/test_mcp.py`:
  - Unit: `test_mcp_search_both_collection_fields_is_error` — both set → `McpErrorResponse` with `code="validation_error"`
  - Unit: `test_mcp_search_collections_calls_search_many` — mock `pipeline.search_many`; assert called when `collections` provided
  - Unit: `test_mcp_search_result_includes_collection_key` — assert `result["collection"]` present in response dict
  - Unit: `test_mcp_search_result_includes_excluded_collections_key` — mock returns excluded collection; assert `result["excluded_collections"]` present
  - Unit: `test_mcp_search_missing_collection_returns_not_found` — `CollectionNotFoundError` → `code="not_found"`
  - Unit: `test_mcp_search_fanout_timeout_returns_timeout` — `FanoutTimeoutError` → `code="timeout"`
  - Checkpoint: `uv run pytest tests/test_mcp.py -v`

---

### Phase 6 — Explain co-design

> **Releasable**: after Task 6.1 — `/explain` (REST + MCP) accepts `collections`, renders per-collection provenance, and enforces `rerank=false`+multi → 422.

#### Task 6.1 — Extend `SearchPipeline.explain` and A4 schemas for multi-collection

- [ ] **File**: `archon_search/pipeline.py`, `archon_search/server/routes_explain.py`, `archon_search/server/mcp.py`
- **Depends on**: Task 3.2
- **Description**:
  - `SearchPipeline.explain` gains `collections: list[str] | None = None`. When `collections` is set:
    1. Validate exactly-one-of `{collection, collections}` (raise `ValueError` if both or neither).
    2. If `rerank=False` and `len(collections) > 1` → raise `ExplainMultiCollectionNoRerankError` (new exception in `pipeline.py`) with message `"reranking cannot be disabled for multi-collection search in v1"`. If `rerank=False` and `len(collections) == 1`, the single-collection explain path is used (same behavior as `rerank=False` with `collection="x"`) — no error.
    3. Load meta, scope, and exclude model-mismatch collections (same as `search_many` step 2). Use `asyncio.timeout()` not `asyncio.wait_for()`.
    4. Fan-out via `asyncio.TaskGroup`; per-leg trim; deterministic merge (ascending collection-name order).
    5. ACL filter on merged pool.
    6. If `rerank=True` (mandatory for multi with len > 1): `reranker.rerank_candidates(query, merged, top_k=len(merged))`.
    7. Final sort: `(-final_score, doc_id, chunk_id)` where `final_score = reranker_score if not None else rrf_score`.
    8. `top_results = candidates[:top_k]`; `near_misses = candidates[top_k : top_k + 20]`.
    9. Return `ExplainPipelineResult(top_results=top_results, near_misses=near_misses, acl_filtered=acl_filtered)`.
  - `ExplainResult` and `ExplainNearMiss` Pydantic models gain `collection: str = ""` field; `from_candidate` classmethods populate it from `ScoredSearchCandidate.collection`.
  - REST `ExplainRequest` gains `collections: list[str] | None = None`; add exactly-one-of validator **in the Pydantic model_validator** (not in the route handler) and the `rerank=false`+multi (`len(collections) > 1`) → 422 guard with reason string `"reranking cannot be disabled for multi-collection search in v1"`. The guard fires at model construction time (consistent with `SearchRequest` validators). Route handler does not duplicate this check.
  - MCP `explain` tool gains `collections: list[str] | None = None`; same validation (manual, not Pydantic); `ExplainMultiCollectionNoRerankError` → `McpErrorResponse(error="reranking cannot be disabled for multi-collection search in v1", code="validation_error")`.
  - **Releasable**: `/explain` supports multi-collection provenance; `rerank=false`+multi is rejected everywhere `/explain` is exposed.
- **Tests (TDD)** — `tests/test_routes_explain.py`, `tests/test_mcp.py`, `tests/test_pipeline.py`:
  - Unit: `test_explain_rerank_false_multi_collections_is_422_rest` — `ExplainRequest(query="q", collections=["a","b"], rerank=False)` raises `ValidationError`
  - Unit: `test_explain_rerank_false_single_collection_is_valid` — `rerank=False` with single `collection` → valid (existing A4 behavior)
  - Unit: `test_explain_rerank_false_multi_mcp_returns_error` — MCP `explain` with `collections=["a","b"]`, `rerank=False` → `McpErrorResponse` with message matching the fixed reason string
  - Unit: `test_explain_multi_collection_result_carries_collection` — `ExplainResult.collection` populated from candidate's `collection` field
  - Unit: `test_explain_near_miss_carries_collection` — same for `ExplainNearMiss`
  - Unit: `test_pipeline_explain_multi_collection_fans_out` — spy on `store.hybrid_search_with_trace`; `pipeline.explain` with 2 collections; assert spy called twice
  - Unit: `test_pipeline_explain_multi_reranks_once` — spy on `reranker.rerank_candidates`; assert called once over merged pool
  - Checkpoint: `uv run pytest tests/test_routes_explain.py tests/test_mcp.py tests/test_pipeline.py -v`

---

### Phase 7 — Observability and telemetry

> **Releasable**: after Task 7.1 — fan-out count and per-leg timings are recorded in telemetry; `/explain` surfaces per-collection leg timing in its B1 breakdown.

#### Task 7.1 — `fanout_collections` telemetry and per-leg timings

- [ ] **File**: `archon_search/telemetry/entry.py`, `archon_search/server/routes_search.py`, `archon_search/server/mcp.py`, `archon_search/pipeline.py`
- **Depends on**: Task 5.1, Task 6.1
- **Description**:
  - Add `EndpointKind.search_multi = "search_multi"` to `EndpointKind` (`telemetry/entry.py`).
  - Add `TelemetryEntry.from_search_multi_result(*, collections: list[str], fanout_count: int, result_count: int, latency_ms: float, excluded_count: int) -> TelemetryEntry` factory (keyword-only, no `query` parameter — preserves no-raw-query invariant). `fanout_count` is the number of collections that were actually searched (after exclusions).
  - In the `/search` handler and MCP `search` tool, call the new factory instead of `from_search_tool_result` when `collections` was supplied; pass collection names (already permitted in `/route` telemetry) and `fanout_count`.
  - Per-leg timing in `SearchPipeline.search_many`: each leg's coroutine captures its own `monotonic()` start and end time internally (before and after the `hybrid_search_with_trace` call within the leg's async task closure). Timing is NOT recorded around the `TaskGroup` as a whole (which would only yield wall-clock for all concurrent legs, not per-leg). After the `TaskGroup` completes, assemble `leg_times: dict[str, float]` from the per-leg results. Record rerank wall-clock time around Step 7. Store all timings in a `FanoutTimings(leg_times: dict[str, float], rerank_time_ms: float)` dataclass added to `pipeline.py`. Include `FanoutTimings` in `SearchPipelineResult` as an optional field (`fanout_timings: FanoutTimings | None = None`). The `/explain` route reads `fanout_timings` and includes it in the B1 breakdown when non-None.
  - The no-raw-query structural invariant is asserted by the existing factory-constructor test pattern: `test_from_search_multi_result_has_no_query_param` — inspect `TelemetryEntry.from_search_multi_result.__code__.co_varnames` and assert `"query"` is absent.
  - **Releasable**: multi-collection requests emit telemetry with fan-out count; per-leg timings are available for B1 integration.
- **Tests (TDD)** — `tests/telemetry/test_entry.py`, `tests/test_pipeline.py`:
  - Unit: `test_from_search_multi_result_no_query_param` — assert `"query"` not in `inspect.signature(TelemetryEntry.from_search_multi_result).parameters`
  - Unit: `test_from_search_multi_result_records_fanout_count` — `TelemetryEntry.from_search_multi_result(collections=["a","b"], fanout_count=2, result_count=5, latency_ms=100.0, excluded_count=0).fanout_count == 2`
  - Unit: `test_search_many_result_includes_fanout_timings` — mock two legs each resolving with stub candidates; assert `SearchPipelineResult.fanout_timings` is non-None, has `leg_times` with an entry for each collection name, and `rerank_time_ms` is a non-negative float
  - Checkpoint: `uv run pytest tests/telemetry/ tests/test_pipeline.py -v`

---

### Phase 8 — Eval fixture and documentation

> **Releasable**: after Task 8.1 — eval harness passes with multi-collection merge fixture; all architecture docs updated.

#### Task 8.1 — Final verification & documentation update

- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - **Eval fixture**: add a multi-collection merge fixture under `tests/eval/` — a fixed two-collection corpus with deterministic candidates and known merge/rerank ordering. The fixture tests merge correctness (provenance tags, dedup behavior, rerank produces correct ordering) NOT routing selection (that is B4). Follow the fixture schema documented in `tests/eval/README.md`. Run `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py`; thresholds unchanged unless waived per `tests/eval/README.md`.
  - **Eval fixture coverage for `SearchResult` with `collection`**: update `tests/eval/` fixtures that construct `SearchResult` directly to include the new `collection` field (the brief flags this risk: eval fixtures may construct `SearchResult` directly, so adding `collection` requires updating them).
  - **Integration tests** (`@pytest.mark.integration`, real LanceDB): two-collection fan-out returns merged ranked results with correct provenance; three-collection fan-out where one collection has no FTS index degrades gracefully (vector-only on that leg) without failing the request; single-collection `collections:["x"]` is identical to `collection:"x"` (field-subset equality).
  - **Documentation**: spawn an agent to:
    - Update `Documentation/Architecture/120_services_and_integration_architecture.md` — add the search fan-out path (embed-once → parallel legs → merge → rerank)
    - Update `Documentation/Architecture/600_api_reference_or_public_interface.md` — add `collections` request field, `collection` per result, `excluded_collections` per response; update REST `/explain` multi-collection behavior; update MCP `search`/`explain` tool shapes; note the breaking change for strict MCP clients
    - Update `Documentation/Architecture/210_performance_and_scalability.md` — add fan-out concurrency model, embed-once win, reranker serialization known constraint, `fanout_leg_trim` as bounded rerank pool
    - Add a forward note in the above docs that B4 supplies the collection shortlist that B3 consumes
    - Do NOT update docs unrelated to B3 changes
  - **Coverage check**: `uv run pytest --cov=archon_search --cov-fail-under=85` passes.
  - **Acceptance criteria** (must all pass):
    - `POST /search` with `collections: ["a", "b"]` returns one merged result list; each result has `collection` field naming origin
    - `POST /search` with `collections: ["a", "b"]` and spy on `embed_one`: called exactly once
    - `POST /search` with `collections: ["a", "b"]` and spy on `rerank_candidates`: called exactly once over the merged pool
    - `POST /search` with `collection: "x"` (single): result set and ordering identical to today. The response differs additively: each result gains `collection: str`, and the response gains `excluded_collections: []`. Field-subset equality means: all pre-B3 fields in the response are unchanged; only these additive fields are new.
    - `POST /search` with `collections: ["x"]` (length-1): identical result set to `collection: "x"` on all pre-B3 fields (field-subset equality). The `fanout_timings` field is present in `SearchPipelineResult` but is not exposed in the REST response — it is only used for B1 telemetry and `/explain` breakdown.
    - `POST /search` with both `collection` and `collections` → HTTP 422
    - `POST /search` with neither `collection` nor `collections` → HTTP 422
    - `POST /search` with `collections` referencing a missing or out-of-namespace collection → HTTP 404; reason/shape identical whether missing or out-of-namespace
    - `POST /search` with `collections` including a model-mismatched collection → HTTP 200; mismatched collection listed in `excluded_collections`; it is NOT searched
    - `POST /search` fan-out with one leg timing out → HTTP 504
    - `POST /search` fan-out with one leg failing → HTTP 500; sibling legs cancelled
    - Metadata lookup failure → HTTP 503
    - MCP `search` with `collections` → same multi-collection behavior; `collection` key present in each result dict; `excluded_collections` key present in response
    - `POST /explain` with `collections` + `rerank=false` → HTTP 422 with message `"reranking cannot be disabled for multi-collection search in v1"`
    - MCP `explain` with `collections` + `rerank=false` → `McpErrorResponse` with same message
    - `POST /explain` with `collections` → per-result `collection` field present in `ExplainResult` and `ExplainNearMiss`
    - Telemetry entry for multi-collection search: `fanout_collections` count recorded; no `query` field
    - `uv run pytest` (default run): ≥85% coverage; no failures
    - `uv run pytest -m eval ...`: eval harness passes; single-collection baseline unchanged
    - `uv run pytest -m integration`: two-collection and three-collection (one no-FTS) integration tests pass
    - `BREAKING.md` contains entry for additive REST fields and MCP contract change
    - `Architecture/120_`, `600_`, `210_` docs updated with B3 changes and B4 forward-reference
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: `uv run pytest && uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py && uv run pytest -m integration`
