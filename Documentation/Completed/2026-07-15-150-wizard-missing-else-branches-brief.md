# Feature Brief: Wizard Re-run Silently Ignores "Disable" for 5 Settings

## Problem
When a user re-runs the wizard to turn off a previously-enabled setting (like file watching or telemetry), the wizard does nothing — the old value stays in the config file unchanged. The user believes they disabled it; the server ignores that intent and keeps running with the old value.

## Goal
Re-running the wizard and answering "no" (or leaving a setting at its default) explicitly writes the off/default value to the config file, so the server always reflects the user's most recent wizard choices.

## Users & Context
Any operator who runs the wizard more than once — changing their mind about telemetry, file watching, eager embedder loading, routing strategy, or log format. They expect the wizard to be the authoritative way to configure the server.

## Core Flow
1. User previously ran the wizard and enabled file watching (`watch = true` written to TOML).
2. User re-runs the wizard and answers "no" to file watching.
3. The wizard writes `watch = false` to TOML (currently: writes nothing — `watch = true` remains).
4. Server restarts and respects the new `watch = false`.

## In Scope
Five settings in `_apply_wizard_features_to_toml` (`archon_search/install.py:247–265`) that lack else-branches:
- `eager_load_embedders` → write `eager_load_embedders = false` in the else branch
- `enable_watch` → write `watch = false`
- `enable_telemetry` → write `enabled = false` under `[telemetry]`
- `routing_strategy` → write `routing_strategy = "centroid"` (the default) when not changed
- `log_format` → write `format = "text"` (the default) when not changed

Bundle with **bug-010** — same function, same fix pattern, same PR.

## Out of Scope
- `disable_reranker`, `host`, `port`, `db_path`, `log_level`, `log_to_stderr`, `top_k` — these are either write-once deployment flags or are handled separately (log_to_stderr covered by bug-002). Not re-run candidates.
- HyDE and RAG Fusion else-branches — already covered by bug-010.
- Wizard UI redesign or interactive confirmation of written values.

## Key Decisions
- **Bundle with bug-010**: same function (`_apply_wizard_features_to_toml`), same root cause (one-directional writes), same fix. Splitting them adds overhead with no benefit.
- **Write the default value explicitly**: writing `watch = false` on a "no" answer is unambiguous; omitting the key and relying on code defaults is fragile (defaults can change).
- **`routing_strategy` and `log_format` special case**: these are conditional on being non-default (`!= "centroid"`, `!= "text"`), so the else-branch writes the default value back — not `false` but the actual default string.

## Edge Cases & Constraints
- **User manually edited TOML**: the wizard's explicit write wins. This is the correct behavior — the wizard is the authoritative configuration tool.
- **`routing_strategy` default**: the else-branch must write `"centroid"`, not `false` or empty. Same for `log_format` → `"text"`.
- **Idempotency**: running the wizard twice with the same answers produces the same TOML — no spurious diffs.

## Decisions

- **`telemetry.enabled = false` vs absent section:** Confirmed identical at runtime. `config.py:561–562` shows `if "enabled" in telemetry_cfg: telemetry.enabled = _coerce_bool(...)` — when the section is absent, `TelemetryConfig()` default (`enabled = False`) is used. Write the explicit `enabled = false` in the else-branch anyway: it locks in the behavior against any future change to `TelemetryConfig`'s default and makes the config readable without knowing the code default.

## Future Iterations
- Wizard diff preview: "Here's what will change in your config" before writing — deferred, UI complexity.
- `archon-search config reset <key>` command to remove a single TOML key without re-running the wizard.

## References
- [[archon_search/install.py:247–265]] `[code-agent]` — `_apply_wizard_features_to_toml`, the five if-only blocks
- [[Documentation/Backlog/bug-010-wizard-rerun-stale-config-brief.md]] `[user]` — covers HyDE/RAG Fusion else-branches; this brief extends the same fix to 5 more settings

## Recommendation
This is a two-line fix per setting — add the else-branch, write the default. Bundle it with bug-010 and ship them together; they are the same bug in the same function. The hardest part is verifying the correct default value for `routing_strategy` and `log_format` rather than writing a wrong default. Do not ship without a test that re-runs the wizard with each setting flipped off and asserts the TOML reflects the change.
