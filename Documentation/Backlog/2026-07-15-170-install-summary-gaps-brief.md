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
4. If `log_to_stderr` was enabled, summary also shows `• Logging to stderr only (no log file)`.

## In Scope
- Add `enable_hyde` bullet to `feature_bullets` in `_render_summary` (`install.py:689–709`) when `features.enable_hyde` is True.
- Add `enable_rag_fusion` bullet when `features.enable_rag_fusion` is True.
- Add `log_to_stderr` note when `features.log_to_stderr` is True.
- Bundle with bug-001 (HyDE/RAG Fusion wizard) — same file, three-line addition in the same function.

## Out of Scope
- Changing the wizard question or install logic (covered by bug-001 and bug-010).
- Adding negative bullets ("HyDE disabled") — the summary only lists enabled features, consistent with existing behavior.

## Key Decisions
- **Bundle with bug-001**: Both touch `install.py` and the HyDE/RAG Fusion wizard flow. A single PR avoids two sequential changes to the same function.
- **Positive-only bullets**: Existing summary only lists enabled features (e.g. no "watch disabled" bullet). HyDE/RAG Fusion follow the same pattern — only print when enabled.

## Edge Cases & Constraints
- **Both HyDE and RAG Fusion enabled**: Print two separate bullets, not one combined line — consistent with how code and graph enrichment each get their own bullet.
- **Provider shown in bullet**: Optionally show the provider (`• HyDE query expansion enabled (anthropic)`) so users can confirm the right provider was set. Simple string interpolation from `features.hyde_provider` if that field exists, else omit.
- **log_to_stderr note placement**: Append after the feature bullets block, not inside it — it is a logging mode, not a feature toggle.

## Open Questions
- Does `WizardFeatures` (or equivalent dataclass) carry `hyde_provider` / `rag_fusion_provider` fields, or only `enable_hyde`/`enable_rag_fusion` booleans? If providers are available, include them in the bullet text. If not, omit — don't add provider fields to `WizardFeatures` for this bug alone.
- Should the summary also confirm that the package extras were successfully installed (e.g. `archon-search[hyde]`)? Depends on whether bug-001 adds the install step — coordinate with that PR.

## Future Iterations
- A "what was changed vs previous install" diff view for re-runs (shows what the wizard changed, not just what is now enabled).

## References
- [[archon_search/install.py:689–709]] `[code-agent]` — `_render_summary` / `feature_bullets` — confirmed missing HyDE/RAG Fusion/log_to_stderr bullets
- [[archon_search/install.py:160–165]] `[code-agent]` — `WizardFeatures` dataclass with `log_to_stderr`, `enable_hyde`, `enable_rag_fusion` fields
- [[Documentation/Backlog/bug-001-hyde-ragfusion-wizard-brief.md]] `[user]` — parent bug covering wizard install of extras and TOML write; this brief should be bundled into that PR

## Recommendation
This is a two- or three-line fix — add the missing bullets to `_render_summary`. The impact is high relative to the effort: without these bullets, every user who enables HyDE or RAG Fusion through the wizard gets zero confirmation that it worked, reinforcing the exact confusion that generated bug-001. Bundle with bug-001. Do not ship the wizard fix from bug-001 without this summary fix alongside it.
