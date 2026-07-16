# Feature Brief: Missing Anthropic Package Check at Server Startup

> **STATUS: SUPERSEDED — resolved by [[Documentation/Backlog/2026-07-15-060-hyde-ragfusion-wizard-brief.md]].**
> The `anthropic` branch of `_check_provider_deps()` was implemented there
> (`archon_search/server/app.py`), with one deliberate deviation from the spec below:
> the guard is **enabled-gated** (`elif provider == "anthropic" and enabled:`), NOT
> unconditional. `anthropic` is the *default* provider (`config.py`), so an
> unconditional `import anthropic` would require the `[hyde]`/`[rag_fusion]` extra on
> every install and break the optional-extras model. The "fire regardless of `enabled`
> state" decision below (In Scope / Key Decisions / Edge Cases) is therefore overridden.
> Coverage lives in `tests/test_hyde_ragfusion_wizard.py::TestCheckProviderDepsAnthropic`.

## Problem
When HyDE or RAG Fusion is enabled with the default `provider = "anthropic"`, the server starts without checking whether the `anthropic` package is installed. The first time a user actually runs a search that triggers query expansion, the server crashes with a confusing import error deep inside the call stack — not a clean message at startup. Every other provider (`ollama`, `openai`) already gets this check; `anthropic` was missed.

## Goal
The server refuses to start — with a clear, actionable error message — if HyDE or RAG Fusion is enabled with `provider = "anthropic"` and the `anthropic` package is not installed. No silent failures at query time.

## Users & Context
Operators who installed archon-search, ran the wizard to enable HyDE or RAG Fusion, but did not install the `archon-search[hyde]` or `archon-search[rag_fusion]` extras. This is the common case after bug-001 is fixed (wizard will write `enabled = true` but the package install step may still be separate for some workflows).

## Core Flow
1. Operator sets `[hyde] enabled = true` (or `[rag_fusion] enabled = true`) with `provider = "anthropic"` in config.
2. Operator starts the server (`archon-search serve`).
3. Server calls `_check_provider_deps()` during startup.
4. `_check_provider_deps()` detects `provider = "anthropic"` and tries `import anthropic`.
5. If the import fails → server raises `ConfigError` with message: `"[hyde] provider='anthropic' but the 'anthropic' package is not installed; run: pip install archon-search[hyde]"`.
6. Server exits immediately with a clear error. No silent degradation.

## In Scope
- Add `elif provider == "anthropic":` branch to `_check_provider_deps()` in `app.py`, mirroring the existing `ollama` and `openai` branches exactly.
- One branch covers both `hyde` and `rag_fusion` (the loop already iterates both).
- Error message names the correct extras: `archon-search[hyde]` for hyde, `archon-search[rag_fusion]` for rag_fusion.

## Out of Scope
- Installing the package automatically (that is bug-001's wizard responsibility).
- Checking the `anthropic` package version compatibility.
- Checking provider deps when `enabled = false` — the docstring on `_check_provider_deps` explicitly fires regardless of `enabled` state; this brief preserves that behaviour for the new branch too.

## Key Decisions
- **Mirror the existing pattern exactly**: the `ollama` and `openai` branches are the template — same try/except ImportError, same ConfigError message format. No new abstractions.
- **Fire regardless of `enabled` state**: consistent with the existing function contract (a mis-configured provider is always an operator error, whether or not the feature is currently enabled).

## Edge Cases & Constraints
- `provider = "anthropic"` is the default for both `HyDEConfig` and `RAGFusionConfig` — this check will fire for any operator who has `[hyde]` or `[rag_fusion]` in their TOML without the extras installed, even with `enabled = false`. This is intentional and consistent with how `ollama`/`openai` behave.
- The `anthropic` package is part of the `archon-search[hyde]` and `archon-search[rag_fusion]` optional extras — both extras pull in `anthropic>=0.40`. The error message for `[rag_fusion]` should say `archon-search[rag_fusion]`, not `[hyde]`, even though both install the same underlying package.

## Open Questions
- None. The fix location (`app.py:_check_provider_deps`, after line 156), the pattern (match existing ollama/openai branches), and the message format are all determined by the existing code.

## Future Iterations
- Once bug-001 (wizard installs extras automatically) lands, this check becomes a safety net rather than the primary signal — it still belongs.
- A `archon-search doctor` command that checks all provider deps without starting the server would give operators a pre-flight tool. Out of scope here.

## References
- [[archon_search/server/app.py]] `[code]` — `_check_provider_deps()` at line 117; existing `ollama`/`openai` branches at lines 127–156
- [[archon_search/config.py]] `[code]` — `HyDEConfig.provider` default `"anthropic"` at line 35; `RAGFusionConfig.provider` default `"anthropic"` at line 46
- [[Documentation/Backlog/bug-001-hyde-ragfusion-wizard-brief.md]] `[user]` — parent bug; this brief is a companion fix

## Recommendation
Build this now — it is a 10-line change that closes a silent failure mode affecting every operator who follows bug-001's wizard fix. The existing code already has the exact pattern; this is copy-adapt-test, not design work. The hardest part is writing the two test cases (`monkeypatch.setitem(sys.modules, "anthropic", None)` with `hyde.enabled = true` and `rag_fusion.enabled = true`). Do not skip the tests — the existing `ollama`/`openai` branches each have them, and this branch must too.
