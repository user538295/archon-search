# ADR C6 — llama.cpp Local LLM Provider (HyDE, RAG Fusion, Graph Enrichment)

**Status**: Accepted

---

## Context

C4 (HyDE) and C5 (RAG Fusion) introduced Anthropic as the only LLM provider for query expansion; G10 later added `ollama`, `openai`, and `claude_cli`, giving operators a zero-transmission (`ollama`) or subprocess-based (`claude_cli`) local alternative. Knowledge-graph enrichment (community summarisation, typed relationship labelling) shipped in E2i BE-0 with a protocol (`LLMEnrichmentClientProtocol`) and one concrete client (`AnthropicEnrichmentClient`), but the client was orphaned — no factory constructed it, and both call sites (`CommunityBuilder._generate_llm_summary`, `GraphExtractor.extract`'s inline LLM path) were stubs (see `530_technical_debt_refactoring_roadmap.md` GRAPH-2).

`llama-server` (the llama.cpp project's OpenAI-compatible HTTP server) is a common way to run a local model on commodity hardware, distinct from `ollama` in daemon model and from `llama-cpp-python`'s embedded/in-process mode. Operators who already run `llama-server` for other tools wanted the same HyDE/RAG-Fusion/graph-enrichment feature parity that `ollama` gets, and wanted graph enrichment to actually work rather than silently degrade.

This ADR (LLCP) resolves two things together: (1) a fifth query-expansion provider, `llama_cpp`, calling llama-server's `/v1/chat/completions`; (2) wiring graph enrichment for the first time, with `llama_cpp` as one of four concrete enrichment adapters alongside `anthropic`, `ollama`, and `openai`.

---

## Decision

### (a) `llama_cpp` as a fifth query-expansion provider, not a generic "OpenAI-compatible" provider

`LlamaCppQueryExpansionProvider` (`archon_search/providers/llama_cpp_provider.py`) calls a fixed `{base_url}/v1/chat/completions` endpoint via `httpx` — a core dependency, so **no new pip extra**. It deliberately does not attempt to be a generic `openai_compatible` provider that could also target LM Studio, vLLM, or Jan.ai: those have their own quirks (model-listing shape, auth headers, chat-template handling) that a one-size-fits-all adapter would paper over incorrectly. A generic adapter is deferred to a future iteration if demand appears (see "What does NOT change" in the team plan).

### (b) Enrichment is greenfield wiring, not a refactor of an existing factory

There was no `QueryExpansionProvider`-style factory for enrichment before this feature — `EnrichmentClientFactory` (`archon_search/enrichment/factory.py`) is new. It mirrors `_build_query_expansion_provider`'s dispatch shape (Q10-style: one function, one place, `provider` string in → concrete adapter out) but is a distinct code path serving a distinct protocol (`LLMEnrichmentClientProtocol`: raise-on-failure, vs. `QueryExpansionProvider`: never-raise). Four concrete adapters ship in `archon_search/enrichment/` (one file per provider, mirroring `archon_search/providers/`): `llama_cpp.py`, `ollama.py` (httpx, not the `ollama` SDK — kept consistent with the other three v1 clients), `openai.py`, and `anthropic.py` (moved here from a standalone module, now actually constructed and injected for the first time).

`claude_cli` is a valid `_PROVIDER_REGISTRY` member and is offered for HyDE/RAG Fusion, but has **no v1 enrichment client** — it is subprocess-based with no HTTP endpoint, and mapping `summarize_community`/`label_relationships` onto `claude -p` invocations is a different design problem deferred post-v1. `EnrichmentClientFactory.build()` logs a WARNING and returns `None` if `[graph] provider = "claude_cli"` is set anyway, rather than raising — consistent with "boot never blocked."

### (c) `[graph] provider` is a discrete field and is itself the enrichment gate

Unlike `HyDEConfig.provider`/`RAGFusionConfig.provider` (default `"anthropic"`, gated by a separate `enabled: bool`), `GraphConfig.provider` defaults to `None` and **is** the enrichment enable gate — there is no `[graph].enrichment_enabled`. This asymmetry is intentional: `[graph] enabled` already gates a large, independently useful subsystem (entity extraction, PPR, communities) that works correctly with zero LLM calls; conflating enrichment's gate with that flag would force every graph-enabled operator to either get LLM calls they didn't ask for or lose graph entirely. Defaulting `provider` to `None` preserves the air-gap guarantee: enrichment is off until explicitly configured, even when `graph.enabled = true`.

The previous (never-shipped) design surfaced in `archon-search.toml.example` used a combined `extraction_model = "provider:model"` string with a prefix parser. This ADR rejects that in favour of the discrete field: prefix-parsing model names is fragile (model names can themselves contain colons, e.g. some Ollama tags) and duplicates the `provider` concept `HyDEConfig`/`RAGFusionConfig` already model as a separate field. `extraction_model` is now always a bare model name.

### (d) Intentional deviations from the C4/C5 precedent

- **No optional-dependency `422`/`ConfigError` for `llama_cpp`.** `_check_provider_deps` (`server/app.py`) never guards `llama_cpp`: `httpx` is a core dependency (no `pip install archon-search[...]` needed), and llama-server needs no API key to check. This mirrors the `claude_cli` precedent (also never guarded) rather than the `ollama`/`openai` precedent (guarded on `ImportError`), because there is no import to fail.
- **No rate limiting for `llama_cpp`.** Query-expansion: rate limiting is skipped at the generator's call site for `llama_cpp`, matching `ollama`/`claude_cli` (local, unthrottled). Enrichment: `GraphConfig.extraction_rate_limit_rpm` is honoured only by `AnthropicEnrichmentClient` (`_check_rate_limit()`); `LlamaCppEnrichmentClient` never calls it and ignores the field entirely — a local server has no account-level quota to protect.
- **Reachability is a runtime, non-blocking concern.** `model_validation.py` gains an async `GET /v1/models` probe (short timeout) surfaced as `llama_cpp_ok: bool | None` in `GET /status`/`GET /ready` — `None` when `llama_cpp` is not configured anywhere, otherwise the probe result. The probe targets the first configured `llama_cpp_base_url` found across `[hyde]`/`[rag_fusion]`/`[graph]` (in that order) — a single boolean even when sections point at different URLs, an accepted trade-off documented as a known limitation in the team plan (indicative, not per-section).
- **Centralised provider registry (Q10).** A single `_PROVIDER_REGISTRY = ("anthropic", "ollama", "openai", "claude_cli", "llama_cpp")` tuple in `config.py` is now the one place a new provider is registered; `_VALID_PROVIDERS`, the wizard's `_prompt_provider`/`_prompt_graph_provider` choice sets, and the TOML writer's provider branches in `install/` all derive from or stay in sync with it (enforced by `tests/test_provider_registry_sync.py`, BE-10). Before this, the provider list was duplicated across at least four sites with no CI guard against drift.

### (e) Evaluated alternatives

| Alternative | Why rejected |
|---|---|
| Generic `openai_compatible` provider (works for llama-server, LM Studio, vLLM, Jan.ai) | Each backend has enough divergence (model-listing endpoint shape, auth, chat-template quirks) that a single adapter would either be too permissive (silent misconfiguration) or accrue backend-specific branches anyway; deferred to a future iteration if concrete demand appears |
| Embedding llama.cpp in-process (`llama-cpp-python`) | Adds a native-extension dependency and a model-loading/memory-management surface the server doesn't otherwise have; server-mode (`llama-server`) keeps model lifecycle fully external and optional |
| Combined `extraction_model = "provider:model"` string (the pre-existing, never-shipped design in `archon-search.toml.example`) | Fragile prefix parsing (model names can contain `:`); duplicates the `provider` concept already modelled as a separate field elsewhere; replaced with a discrete `[graph] provider` field |
| `claude_cli` enrichment via subprocess | No HTTP endpoint to target with the same `httpx`-based client shape as the other three; would need a different design (prompt templating over `claude -p`, response parsing from stdout) — deferred post-v1, not blocking the other four providers |
| Per-section `llama_cpp_ok` (one probe result per `[hyde]`/`[rag_fusion]`/`[graph]`) | Adds three fields and three async probes to `GET /status` for a corner case (divergent URLs across sections is rare in practice); a single indicative boolean was judged sufficient, with the limitation documented |

### (f) Final decision and rationale

`llama_cpp` ships as a fifth query-expansion provider and one of four wired enrichment providers, both dispatched through centralised, CI-guarded provider registries. No new Python dependency is installed (`httpx` is core). Reachability and model-loaded state are runtime, non-blocking concerns surfaced via `GET /status`, matching the `ollama` precedent rather than the `anthropic`/`openai` optional-dependency precedent. Graph enrichment moves from "protocol and one orphaned client" to "four constructible, injected adapters" without changing the raise-on-failure adapter contract or the silent-fallback behaviour at the `CommunityBuilder`/`GraphExtractor` call sites.

---

## Consequences

- Operators running `llama-server` locally get HyDE, RAG Fusion, and graph enrichment fully on-device, with no API key and no external network call, mirroring the `ollama` zero-transmission story.
- **Practical gotcha (not enforced in code):** reasoning-style local models (large "gpt-oss"-style models that emit hidden chain-of-thought) are a poor fit for HyDE/RAG-Fusion's default `max_tokens` budgets (200/450 respectively) — the model spends the entire budget on hidden reasoning before producing visible content, so `hyde_applied`/`rag_fusion_applied` silently stay `False` even though the server is reachable and correctly configured. Operators should point `llama_cpp` at a small, direct-response instruct model instead.
- `GraphConfig` gains `provider`, `llama_cpp_base_url`, `ollama_base_url`, `extraction_timeout_seconds`, `extraction_rate_limit_rpm`, and `extraction_token_budget` — additive, no `STORE_SCHEMA_VERSION` bump, no migration.
- The `archon-search.toml.example` `[graph]` section's combined `"provider:model"` documentation is corrected to the discrete-field format; the operator guide (`Documentation/OperatorGuide/60_graph_operations.md`) is updated to match.
- `tests/test_provider_registry_sync.py` (BE-10) is a permanent CI guard against the provider list drifting back out of sync across `_VALID_PROVIDERS`, the wizard prompts, and the TOML writer.
- `claude_cli` enrichment remains unimplemented; re-enabling it is a distinct, separately-scoped follow-up (different design, subprocess-based).

---

## References

- `Documentation/ADRs/C4-hyde-external-llm-dependency.md` / `C5-rag-fusion-external-llm-dependency.md` — the precedent this ADR extends.
- `Documentation/Backlog/llama-cpp-local-provider-brief.md` — source brief.
- `Documentation/Backlog/llama-cpp-local-provider-team-plan.md` — full team plan (contracts, scenarios, architecture, acceptance criteria).
- `archon_search/providers/llama_cpp_provider.py` — query-expansion adapter.
- `archon_search/graph_enrichment_protocol.py`, `archon_search/enrichment/` — enrichment protocol and adapters.
- `archon_search/config.py` (`_PROVIDER_REGISTRY`, `GraphConfig`) — centralised registry and new config fields.
- `tests/test_provider_registry_sync.py` — CI guard against provider-list drift.
- `tests/integration/test_llama_cpp_e2e.py` — real e2e tests against a live llama-server (T-1).
