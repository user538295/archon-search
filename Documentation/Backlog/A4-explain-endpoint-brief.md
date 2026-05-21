# Feature Brief: Explain / Debug Endpoint (A4 — Roadmap Item 12)

> **Order**: Ships AFTER A3 (search failure semantics). A4 inherits A3's error taxonomy: pipeline-stage failures return HTTP 500, 503 is reserved for meta-lookup failures only.

## Problem
When a search result is wrong, missing, or surprising, there is no way to see *why*: the hybrid pipeline computes vector ranks, FTS ranks, RRF fusion, reranker scores, and routing centroid similarities, then discards all of them and returns a single opaque `score`. Debugging requires reading source code or running eval harnesses.

## Goal
Ship `POST /explain` (REST) and a parallel `explain` MCP tool that return the full per-stage score breakdown for a query — vector rank/score, FTS rank/score, RRF fused score, reranker score, plus the routing decision when no collection is pinned. Every later ranking change (filters, HyDE, RAG Fusion, stronger routing) becomes debuggable from the outside.

## Users & Context
- **Maintainers and contributors** tuning routing, reranking, or hybrid scoring. They need to see why a result was (or wasn't) returned without instrumenting code.
- **Power users / integrators** building on the REST or MCP API who hit unexpected results and need to self-diagnose before filing an issue.
- **Eval / benchmark workflows** that today reach into private `_diagnostics` paths to reconstruct score provenance.

Triggered when a search result is wrong, missing, or unexpectedly ranked — typically interactive debugging during development or threshold tuning.

## Roadmap Deviation (Item 12 → A4 v1)

The roadmap line (`03_world_class_roadmap.md:51`) lists seven fields: *vector rank, FTS rank, fused score, reranker score, matched filters, routing path, expansion-feature usage*. v1 ships **five of seven**:

| Roadmap field | v1 status | Reason |
|---|---|---|
| vector rank | **in** | Already computed in `store.hybrid_search` |
| FTS rank | **in** | Already computed in `store.hybrid_search` |
| fused (RRF) score | **in** | Already computed in `store.hybrid_search` |
| reranker score | **in** | Available via `_rerank_with_trace` |
| routing path | **in** | Promoted from `MultiCollectionRouter.rank()` (currently internal) |
| matched filters | **deferred to A4.1** | Depends on A2 (filters on `/search`); additive when A2 ships |
| expansion-feature usage | **deferred to A4.2** | Depends on Phase B/C (HyDE, RAG Fusion); additive when expansions ship |

No placeholder fields. Deferred items become additive schema extensions, not breaking changes. This is the only deviation from the roadmap text and is intentional.

## Core Flow
1. Caller sends `POST /explain` (or invokes the MCP `explain` tool) with `{query, collection?, top_k?, rerank?}`. `collection` and `rerank` are optional; `rerank` defaults to `true`; `top_k` defaults to `5` (same as `/search`).
2. If `collection` is omitted, the router runs first via a new `MultiCollectionRouter.rank_with_scores()` method that returns `(collection, centroid_score)` pairs **bypassing the confidence-threshold gate** so every candidate collection is surfaced regardless of routing confidence. Sorted descending by score; ties broken alphabetically by collection name. The pre-existing `rank()` method is **not modified** (existing callers `select()` and `get_pre_context()` keep their current contract).
3. The hybrid pipeline runs against the chosen/pinned collection via a new public `SearchPipeline.explain()` orchestration. This method **reuses the exact same retrieval pool config value as `SearchPipeline.search()`** — both call `store.hybrid_search*` with `top_k=self._top_k_retrieve` (default `15`, from `SearchConfig.top_k_retrieve`). Inside the store, this is amplified to `max(top_k_retrieve * 3, 20)` per search leg. Because both code paths use identical inputs, the top-`top_k` slice of `/explain` and `/search` are guaranteed identical when `rerank=true` **and** when `/explain` is called with `top_k == config.top_k_return` (the only `top_k` value `/search` returns; `/search` has no request-level `top_k`). The near-miss pool of "up to 20" is drawn from whatever remains in the retrieval pool after the top-`top_k` slice — it does **not** widen the retrieval pool beyond what `/search` already uses.
4. The response returns the top `top_k` results with full text + per-stage score breakdown, **plus up to 20 near-miss candidates** (or fewer if the pool is smaller) with score breakdowns only — the `text` field is omitted entirely from near-miss objects via a distinct Pydantic model `ExplainNearMiss` that has no `text` field at all.
5. ACL filtering runs identically to `/search`; filtered results are excluded from both `results` and `near_misses`, and `acl_filtered: bool` is reported. Near-miss count is computed against the **post-ACL** pool.
6. A telemetry entry is emitted (no query text) with endpoint kind `explain`, latency, collection, and result count — consistent with `/search` and `/route`.

## Public Contract

### Request
```json
{
  "query": "string (required, non-empty)",
  "collection": "string (optional; when omitted, router selects)",
  "top_k": 5,
  "rerank": true
}
```

### Response (JSON)
```json
{
  "rerank": true,
  "routing": {
    "invoked": true,
    "chosen_collection": "docs",
    "confidence_threshold": 0.30,
    "chosen_below_threshold": false,
    "candidates": [
      {"collection": "docs", "centroid_score": 0.83},
      {"collection": "code", "centroid_score": 0.61}
    ]
  },
  "collection": "docs",
  "acl_filtered": false,
  "results": [
    {
      "doc_id": "...",
      "chunk_id": "...",
      "source_path": "...",
      "text": "...",
      "score": 0.91,
      "breakdown": {
        "vector_rank": 1,
        "vector_score": 0.74,
        "vector_score_kind": "distance",
        "fts_rank": 3,
        "fts_score": 4.2,
        "fts_score_kind": "bm25",
        "rrf_score": 0.032,
        "reranker_score": 0.91
      }
    }
  ],
  "near_misses": [
    {
      "doc_id": "...",
      "chunk_id": "...",
      "source_path": "...",
      "score": 0.42,
      "breakdown": { "... same shape as results.breakdown ..." }
    }
  ]
}
```

- **Metadata parity with `SearchResult` (A1+A2)**: `ExplainResult` AND `ExplainNearMiss` MUST carry the same metadata fields as `SearchResult` — `file_type: str | None`, `indexed_at: str | None`, `updated_at: str | None`, `ingested_by: IngestedBy | None`, `language: str | None`, `metadata: dict[str, str]`. Without these, debugging "why did my A2 filter exclude this doc?" from the `/explain` response alone is impossible. The fields are populated by the `from_candidate()` factory from the candidate row.
- **ACL on `routing.candidates`**: the `routing.candidates` list MUST be filtered to the ACL-allowed collection set for the caller. Do NOT expose the full collection universe via `/explain`'s routing block. This is the same ACL boundary that gates `results` and `near_misses`. (Assumption: collection names in this project are treated as principal-restricted, same as document content. If a future decision deems collection names non-sensitive, revisit — but the safe default is filter-to-ACL.)
- The `routing` block is present only when `collection` was omitted in the request. When `collection` is pinned, `routing: null`. The response does **not** echo the input `query` — callers already have it, and matching `SearchResponse` / `RouteResponse` reduces surface area.
- The new public Pydantic models (`ExplainRequest`, `ExplainResponse`, `ExplainResult`, `ExplainNearMiss`, `ExplainScoreBreakdown`, `RoutingExplain`, `RoutingCandidate`) live in `archon_search/server/routes_explain.py` (co-located with the route, matching the `routes_search.py` pattern) and are **decoupled from the private `_diagnostics.SearchScoreBreakdown`**. Mapping happens through classmethods on the public schemas — primarily `ExplainResponse.from_pipeline_result(...)`, mirroring `SearchResultSchema.from_result` in `routes_search.py`. There is no dedicated `explain_mapper.py` module. Renaming a private field is not a breaking change; renaming a public schema field is.
- `vector_score_kind` and `fts_score_kind` are surfaced verbatim from the internal type — the value `"distance"` is what `_hybrid_search_with_trace` currently emits for LanceDB cosine indexes (it is a similarity-derived distance, lower is closer). The public schema documents this in its docstring; the value is **not** transformed to `"cosine"`.
- MCP tool returns the exact same JSON structure as REST. Serialization is pinned to Pydantic's `model_dump(mode="json", exclude_none=False)` — `None` values are included (`reranker_score: null` when `rerank=false`); fields that are structurally absent (e.g., `text` on `ExplainNearMiss`) are omitted because the model has no such field at all. This makes REST/MCP outputs byte-equivalent under `json.dumps(..., sort_keys=True)`.

## In Scope
- New `POST /explain` REST endpoint behind existing bearer-auth middleware.
- New `explain` MCP tool (10th tool), sharing auth and pipeline with REST.
- New public Pydantic schemas in `archon_search/server/routes_explain.py` (co-located with the route, matching the `routes_search.py` pattern — not in `schemas.py`): `ExplainRequest`, `ExplainResponse`, `ExplainResult`, `ExplainNearMiss` (no `text` field), `ExplainScoreBreakdown`, `RoutingExplain`, `RoutingCandidate`.
- New classmethod `ExplainResponse.from_pipeline_result(...)` (plus `from_candidate` / `from_breakdown` on related schemas) translating internal `ScoredSearchCandidate` + routing data into the public schema. This is the seam between private and public types — co-located with the schemas in `routes_explain.py`.
- New orchestration method `SearchPipeline.explain()` that calls `store.hybrid_search_with_trace` (a new thin instance-method delegate to the existing module-level `_hybrid_search_with_trace`) with `candidate_depth=self._top_k_retrieve` (same config value as `SearchPipeline.search()`), then `_rerank_with_trace` over the surviving pool, ACL-filters, splits top-`top_k` from the remaining near-miss pool, and returns an `ExplainPipelineResult`. Mapping to the wire schema happens in the route layer.
- New `MultiCollectionRouter.rank_with_scores()` method that returns `list[tuple[CollectionMeta, float | None]]` **with the confidence-threshold gate bypassed**. Implemented via a refactor: an extracted `_score_collections` helper is now shared between `rank()` and `rank_with_scores()`. `rank()`'s public behavior is preserved except that equal-similarity entries now have a stable ascending-name tie-break (previously undefined order — a determinism improvement, not a break).
- Additive field `acl: list[str] | None = None` on `_diagnostics.ScoredSearchCandidate`, populated by `_hybrid_search_with_trace`, so `apply_acl_filter` can run on the trace-path candidate list.
- Raw vector and FTS score extraction in `_hybrid_search_with_trace` (already present per `store.py:807`). For `vector_score_kind` the existing value `"distance"` is surfaced as-is; for `fts_score_kind` the existing value (`"bm25"` or whatever the helper emits) is surfaced as-is. If LanceDB's pinned version does not expose a raw FTS score on the row, set `fts_score: null` while keeping `fts_rank`.
- Extension of `telemetry.EndpointKind` enum with `explain`; new factory `TelemetryEntry.from_explain_result()` accepting only `collection`, `result_count`, `latency_ms` (no query, no result doc ids — `result_count` is a scalar to avoid path leakage in the explain telemetry). Update `DOCUMENTED_SCHEMA_FIELDS` accordingly.
- OpenAPI schema entry for `/explain`.
- Documentation update: `600_api_reference_or_public_interface.md` adds the new route + MCP tool with the JSON example above.

## Out of Scope
- **`matched_filters` field** — deferred to A4.1, additive once A2 ships.
- **`expansions` field** — deferred to A4.2, additive once Phase B/C ships.
- **Per-stage timing breakdown** — separate observability concern.
- **Persisting explain traces** — explain is request/response only.
- **UI / visualization** — JSON only.
- **Explaining ingestion or chunking decisions** — retrieval-side only.
- **Refactoring `_rerank_with_trace` / `_hybrid_search_with_trace` to remove the underscore prefix** — they remain private; the public contract is the new Pydantic schemas, not the internal dataclasses.

## Key Decisions
- **Separate `/explain` endpoint, not a `?explain=true` flag on `/search`**: keeps `/search` lean and avoids polymorphic responses. Matches roadmap text.
- **One endpoint covers retrieval + routing**: `collection` is optional. With a collection, retrieval explain only (`routing: null`). Without one, routing + retrieval explain.
- **`rerank: bool = true` default**: faithful to `/search` by default; opt-out for cheap iteration.
- **Near-miss pool of up to 20, scores only**: most common debug question is "why isn't my doc here?". Candidate retrieval uses `candidate_depth=self._top_k_retrieve` (default `15`), which the store amplifies to `max(top_k_retrieve * 3, 20)` per search leg — the same pool `/search` already uses — so `/explain` does not diverge from `/search` retrieval. The near-miss pool is whatever remains in that pool after the top-`top_k` slice (capped at 20).
- **All candidate collections returned with centroid scores**: routing universe is small; full transparency is essentially free.
- **No placeholder fields**: `matched_filters` and `expansions` land with their features.
- **New MCP tool, not a `search` flag**: parity with REST.
- **Public schemas decouple from private `_diagnostics` types**: `_diagnostics.SearchScoreBreakdown` stays private and mutable; `ExplainScoreBreakdown` is the public contract. Mapping happens at the route layer.
- **Minimal telemetry on `/explain`**: emit endpoint kind, collection, latency, result count. No query text (structural invariant). A debug endpoint with zero observability would be hostile during incidents.
- **Determinism by explicit tie-breaking**: results sorted by final `score` descending, tied by `(doc_id, chunk_id)` ascending. Near-misses sorted by `score` descending with same tie-break. Routing candidates sorted by `centroid_score` descending, tied by `collection` name ascending.

## Edge Cases & Constraints

| Scenario | Behavior |
|---|---|
| Empty query | `422 Unprocessable Entity` (mirror `/search` Pydantic validation). |
| `collection` passed but does not exist | `404 Not Found` (mirror `/search`). |
| `collection` omitted AND no collections exist | `404 Not Found` with body `{"detail": "no collections available"}`. |
| `collection` omitted AND router fails (e.g., centroid not built) | `503` with router error message (mirror `/route`). 503 is reserved for meta-lookup failures only — pipeline-stage failures return 500 (see A3 error taxonomy). |
| Router picks a collection that then 404s during search | `500` with diagnostic message linking router decision and search failure. |
| Pipeline-stage failure (store / embedder / reranker) | `500` (aligns with A3's `/search` taxonomy — pipeline-stage failures are not swallowed and are not 503). |
| ACL filters all results | `results: []`, `acl_filtered: true`. Near-misses also ACL-filtered — explain is **not** an ACL bypass. |
| `rerank=false` | `reranker_score: null` for every result; results sorted by `rrf_score` descending. |
| `top_k` exceeds candidate pool | Return what's available; no error. |
| Corpus too small for 20 near-misses | Return fewer; valid response. |
| Raw vector/FTS score field unavailable from LanceDB | Set `vector_score` / `fts_score` to `null`, keep ranks. |

**Privacy / leakage:**
- `source_path` is included in every result and near-miss. This matches `/search` behavior today; not a new exposure. Documented in `150_security_and_privacy_architecture.md` update.
- Query text is NOT echoed in the response body (caller already has it; matches `SearchResponse` / `RouteResponse`). It MUST NOT pass through `telemetry.entry` factories — enforced by the existing no-`query`-param invariant and verified by test (see Acceptance Criteria).

**Routing in-process call (divergence from `/route`):**
- When `collection` is omitted, `/explain` reads collection metadata via a direct in-process call to `pipeline.get_all_collections_meta(namespace=ns)` and invokes `MultiCollectionRouter.rank_with_scores` locally. It does **not** use `MultiCollectionRouter.fetch_metadata()`'s HTTP self-call pattern. This is intentionally cheaper than `/route` and avoids any thread-pool exhaustion risk from a self-call. If `/route` ever moves to a direct in-process call, the two converge.

**Breaking changes:** none. Purely additive endpoints, new schemas, additive telemetry enum value.

## Acceptance Criteria

Concrete assertions a planner can convert into tests. Each criterion lists its pytest marker (default = unrestricted, `integration` = marker-gated).

1. **Endpoint exists and is authenticated** [default] — `POST /explain` without bearer token returns `401`; with valid token returns `200`.
2. **REST↔MCP response equivalence** [default] — for the same inputs against an in-process pre-built fixture LanceDB table (see §"Test fixtures" below), `json.loads(json.dumps(rest_response)) == json.loads(json.dumps(mcp_response))` (deep-dict equality after a JSON round trip; this dodges float-formatting drift while still asserting structural parity). Both responses originate from `ExplainResponse.model_dump(mode="json", exclude_none=False)` so equivalence is exact.
3. **`/search` top-k equality when `rerank=true` AND `top_k == config.top_k_return`** [default] — for the same `{collection, query}` against the same pre-built fixture and `top_k == config.top_k_return`, the ordered list of `(doc_id, chunk_id)` pairs in the **top-`top_k` slice of `explain.results`** equals the ordered list returned by `/search`. The equality is contracted **only at `top_k == config.top_k_return`** because `/search` has no request-level `top_k` and always returns `top_k_return` results. For other `top_k` values, top-k equality is not contracted. `SearchPipeline.explain()` and `SearchPipeline.search()` use the same `self._top_k_retrieve` config value into the store and the same reranker.
4. **`rerank=false` ordering** [default] — every `result.breakdown.reranker_score is None`; the list is sorted by `breakdown.rrf_score` descending with `(doc_id, chunk_id)` ascending tie-break.
5. **Near-miss pool size** [default] — let `P` = count of candidates surviving ACL filtering from the `max(self._top_k_retrieve * 3, 20)` retrieval pool. Assert `len(near_misses) == min(20, max(0, P - top_k))`. Verified with three corpus sizes: (a) `P >> top_k + 20` → exactly 20, (b) `P` between `top_k` and `top_k + 20` → `P - top_k`, (c) `P <= top_k` → 0.
6. **Near-miss `text` field absent** [default] — assert `"text" not in nm` for each item in `response_json["near_misses"]`. The `ExplainNearMiss` model has no `text` field, so absence is structural.
7. **Routing block presence/absence** [default] — request with `collection` set → `response_json["routing"] is None`. Request with `collection` omitted → `routing.candidates` non-empty, sorted by `centroid_score` desc with alpha tie-break on `collection`.
8. **Routing covers all collections (no confidence gating)** [default] — `set(c.collection for c in routing.candidates) == set(list_collections())`. Specifically includes collections that would be filtered out by `MultiCollectionRouter`'s confidence-threshold gate — verifies `rank_with_scores()` bypasses the gate.
9. **Telemetry no-query (positive)** [default] — call `/explain` with telemetry enabled (writer pointed at a tmp dir), then read the JSONL line via `telemetry.reader`. Assert: `"query"` not in the parsed dict; the literal query string is not a substring of the raw JSONL line; `endpoint == "explain"`; `result_count == len(response.results)`.
10. **Telemetry factory rejects query (structural)** [default] — `assert "query" not in inspect.signature(TelemetryEntry.from_explain_result).parameters`. Plus an instantiation test that calling `from_explain_result(query="x", ...)` raises `TypeError`.
11. **ACL parity (REST only)** [default] — using an ACL fixture that filters all docs for a given principal: REST `/explain` returns `results: []`, `acl_filtered: true`, `near_misses: []`. REST `/search` for the same principal/inputs returns `results: []`, `acl_filtered: true`. Compared side-by-side. MCP `explain` operates in `DEFAULT_NAMESPACE` only (matching the existing MCP `search` tool), so ACL parity is not asserted for MCP in v1.
12. **Edge-case error envelopes** [default] — five separate tests asserting (HTTP status, response body shape) for: empty query → `422`; missing pinned collection → `404`; collectionless + no collections → `404` body `{"detail": "no collections available"}`; collectionless + router error → `503`; `top_k > 100` → `422`.
13. **MCP error envelope parity** [default] — three tests asserting the MCP tool surfaces the same error conditions as REST (empty query, missing collection, collectionless + no collections) using the MCP server's error mapping. Pins the contract that MCP failures are not silently swallowed.
14. **Eval-harness consistency via public mapping** [eval] — a single fixture query where the test calls `_rerank_with_trace` directly, wraps the `ScoredSearchCandidate` output in an `ExplainPipelineResult`, calls `ExplainResponse.from_pipeline_result(...)`, and asserts the resulting `ExplainResponse` equals the response from a live `/explain` call against the same corpus. Compares public schema to public schema (after the mapping), not private fields. Guarantees the route layer does not drift from the eval ground truth.
15. **Coverage** [default] — the default `--cov-fail-under=85` gate stays green without amendment. If coverage dips below 85% during implementation, add focused unit tests (especially on the schema classmethods and the route handler branches) before lowering thresholds. No fixed LOC budget — coverage is the only enforceable constraint.

### Test fixtures

- **Pre-built LanceDB fixture**: tests #2, #3, #5, #7, #8 use a single small pre-built LanceDB table committed under `tests/fixtures/explain_corpus/` (5–6 documents, ~30 chunks). fastembed embeddings are deterministic for fixed input strings; FTS over fixed text is deterministic; reranker scores are deterministic for fixed inputs. Determinism comes from fixed corpus + fixed query strings + the existing deterministic backends — no random seeding required.
- **ACL fixture**: existing `tests/test_acl.py` patterns are reused.
- **Eval fixture (#14)**: uses the existing `tests/eval/corpus/` and `archon_search/eval/backends.py` deterministic backends so the public-mapping comparison is reproducible across CI runs.

## Open Questions
- Does LanceDB expose raw FTS BM25 scores in the result row, or only via a separate API? **Verify during planning**; fallback is `fts_score: null` (rank still available).
- Should `top_k` on `/explain` cap at the same maximum as `/search` (100), or lower to discourage hot-path abuse? Default to **100** (parity) unless planning surfaces a reason.
- Should the eval-harness consistency test (criterion #13) live under the default `eval` marker (gated, slow) or be a fast unit test using `MockReranker`? Default to **eval marker** to use real components.

## Future Iterations
- **A4.1**: `matched_filters` field per result, once A2 ships. Additive.
- **A4.2**: `expansions` field, once Phase B/C ships. Additive.
- **A4.3**: `stage_timings_ms` per stage. Additive.
- **CLI subcommand** `archon-search explain "query" [--collection X]` that pretty-prints JSON. Defer until requested.
- **`/explain/route`** — routing-only variant if collectionless `/explain` cost becomes a complaint.

## Recommendation
Ship this next, but **do not call it plumbing**. The intermediate data exists internally, but production-grade exposure requires: a new pipeline orchestration method, a new router scoring method that bypasses the confidence gate, a public schema layer decoupled from private diagnostics types, a dedicated private-to-public mapper, and minimal telemetry. That is real work — bounded (~200–250 LOC of production code, ~400–600 LOC of tests) but real, and the brief now reflects it. The hardest part is *resisting scope creep* and *enforcing the public/private schema boundary*: every test in §"Acceptance Criteria" exists to lock that boundary in place. What must not be compromised: the telemetry no-raw-query invariant, the public/private schema separation, and the determinism guarantees — without those three, this endpoint becomes a debugging hazard instead of a tool.
