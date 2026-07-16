# Feature Brief: Fix HyDE/RAG Fusion Wizard — Install Packages and Confirm

## Problem

A user who answers "yes" to enabling HyDE and RAG Fusion in the setup wizard ends up with both features silently disabled. The config file has no `[hyde]` or `[rag_fusion]` sections at all, so the server defaults to `enabled = false`. Even when sections are written correctly, the AI provider package is never installed — so the feature is configured but broken.

## Goal

After a user enables HyDE and RAG Fusion in the wizard:
1. The TOML config contains `[hyde] enabled = true` and `[rag_fusion] enabled = true`.
2. The required AI provider package is installed (e.g. `archon-search[hyde]` for Anthropic).
3. The install summary confirms which features were enabled — visibly, before the user exits the wizard.
4. Running `archon-search status` shows `hyde: enabled: true` and `rag_fusion: enabled: true`.

## Users & Context

Operators installing archon-search for the first time or re-running the wizard after upgrading. They expect that answering "yes" to a wizard prompt is sufficient to enable and activate a feature — not a half-step that requires manual follow-up.

## Core Flow

1. User runs `archon-search install` (or re-runs the wizard).
2. Wizard asks: "Enable AI query expansion (HyDE + RAG Fusion)? [y/N]"
3. User types `y` and presses Enter.
4. Wizard shows provider/model sub-prompts (already exists).
5. **New:** Wizard installs the required provider package (e.g. `archon-search[hyde]`, `archon-search[rag_fusion]`) — same pattern as `_install_graph_extra()` for the graph feature.
6. **New:** On failure to install, wizard rolls back the TOML sections (same pattern as `_revert_graph_enabled_flag()`).
7. **New:** Install summary prints "• HyDE: enabled (provider: anthropic)" and "• RAG Fusion: enabled (provider: anthropic)" under "Optional features:".
8. Server starts; `GET /status` reports both features enabled.

## In Scope

- Add `_install_hyde_extra()` and `_install_rag_fusion_extra()` using the existing `_install_extra()` helper (`archon_search/install.py:1265`).
- Call them inside `SearchInstaller.run()` when `features.enable_hyde` / `features.enable_rag_fusion` is True, after the TOML is written — mirroring lines 1958–1974 for code/graph.
- Add rollback (`_revert_hyde_enabled_flag()`) on install failure — same pattern as `_revert_graph_enabled_flag()`.
- Add HyDE and RAG Fusion bullets to the install summary (`_build_install_summary()`, currently `archon_search/install.py:655`).
- Investigate the missing-sections bug: add a post-write assertion that reads back `archon-search.toml` and verifies the sections are present when `enable_hyde=True`. This will surface any silent failure in `_apply_wizard_features_to_toml`.

## Out of Scope

- Changing the default from `[y/N]` to `[Y/n]` — keeping opt-in for external API calls is correct.
- Adding separate wizard steps for HyDE vs RAG Fusion — they share a provider and are always enabled together today.
- Making `anthropic` a required (non-optional) dependency — defeats the optional-extras philosophy.

## Key Decisions

- **Install extras, don't just configure:** Writing `enabled = true` without the package is a broken state. The graph feature's pattern (install + rollback on failure) is the right model.
- **Provider-aware install:** For `provider='anthropic'` → install `archon-search[hyde]`; for `provider='openai'` → install `archon-search[openai-provider]`; for `provider='ollama'` → install `archon-search[ollama]`. The install step must branch on `features.hyde_provider` and `features.rag_fusion_provider`.
- **Summary confirmation is mandatory:** The current summary (lines 688–709 of `install.py`) lists code/graph extras but has no entry for HyDE/RAG Fusion — an operator has no visible proof the setting took effect.

## Edge Cases & Constraints

- **Package install fails:** Roll back `[hyde]`/`[rag_fusion]` TOML sections (same as `_revert_graph_enabled_flag`). Print a clear warning with the manual install command.
- **Anthropic provider + `_check_provider_deps`:** The server's startup check (`app.py:117`) already catches `ollama`/`openai` missing packages but does NOT check the `anthropic` package. If `archon-search[hyde]` install is skipped, server starts fine but HyDE fails at runtime. The startup check should be extended to check `anthropic` import when `provider='anthropic'` (or rely solely on the wizard to install it).
- **Re-run over existing config:** When the wizard re-runs on an existing install, `_apply_wizard_features_to_toml` merges into the existing TOML. If a previous run wrote `[hyde] enabled = true` but the user now answers "no", the section must be removed or `enabled` set to `false`. The current code writes nothing when `enable_hyde=False` — it does not clear a pre-existing `[hyde] enabled = true`. This is a latent bug but out of scope here (separate fix).
- **Provider package for RAG Fusion vs HyDE:** Both features currently default to `provider='anthropic'` and both depend on the same `anthropic` package. Installing `archon-search[hyde]` once covers both. The install logic should deduplicate if both are enabled with the same provider.

## Open Questions (Resolved)

- **Missing-sections failure mode — RESOLVED: add the post-write assertion now AND chase the root cause when it fires.** Add an assertion that re-reads the freshly-loaded config and verifies `hyde.enabled`/`rag_fusion.enabled` match the wizard's intent, raising `InstallError` on mismatch. **Placement correction (verified):** the assertion must run in `run()` right after the Step 8b `load_config(config_path)` reload — NOT inside `_write_profile_config` as originally proposed. Fresh installs (the primary bug scenario) write config via `_profile_toml` + `atomic_write_bytes` (`install.py:2055`) and never call `_write_profile_config`; only idempotent/force reinstalls do. All three branches converge at the Step 8b reload, so asserting on the reloaded `cfg` object is the single point that covers every path and proves the full write→parse→load round-trip. `run()` catches the `InstallError` and returns 1 with a clear stderr message (the CLI does `sys.exit(...run(...))`, so an uncaught raise would crash). Guarded by `not self.dry_run`.

- **Extend `_check_provider_deps` for `anthropic` — RESOLVED: yes, but ENABLED-GATED; do NOT add `claude_cli`.** `app.py:117–156` guards only `ollama` and `openai`. Add an `anthropic` branch (`import anthropic`) raising an actionable `ConfigError`. **Correction (verified):** the anthropic guard must fire ONLY when the feature is enabled — unlike the unconditional ollama/openai guards. The default provider is `anthropic` (`config.py:35`, `HyDEConfig.provider = "anthropic"`), so an unconditional `import anthropic` check would require the package on *every* server start, breaking the optional-extras philosophy (explicitly out of scope). Gate it on `config.hyde.enabled` / `config.rag_fusion.enabled`. `claude_cli` must NOT get a guard: it has no pip package and resolves availability via `shutil.which("claude")` with graceful runtime degradation (`claude_cli_provider.py:77`) — a startup hard-fail would contradict that design.

- **Install one extra or two — RESOLVED: install per-feature, deduplicated by resolved package.** Confirmed there is NO combined `ai-expansion` extra; `archon-search[hyde]` and `archon-search[rag_fusion]` are separate names (`pyproject.toml:27–28`) that both resolve to `anthropic>=0.40`. Map each enabled feature to its provider's extra (`anthropic → [hyde]`/`[rag_fusion]`, `openai → [openai-provider]`, `ollama → [ollama]`), collapse to a unique set, and install each once. This is the only approach that stays correct when the two features use different providers (e.g. HyDE on Anthropic + RAG Fusion on Ollama). Do NOT add a combined extra — it aliases one dependency, adds packaging/documentation weight for no capability gain, and still breaks in the multi-provider case.

## Future Iterations

- Separate enable questions for HyDE vs RAG Fusion (currently bundled as one yes/no).
- "Disable HyDE/RAG Fusion" path in the wizard for operators who want to turn them off on re-run.
- `archon-search doctor` command that checks all optional-feature configurations and reports broken states (enabled but package missing).

## References

- [[archon_search/install.py]] `[code-agent]` — wizard logic: `_prompt_optional_features` (line 936), `_apply_wizard_features_to_toml` (line 228), `_install_extra` (line 1265), `_install_graph_extra` / `_install_code_extra` (lines 1304–1319), `_revert_graph_enabled_flag` (line 1973), install summary `_build_install_summary` (line 655), run() hyde/rag_fusion overlay (lines 1757–1760)
- [[archon_search/server/app.py]] `[code-agent]` — `_check_provider_deps` (line 117): does NOT check anthropic package presence for `provider='anthropic'`
- [[archon_search/config.py]] `[code-agent]` — `HyDEConfig.enabled = False` (default), `RAGFusionConfig.enabled = False` (default)

## Recommendation

This is a real, user-facing bug — not a nice-to-have. A feature that shows as enabled in the wizard and disabled everywhere else is a trust-breaking experience. The fix is well-precedented: the graph extra install pattern already does everything needed (install package, roll back on failure, confirm in summary). Port that pattern to HyDE and RAG Fusion. The missing-sections investigation should run in parallel — a post-write assertion is the fastest way to surface whether the bug is in `_apply_wizard_features_to_toml` or in a downstream step.
