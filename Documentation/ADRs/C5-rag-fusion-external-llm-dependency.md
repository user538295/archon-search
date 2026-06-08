# ADR C5 — RAG Fusion: External LLM Dependency, Privacy, and HyDE Mutual Exclusion

**Status**: Accepted

---

## Context

C5 introduces RAG Fusion (multi-query decomposition): user queries are sent to Anthropic's API to generate N semantic variants, which are then searched in parallel and fused via second-pass Reciprocal Rank Fusion (RRF). This creates an external LLM dependency similar to C4 (HyDE), but with different privacy and operational characteristics.

Key questions this ADR resolves:

1. Why LLM-based decomposition vs. heuristic/rule-based?
2. Privacy trade-off: query text leaves the machine.
3. HyDE mutual exclusion design decision and rationale.
4. Shared Anthropic API key and combined rate-limit operational risk.
5. Evaluated alternatives.
6. The final decision and rationale.

---

## Decision

### (a) Why LLM-based decomposition vs. heuristic/rule-based

The purpose of RAG Fusion is to surface documents that the original query phrasing would miss — either because the user chose different vocabulary than the document, or because the query is multi-faceted and a single embedding vector cannot represent all of its aspects simultaneously.

Rule-based decomposition (splitting on conjunctions, stemming, synonym expansion) can mechanically broaden a query, but it cannot generate semantically distinct reformulations that capture different *facets* of the same information need. For example, the query "how do I remove the CLI tool on macOS?" could be validly reformulated as "uninstall archon-search on macOS", "delete archon-search CLI", and "remove archon-search binary from PATH" — phrasings that a rule system cannot reliably generate without a comprehensive synonym database.

An LLM generates high-quality semantic reformulations by understanding the intent behind the query and producing alternative expressions that preserve the meaning while varying the surface form. This is the core mechanism that makes RAG Fusion effective.

The cost of this quality is an external API call per request. The kill-switch (`[rag_fusion] enabled = false`) and silent fallback on all failure paths ensure that the external dependency is never a reliability bottleneck.

### (b) Privacy trade-off — query text leaves the machine

When `[rag_fusion] enabled = true` in config *and* a caller includes `rag_fusion=true` in their request, the user's raw query (up to 2000 characters) is sent to Anthropic's API servers over HTTPS. This is a deliberate operator opt-in.

**Gating requirements** — RAG Fusion transmission occurs only when all three conditions hold simultaneously:

1. The operator has installed `archon-search[rag_fusion]` (optional dependency).
2. The operator has set `[rag_fusion] enabled = true` in `~/.archon-search/archon-search.toml`.
3. The caller includes `rag_fusion=true` in the request body.

**Invariants preserved despite the external call:**

- The LLM-generated query variants are consumed only by the local embedder. They are **never logged, stored in LanceDB, or returned to the caller**.
- Log messages in `archon_search/rag_fusion.py` use `_query_fingerprint(query)` (SHA-256 truncated to 16 hex chars from `archon_search/_privacy.py`) — the raw query is never passed to any logging call. A CI guard (`tests/test_no_query_log_in_rag_fusion.py`) enforces this structurally.
- `TelemetryEntry` factories receive no query text — the RAG Fusion path does not weaken the telemetry structural invariant (`rag_fusion_applied: bool | None` and `rag_fusion_queries_used: int | None` are the only new telemetry fields).
- Fallback is silent on runtime errors (timeout, API error, missing key, rate limit): `rag_fusion_applied: false` in the response, not an error. Availability is never degraded.
- **Exception**: if the `anthropic` package is not installed, the route handler returns `422` (`RAGFusionDependencyError`) — this is a configuration error, not a runtime fallback. Installing `archon-search[rag_fusion]` resolves it.

**Operators who cannot allow query text to leave the machine** must keep `[rag_fusion] enabled = false` (the default). This applies to air-gapped deployments and deployments with strict data-residency requirements.

**Operator visibility:** when `enabled = true`, the server logs an INFO message at startup naming the model. This makes the data-transmission fact visible in server logs.

### (c) HyDE mutual exclusion design decision

RAG Fusion and HyDE cannot be composed additively:

- HyDE produces a single embedding vector from a hypothetical answer passage, then does one ANN lookup.
- RAG Fusion produces N query variants, embeds each, and does N+1 ANN lookups fused via RRF.
- Combining them would require N+1 separate HyDE calls (one per query variant) — multiplying both LLM latency and API cost by N+1.

**Decision: `rag_fusion=true` takes precedence. When both `rag_fusion=true` and `hyde=true` are present in a request, RAG Fusion executes and HyDE is skipped (`hyde_applied: false` in the response).** This is not a pipeline-layer concern; the mutual exclusion is enforced at the route/MCP handler level, before the pipeline is called. This matches the pattern established by `resolve_hyde_vector` in C4 and avoids coupling the pipeline to HyDE internals.

The rationale for RAG Fusion winning the exclusion:

- RAG Fusion subsumes HyDE's intent: both improve recall for vocabulary-mismatch queries. RAG Fusion does so by searching with multiple query phrasings; HyDE does so by approximating the answer's embedding. Using both simultaneously would be redundant and expensive.
- If a caller sends both flags, they are opting into the higher-recall (and higher-cost) feature. Silently downgrading to HyDE-only would violate caller intent.

### (d) Shared Anthropic API key and combined rate-limit operational risk

Both HyDE (`archon_search/hyde.py`) and RAG Fusion (`archon_search/rag_fusion.py`) read `ANTHROPIC_API_KEY` from the environment and call the Anthropic API. Each maintains its own per-process token-bucket rate limiter (`[hyde].max_requests_per_minute` and `[rag_fusion].max_requests_per_minute` respectively).

**Operational risk**: these limiters are independent. In steady state, both enabled features can together make up to `max(hyde_rpm, rag_fusion_rpm)` calls per minute from a single process — or more precisely, up to `hyde_rpm + rag_fusion_rpm` calls per minute if every request triggers both features. Since both flags on a single request are mutually exclusive (decision (c) above), the actual peak per request is `max(hyde_rpm, rag_fusion_rpm)`. However, different requests can independently invoke HyDE or RAG Fusion, so the combined peak across requests can approach `hyde_rpm + rag_fusion_rpm`.

**In multi-worker deployments**, each worker runs its own in-memory rate limiter. The effective combined rate is up to `workers × (hyde_rpm + rag_fusion_rpm)`. Operators running N workers must ensure `N × (hyde_rpm + rag_fusion_rpm)` does not exceed their Anthropic account rate limit.

**Operator guidance** (also in `archon-search.toml.example`):

```toml
[hyde]
max_requests_per_minute = 30  # tune these together

[rag_fusion]
max_requests_per_minute = 30  # combined <= account rate limit / workers
```

This is documented as accepted risk in v1. Distributed rate limiting (shared across workers) is out of scope for this release.

### (e) Evaluated alternatives

| Alternative | Why rejected |
|---|---|
| Heuristic/rule-based decomposition (split on conjunctions, synonym expansion) | Cannot generate semantically diverse reformulations; quality ceiling is too low to justify the complexity |
| Additive HyDE + RAG Fusion combination | Multiplies LLM cost by N+1; RRF over HyDE-embedded variants adds complexity for uncertain incremental gain; out of scope for v1 |
| Distributed rate limiting (Redis token bucket shared across workers) | Adds an infrastructure dependency; solving a problem that only affects multi-worker deployments, which are rare for this tool's target use case |
| Per-request `num_queries` override | Adds API surface complexity; default N=2 is appropriate for the vast majority of use cases; can be revisited in a future release |
| Separate API key for RAG Fusion vs. HyDE | Adds secret-management burden without meaningful isolation benefit; the shared key already gates both behind operator opt-in |

### (f) Final decision and rationale

RAG Fusion is implemented as an optional operator-controlled feature (`[rag_fusion] enabled = false` by default) that:

1. Sends the user's raw query to Anthropic's API only when the operator has explicitly opted in at both the config level and the dependency level.
2. Generates N=2 LLM-powered semantic query variants (configurable 1–5) and fuses the N+1 result sets via second-pass RRF.
3. Falls back silently to single-query search on runtime failure paths (timeout, API error, missing key, rate limit), preserving availability. Missing `anthropic` package returns `422` (configuration error, not a silent fallback).
4. Enforces mutual exclusion with HyDE at the route/MCP handler level, with RAG Fusion taking precedence.
5. Shares the `ANTHROPIC_API_KEY` with HyDE, requiring operators to tune both `max_requests_per_minute` values to stay within their account rate limit.
6. Preserves all privacy invariants: no raw query text in logs or telemetry; CI guard enforces this structurally.

This balances recall improvement against operational simplicity, explicit privacy consent, and zero degradation for callers who do not opt in.

---

## Consequences

- `archon_search/rag_fusion.py` introduces a new optional external dependency (`anthropic>=0.40`); install with `pip install archon-search[rag_fusion]`.
- `ANTHROPIC_API_KEY` is now used by up to two features simultaneously; combined rate-limit planning is required for multi-worker deployments.
- The no-raw-query telemetry invariant is preserved; `tests/test_no_query_log_in_rag_fusion.py` guards it in CI.
- HyDE and RAG Fusion cannot be combined additively in v1; this is documented in the operator guide.
- The `archon_search/_privacy.py` module was extracted in C5 to provide `_query_fingerprint` as a shared utility for both `hyde.py` and `rag_fusion.py`, avoiding duplicate implementations.

---

## References

- `Documentation/ADRs/C4-hyde-external-llm-dependency.md` — C4 precedent for this architectural pattern.
- `Documentation/Backlog/C5-rag-fusion-brief.md` — full design brief.
- `archon_search/rag_fusion.py` — implementation.
- `archon_search/_privacy.py` — shared `_query_fingerprint` utility.
- `tests/test_no_query_log_in_rag_fusion.py` — CI privacy guard.
- `archon-search.toml.example` — `[rag_fusion]` section with rate-limit warning.
