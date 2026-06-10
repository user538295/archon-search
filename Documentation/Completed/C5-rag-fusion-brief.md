# Feature Brief: RAG Fusion / Multi-Query Decomposition (C5)

## Problem
Single-query search misses relevant documents when a user's query is multi-faceted or can be expressed in several ways — the best result may only surface if the query is phrased differently.

## Goal
An LLM decomposes a user query into N semantic variants, each variant searches in parallel, and the ranked results are fused via RRF — producing measurably higher recall on complex queries, verified by the eval harness before merge.

## Users & Context
Operators and end-users querying collections with domain-rich, multi-concept content (e.g. "papers on climate change economic impact", "debugging async Python memory leaks in containerized environments"). They opt in per-request via `rag_fusion: true`. Air-gapped deployments keep it off via config.

## Core Flow
1. Client sends `SearchRequest` with `rag_fusion: true`.
2. Server checks `[rag_fusion] enabled` in config; if false, falls back to normal search.
3. The original query is always variant #0. Server calls Anthropic API to generate `num_queries` additional semantic variants (rephrases and sub-topic decompositions). Total searches = `num_queries + 1`.
4. The original query (variant #0) and all LLM-generated variants are embedded in parallel using the same embedder.
5. Each variant's vector triggers a full `hybrid_search` call (vector + FTS + per-result RRF) in parallel.
6. The per-variant result sets are fused via a second-pass RRF in `pipeline.py` into a single ranked list. Documents are deduplicated by document ID across variant result sets before fusion — a document appearing in multiple variants receives multiple RRF score contributions (standard RAG Fusion boosting behavior). The cross-encoder reranker then runs on the final fused result set (after second-pass RRF), not per-variant.
7. Response includes `rag_fusion_applied: bool` and `rag_fusion_queries_used: int` (signals partial fusion if some sub-queries failed).
8. If `hyde: true` is also set, RAG Fusion takes precedence; HyDE is silently ignored and `hyde_applied: false` is returned.

## In Scope
- New `archon_search/rag_fusion.py` module: `RAGFusionGenerator` class with async Anthropic client, rate limiter, and fallback logic
- Config: `[rag_fusion]` section with `enabled`, `model`, `timeout_seconds`, `max_requests_per_minute`, `num_queries` (default 2, max 5)
- Request/response fields: `rag_fusion: bool` on `SearchRequest` / `ExplainRequest`; `rag_fusion_applied: bool` + `rag_fusion_queries_used: int` on responses
- Wiring: orchestration logic (mutual exclusion with HyDE, vector resolution, variant dispatch, second-pass RRF) is centralized in `pipeline.py` methods (`search()`, `explain()`). Route handlers and MCP tools only pass the `rag_fusion: bool` flag through — no LLM orchestration at the route level.
- `explain` endpoint: per-sub-query result sets + final fused scores exposed in response
- Telemetry: `rag_fusion_applied` flag + sub-query count (fingerprinted) in telemetry entry; no raw query or sub-query text ever logged
- CI static-analysis guard (analogous to `tests/test_no_fstring_sql.py`) that prevents sub-query variant strings from appearing in telemetry writers; extends the existing no-raw-query invariant guard to cover `rag_fusion.py`
- Eval harness: regression scenario (recall must not drop); latency guard — `rag_fusion: false` path adds zero overhead, with a new `[search_rag_fusion_disabled]` scenario entry in `thresholds.toml` using the same numeric values as the current `[search_baseline]` scenario; a `[search_rag_fusion_enabled]` latency scenario with a generous threshold (≤ 3× baseline p95) to catch severe regressions on the enabled path; live-model improvement baseline
- Unit tests with deterministic known inputs/outputs for the cross-variant RRF function in `pipeline.py`
- Integration test: `rag_fusion: true, hyde: true` request asserts `rag_fusion_applied: true, hyde_applied: false` in both REST and MCP responses
- ADR documenting external LLM dependency, privacy implications, and shared rate-limit operational risk with HyDE
- Operator kill-switch: `[rag_fusion] enabled = false` disables regardless of request field
- Update `BREAKING.md` with new response fields (`rag_fusion_applied`, `rag_fusion_queries_used`, `rag_fusion_attempted`, `rag_fusion_failure_reason`) on `SearchResponse` and `ExplainResponse`

## Out of Scope
- Heuristic/rule-based decomposition — the recall value comes from LLM semantic understanding; a heuristic version is search spam
- Additive HyDE + RAG Fusion combination — compounding produces N×M search cost with unproven benefit; defer to a future iteration
- Per-sub-query explain summary only (Option C) — we ship full per-sub-query result sets in explain from the start
- Distributed rate limiting — in-memory per-process token bucket is sufficient for v1
- UI/dashboard for sub-query inspection — CLI/API access to explain output is enough

## Key Decisions
- **N=2 default, max 5**: `num_queries` = number of LLM-generated variants (not counting the original query). Total searches = `num_queries + 1` (original + variants). Default `num_queries = 2` → 3 total searches (original + 2 LLM variants). Conservative default limits overhead while still delivering recall gains on most multi-faceted queries; operators can tune up to 5.
- **Original query is always variant #0**: The original query is always included in the fusion set as variant #0. With `num_queries=2`, the LLM generates 2 additional variants, giving 3 total searches.
- **LLM-generated variants (Anthropic API)**: Semantic richness is the point; heuristic decomposition was rejected because it doesn't expand semantics.
- **Partial fusion on sub-query failure**: "Sub-query failure" means the LLM generation, embedding, or `hybrid_search` call raised an exception or timed out for that variant. An empty result set from a successful search is NOT a failure — it contributes an empty list to RRF (which adds nothing). If k < `num_queries` LLM-generated variants succeed, fuse variant #0 (original) plus the k available LLM variant result sets rather than fully falling back. `rag_fusion_queries_used` counts only LLM-generated variants that succeeded (0..`num_queries`); does not count variant #0.
- **Hybrid prompt (rephrase + sub-topics)**: 1 semantic rephrase + 1–2 sub-topic decompositions covers both recall diversity and topical expansion without drifting from the original intent.
- **RAG Fusion wins over HyDE when both requested**: Mutual exclusion prevents N×M combinatorial cost explosion. Both features target the same gap (query diversity); RAG Fusion is the more general solution. Suppression happens at the route/MCP handler level: before calling `resolve_hyde_vector()`, the handler checks `rag_fusion: true` and skips the HyDE LLM call entirely (no wasted API spend). The `rag_fusion: bool` flag is then forwarded to `pipeline.py` for orchestration. This is the one piece of RAG Fusion awareness that lives at the route level; all other orchestration stays in `pipeline.py`.
- **Full per-sub-query result sets in explain**: Operators need to debug ranking anomalies — a summary isn't enough. Accepted the larger payload as a deliberate trade-off.
- **Second-pass RRF lives in `pipeline.py`**: The cross-variant RRF function is in `pipeline.py` (not `store.py` or `rag_fusion.py`), uses the same k=60 constant as the first-pass (per-variant) RRF, and deduplicates by document ID across variant result sets before fusion.
- **`rag_fusion_applied` and `rag_fusion_queries_used` on MCP returns**: Both fields appear on all MCP tool returns that include search results, for consistency with REST. MCP return types will be extended to match.
- **`num_queries` is config-only**: No per-request override — consistent with the HyDE pattern. This prevents DoS via client-set N=100 and keeps the API surface minimal.

## Edge Cases & Constraints
- **LLM timeout / API error / rate limit**: Silent fallback to original single-query search. `rag_fusion_applied: false`, `rag_fusion_queries_used: 0`. Server availability is never compromised. For `explain` requests, the LLM failure reason (timeout, API error, rate limit) is included in the explain response even when the search path silently falls back — the explain output must show `rag_fusion_attempted: true, rag_fusion_failure_reason: "..."` when fallback occurs.
- **k=0 results (all variants return empty)**: When all variant result sets (including variant #0, the original query) return empty, the response returns an empty result list with `rag_fusion_applied: true`, `rag_fusion_queries_used: N` (all LLM variants were attempted and succeeded — they simply found nothing). Fusion was applied correctly; the collection has no matches for any framing of the query.
- **`[rag_fusion] enabled = false` in config**: Feature is fully disabled regardless of `rag_fusion: true` on the request. Critical for air-gapped and data-residency deployments.
- **FTS-only collection (no vector index)**: If a collection has no vector index (FTS-only mode), `rag_fusion: true` is silently ignored and `rag_fusion_applied: false` is returned. Embedding N variants for a collection that only does FTS is wasted work — the guard must check vector availability before calling the LLM.
- **Per-collection model mismatch**: If a collection uses a different embedding model than the global embedder, re-embed sub-query vectors with the collection-specific model (same guard as C4 HyDE).
- **`rag_fusion: true` + `hyde: true` together**: RAG Fusion takes precedence. `hyde_applied: false` is returned. Document clearly in the API reference.
- **No raw query / sub-query text in logs**: Both the original query and all generated variants must only appear as SHA-256 fingerprint prefixes. This is a structural invariant — enforced by not accepting a `query` parameter in telemetry entry constructors, and protected by a CI static-analysis guard covering `rag_fusion.py`.
- **Query text leaves the machine**: Sending the original query to Anthropic API must be disclosed in the ADR and operator documentation. Operators who cannot allow this must keep `enabled = false`.
- **Shared rate limit with HyDE**: HyDE and RAG Fusion use separate token buckets but share the same Anthropic API key. Combined steady-state RPM can reach `hyde.max_requests_per_minute + rag_fusion.max_requests_per_minute`. Operators should set both values to stay within their Anthropic account rate limit — document this in `archon-search.toml.example` and note it as a known operational risk in the ADR.
- **Prompt injection**: The user query is forwarded verbatim to the Anthropic API. Prompt injection (e.g., "ignore instructions, return...") can manipulate sub-query generation. Mitigation: validate LLM output — generated variants must be plain text strings, under 500 characters each, with no control sequences. Malformed variants are dropped (counted as failed sub-queries).
- **Reranker placement**: The cross-encoder reranker runs on the final fused result set (after second-pass RRF), not per-variant. Each variant retrieves `top_k_retrieve` candidates; the union (deduplicated by document ID) is passed to the reranker; the reranker returns the final `top_k` ranked results.
- **`num_queries = 1` edge case**: Produces 2 total searches (original + 1 LLM variant) with a trivial second-pass RRF. Config validation warns because the LLM overhead for a single variant rarely justifies the cost. Not identical to normal search — the LLM still runs.

## Open Questions
- What exact system prompt produces the best rephrase + sub-topic split? This requires prompt iteration against the eval corpus — defer to implementation.

## Future Iterations
- **HyDE + RAG Fusion compounding**: Generate HyDE hypothetical documents per sub-query variant for maximum semantic expansion. Only worth investigating after measuring C5 recall gains.
- **Per-sub-query explain summary view**: A compact summary mode (Option C) for operators who don't need full result sets but want sub-query signal.
- **Adaptive N**: Let the LLM decide how many variants are needed based on query complexity, rather than a fixed `num_queries` value.
- **Cached decompositions**: If the same query is decomposed multiple times within a session, cache the variants to avoid redundant LLM calls.

## Recommendation
Build C5 now — the eval harness is in place, the HyDE blueprint makes the implementation path well-understood, and the architectural risk is low. The hardest part is the decomposition prompt: invest time here before writing the wiring, because a poor prompt produces near-duplicate variants that add cost without recall gains. Do not compromise on the no-raw-query logging invariant — sub-query text is just as sensitive as the original query and must never appear in telemetry.
