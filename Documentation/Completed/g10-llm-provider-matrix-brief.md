# Feature Brief: G10 — LLM Provider Matrix for Query Expansion

## Problem

Operators who can't or don't want to use Anthropic's API — because of cost, data privacy requirements, or air-gapped deployments — cannot use HyDE or RAG Fusion at all today. Both features are locked to Anthropic.

## Goal

An operator sets one config field (`provider = "ollama"`) and HyDE and RAG Fusion run on a local model with no API key, no network calls, and no per-query cost. OpenAI is also a supported alternative for operators already on that platform.

## Users & Context

Operators who deploy archon-search and want to improve search quality but face one of:
- A data residency requirement (queries must not leave the machine)
- No Anthropic account, or an existing OpenAI subscription they'd rather use
- A cost-sensitive deployment where per-query API charges add up

They configure the server once, in `~/.archon-search/archon-search.toml`, and the change applies to every search from that point on.

## Core Flow

1. Operator installs the relevant extra for their chosen provider (`archon-search[ollama]` for local, or the `openai` package is already available for OpenAI users).
2. Operator opens their config file and sets `provider` under `[hyde]` and/or `[rag_fusion]` — e.g. `provider = "ollama"`, `model = "llama3.2"`.
3. For Ollama: no further setup needed if Ollama is running locally on its standard port. For a non-standard address: add `ollama_base_url`.
4. Operator restarts the server. `GET /status` confirms which provider is active for each feature.
5. Searches using `hyde=true` or `rag_fusion=true` now route to the configured provider. Silent fallback to plain search applies if the provider is unreachable — same behaviour as today.

## In Scope

- `provider` config field for `[hyde]` and `[rag_fusion]` sections (values: `anthropic`, `openai`, `ollama`; default: `anthropic`)
- Ollama support: `model`, `ollama_base_url` (default `http://localhost:11434`), `timeout_seconds`; installed via `archon-search[ollama]` optional extra
- OpenAI support: `model` (e.g. `gpt-4o-mini`); uses the `openai` package
- `QueryExpansionProvider` protocol with `generate_hypothetical_doc(query)` and `decompose_query(query)` methods — wraps all three providers
- Existing Anthropic implementations refactored to implement the protocol (no behaviour change)
- `GET /status` exposes the active provider for each feature
- Silent fallback preserved: unreachable provider → plain search, same as today
- Rate limiting (`max_requests_per_minute`) honoured for Anthropic and OpenAI; silently ignored for Ollama (local model, no API cap)
- Privacy warnings in `archon-search.toml.example` updated: Ollama explicitly noted as the zero-transmission option

## Out of Scope

- **Graph enrichment provider switching** (community summaries, typed relationship extraction via `AnthropicEnrichmentClient`) — same pattern, separate follow-up ticket (`G10b`). Scoping it here would increase effort by ~50% and delay the query-expansion unlock.
- **Ollama model download or management** — Ollama handles its own models; archon-search just calls the API.
- **Provider health checks at startup** — the existing silent-fallback mechanism is sufficient; a startup probe adds complexity without meaningful user benefit.

## Key Decisions

- **Separate `provider` and `model` fields (not a combined `"ollama:llama3.2"` string):** Existing operators change nothing on upgrade — `provider` defaults to `"anthropic"` and their current `model` value keeps working. A combined string would silently break every existing config.
- **Ollama base URL defaults to `http://localhost:11434`:** This is Ollama's universal default address. Requiring operators to set it explicitly adds a step with no benefit for the common case.
- **Query expansion only, not graph enrichment:** Keeps the effort estimate honest; the graph-enrichment protocol (`LLMEnrichmentClientProtocol`) already exists as a clean abstraction, so `G10b` is a small, well-defined follow-up.
- **Rate limiting ignored for Ollama:** A local model has no API cap. Silently ignoring `max_requests_per_minute` when `provider = "ollama"` is the right call — warning the operator about a limit that doesn't apply would be noise.

## Edge Cases & Constraints

- **Ollama not running:** Client call times out; silent fallback to plain search fires, same as the current Anthropic-unreachable path. No new error handling needed.
- **OpenAI package not installed when `provider = "openai"` is set:** Server raises a clear `ConfigError` at startup (mirrors the existing `ImportError` guard pattern in `hyde.py` and `rag_fusion.py`).
- **`ollama` package not installed when `provider = "ollama"` is set:** Same — `ConfigError` at startup.
- **HyDE and RAG Fusion can use different providers:** Each section has its own `provider` field; they are independent. An operator can run `[hyde] provider = "ollama"` and `[rag_fusion] provider = "anthropic"` simultaneously.
- **Shared rate limit warning (Anthropic):** The existing TOML warning that `hyde.max_requests_per_minute + rag_fusion.max_requests_per_minute` must not exceed the Anthropic account cap remains valid and stays in the example config.
- **Privacy invariant preserved:** Raw query text is never logged or stored regardless of provider — the `_query_fingerprint()` telemetry pattern applies to all three.

## Open Questions

- Should `ollama_base_url` be a top-level `[ollama]` section (shared across HyDE and RAG Fusion) or repeated per feature section? Repeated-per-feature is simpler to implement and consistent with existing per-feature config; a shared section avoids duplication for operators running both. `/plan-maker` should decide.
- The `openai` package is already a dev dependency (added for G9 E2E tests). For production, it should be an optional extra (`archon-search[openai-provider]` or similar). Confirm naming and whether it conflicts with the dev dep.
- `GET /status` currently returns a flat structure. Where does `hyde.provider` and `rag_fusion.provider` appear — in the existing `hyde`/`rag_fusion` sub-objects or as new top-level keys? Check `routes_status.py` schema before planning.

## Future Iterations

- **G10b — Graph enrichment provider switching:** Add Ollama/OpenAI support to `AnthropicEnrichmentClient` via the existing `LLMEnrichmentClientProtocol`. The protocol is already in place; this is a small, self-contained follow-up that completes the "fully local" story.
- **Ollama streaming:** Ollama supports streaming token output. HyDE and RAG Fusion consume the full response text, so streaming isn't needed today — but it would reduce time-to-first-result for large models.
- **Provider health surfaced in `GET /ready`:** Today `/ready` only checks the embedding model. A provider ping (with timeout) could signal readiness for HyDE/RAG Fusion too.

## Recommendation

This is the right feature to build now. The Ollama path is the highest-value unlock — it turns two search-quality features from "paid opt-in" to "always available and fully local," which is a genuine differentiator for privacy-sensitive deployments. The hardest part is getting the protocol abstraction clean enough that adding a fourth provider later costs nothing; that abstraction already exists as a template (`LLMEnrichmentClientProtocol` in `graph_enrichment_protocol.py`). What must not be compromised: the silent fallback guarantee and the no-raw-query-in-logs invariant — both are load-bearing trust properties that must hold for every provider.
