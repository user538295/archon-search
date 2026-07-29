# ADR: C4 — HyDE External LLM Dependency

**Status**: Accepted
**Deciders**: archon-search contributors
**Date**: 2026-06-07
**Supersedes**: nothing
**Superseded by**: nothing

---

## Context

Dense retrieval underperforms when the user's query vocabulary is far from the document vocabulary in embedding space. HyDE (Hypothetical Document Embeddings, Gao et al. 2022) addresses this by asking an LLM to write a short hypothetical answer passage, embedding that passage, and using the resulting vector for ANN lookup. The hypothesis lands closer to the answer in embedding space than the original query.

To generate the hypothesis, the LLM must receive the user's raw query. This is a fundamental requirement of the approach — it cannot be avoided by pre-processing or hashing.

archon-search is designed as a **local, single-user service** with an explicit privacy guarantee: no raw query text leaves the host. HyDE breaks this guarantee for callers who opt in. The question is whether, and how, to accommodate this trade-off.

---

## Decision drivers

1. **Privacy-first posture**: the existing telemetry invariant is structural — `TelemetryEntry` has no `query` field and none of its factories accept one. HyDE cannot silently inherit this invariant; it must be explicitly opt-in.
2. **Availability over quality**: the server must not degrade availability when the LLM API is unreachable. Silent fallback to the original query embedding is required.
3. **Operator visibility**: operators running the server must receive an explicit startup warning that `hyde.enabled = true` causes query text to leave the host.
4. **Optional dependency**: the `anthropic` package is non-trivial; it must not become a required installation for users who do not use HyDE.

---

## Evaluated alternatives

### A — Skip HyDE entirely

Do not implement HyDE. Dense retrieval continues to underperform on vocabulary-mismatch queries.

**Rejected.** The recall improvement on mismatch queries is well-documented (Gao et al. 2022, multiple replication studies). Depriving operators of an easily toggleable mechanism is not justified when the opt-in mechanism can be made explicit and auditable.

### B — Local model for hypothesis generation

Use a small, locally-running LLM (e.g. `llama.cpp` or `ollama`) rather than an API call. All query text stays on the host.

**Rejected for v1** for the following reasons:
- Bundling or requiring a local LLM runtime is a substantial engineering and packaging effort (runtime detection, cross-platform support, model download, GPU detection).
- Local inference at the latency budget imposed by HyDE (<5 s default timeout) requires an actively warmed GPU or a very small model; small models produce lower-quality hypotheses.
- The target deployment is a personal machine where an API key is more likely available than a GPU-accelerated local LLM.
- This alternative remains open for a future ADR if demand materialises.

### C — Remote API call (Claude Haiku) with explicit opt-in — **chosen**

Use `anthropic.AsyncAnthropic` to call `claude-haiku-4-5-20251001` (or operator-configured model). The call is:
- **Per-request opt-in** (`hyde=true` in the request body).
- **Operator-level kill-switch** (`[hyde] enabled = false` in TOML; default).
- **Silent fallback** on timeout, API error, rate limit, or missing API key. `hyde_applied: false` in the response tells the caller the fallback fired.
- **No telemetry exposure**: `archon_search/hyde.py` never logs the query or hypothesis text verbatim. Log messages use `_query_fingerprint(query)` (SHA-256, truncated to 16 hex chars) for correlation without reversal. A CI guard (`tests/test_no_query_log_in_hyde.py`) enforces this structurally.
- **Startup INFO log** when `enabled = true`: `"HyDE is enabled — search query text will be sent to Anthropic's API (model: %s)"`. This makes the data-transmission explicit in server logs.

---

## Decision

Use alternative C.

The external LLM dependency is acceptable because:

1. HyDE is **opt-in at two levels** (operator config + per-request flag). Operators who do not set `enabled = true` in TOML pay zero overhead and zero privacy risk.
2. The API key is **operator-provisioned** (`ANTHROPIC_API_KEY` env var). The server never auto-discovers or auto-installs credentials.
3. The fallback path is **always available**. A missing key, a timeout, or a rate-limit causes `hyde_applied: false` — not a 5xx or degraded availability.
4. The privacy trade-off is **explicitly documented** in: this ADR, `archon-search.toml.example`, the operator guide (`UserManual/60_searching.md`), and the `[hyde] enabled` TOML comment. Operators who enable HyDE do so knowingly.
5. The `anthropic` package is an **optional dependency** (`pip install archon-search[hyde]`). Installations without it return a clear 422 when `hyde=true` is requested, rather than silently degrading.

---

## Privacy trade-off (explicit statement)

When `[hyde] enabled = true` and a caller sends `hyde=true`:

- The user's raw query (up to 2000 chars) is sent to Anthropic's API servers over HTTPS.
- Anthropic's data-processing terms apply to that traffic.
- The hypothesis text returned by the API is consumed only by the local embedder and is **never logged, stored, or returned to the caller**.
- `doc_id`s, collection names, and result content are never sent.

This is the only point in archon-search v1 where user data leaves the host by design. It is gated behind two explicit operator decisions (install the optional dep, set `enabled = true`) and one per-request decision (`hyde=true`).

---

## Consequences

**Positive**:
- Recall improvement on vocabulary-mismatch queries is available to callers who opt in.
- The feature is zero-cost for callers who do not opt in.
- The implementation is simple: one optional dependency, one module (`archon_search/hyde.py`), one helper (`resolve_hyde_vector`), one config section.

**Negative / accepted**:
- Query text leaves the host when HyDE is active. This breaks the "no external transmission" invariant for opted-in traffic.
- Rate limits are per-process in-memory. Multi-worker deployments multiply the effective rate by worker count. Documented in the operator guide.
- Missing `ANTHROPIC_API_KEY` is discovered on the first `hyde=true` request, not at startup. A WARNING is logged; subsequent requests are not repeated-warned.
- The quality improvement from HyDE is empirical and vocabulary-specific. The eval harness verifies non-regression (recall does not decrease), not improvement — measuring improvement requires real model weights and a live API call.

---

## Related

- `Documentation/Backlog/C4-hyde-query-expansion-brief.md` — full design brief
- `Documentation/Backlog/C4-hyde-query-expansion-plan.md` — implementation plan
- `archon_search/hyde.py` — implementation
- `Documentation/Architecture/150_security_and_privacy_architecture.md` — privacy model
- `tests/test_no_query_log_in_hyde.py` — CI telemetry invariant guard
