# B3 — Server-Side Multi-Collection Search Primitive

**Purpose**: Ship a server-side primitive that embeds a query once, fans out hybrid retrieval across an explicit set of collections in parallel, merges the candidate pools with full provenance, and runs one global rerank pass — returning a single unified result list in which every result is tagged with its source collection.
**Audience**: archon-search contributors implementing B3 and reviewers of the resulting PRs.
**Status**: To Do

> **Note**: Line number references in this plan are approximate and may be stale. Use the function/class names to locate the relevant code.

> **Order**: Ships AFTER A1 (metadata schema), A3 (search-failure semantics), A4 (explain endpoint). Lands in Phase B after B1 (per-stage timings). Gated by the deterministic eval harness (`tests/eval/`); B6 is NOT a prerequisite.

---

## Background

The only search primitive today is single-collection: `SearchPipeline.search()` embeds once, calls `store.hybrid_search()` against one collection, and rernanks one pool. REST `/search` and MCP `search` both expose only `collection: str`. The `/route` endpoint tells callers which collections are relevant but does not search them.

Multi-collection queries are therefore assembled client-side, paying N embeddings for one query, per-collection reranking that produces incomparable RRF scores (local rank spaces), and no debuggable routing path through `/explain`.

The cross-encoder reranker already scores `(query, candidate.text)` pairs independent of collection. `ScoredSearchCandidate` already carries a `collection` field (`ScoredSearchCandidate` in `_diagnostics.py`). The missing piece is a production fan-out that unifies the two diverging candidate/rerank type systems: the production path returns plain `SearchResult` (no rank provenance) while the provenance-carrying `ScoredSearchCandidate` only flows through the eval/debug-only `_hybrid_search_with_trace` (marked private), located in `store.py`.

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
- Filters (`SearchFilters`) in multi-collection search — the `_hybrid_search_with_trace` trace path (used by `search_many`) does not support SQL predicates. `POST /search` with both `collections` and `filters` → HTTP 422 with message `"filters are not supported for multi-collection search in v1"`. The `SearchRequest` model_validator enforces this.

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 8.1 — Final verification & documentation update].

---

## What does NOT change

- `SearchPipeline.search()` observable contract: same result set and ordering as today. Additive changes only: `SearchResult` gains `collection: str = ""`, `SearchPipelineResult` gains `excluded_collections: list[ExcludedCollection] = []` and `fanout_timings: FanoutTimings | None = None`. All pre-B3 fields are unchanged (field-subset equality, not byte-for-byte).
- Single-collection eval baseline (`tests/eval/baselines/baseline.json`) must not move
- `Reranker` stable-sort contract (Reranker stable-sort contract — search for `.sort(` in `rerank_candidates` / `_rerank_with_trace`; pinned by `test_P14_6_reranker_stable_order_on_equal_scores`) — no new rerank tie-break key
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
- **asyncio.timeout() for fan-out timeout (not asyncio.wait_for)**: When `asyncio.timeout()` fires, it raises `TimeoutError` via task cancellation. The `TaskGroup.__aexit__` still cancels and awaits child task cleanup before re-raising. The advantage over `asyncio.wait_for` is that `asyncio.timeout()` as a context manager makes intent clearer and composes better with `TaskGroup`. Leg coroutines must be cancellation-safe (no uncancellable blocking I/O) for the timeout to be effective.
- **All collections model-mismatched → empty result set (HTTP 200)**: if every requested collection is excluded due to model mismatch, `search_many` performs zero fan-out legs and returns an empty `SearchPipelineResult` (results=[], excluded_collections=[all]). This is a valid HTTP 200, not a 422 or 404.
- **`fanout_leg_trim` and `max_fanout` must be ≥ 1**: values ≤ 0 are rejected by config validation with `ConfigError`. See Task 3.1.
- **Trim-before-ACL recall interaction**: per-leg trim (`fanout_leg_trim`) is applied before the ACL filter. If the top-N candidates by RRF score all fail ACL, lower-ranked candidates that would pass ACL are unreachable. Operators with fine-grained ACL policies should set `fanout_leg_trim` generously to mitigate this.
- **Config section split**: `top_k_retrieve`/`top_k_return` live under `[database]` while fan-out keys (`max_fanout`, `fanout_leg_trim`, `fanout_timeout_seconds`) live under `[search]`. This is acknowledged tech debt; a future cleanup will migrate all search-execution parameters to `[search]`.

---

## Architecture

### New types

- `ExcludedCollection(name: str, reason: str)` — dataclass in `archon_search/_types.py`; reason values: `"embedding_model_mismatch"`
- `SearchPipelineResult.excluded_collections: list[ExcludedCollection]` — added field (default empty list)
- `SearchResult.collection: str` — new field, populated at the row-to-`SearchResult` site (in `hybrid_search`, the row-to-`SearchResult` mapping site — search for `SearchResult(` in `hybrid_search` function body)

### Modified types

- `Reranker`: `rerank(query, candidates: list[SearchResult], top_k)` is kept for backward compat; `rerank_candidates(query, candidates: list[ScoredSearchCandidate], top_k) -> list[ScoredSearchCandidate]` is the new unified production-grade surface (promoted from `_rerank_with_trace`, which becomes a thin alias calling `rerank_candidates`)
- `_hybrid_search_with_trace` signature unchanged; the sort order reconciled to `(-rrf_score, chunk_id)` (was already that in the trace path; the production `hybrid_search` retrieval sort — search for `.sort(` in `hybrid_search` after score computation — gains an explicit `chunk_id` tie-break)

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
3. Fan-out: use `asyncio.timeout(self._fanout_timeout_seconds)` context manager (Python 3.11+, not `asyncio.wait_for`) wrapping an `asyncio.TaskGroup`. Each leg's coroutine captures its own `monotonic()` start/end time internally and returns `(collection_name, list[ScoredSearchCandidate], leg_time_ms)`. When `asyncio.timeout()` fires, it raises `TimeoutError` via task cancellation; `TaskGroup.__aexit__` still cancels and awaits child task cleanup before re-raising — leg coroutines must be cancellation-safe. On `TimeoutError` raised by `asyncio.timeout()`, convert to `FanoutTimeoutError`. On `ExceptionGroup` from `TaskGroup`: unwrap with `except* Exception as eg`, log the full `ExceptionGroup` at ERROR level (`logger.error('search_many fan-out: %d legs failed: %s', len(eg.exceptions), eg)`), then re-raise `eg.exceptions[0]` as a plain exception so the route handler's existing 500 mapping fires without needing to handle `ExceptionGroup`.
4. Per-leg trim to top-`fanout_leg_trim` by local RRF score (`max(self._fanout_leg_trim, 1)` to guard against zero). Concatenate legs in ascending collection-name order; within each leg candidates are in `(-rrf_score, chunk_id)` order. `ScoredSearchCandidate` must carry an `acl` field (confirmed present or added in Task 1.1/2.1 — see note below).
5. `apply_acl_filter(merged, lambda c: c.acl, namespace)` — `ScoredSearchCandidate.acl` must exist. If it is not currently present, Task 2.1 must add it when promoting the trace path (or confirm it is already there by reading `_diagnostics.py` before implementation).
6. `reranker.rerank_candidates(query, merged, top_k=top_k_return)` → unified top-k
7. Convert `ScoredSearchCandidate` → `SearchResult` via `_candidate_to_search_result(c)` private helper. `score` is set to `c.score_breakdown.reranker_score`; implementer must assert this is non-None since rerank is unconditional in `search_many`. `collection` is copied from `c.collection`.

> **Note on `ScoredSearchCandidate.acl`**: before implementing Task 3.2, verify that `_diagnostics.py:ScoredSearchCandidate` carries an `acl: list[str] | None` field. If absent, Task 2.1 must add it as part of the unification work (the field already flows through `store.py`'s `_hybrid_search_with_trace` path as it constructs `ScoredSearchCandidate`).

### New config keys (added to `SearchConfig`)

| Key | Type | Default | Section |
|---|---|---|---|
| `max_fanout` | `int` | `8` | `[search]` |
| `fanout_leg_trim` | `int` | `40` | `[search]` |
| `fanout_timeout_seconds` | `float` | `30.0` | `[search]` |

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

- [x] **File**: `archon_search/_types.py`, `archon_search/store.py`, `archon_search/server/schemas.py`, `archon_search/server/routes_search.py`, `archon_search/server/routes_explain.py`
- **Depends on**: nothing
- **Description**:
  - Add `collection: str = ""` field to `SearchResult` dataclass (`_types.py`). Default empty string ensures backward compat at call sites that construct `SearchResult` directly (tests, eval fixtures).
  - In `store.py`, `hybrid_search` populates the new field at the row-to-`SearchResult` mapping site (search for `SearchResult(` in `hybrid_search` function body). The `collection` parameter is already available in `hybrid_search(self, collection, ...)` — pass it through: `SearchResult(..., collection=collection)`.
  - In `SearchResultSchema` (`routes_search.py` — search for `SearchResultSchema`), add `collection: str = ""` and extend `from_result` to include `collection=r.collection`.
  - Add `excluded_collections: list[ExcludedCollectionSchema] = Field(default_factory=list)` to `SearchResponse` (`routes_search.py` — search for `SearchResponse`) so the field exists from day one; it is always empty for single-collection requests.
  - Add `ExcludedCollectionSchema(name: str, reason: str)` Pydantic model to `archon_search/server/schemas.py` (the canonical location for shared response models). Import it in `routes_search.py` and `routes_explain.py`.
  - Add `ExcludedCollection(name: str, reason: str)` dataclass to `_types.py`.
  - Add `excluded_collections: list[ExcludedCollection] = field(default_factory=list)` to `SearchPipelineResult`.
  - The `/search` handler (search for the `/search` handler in `routes_search.py`) maps `result.excluded_collections` into `SearchResponse.excluded_collections` using: `excluded_collections=[ExcludedCollectionSchema(name=e.name, reason=e.reason) for e in result.excluded_collections]`. (Empty list for now on single-collection path.) No behavior change.
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

- [x] **File**: `archon_search/reranker.py`
- **Depends on**: Task 1.1
- **Description**:
  - Rename `_rerank_with_trace` to `rerank_candidates` (public production method). Signature stays identical: `async def rerank_candidates(self, query: str, candidates: list[ScoredSearchCandidate], top_k: int) -> list[ScoredSearchCandidate]`.
  - Keep `_rerank_with_trace` as a thin alias: `async def _rerank_with_trace(self, query, candidates, top_k): return await self.rerank_candidates(query, candidates, top_k)`. This preserves backward compat for any direct call sites that are not migrated in this task (A4 call sites and eval migrated in Task 2.2).
  - No behavior change. Stable-sort contract (Reranker stable-sort — search for `.sort(` in `rerank_candidates`) unchanged; `test_P14_6_reranker_stable_order_on_equal_scores` must still pass.
  - **Pre-condition check**: before implementing, read `archon_search/_diagnostics.py` and confirm `ScoredSearchCandidate` carries `acl: list[str] | None`. If absent, add it in this task (it already flows through the `_hybrid_search_with_trace` path in `store.py`). This field is required by `apply_acl_filter` in `search_many` Step 6.
  - **Releasable**: `Reranker.rerank_candidates` is callable from production code; `ScoredSearchCandidate.acl` is confirmed present.
- **Tests (TDD)** — `tests/test_reranker.py`:
  - Unit: `test_rerank_candidates_is_public` — assert `hasattr(Reranker, "rerank_candidates")` and it is not prefixed with underscore
  - Unit: `test_rerank_candidates_returns_scored_candidates` — pass a list of two `ScoredSearchCandidate` objects with stub backend; assert output is sorted by `reranker_score` descending
  - Unit: `test_rerank_candidates_stable_sort_on_equal_scores` — equal reranker scores preserve input order (extends existing `test_P14_6_*` contract)
  - Unit: `test_rerank_with_trace_alias_delegates` — `_rerank_with_trace` call returns same result as `rerank_candidates`
  - Checkpoint: `uv run pytest tests/test_reranker.py -v`

#### Task 2.2 — Migrate A4 `explain()` and `eval/_tracing.py` to `rerank_candidates`

- [x] **File**: `archon_search/pipeline.py`, `archon_search/eval/_tracing.py`
- **Depends on**: Task 2.1
- **Description**:
  - In `SearchPipeline.explain()` (search for `explain` method in `pipeline.py`): replace `self._reranker._rerank_with_trace(...)` with `self._reranker.rerank_candidates(...)`. Signature is identical; no behavior change.
  - In `archon_search/eval/_tracing.py` (around lines 102, 107): replace any direct calls to `reranker._rerank_with_trace` with `reranker.rerank_candidates`. If `_tracing.py` calls `_hybrid_search_with_trace` as the module-level function (not the instance method), leave it — that is a separate path. Only the reranker call site is changed here.
  - The A4 `explain()` behavior is identical; no test changes needed beyond confirming existing A4 tests still pass.
  - **Releasable**: the unified `rerank_candidates` is the sole reranker surface used by the explain and eval-trace paths. The single-collection `search()` path still uses `rerank()` (takes `list[SearchResult]`) and is unaffected by this task; `search_many` adopts `rerank_candidates` when it lands (Task 3.2).
- **Tests (TDD)** — `tests/test_pipeline.py`, `tests/eval/test_eval_suite.py` (run without `-m eval` just to confirm no import errors):
  - Unit: `test_explain_uses_rerank_candidates` — spy on `reranker.rerank_candidates`; call `pipeline.explain(...)`; assert spy called once
  - Unit: `test_explain_does_not_call_private_rerank_with_trace` — assert `reranker._rerank_with_trace` spy NOT called directly by `explain()` (it may be called as alias, but `rerank_candidates` must be the one that does the work)
  - Checkpoint: `uv run pytest tests/test_pipeline.py -v`

#### Task 2.3 — Reconcile retrieval sort tie-break to `(-rrf_score, chunk_id)`

- [x] **File**: `archon_search/store.py`
- **Depends on**: Task 2.2
- **Description**:
  - In `hybrid_search` production path, the candidates dict is converted to a list and sorted. Locate the sort (search for `.sort(` in `hybrid_search` after score computation). Currently it sorts by score descending with no explicit tie-break. Add `.sort(key=lambda r: (-r.score, r.chunk_id))` (or equivalent) — identical to the trace path's `(-rrf_score, chunk_id)` sort.
  - In `_hybrid_search_with_trace` (module-level function), confirm the sort already uses `(-c.score_breakdown.rrf_score, c.chunk_id)` (search for `.sort(` in the function body). If there is any deviation, align it.
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

#### Task 3.1 — New config keys for fan-out and `SearchPipeline` constructor update

- [x] **File**: `archon_search/config.py`, `archon_search/pipeline.py`
- **Depends on**: nothing (can run in parallel with Phase 2)
- **Description**:
  - **Sub-task 3.1a — `SearchPipeline` constructor parameters**: Update `SearchPipeline.__init__` to accept three new scalar parameters: `max_fanout: int`, `fanout_leg_trim: int`, and `fanout_timeout_seconds: float`. Store them as instance attributes: `self._max_fanout`, `self._fanout_leg_trim`, and `self._fanout_timeout_seconds`. Update the `create_pipeline` factory function (search for `create_pipeline` in `pipeline.py`) to read these from config (`config.max_fanout`, `config.fanout_leg_trim`, `config.fanout_timeout_seconds`) and pass them to the constructor. All references in Task 3.2 use `self._max_fanout`, `self._fanout_leg_trim`, `self._fanout_timeout_seconds` — never `self._config.*`.
  - **Sub-task 3.1b — Config keys**: Add to `SearchConfig`:
    - `max_fanout: int = 8` — cap on number of collections in `collections` list; 422 if exceeded. Must be ≥ 1; `load_config` raises `ConfigError` if `max_fanout < 1`.
    - `fanout_leg_trim: int = 40` — per-leg candidates kept by local RRF before merge. Must be ≥ 1; `load_config` raises `ConfigError` if `fanout_leg_trim < 1`.
    - `fanout_timeout_seconds: float = 30.0` — whole-fan-out timeout using `asyncio.timeout()`. Must be > 0; `load_config` raises `ConfigError` if `fanout_timeout_seconds <= 0`.
  - Add TOML loading in `load_config` for the new keys under `[search]` section (not `[database]` — these keys govern search execution bounds, not storage schema). If a `[search]` section doesn't exist in `config.py`, add it alongside the fan-out keys. Add a comment in `archon-search.toml.example` clarifying they control the fan-out execution window.
  - **Tech-debt note**: `top_k_retrieve`, `top_k_return`, `candidate_depth`, and other search-execution parameters currently live under `[database]` for historical reasons. The new fan-out keys are placed under `[search]` as the start of a logical grouping. A future refactoring task should migrate all search-execution parameters from `[database]` to `[search]`. This is registered as tech debt in `Architecture/530_technical_debt_refactoring_roadmap.md` (update that doc in Task 8.1's documentation sub-task).
  - Add `archon-search.toml.example` entries for the three keys under `[search]` with comments.
  - **Releasable**: config keys are readable by the pipeline; `SearchPipeline` stores them as instance attributes.
- **Tests (TDD)** — `tests/test_config.py`:
  - Unit: `test_max_fanout_default` — `SearchConfig().max_fanout == 8`
  - Unit: `test_fanout_leg_trim_default` — `SearchConfig().fanout_leg_trim == 40`
  - Unit: `test_fanout_timeout_seconds_default` — `SearchConfig().fanout_timeout_seconds == 30.0`
  - Unit: `test_max_fanout_loaded_from_toml` — parse a TOML string with `[search]\nmax_fanout = 4`; assert `config.max_fanout == 4`
  - Unit: `test_max_fanout_zero_raises_config_error` — parse TOML with `max_fanout = 0`; assert `ConfigError` raised
  - Unit: `test_max_fanout_negative_raises_config_error` — parse TOML with `max_fanout = -1`; assert `ConfigError` raised
  - Unit: `test_fanout_leg_trim_zero_raises_config_error` — parse TOML with `fanout_leg_trim = 0`; assert `ConfigError` raised
  - Unit: `test_fanout_timeout_zero_raises_config_error` — parse TOML with `fanout_timeout_seconds = 0.0`; assert `ConfigError` raised
  - Checkpoint: `uv run pytest tests/test_config.py -v`

#### Task 3.2 — Implement `SearchPipeline.search_many`

- [x] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 2.3, Task 3.1
- **Description**:
  - Add `search_many(self, query: str, collections: list[str], namespace: str = DEFAULT_NAMESPACE) -> SearchPipelineResult` to `SearchPipeline`.
  - Step 1: `vector = await self._embedder.embed_one(query)` — exactly once.
  - Step 2: `all_meta = await self.get_all_collections_meta(namespace)` — use the pipeline's own namespace-filtered method (not `self.store.get_all_collections_meta()` directly); if this raises, catch it in `search_many` and re-raise as `MetadataLookupError(original_exception)` (new exception class in `pipeline.py`). This allows the route handler to map `MetadataLookupError` → 503 distinctly from other pipeline failures → 500. Build a dict `{name: meta}`. Validate each name in `collections` is in the dict; if any are missing, raise `CollectionNotFoundError(names: list[str])` (new exception in `pipeline.py`). Exclude collections where `meta.embedding_model != self._embedder.model_name`; add to `excluded_collections`. If all requested collections are excluded (in-scope list empty), return `SearchPipelineResult(results=[], acl_filtered=False, excluded_collections=excluded_collections)` immediately — no fan-out, no error.
  - Step 3: Fan out. Use a **nested try structure** to avoid mixing `except` and `except*` in the same try block (which is invalid Python 3.11+):

    ```python
    try:
        async with asyncio.timeout(self._fanout_timeout_seconds):
            try:
                async with asyncio.TaskGroup() as tg:
                    # start leg tasks
            except* Exception as eg:
                logger.error('search_many fan-out: %d legs failed: %s', len(eg.exceptions), eg)
                raise eg.exceptions[0] from eg.exceptions[0]
    except TimeoutError:
        raise FanoutTimeoutError()
    ```

    The inner `except*` handles `ExceptionGroup` from `TaskGroup` leg failures. The outer `except` handles `TimeoutError` from `asyncio.timeout()`. These cannot be in the same try block.

    Each leg is started as a task calling an inner coroutine that: (a) records its own `monotonic()` start time, (b) calls `self.store.hybrid_search_with_trace(coll, vector, query, candidate_depth=max(self._top_k_retrieve * 3, 20))`, (c) records its own `monotonic()` end time, and (d) returns `(coll_name, candidates, leg_time_ms)`. When `asyncio.timeout()` fires, it raises `TimeoutError` via task cancellation; `TaskGroup.__aexit__` still cancels and awaits child task cleanup before re-raising — leg coroutines must be cancellation-safe.
  - Step 4: Per-leg trim. For each leg's `list[ScoredSearchCandidate]`, sort by `(-score_breakdown.rrf_score, chunk_id)` and keep top `max(self._fanout_leg_trim, 1)`.
  - Step 5: Merge. Concatenate legs in ascending collection-name order (sorted). Each candidate retains `collection` provenance from `ScoredSearchCandidate.collection`.
  - Step 6: ACL. `merged, acl_filtered = apply_acl_filter(merged, lambda c: c.acl, namespace)`. Requires `ScoredSearchCandidate.acl: list[str] | None` — verify or add in Task 2.1.
  - Step 7: Rerank. `ranked = await self._reranker.rerank_candidates(query, merged, top_k=self._top_k_return)`.
  - Step 8: Convert. `results = [_candidate_to_search_result(c) for c in ranked]` where `_candidate_to_search_result` is a private helper that maps `ScoredSearchCandidate` → `SearchResult`. The helper maps ALL fields: `doc_id`, `chunk_id`, `text`, `source_path`, `file_type`, `language`, `indexed_at`, `updated_at`, `ingested_by`, `metadata`, `acl` are copied verbatim. `score` is set to `c.score_breakdown.reranker_score` (assert non-None, since rerank is unconditional in `search_many`). `collection` is copied from `c.collection`. This is a complete field mapping — no fields are dropped silently.
  - Collect per-leg `leg_time_ms` values from Step 3 into `FanoutTimings(leg_times: dict[str, float], rerank_time_ms: float)` (rerank time measured around Step 7). `FanoutTimings` is a dataclass defined in `archon_search/_types.py` (the canonical location for shared data types used across layers); import it in `pipeline.py`, `routes_explain.py`, and `telemetry/`. Store as `SearchPipelineResult.fanout_timings`.
  - Return `SearchPipelineResult(results=results, acl_filtered=acl_filtered, excluded_collections=excluded_collections, fanout_timings=fanout_timings)`.
  - Extract the fan-out+merge+ACL core as a private method `_fanout_merge_acl(self, query: str, vector, collections_in_scope: list[str], namespace: str, candidate_depth: int) -> tuple[list[ScoredSearchCandidate], bool, dict[str, float]]` (returns merged+ACL-filtered candidates, `acl_filtered` flag, and `leg_times: dict[str, float]` mapping collection name to its retrieval time in ms). The `leg_times` dict is assembled inside the method from per-leg timing data (Step 3). `search_many` uses `leg_times` to build `FanoutTimings`. Task 6.1's `explain` multi-collection path calls the same method and may use `leg_times` for B1 breakdown. This shared primitive will also be called by Task 6.1's `explain` multi-collection path — see Task 6.1 implementation note.
  - Add `CollectionNotFoundError(names: list[str])`, `FanoutTimeoutError`, and `MetadataLookupError(cause: Exception)` exceptions to `pipeline.py`.
  - **Releasable**: `pipeline.search_many` is callable and tested at unit level; no API surface yet.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Unit: `test_search_many_embeds_once` — spy on `embedder.embed_one`; call `search_many` with 3 collections; assert spy called exactly once
  - Unit: `test_search_many_reranks_once` — spy on `reranker.rerank_candidates`; call `search_many` with 2 collections; assert spy called exactly once and `len(spy.call_args.args[1]) == sum_of_trimmed_per_leg_pool`
  - Unit: `test_search_many_result_carries_collection_provenance` — use two mock collections each returning distinct `ScoredSearchCandidate` objects with distinct `collection` values; assert each returned `SearchResult.collection` matches its origin
  - Unit: `test_search_many_merge_order_deterministic` — two collections with identical stub candidates; assert output ordering is stable across calls
  - Unit: `test_search_many_namespace_scope_excludes_out_of_namespace` — mock metadata returns one collection in namespace A and one in namespace B; call with `namespace="A"`; assert collection B not searched (its leg never runs)
  - Unit: `test_search_many_missing_collection_raises_collection_not_found` — requested name absent from metadata → `CollectionNotFoundError`
  - Unit: `test_search_many_model_mismatch_excludes_and_reports` — collection with `embedding_model="other-model"` → excluded, present in `SearchPipelineResult.excluded_collections`; its `hybrid_search_with_trace` never called
  - Unit: `test_search_many_leg_failure_cancels_siblings_and_raises` — first leg's task raises a plain `RuntimeError("leg failed")`; assert `search_many` raises a plain `RuntimeError` (NOT an `ExceptionGroup`); confirm `hybrid_search_with_trace` for the other leg is eventually cancelled. The sibling mock must: (1) include `await asyncio.sleep(999)` to block until cancelled, (2) wrap it in `try/except asyncio.CancelledError` that sets an `asyncio.Event` flag before re-raising `CancelledError`. After `search_many` raises, assert the event is set. The event must be awaited or checked after the exception to ensure TaskGroup cleanup has run. Verify the raised exception is a plain exception (not `ExceptionGroup`) — the `except*` unwrapping must have occurred.
  - Unit: `test_search_many_timeout_raises_fanout_timeout_error` — inject a slow leg by mocking `hybrid_search_with_trace` with a never-resolving coroutine; set `fanout_timeout_seconds=0.001`; assert `FanoutTimeoutError` raised. The test must mock ALL preceding awaits (`embed_one` and `get_all_collections_meta`) to return immediately before the TaskGroup starts. Add a wall-clock assertion: `elapsed_seconds < 2.0` to verify the timeout actually fires within a reasonable bound.
  - Unit: `test_search_many_single_collection_matches_search` — **Drop this unit test.** This equivalence is verified by the Phase 8 integration test (`collections=["x"]` vs `collection="x"` field-subset equality). A unit test would only verify mock construction equivalence across two different code paths (`hybrid_search` vs `hybrid_search_with_trace`), not actual behavior. Rely on Phase 8 integration test instead.
  - Unit: `test_same_chunk_id_in_two_collections_both_survive` — same `chunk_id` in collections A and B; assert both appear in merged pool (no cross-collection dedup)
  - Unit: `test_search_many_all_collections_model_mismatched_returns_empty` — all requested collections have `embedding_model` mismatching embedder; assert `search_many` returns `SearchPipelineResult(results=[], excluded_collections=[all_requested])`; assert `hybrid_search_with_trace` never called
  - Unit: `test_search_many_leg_trim_below_top_k_return` — set `fanout_leg_trim=1`, `top_k_return=5`, 2 stub collections each returning 10 candidates; assert result count is 2 (not 5) without error. Verifies reranker gracefully handles merged pool smaller than `top_k_return`.
  - Unit: `test_search_many_meta_lookup_raises_propagates` — mock `get_all_collections_meta` to raise `RuntimeError('store error')`; assert `search_many` raises `MetadataLookupError` (not `RuntimeError` directly).
  - Unit: `test_search_many_heterogeneous_leg_pool_sizes` — mock leg A returning 40 candidates, mock leg B returning 0 candidates (FTS fallback); assert `search_many` completes without error and results contain only candidates from leg A.
  - Checkpoint: `uv run pytest tests/test_pipeline.py -v`

---

### Phase 4 — REST surface

> **Releasable**: after Task 4.1 — `POST /search` accepts `collections` and returns merged results with `collection` provenance and `excluded_collections`.

#### Task 4.1 — `SearchRequest` validator and handler update

- [x] **File**: `archon_search/server/routes_search.py`
- **Depends on**: Task 3.2
- **Description**:
  - Change `SearchRequest.collection: str` to `collection: str | None = None`. Keep `collection_nonempty` validator but guard it with `if v is not None`.
  - Add `collections: list[str] | None = None`.
  - Add a `model_validator(mode="after")` that enforces exactly-one-of `{collection, collections}`:
    - Both set → `ValueError("supply either collection or collections, not both")`
    - Neither set → `ValueError("supply either collection or collections")`
    - `collections` empty list → `ValueError("collections must not be empty")`
    - `collections` length > `_FANOUT_VALIDATION_LIMIT` (a module-level constant in `routes_search.py`, default 8, matching the default `max_fanout` config value) → `ValueError(f"collections length exceeds maximum of {_FANOUT_VALIDATION_LIMIT}")`. **Important**: this constant is a Pydantic-layer guard and intentionally matches the default `max_fanout=8`. If an operator increases `max_fanout` in `archon-search.toml` beyond 8, they must also update `_FANOUT_VALIDATION_LIMIT` in the source. This divergence is a known limitation (see Known Limitations). Do NOT read config at Pydantic model construction time — Pydantic validators must not have external dependencies. On app startup (in `app.py` startup event or lifespan), add a validation check: `if config.max_fanout != _FANOUT_VALIDATION_LIMIT: logger.warning('max_fanout config (%d) differs from _FANOUT_VALIDATION_LIMIT constant (%d) in routes_search.py; update the constant or requests with >%d collections will be rejected', config.max_fanout, _FANOUT_VALIDATION_LIMIT, _FANOUT_VALIDATION_LIMIT)`.
    - Per-item: strip whitespace; if blank → `ValueError("collection names must not be empty or whitespace")`
    - Deduplicate while preserving first-occurrence order.
    - `collections` set AND `filters` set → `ValueError("filters are not supported for multi-collection search in v1")`. The `SearchRequest` model_validator must enforce this (filters are not supported in the `_hybrid_search_with_trace` trace path used by `search_many`).
  - Update the `/search` handler:
    - If `body.collection` is set: follow the existing single-collection path (unchanged, keep its existing `asyncio.wait_for` wrapper).
    - If `body.collections` is set: call `pipeline.search_many(body.query, body.collections, namespace=ns)` **WITHOUT** the outer `asyncio.wait_for` wrapper. The inner `asyncio.timeout()` inside `search_many` is the timeout mechanism. Using both `asyncio.wait_for` (outer) and `asyncio.timeout()` (inner) at equal 30s creates conflicting cancellation semantics — they race and the outer `wait_for` can cancel the inner `TaskGroup` before it finishes child cleanup. Remove `asyncio.wait_for` wrapping for the multi-collection path only; the single-collection path is unchanged.
    - Map `CollectionNotFoundError` → `JSONResponse({"detail": "collection not found"}, status_code=404)`.
    - Map `FanoutTimeoutError` → `HTTPException(status_code=504, detail="Search timed out")`.
    - Map `MetadataLookupError` → `JSONResponse({"detail": "service unavailable"}, status_code=503)`.
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
  - Unit: `test_search_request_single_item_collections_is_valid` — `SearchRequest(collections=['x'], query='q')` → valid; `len(body.collections) == 1`
  - Unit: `test_search_request_collections_with_filters_is_422` — `SearchRequest(collections=["x"], query="q", filters=<any_filter>)` raises `ValidationError` with message `"filters are not supported for multi-collection search in v1"`
  - Unit: `test_fanout_validation_limit_matches_config_default` — assert `_FANOUT_VALIDATION_LIMIT == SearchConfig().max_fanout` (ensures the constant and config default stay in sync)
  - Unit: `test_search_handler_multi_collection_calls_search_many` — mock `pipeline.search_many`; POST `/search` with `collections`; assert `search_many` called
  - Unit: `test_search_handler_missing_collection_returns_404` — `search_many` raises `CollectionNotFoundError`; assert HTTP 404
  - Unit: `test_search_handler_fanout_timeout_returns_504` — `search_many` raises `FanoutTimeoutError`; assert HTTP 504
  - Unit: `test_search_handler_meta_lookup_failure_returns_503` — mock `search_many` raising `MetadataLookupError`; assert HTTP 503
  - Unit: `test_search_response_includes_excluded_collections` — `search_many` returns excluded collection; assert schema field populated
  - Contract: `test_search_response_json_includes_collection_key` — deterministic mock; assert `results[0]["collection"]` present in JSON response
  - Checkpoint: `uv run pytest tests/test_routes_search.py -v`

---

### Phase 5 — MCP surface

> **Releasable**: after Task 5.1 — MCP `search` tool accepts `collections` and returns results with `collection` and `excluded_collections`.

#### Task 5.1 — MCP `search` tool: `collections` parameter and `BREAKING.md`

- [x] **File**: `archon_search/server/mcp.py`, `BREAKING.md`
- **Depends on**: Task 4.1
- **Description**:
  - In the MCP `search` tool function (search for the MCP `search` tool function in `mcp.py`), add `collections: list[str] | None = None` parameter alongside the existing `collection: str | None = None`.
  - Add the same exactly-one-of validation logic as `SearchRequest` (the MCP layer validates manually because it doesn't use Pydantic model validators):
    - Both → return `McpErrorResponse(error="supply either collection or collections, not both", code="validation_error")`
    - Neither → return `McpErrorResponse(error="supply either collection or collections", code="validation_error")`
    - `collections` empty → `McpErrorResponse(error="collections must not be empty", code="validation_error")`
    - Length > `_FANOUT_VALIDATION_LIMIT` → `McpErrorResponse(error=f"collections length exceeds {_FANOUT_VALIDATION_LIMIT}", code="validation_error")`. Use `_FANOUT_VALIDATION_LIMIT` imported from `archon_search.server.routes_search` as the shared constant (do NOT define a separate constant in `mcp.py`). This ensures both REST and MCP use the same validation limit and it only needs to be updated in one place.
    - Per-item whitespace → `McpErrorResponse(error="collection names must not be whitespace", code="validation_error")`
    - Deduplicate.
  - If `collections` set: call `pipeline.search_many(query, collections, ...)`. Map `CollectionNotFoundError` → `McpErrorResponse(code="not_found")`. Map `FanoutTimeoutError` → `McpErrorResponse(code="timeout")`.
  - `asdict()` on `SearchResult` already picks up `collection` (Task 1.1). `excluded_collections` is added to the MCP result dict manually (list of `{"name": ..., "reason": ...}` dicts).
  - Add a `BREAKING.md` entry under today's date: REST `/search` and `/explain` responses gain additive `collection` key per result and `excluded_collections` key per response (non-breaking for tolerant JSON clients). MCP `search`/`explain` response shapes gain `collection` and `excluded_collections` (true contract change for strict-validating MCP clients — same class as A1's additive-key break). The new `collections` request field is additive/optional on both surfaces. REST `SearchRequest`: `collection` field changes from required (`str`) to optional (`str | None = None`). Existing clients that omit `collection` will receive a different 422 message ('supply either collection or collections' instead of Pydantic's 'field required'). Clients that explicitly send `collection: null` will get the same new error. This is a request-schema behavioral change.
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

- [x] **File**: `archon_search/pipeline.py`, `archon_search/server/routes_explain.py`, `archon_search/server/mcp.py`
- **Depends on**: Task 3.2
- **Description**:
  - `SearchPipeline.explain` gains `collections: list[str] | None = None`. When `collections` is set:
    1. Validate exactly-one-of `{collection, collections}` (raise `ValueError` if both or neither).
    2. If `rerank=False` and `len(collections) > 1` → raise `ExplainMultiCollectionNoRerankError` (new exception in `pipeline.py`) with message `"reranking cannot be disabled for multi-collection search in v1"`. If `rerank=False` and `len(collections) == 1`, the single-collection explain path is used (same behavior as `rerank=False` with `collection="x"`) — no error.
    3. Load meta, scope, and exclude model-mismatch collections (same as `search_many` step 2). Use `asyncio.timeout()` not `asyncio.wait_for()`.
    4. Fan-out via `asyncio.TaskGroup`; per-leg trim; deterministic merge (ascending collection-name order).
  - **Implementation note**: Steps 3-6 of `explain` multi-collection duplicate the fan-out/merge/ACL logic from `search_many`. Both must use the shared private method `_fanout_merge_acl(self, query: str, vector, collections_in_scope: list[str], namespace: str, candidate_depth: int) -> tuple[list[ScoredSearchCandidate], bool, dict[str, float]]` defined in Task 3.2. Return tuple is `(merged_candidates, acl_filtered, leg_times)`. `search_many` calls `_reranker.rerank_candidates(query, merged, top_k=self._top_k_return)` after this call, using `leg_times` to build `FanoutTimings`. `explain` calls `_reranker.rerank_candidates(query, merged, top_k=len(merged))` before sorting, and may use `leg_times` for B1 breakdown. This is not optional — duplicating the fan-out logic will cause divergence bugs within 2-3 releases.
    5. ACL filter on merged pool.
    6. If `rerank=True` (mandatory for multi with len > 1): `reranker.rerank_candidates(query, merged, top_k=len(merged))`.
    7. Final sort: `(-final_score, doc_id, chunk_id)` where `final_score = reranker_score if not None else rrf_score`.
    8. `top_results = candidates[:top_k]`; `near_misses = candidates[top_k : top_k + 20]`.
    9. Return `ExplainPipelineResult(top_results=top_results, near_misses=near_misses, acl_filtered=acl_filtered)`.
  - `ExplainResult` and `ExplainNearMiss` Pydantic models gain `collection: str = ""` field; `from_candidate` classmethods populate it from `ScoredSearchCandidate.collection`.
  - **`ExplainResponse` top-level collection field**: When the request uses `collections` (multi), the top-level `collection` field in `ExplainResponse` (if present) must be set to `''` (empty string) or removed in favor of per-result `collection` fields. The authoritative collection provenance is on each `ExplainResult.collection` and `ExplainNearMiss.collection`. Check the current `ExplainResponse` schema in `routes_explain.py` and `schemas.py`; if a top-level `collection: str` field exists, change it to `collection: str = ''` (additive/backward-compat). Do NOT add a `collections: list[str]` field to `ExplainResponse` — this is out of scope. The `excluded_collections: list[ExcludedCollectionSchema]` field added to `SearchResponse` in Task 1.1 should also be added to the explain response for symmetry. Add this field to the explain response schema in Task 6.1's file list.
  - REST `ExplainRequest` gains `collections: list[str] | None = None`; add exactly-one-of validator **in the Pydantic model_validator** (not in the route handler) and the `rerank=false`+multi (`len(collections) > 1`) → 422 guard with reason string `"reranking cannot be disabled for multi-collection search in v1"`. The guard fires at model construction time (consistent with `SearchRequest` validators). Route handler does not duplicate this check. **Validator ordering**: deduplication of `collections` must be a `field_validator` (runs before `model_validator`). The `rerank=False` + `len(collections) > 1` guard must be in `model_validator(mode='after')` which runs after all field validators. This ensures dedup happens first, so `collections=['a','a']` deduplicates to `['a']` (length 1) before the rerank guard checks `len > 1`. This allows `rerank=False` + `collections=['a','a']` (which deduplicates to `['a']`) to pass validation.
  - MCP `explain` tool gains `collections: list[str] | None = None`; same validation (manual, not Pydantic); `ExplainMultiCollectionNoRerankError` → `McpErrorResponse(error="reranking cannot be disabled for multi-collection search in v1", code="validation_error")`.
  - **Releasable**: `/explain` supports multi-collection provenance; `rerank=false`+multi is rejected everywhere `/explain` is exposed.
- **Tests (TDD)** — `tests/test_routes_explain.py`, `tests/test_mcp.py`, `tests/test_pipeline.py`:
  - Unit: `test_explain_rerank_false_multi_collections_is_422_rest` — `ExplainRequest(query="q", collections=["a","b"], rerank=False)` raises `ValidationError`
  - Unit: `test_explain_rerank_false_single_collection_is_valid` — `rerank=False` with single `collection` → valid (existing A4 behavior); also covers `collections=["x"]` (single-item list, new boundary case) — both `collection="x"` and `collections=["x"]` must be valid with `rerank=False`
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

- [x] **File**: `archon_search/telemetry/entry.py`, `archon_search/server/routes_search.py`, `archon_search/server/mcp.py`, `archon_search/pipeline.py`
- **Depends on**: Task 5.1, Task 6.1
- **Description**:
  - Add `EndpointKind.search_multi = "search_multi"` to `EndpointKind` (`telemetry/entry.py`).
  - Add `TelemetryEntry.from_search_multi_result(*, collections: list[str], fanout_count: int, result_count: int, latency_ms: float, excluded_count: int) -> TelemetryEntry` factory (keyword-only, no `query` parameter — preserves no-raw-query invariant). `fanout_count` is the number of collections that were actually searched (after exclusions).
  - In the `/search` handler and MCP `search` tool, call the new factory instead of `from_search_tool_result` when `collections` was supplied; pass collection names (already permitted in `/route` telemetry) and `fanout_count`.
  - Per-leg timing in `SearchPipeline.search_many`: each leg's coroutine captures its own `monotonic()` start and end time internally (before and after the `hybrid_search_with_trace` call within the leg's async task closure). Timing is NOT recorded around the `TaskGroup` as a whole (which would only yield wall-clock for all concurrent legs, not per-leg). After the `TaskGroup` completes, assemble `leg_times: dict[str, float]` from the per-leg results. Record rerank wall-clock time around Step 7. Store all timings in a `FanoutTimings(leg_times: dict[str, float], rerank_time_ms: float)` dataclass defined in `archon_search/_types.py` (see Task 3.2 Step 8 — canonical shared type location); import it in `pipeline.py`, `routes_explain.py`, and `telemetry/`. Include `FanoutTimings` in `SearchPipelineResult` as an optional field (`fanout_timings: FanoutTimings | None = None`). The `/explain` route reads `fanout_timings` and includes it in the B1 breakdown when non-None.
  - The no-raw-query structural invariant is asserted by the existing factory-constructor test pattern: `test_from_search_multi_result_has_no_query_param` — inspect `TelemetryEntry.from_search_multi_result.__code__.co_varnames` and assert `"query"` is absent.
  - **Releasable**: multi-collection requests emit telemetry with fan-out count; per-leg timings are available for B1 integration.
- **Tests (TDD)** — `tests/telemetry/test_entry.py`, `tests/test_pipeline.py`:
  - Unit: `test_from_search_multi_result_no_query_param` — assert `"query"` not in `inspect.signature(TelemetryEntry.from_search_multi_result).parameters`; also assert that calling `TelemetryEntry.from_search_multi_result(collections=['a'], fanout_count=1, result_count=1, latency_ms=10.0, excluded_count=0, query='test')` raises `TypeError` (unexpected keyword argument)
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
