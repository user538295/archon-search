# Feature Brief: Ollama Model Picker in Install Wizard

## Problem
When a user chooses Ollama as their AI provider during setup, they must type a model name from memory — if they get it wrong, the server silently ignores the AI feature with no visible error.

## Goal
When the user picks Ollama, the wizard fetches the list of models already installed on their machine and shows it as a numbered menu. The user picks by number instead of typing. If the list cannot be fetched, the wizard falls back to free-text entry with a clear explanation.

## Users & Context
Operators and developers setting up archon-search for the first time (or reconfiguring it). They've chosen Ollama because they want local AI without a cloud API key. They may not remember the exact model names installed on their machine — especially if Ollama was set up separately.

## Core Flow

1. User selects Ollama as the provider for HyDE (or RAG Fusion).
2. Wizard asks: "Where is your Ollama server?" — default is `http://localhost:11434`, so most users just press Enter.
3. Wizard contacts that address and fetches the list of installed models.
4. **If models are found:** wizard shows a numbered list. User types a number and continues.
5. **If Ollama is unreachable or has no models:** wizard explains what happened, suggests `ollama pull <model-name>` to install a model, and falls back to free-text entry.
6. The chosen model name is saved to config, same as today.
7. For RAG Fusion: the same flow runs independently (separate base URL prompt and model picker).

## In Scope
- Fetching the model list from the Ollama server address the user provides
- Numbered model picker replacing the free-text prompt when models are available
- Graceful fallback to free-text when the server is unreachable or returns an empty list
- Applying the same flow independently for both HyDE and RAG Fusion
- Updating `Documentation/UserManual/02_wizard.md` to reflect the new flow

## Out of Scope
- Validating the typed model name in free-text fallback — the server's own startup check already catches empty/invalid names (`config.py` raises `ConfigError`)
- Offering a "use same model for both" shortcut when both features pick Ollama — adds code complexity for seconds of savings in a one-time wizard
- Pulling or managing Ollama models from within the wizard — out of scope; `ollama pull` is the operator's tool

## Key Decisions
- **Base URL before model name:** The wizard must know the server address before it can fetch models, so the prompt order flips: base URL first, then model picker. For 95% of users (local Ollama at the default address) this is one Enter keypress.
- **Show picker twice, independently:** HyDE and RAG Fusion each get their own picker. This keeps each feature's configuration self-contained, matches how they're stored in config, and avoids a special-case code path for the "same model" scenario.
- **Free-text fallback is honest:** When the fetch fails or returns nothing, the wizard says why and suggests a fix — it does not silently drop the picker as if nothing happened.

## Edge Cases & Constraints
- **Ollama not running:** `urllib.request` call fails with a connection error → caught, wizard prints a clear message, falls back to free-text.
- **Ollama running, zero models installed:** API returns an empty list → wizard prints a nudge (`run "ollama pull <model-name>" to install one`), falls back to free-text.
- **Custom Ollama address:** User enters a non-default base URL at the first prompt; wizard fetches from that address. If it fails, same fallback as above.
- **No new dependencies:** `urllib.request` and `urllib.error` are already imported in `install.py` (lines 14–15) — the fetch requires zero new packages.
- **Existing tests:** `tests/test_install_wizard_features.py` covers the current model-prompt flow; the prompt-order change and new fallback paths will need test updates.

## Open Questions
- Should the base URL prompt appear even when the user previously entered a custom URL (e.g. in a re-run of the wizard), or should it default to whatever is already in the config file?
- The `_OLLAMA_BASE_URL_DEFAULT` constant lives in `config.py` — should the wizard import it, or duplicate the string?
- How should the numbered picker handle more than ~20 models (long list)? Paginate, truncate, or show all and scroll?

## Future Iterations
- Validate the free-text model name at wizard time by probing the Ollama API directly (e.g. `POST /api/show`) — gives immediate feedback instead of deferring to server startup.
- If both HyDE and RAG Fusion use the same Ollama base URL, offer a "use same model?" shortcut to reduce repetition.

## References
- `archon_search/install.py` `[user+code-agent]` — wizard file; current model prompts at lines 1152 & 1178; `urllib.request`/`urllib.error` already imported at lines 14–15
- `http://localhost:11434/api/tags` `[user]` — Ollama API endpoint that returns installed models
- `Documentation/UserManual/02_wizard.md` `[docs-agent]` — wizard user guide; Step 5h covers provider selection and will need updating
- `Documentation/Completed/g10-llm-provider-matrix-team-plan.md` `[docs-agent]` — G10 plan; BE-8 task documents the current manual model-name prompt design
- `Documentation/Completed/C15-wizard-configurability-expansion-brief.md` `[docs-agent]` — Tier 2 covers HyDE/RAG Fusion toggle; context for how this fits the broader wizard configurability work
- `archon_search/providers/ollama_provider.py` `[code-agent]` — how the model name is consumed at runtime (passed to `ollama.AsyncClient.chat(model=...)`)
- `archon_search/config.py` `[code-agent]` — `HyDEConfig` and `RAGFusionConfig`; validates model must be non-empty for non-Anthropic providers at lines 615–616 and 648–649
- `tests/test_install_wizard_features.py` `[code-agent]` — existing test suite for the wizard; covers provider selection, model-name retry logic, EOF handling, and TOML writing

## Recommendation
This is the right feature to build now — it fixes a real, quiet failure mode (mistyped model names that silently disable an AI feature) with very little code: one HTTP call using an already-imported library, one numbered menu, one fallback message. The hardest part is the prompt-order flip, which changes existing test expectations but not the overall structure. The one thing that must not be compromised is the fallback: if Ollama is unreachable or empty, the wizard must tell the user exactly that and still let them complete setup.
