# Feature Brief: Wizard Post-Install Summary Missing HyDE, RAG Fusion, and Log Mode

## Problem
After completing the wizard, the summary screen never mentions HyDE, RAG Fusion, or log-to-stderr — even when the user explicitly enabled them. The user has no way to confirm their choices took effect without opening the config file manually.

## Goal
Every feature the user enabled during the wizard appears in the post-install summary. After running the wizard with HyDE or RAG Fusion enabled, the user sees confirmation bullets for those features alongside the existing graph/code/telemetry bullets.

## Users & Context
Any user running `archon-search wizard` or `archon-search install` who enables AI query expansion (HyDE or RAG Fusion) or container/stderr logging mode. They are on the final screen of the install flow and expect to see a complete recap of what was configured.

## Core Flow
1. User completes the wizard, including answering yes to HyDE and/or RAG Fusion.
2. Wizard writes the config and prints the install summary.
3. Summary now includes: `• HyDE query expansion enabled` and/or `• RAG Fusion query expansion enabled` in the feature bullets list.

## In Scope
- Add `enable_hyde` bullet to `feature_bullets` in `_render_summary` (`install.py:689–709`) when `features.enable_hyde` is True.
- Add `enable_rag_fusion` bullet when `features.enable_rag_fusion` is True.
- Bundle with bug-001 (HyDE/RAG Fusion wizard) — same file, two-line addition in the same function.

## Out of Scope
- Changing the wizard question or install logic (covered by bug-001 and bug-010).
- Adding negative bullets ("HyDE disabled") — the summary only lists enabled features, consistent with existing behavior.

## Key Decisions
- **Bundle with bug-001**: Both touch `install.py` and the HyDE/RAG Fusion wizard flow. A single PR avoids two sequential changes to the same function.
- **Positive-only bullets**: Existing summary only lists enabled features (e.g. no "watch disabled" bullet). HyDE/RAG Fusion follow the same pattern — only print when enabled.

## Edge Cases & Constraints
- **Both HyDE and RAG Fusion enabled**: Print two separate bullets, not one combined line — consistent with how code and graph enrichment each get their own bullet.
- **Provider shown in bullet**: Optionally show the provider (`• HyDE query expansion enabled (anthropic)`) so users can confirm the right provider was set. Simple string interpolation from `features.hyde_provider` if that field exists, else omit.

## Decisions

- **HyDE/RAG Fusion provider in bullet:** Already resolved in code. `install.py:733–736` already prints provider-aware bullets (`f"• HyDE: enabled (provider: {features.hyde_provider})"`) and `WizardFeatures` carries `hyde_provider: str` and `rag_fusion_provider: str`. No code change needed here. Before shipping, do a quick pass comparing every field in `WizardFeatures` against the summary block — the brief incorrectly thought HyDE was missing, suggesting it was written from a stale code read.
- **Extras-install confirmation in summary:** Do not add anything about extras until bug-001 (which adds the `archon-search[hyde]` install step) ships. A placeholder is more confusing than helpful. Add the confirmation bullet in the same PR as bug-001.

## Future Iterations
- A "what was changed vs previous install" diff view for re-runs (shows what the wizard changed, not just what is now enabled).

## References
- [[archon_search/install.py:689–709]] `[code-agent]` — `_render_summary` / `feature_bullets` — confirmed missing HyDE/RAG Fusion bullets
- [[archon_search/install.py:160–165]] `[code-agent]` — `WizardFeatures` dataclass with `enable_hyde`, `enable_rag_fusion` fields
- [[Documentation/Backlog/bug-001-hyde-ragfusion-wizard-brief.md]] `[user]` — parent bug covering wizard install of extras and TOML write; this brief should be bundled into that PR

## Recommendation
This is a two- or three-line fix — add the missing bullets to `_render_summary`. The impact is high relative to the effort: without these bullets, every user who enables HyDE or RAG Fusion through the wizard gets zero confirmation that it worked, reinforcing the exact confusion that generated bug-001. Bundle with bug-001. Do not ship the wizard fix from bug-001 without this summary fix alongside it.
