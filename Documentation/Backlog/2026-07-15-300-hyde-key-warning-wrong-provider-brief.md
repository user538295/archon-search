# Feature Brief: Fix Wrong API Key Warning in Status Command

## Problem
When HyDE or RAG Fusion is configured to use OpenAI (or Ollama) but the required key is missing, `archon-search status` tells the user to set `ANTHROPIC_API_KEY` — the wrong environment variable. Users following this advice will set the wrong key and stay broken.

## Goal
`archon-search status` prints the correct environment variable name for whichever AI provider is configured — `ANTHROPIC_API_KEY` for Anthropic, `OPENAI_API_KEY` for OpenAI, and no warning at all for Ollama (which needs no key).

## Users & Context
Any operator who configured HyDE or RAG Fusion with `provider = "openai"` or `provider = "ollama"` in their TOML and runs `archon-search status` to diagnose why query expansion isn't working. The misleading message sends them down a dead-end troubleshooting path.

## Core Flow
1. User runs `archon-search status`.
2. Server returns status including `hyde.provider` and `rag_fusion.provider` alongside `key_available: false`.
3. CLI reads the `provider` field from the response.
4. If `provider = "anthropic"` and `key_available = false` → print "Set ANTHROPIC_API_KEY to enable HyDE."
5. If `provider = "openai"` and `key_available = false` → print "Set OPENAI_API_KEY to enable HyDE."
6. If `provider = "ollama"` → no warning (Ollama is keyless; `key_available` is always `true` for it).

## In Scope
- Fix `_print_expansion_key_warnings` in `archon_search/cli/status.py` (lines 64, 71) to read `provider` from the response dict and map it to the correct env var name.
- Covers both `hyde` and `rag_fusion` warning paths.

## Out of Scope
- Changing what the server returns — `HydeStatusDetail.provider` already exists (added in G10 BE-5).
- Adding new provider types — only the three existing providers (`anthropic`, `openai`, `ollama`) are in scope.

## Key Decisions
- **Read `provider` from response, not from local config:** The status command talks to the server via REST; it should trust the server's reported provider rather than re-reading the TOML locally. This keeps the fix to one file.
- **No warning for Ollama:** Ollama is keyless by design — `key_available` is always `true` for it, so the warning path is never reached. No special-casing needed.
- **Fallback to `"anthropic"` if `provider` key is absent:** Handles older server versions that predate the `provider` field without crashing.

## Edge Cases & Constraints
- **Older server without `provider` field:** `hyde.get("provider", "anthropic")` defaults to Anthropic — same behavior as today, no regression.
- **Unknown provider string:** Map unknown values to a generic "check your provider's API key" message rather than crashing.

## Open Questions
- Confirm `HydeStatusDetail` and `RagFusionStatusDetail` both serialize `provider` into the `/status` JSON response (verify `routes_status.py` / `_build_hyde_status`). If the field is present in the Pydantic model but excluded from the response serialization, the fix needs a server-side change too.

## Future Iterations
- A follow-on improvement: include the exact env var name in the server response itself (in `HydeStatusDetail`), so any client — REST, CLI, MCP — can show the right message without per-client mapping logic.

## References
- [[archon_search/cli/status.py]] `[code-agent]` — `_print_expansion_key_warnings` at lines 64, 71
- [[archon_search/server/schemas.py]] `[code-agent]` — `HydeStatusDetail`, `RagFusionStatusDetail` (both have `provider: str` field from G10 BE-5)
- [[archon_search/server/routes_status.py]] `[code-agent]` — `_build_hyde_status`, `_build_rag_fusion_status`

## Recommendation
This is a two-line fix in `status.py` with zero risk of regression. The server already returns the `provider` field; the CLI just ignores it. Fix it now — users configuring OpenAI or Ollama providers hit a misleading dead end every time they check status, and the correct information is already available in the response.
