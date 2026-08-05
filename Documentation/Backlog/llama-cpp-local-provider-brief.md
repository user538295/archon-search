# Feature Brief: llama.cpp Local Provider

## Problem
Users who want a fully local, private setup can't use archon-search's most powerful search features (smarter query understanding via HyDE and RAG Fusion, and knowledge-graph summarisation) without an Anthropic or OpenAI API key. llama.cpp is a popular way to run capable AI models on your own hardware — but it's not supported today.

## Goal
A user running `llama-server` locally can set `provider = "llama_cpp"` in their config and get full feature parity with the cloud providers: smarter search (HyDE + RAG Fusion) and knowledge-graph enrichment — all on-device, no API key, no external network calls.

## Users & Context
Developers and operators who prioritise privacy, work air-gapped, or want to avoid per-query cloud costs. They already run Ollama or llama.cpp alongside their search server. They expect local providers to be first-class citizens, not afterthoughts.

## Core Flow
1. User installs and starts `llama-server` (the HTTP server that ships with llama.cpp) on their machine, pointing it at a downloaded model file.
2. User sets `provider = "llama_cpp"` and `llama_cpp_base_url = "http://localhost:8080"` under `[hyde]`, `[rag_fusion]`, and/or `[graph]` in their config file.
3. On server start, the wizard (if run) queries `llama-server`'s `/v1/models` endpoint and presents a model picker — same UX as the existing Ollama wizard step.
4. archon-search connects to `llama-server` over HTTP for all AI calls (query expansion, graph summarisation). No API key is checked; no external traffic leaves the machine.
5. If `llama-server` is unreachable, archon-search falls back to plain search (same silent-fallback behaviour as Ollama today).

## In Scope
- New `LlamaCppQueryExpansionProvider` for HyDE and RAG Fusion — calls `llama-server`'s `/v1/chat/completions` via `httpx` (already a core dependency; no new package).
- New `LlamaCppEnrichmentClient` for graph community summarisation and relationship labelling — same `httpx` path, implements the existing `LLMEnrichmentClientProtocol`.
- Ollama graph enrichment (`OllamaEnrichmentClient`) — previously deferred as G10b; included now alongside llama.cpp.
- `OpenAIEnrichmentClient` and `ClaudeCLIEnrichmentClient` — since the enrichment factory is being made pluggable anyway, all four existing providers get full graph enrichment parity in one move. Marginal extra work once the refactor is done.
- Config: `"llama_cpp"` added to `_VALID_PROVIDERS`; `llama_cpp_base_url` field added to `HyDEConfig`, `RAGFusionConfig`, and `GraphConfig`; `"llama_cpp:model"` and `"ollama:model"` accepted in the `extraction_model` field (the `"provider:model"` format already exists).
- Wizard model picker: queries `llama-server`'s `/v1/models` endpoint, same pattern as the Ollama `/api/tags` picker.
- Startup validation: `_check_provider_deps()` updated to handle `llama_cpp` (no key check; warn if `llama-server` is unreachable but don't block boot).
- Rate limiting: skipped for `llama_cpp` (same as Ollama — local model, no API cap).
- No new Python install extras — `httpx` is already installed.

## Out of Scope
- **llama.cpp embedded/in-process mode** (`llama-cpp-python` bindings) — loads a ~1–4 GB model into the same process as the search server; memory pressure and crash risk outweigh any latency benefit. Server mode covers all use cases.
- **Reranker or embedder via llama.cpp** — those stay as local ONNX models via fastembed; they're already zero-cost and fast.
- **Authentication / API key support for llama-server** — llama-server in local mode requires no key; token auth on a local server is an edge case deferred to a future brief.

## Key Decisions
- **Server mode (HTTP) over embedded mode**: keeps archon-search lean; matches the Ollama mental model users already know; adds zero new Python dependencies.
- **`httpx` over the `openai` SDK**: `httpx` is already a core dependency, so the llama.cpp provider ships with no new install step; the `openai` SDK would require the existing `openai-provider` extra and create user confusion ("why do I need an openai package for llama.cpp?").
- **Named `llama_cpp` provider** (not routed through `openai` provider with a custom URL): unambiguous config, no key-check confusion, follows the established Ollama pattern exactly.
- **All providers get graph enrichment parity**: the enrichment factory refactor is the hard part; once done, adding all five clients (Anthropic already exists; Ollama, llama.cpp, OpenAI, claude_cli are new) costs little. Scoping it to only local providers would leave a half-done abstraction.

## Edge Cases & Constraints
- **`llama-server` not running at query time**: provider returns `None`/`[]`; HyDE and RAG Fusion fall back to plain search silently (existing fallback path, no new behaviour needed).
- **`llama-server` not running at startup**: startup validation logs a WARNING but does not block server boot — same policy as the Ollama provider.
- **Model not loaded in `llama-server`**: the HTTP call returns an error; provider catches it, logs a WARNING with the query fingerprint, returns `None`/`[]`.
- **Graph enrichment failure**: `LlamaCppEnrichmentClient` raises on failure (matching `AnthropicEnrichmentClient`'s contract); callers (`CommunityBuilder`, `GraphExtractor`) catch and substitute `None`/empty list — no change to caller behaviour.
- **Wizard with unreachable `llama-server`**: falls back to free-text model name entry, same as the Ollama wizard fallback.
- **`extraction_model` parsing**: the existing `"provider:model"` parser must be updated to accept `"llama_cpp:"`, `"ollama:"`, `"openai:"`, and `"claude_cli:"` prefixes without breaking existing `"anthropic:"` values.

## Open Questions
- Should `LlamaCppEnrichmentClient` and `OllamaEnrichmentClient` live in `llm_enrichment_client.py` alongside `AnthropicEnrichmentClient`, or in separate files under a new `enrichment/` sub-package? (The query expansion providers each have their own file under `providers/` — consistency may favour the same split.)
- `GraphConfig` currently has `extraction_model: str | None`. Should it gain a separate `extraction_base_url: str | None` field (for llama.cpp), or should the URL be embedded in a new `"provider:model@base_url"` format? The existing `"provider:model"` format doesn't carry a URL.
- Should the startup model-availability probe (`model_validation.py`) extend to `llama-server` reachability, or is the existing warn-and-continue policy sufficient?
- `OllamaEnrichmentClient` can reuse the `ollama` Python SDK (already installed via `archon-search[ollama]`) rather than `httpx` — confirm whether that's consistent with the "no new deps for enrichment" intent, or whether httpx is preferred for uniformity.

## Future Iterations
- **OpenAI-compatible generic provider** (`openai_compatible` or similar) — covers LM Studio, vLLM, Jan.ai, and any other OpenAI-API server. llama.cpp is the concrete demand now; generalise when a second use case arrives.
- **`llama_cpp` install extra** — if the ecosystem shifts and a Python client SDK for llama.cpp's HTTP API becomes standard, a dedicated extra can be added without breaking existing configs.
- **Authentication on llama-server** — local bearer token support if operators lock down their llama-server instance.

## References
- **Team plan:** [llama-cpp-local-provider-team-plan.md](./llama-cpp-local-provider-team-plan.md)
- [`archon_search/providers/ollama_provider.py`](../../archon_search/providers/ollama_provider.py) `[code-agent]` — OllamaQueryExpansionProvider: the direct template for the new llama.cpp adapter
- [`archon_search/providers/openai_provider.py`](../../archon_search/providers/openai_provider.py) `[code-agent]` — OpenAIQueryExpansionProvider: shows the response-shape normalisation pattern
- [`archon_search/query_expansion_protocol.py`](../../archon_search/query_expansion_protocol.py) `[code-agent]` — QueryExpansionProvider protocol and `provider_key_available` helper
- [`archon_search/config.py`](../../archon_search/config.py) `[code-agent]` — HyDEConfig, RAGFusionConfig, GraphConfig, `_VALID_PROVIDERS`
- [`archon_search/server/app.py`](../../archon_search/server/app.py) `[code-agent]` — `_build_query_expansion_provider` factory and `_check_provider_deps` startup validation
- [`archon_search/llm_enrichment_client.py`](../../archon_search/llm_enrichment_client.py) `[code-agent]` — AnthropicEnrichmentClient: template for the new enrichment adapters
- [`archon_search/graph_enrichment_protocol.py`](../../archon_search/graph_enrichment_protocol.py) `[code-agent]` — LLMEnrichmentClientProtocol: the interface both new clients must implement
- [`Documentation/ADRs/C4-hyde-external-llm-dependency.md`](../ADRs/C4-hyde-external-llm-dependency.md) `[docs-agent]` — explicitly deferred llama.cpp for v1; this brief is the "future ADR" it anticipated
- [`Documentation/Completed/g10-llm-provider-matrix-brief.md`](../Completed/g10-llm-provider-matrix-brief.md) `[docs-agent]` — G10 brief that introduced the provider abstraction; llama.cpp follows the same architecture
- [`Documentation/Completed/2026-07-15-030-llm-provider-selection-brief.md`](../Completed/2026-07-15-030-llm-provider-selection-brief.md) `[docs-agent]` — noted llama.cpp/LM Studio as a future iteration; this brief delivers it
- [`Documentation/Completed/e2i-llm-graph-enrichment-brief.md`](../Completed/e2i-llm-graph-enrichment-brief.md) `[docs-agent]` — graph enrichment brief; planned Ollama via G10b; both Ollama and llama.cpp are now in scope here

## Recommendation
This is the right feature to build now. The provider abstraction from G10 was designed exactly for this — adding llama.cpp is incremental, not architectural. The hardest part is not the query-expansion adapter (that's a near-copy of the Ollama provider) but making graph enrichment pluggable: `llm_enrichment_client.py` currently has a single Anthropic-only concrete class, and the factory routing logic needs to be pulled out and generalised. Do that first — it unlocks both Ollama and llama.cpp enrichment in one move. What must not be compromised: the silent-fallback guarantee (a misconfigured or offline llama-server must never crash a search request) and the no-raw-query-in-logs invariant that applies to all providers.
