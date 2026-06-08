# ADR C5 — RAG Fusion: External LLM Dependency, Privacy, and HyDE Mutual Exclusion

**Status**: Draft — to be finalized in Task 7.1

---

## Context

C5 introduces RAG Fusion (multi-query decomposition): user queries are sent to Anthropic's API to generate N semantic variants, which are then searched in parallel and fused via second-pass Reciprocal Rank Fusion (RRF). This creates an external LLM dependency similar to C4 (HyDE).

Key architectural questions to resolve:

1. Why LLM-based decomposition vs. heuristic/rule-based?
2. Privacy trade-off: query text leaves the machine
3. HyDE mutual exclusion design decision and rationale
4. Shared Anthropic API key and combined rate-limit operational risk
5. Evaluated alternatives
6. The final decision and rationale

---

## To Be Finalized in Task 7.1

This stub satisfies the ADR-required-before-merge gate. The full ADR will be written in Task 7.1 and will document:

- (a) Why LLM-based decomposition vs. heuristic (semantic richness is the point; rule-based decomposition cannot capture the full diversity of user intent)
- (b) Privacy trade-off (query text leaves the machine; operators who cannot allow this must keep `[rag_fusion] enabled = false`)
- (c) HyDE mutual exclusion design decision and rationale (route/MCP handler level, not pipeline layer)
- (d) Shared Anthropic API key operational risk (`[hyde].max_requests_per_minute + [rag_fusion].max_requests_per_minute` must not exceed account rate limit)
- (e) Evaluated alternatives (heuristic decomposition, additive HyDE+RAG Fusion, distributed rate limiting)
- (f) The final decision and rationale
