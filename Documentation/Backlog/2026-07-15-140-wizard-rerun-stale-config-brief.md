# Feature Brief: Wizard Re-run Does Not Disable HyDE / RAG Fusion

## Problem
When a user re-runs the setup wizard and answers "no" to enabling HyDE or RAG Fusion, the wizard makes no change to the config file — leaving whatever was previously written there (including `enabled = true`) completely intact. The user believes they turned the feature off; the server still runs with it on.

## Goal
After the wizard completes, the config file always reflects exactly what the user chose — in both directions. Answering "yes" writes `enabled = true`; answering "no" writes `enabled = false`. The user's explicit choice during a wizard run always wins over whatever was there before.

## Users & Context
Any operator who ran the wizard once (enabling HyDE or RAG Fusion), then later re-runs it to change their setup — for example, to switch provider, disable AI query expansion, or reconfigure from scratch.

## Core Flow
1. User runs `archon-search wizard` (re-run of an existing install).
2. Wizard prompts: "Enable AI query expansion (HyDE + RAG Fusion)? [y/N]"
3. User answers "no."
4. Wizard writes `enabled = false` under `[hyde]` and `[rag_fusion]` in the config file (currently: writes nothing, leaving the old value).
5. Server restarts without AI query expansion active — matching what the user chose.

## In Scope
- Writing `enabled = false` to `[hyde]` and `[rag_fusion]` when the user answers "no" during any wizard run (first-time or re-run)
- Same fix applies symmetrically to both features

## Out of Scope
- Uninstalling the `archon-search[hyde]` / `[rag_fusion]` Python packages when the user disables the feature (package presence does not affect runtime if `enabled = false`)
- Handling the case where the user has manually edited `[hyde]` with provider/model settings they want to keep — the wizard's explicit "no" must overwrite them (see Edge Cases)

## Key Decisions
- **Write `enabled = false`, don't delete the section:** Deleting the entire `[hyde]` block would erase any custom `provider`, `model`, or `ollama_base_url` settings the user may have set. Writing only `enabled = false` disables the feature while preserving their other settings for future re-activation.
- **Wizard's explicit "no" always wins over manual edits:** The wizard is the authoritative configuration surface. If a user manually set `enabled = true` and the wizard "no" path fires, the file must end up with `enabled = false`. This is consistent with how all other wizard-written settings behave.

## Edge Cases & Constraints
- **First-time install, user answers "no":** Currently no `[hyde]` section exists, so writing `enabled = false` would create one. This is fine — it makes the config explicit and prevents ambiguity on future re-runs.
- **User previously set a custom provider/model, then re-runs and says "no":** The section is preserved with `enabled = false`; provider/model keys remain. Re-enabling later (say "yes" on the next wizard run) will pick up the existing provider/model values.
- **Non-interactive / scripted wizard run with `--no-hyde` flag:** Same fix applies — the flag sets `enable_hyde = False`, which must produce `enabled = false` in TOML, not silence.

## Decisions

- **Other write sites:** `doc["hyde"]["enabled"] = True` appears exactly once, in `_apply_wizard_features_to_toml` (`install.py:326`). The wizard runs synchronously — no race possible. Add the else-branch and ship; no separate audit needed.
- **First-install "no":** Always write `enabled = false`, even when no `[hyde]` section previously existed. The value is the same as the code default so it cannot change behavior, and it makes the config self-documenting on every run. Skipping it on first install would make the else-branch behave differently on first vs re-run with no benefit.

## Future Iterations
- Companion fix (bug-001): wizard should also install `archon-search[hyde]` / `[rag_fusion]` extras when the user says "yes," mirroring the `[graph]` extra install pattern.
- `_check_provider_deps` in `app.py` should gain an `anthropic` package check so the server emits a clear startup error when `enabled = true` but the extra is missing (currently silently fails at call time).

## References
- [[archon_search/install.py:298–319]] `[code-agent]` — `_write_profile_config`: `if features.enable_hyde:` with no else branch — the bug site
- [[archon_search/install.py:936–1209]] `[code-agent]` — `_prompt_optional_features`: returns `WizardFeatures` with `enable_hyde` / `enable_rag_fusion` bools
- [[archon_search/install.py:164–165]] `[code-agent]` — `WizardFeatures` dataclass: `enable_hyde: bool = False`, `enable_rag_fusion: bool = False`
- [[archon_search/config.py]] `[code-agent]` — `HyDEConfig`, `RAGFusionConfig` dataclasses with `enabled: bool = False` defaults

## Recommendation
This is a one-branch fix: add `else: doc["hyde"]["enabled"] = False` (and the same for `rag_fusion`) after the existing `if features.enable_hyde:` block in `_write_profile_config`. Small blast radius, zero risk of breaking first-time installs, and it makes the wizard trustworthy as a reconfiguration tool — not just a one-shot setup. Do this in the same task as bug-001 since both touch `_write_profile_config` and `_prompt_optional_features`.
