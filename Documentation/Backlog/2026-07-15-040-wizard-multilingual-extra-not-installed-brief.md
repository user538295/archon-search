# Feature Brief: Wizard Does Not Install the Multilingual Package

## Problem
When a user chooses a multilingual language profile during the setup wizard, the server crashes on every start — because the wizard writes the choice to the settings file but never installs the software library the server needs to run it (`fasttext-wheel`, provided by the `archon-search[multilingual]` package extra).

## Goal
A user who completes the wizard with a multilingual profile selected gets a running server — no manual steps, no crash, no error log to decode.

## Users & Context
Any new user (or user re-running the wizard to switch profiles) who picks a multilingual profile during setup. They've just waited through a model download and expect the server to be ready. Instead, the server silently fails to start and the only clue is a technical error in a log file they may never find.

## Core Flow
1. User runs the setup wizard and chooses a multilingual language profile (or passes `--multilingual`).
2. The wizard installs the `archon-search[multilingual]` package (which includes `fasttext-wheel`) — the same way it already installs `archon-search[graph]` and `archon-search[code]` for other optional features.
3. If the install fails, the wizard reverts the multilingual setting in the config file so the server can still start (in English-only mode) rather than crashing.
4. The wizard continues to model download, service start, and readiness check as normal.

## In Scope
- Adding an `_install_multilingual_extra()` function (mirrors `_install_code_extra` / `_install_graph_extra` in `install.py`).
- Adding `install_multilingual_extra: bool` to the `WizardFeatures` dataclass and wiring it into the `run()` method wherever a multilingual profile is selected.
- A rollback guard that reverts `multilingual = false` in the config if the install step fails (mirrors `_revert_graph_enabled_flag()`).
- Tests: a new test that the multilingual install path is triggered when a multilingual profile is chosen, and that rollback fires on install failure.

## Out of Scope
- Changing which profiles are multilingual or what "multilingual" means — that is a separate product decision.
- Changing the fasttext model download step (Step 3b) — that is already working correctly.
- Installing spaCy or graph extras — covered separately.

## Key Decisions
- **Rollback on failure**: If the package install fails, revert `multilingual = false` in the config so the server starts in English-only mode rather than crashing. Same pattern as the graph rollback (`_revert_graph_enabled_flag`). This is better than leaving the server dead.
- **Install at the same step as code/graph extras**: The install goes in Step 14 of `run()` alongside `_install_code_extra` and `_install_graph_extra`, so the sequence is consistent and the prewarm (Step 14b) happens after all extras are present.

## Edge Cases & Constraints
- **Re-run wizard, multilingual already installed**: `_install_extra` calls `uv pip install` which is idempotent — reinstalling a package that is already present is a no-op with no user-visible effect.
- **`uv` not on PATH**: `_install_extra` already has a fallback to `pip` — the multilingual path inherits this automatically.
- **Dry-run mode**: `_install_extra` prints `[dry-run] Would install archon-search[multilingual]` and returns early — the new call site inherits this behavior.
- **Non-interactive mode with `--multilingual`**: Must trigger the install the same way interactive mode does; `WizardFeatures.install_multilingual_extra` must be set in both paths.

## Open Questions
- `_revert_graph_enabled_flag` reverts `graph.enabled` because that is an explicit config key. There is no `multilingual.enabled` key — `multilingual = true/false` lives in `[database]`. Plan-maker should confirm whether a `_revert_multilingual_flag()` helper is needed or whether setting `multilingual = false` in `[database]` is the correct revert target.
- The `WizardFeatures` dataclass is also used in `_apply_wizard_features_to_toml`. Since `install_multilingual_extra` controls a subprocess install (not a config key), confirm it should be excluded from `_apply_wizard_features_to_toml` (same as `install_code_extra`).
- Confirm exact placement in `run()`: Step 3b (immediately after fasttext model download, which is already multilingual-gated) vs Step 14 (alongside code/graph install). Step 14 is preferred for consistency but plan-maker should verify no dependency ordering issues.

## Future Iterations
- A unified "install all missing extras" recovery path at server start, rather than a silent crash (would require changes in `app.py`, out of scope here).

## References
- `archon_search/install.py` `[user+code-agent]` — Wizard flow; `WizardFeatures` (lines 143–173), `_install_code_extra` (1304–1310), `_install_graph_extra` (1312–1339), `_revert_graph_enabled_flag` (1341–1363), `run()` Step 14 (1957–1973)
- `pyproject.toml` `[code-agent]` — `[multilingual]` optional dependency (`fasttext-wheel>=0.9.2`, line 26)
- `archon_search/server/app.py` `[code-agent]` — `_check_multilingual_deps` (lines 86–114) raises `RuntimeError` when `fasttext-wheel` is absent
- `tests/test_e2e_wizard_optional_features.py` `[code-agent]` — Existing wizard integration tests; no multilingual extra install test exists
- `tests/test_install_wizard_features.py` `[code-agent]` — `WizardFeatures` tests; no `install_multilingual_extra` field
- `Documentation/Completed/C8-wizard-optional-features-plan.md` `[docs-agent]` — Explicitly named this gap as a future enhancement: "_install_multilingual_extra() analogous to _install_code_extra()"
- `Documentation/UserManual/02_wizard.md` `[docs-agent]` — Wizard user docs; does not warn users that multilingual extra is not installed

## Recommendation
This is the right fix to ship first — it is a silent server crash that affects every user who chooses a multilingual profile. The fix is small, the pattern exists (graph extra install + rollback), and the gap was already named in an internal plan from a prior release. The hardest part is correctly wiring the `WizardFeatures` flag through both interactive and non-interactive paths, and ensuring rollback covers all early-exit points — the same discipline `_revert_graph_enabled_flag` already requires.
