# Feature Brief: Interactive Setup Wizard (the "Cockpit")

## Problem
The first thing a new user does is run setup, and today that is a plain list of typed questions — functional, but forgettable. The moment that should make someone feel the tool is powerful and will "just work" instead feels like filling in a form.

## Goal
On first run, a person at a terminal is dropped into a full-screen, animated setup experience — a "spaceship cockpit" — that makes configuring the tool feel deliberate, confident, and polished, and ends with a working, running install. Success = a first-timer completes setup through the cockpit and comes away feeling the product is high-quality, while every automated and screen-reader install keeps working exactly as before.

## Users & Context
- **First-time users at a real terminal** — the target audience. They are setting the tool up by hand, once, and first impressions matter most here.
- **Returning users** re-running setup on a machine that already has it (changing configuration, or wiping and starting over).
- **Automated installs** — continuous-integration pipelines, containers, scripted provisioning. No human is watching; no cockpit should ever appear.
- **Screen-reader users** — must always have a plain-text path that a screen reader can read aloud.

## Core Flow
1. A user runs the setup command on a terminal.
2. The tool notices a real person is present and launches the cockpit. (If there's no real terminal — a pipe, a container, an automated run — or the user asked to turn it off, it silently uses the plain typed-question flow instead.)
3. The cockpit walks the user through the common setup choices — which profile, whether the corpus has non-English documents, graphics-card acceleration, and the headline optional features — as animated screens.
4. If the user triggers a rarely-used advanced branch (choosing a specific model provider, or accepting a third-party license), the cockpit **closes** and those few steps finish as plain typed prompts in the same run. The cockpit does not reopen — it's a one-way handoff.
5. The tool does the slow work — downloading model files, starting the background service — while showing **live progress** (a filling gauge and moving spinner) so the screen never looks frozen. The user can cancel cleanly at any point.
6. Setup finishes with a working, running install and clear next steps.

**Re-run variant:** if setup detects the tool is already installed, the cockpit opens on a "you already have a setup here" screen offering **Keep / Reconfigure / Delete & start over**. Deleting the existing data requires an explicit typed confirmation so it can't happen by accident.

## In Scope
- A full-screen animated cockpit for the common first-run setup path, built on the existing prototype's look (colours, gauges, spinners).
- Automatic choice of cockpit vs. plain prompts based on whether a real person is at a terminal, plus a `--no-tui` switch to force the plain flow.
- One shared place where every setup answer is collected, so the cockpit, the plain prompts, and automated mode all feed the same install logic (no duplicated setup rules).
- Live progress and clean cancellation during the slow download/service-start steps.
- A cockpit re-run screen (Keep / Reconfigure / Delete) for machines that already have the tool.
- The graphics engine shipped as part of a normal install, so first-timers get the cockpit with no extra step.
- Delivered incrementally — a working cockpit ships one screen deep first, then grows screen by screen, never leaving setup broken between steps.

## Out of Scope
- **Cinematic screens for the rare advanced branches** (provider pickers, license text) — these stay as plain prompts for now; reason: they're touched once by few users and aren't worth the build cost yet.
- **Perfectly seamless return to the cockpit after an advanced prompt** — the handoff is a one-way door; reason: bouncing in and out of a full-screen app risks flicker, the opposite of "flawless."
- **Non-English cockpit text** — English-only, matching the project's existing stance; reason: no translation layer exists and none is planned.
- **Changing what setup actually configures** — this feature changes *how questions are asked*, not the profiles, features, or resulting configuration.

## Key Decisions
- **Auto-detect with an off-switch (#1: Option A + `--no-tui`)**: the cockpit appears automatically for a person at a terminal and steps aside automatically for pipes/containers/automated runs; `--no-tui` forces plain prompts. Chosen over opt-in (nobody would discover it, defeating a first-run goal) and over cockpit-always (would trap screen readers and scripts).
- **Hybrid now, full later (#2: Option A)**: cockpit for the common path, plain prompts for the advanced tail. Chosen over building every screen at once, which is the "tons of work" big-bang the user explicitly wanted to avoid.
- **Shipped by default (#3: Option A)**: the graphics engine is part of a normal install so the *first* run is the cockpit. Chosen over an optional add-on, which would have to be installed *before* first run — a chicken-and-egg problem that defeats a first-run wow.
- **One-way handoff (#4: Option A)**: when the cockpit hands off to a plain prompt it closes for good. Chosen over drop-out-and-return, which risks flicker on odd terminals.
- **Live progress + cancel (#5: Option A)**: the slow steps show a live gauge and can be cancelled. Chosen over a static "working…" screen, which reads as broken and would undercut the entire point of the cockpit.
- **Cockpit handles re-runs (#6: Option A)**: returning users get a Keep / Reconfigure / Delete screen, with a typed confirmation guarding data deletion. Chosen over first-run-only; consequence: the cockpit is no longer strictly first-run — re-run is now in scope.

## Edge Cases & Constraints
- **Terminal too small for the cockpit**: show a "make the window bigger" hint, or fall back to plain prompts below a minimum size, rather than rendering a broken layout.
- **Quitting before the final confirmation**: nothing is written — a safe abort that leaves the machine untouched.
- **Cancelling during the slow download/service-start**: aborts cleanly and releases the setup lock.
- **Deleting an existing install**: always requires an explicit typed confirmation (preserving today's data-loss safety).
- **Accessibility promise changes**: the project currently guarantees a plain-text, no-graphics command line for screen readers. Shipping a cockpit by default rewrites that guarantee — the **plain typed-prompt flow becomes the documented screen-reader-safe path**, and the accessibility document must be updated to say so when this ships.
- **A past decision is reversed**: the project previously chose to keep plain prompts and *not* add richer menus until there was evidence they were a bottleneck. This feature supersedes that decision; the new brief is the record of the reversal (the old completed brief stays as history).
- **English-only** setup text, matching the project's internationalization stance.

## Open Questions
*(Technical — for `/plan-maker` and engineers.)*
- **The seam.** Grow the existing `WizardFeatures` dataclass (`install.py:159`) into a complete `WizardChoices` answer-sheet that also carries `profile_name`, `multilingual`, GPU-confirm, the two license acceptances, `skip_preload`/`force`/`delete_db`, the overwrite-confirm answer, the final proceed answer, and `server_key` — everything currently passed as separate `run()` args or read inline. All three front-ends fill this one struct; `SearchInstaller.run` consumes it.
- **Unwelding order.** Split each `_prompt_*/_select_*/_pick_*` into a pure decision function + the asking. `_prompt_provider` (`install.py:1143`) already injects an `ask_choice` callable and is the closest to the target pattern; `_select_profile` (`install.py:950`) and `_prompt_gpu_confirm`/`_prompt_multilingual` are nearly separable; the Ollama/Claude/free-text pickers and license gates are the most welded.
- **TTY detection.** No `isatty()` check exists anywhere in the install flow today; `input()` raising `EOFError` is the current implicit no-terminal fallback. Decide where the single detection point lives (likely in the `wizard` command, `install_cmd.py:123`) and how `--no-tui` overrides it.
- **Async work under Textual.** `_prewarm_models` (`install.py:487`, already uses a `threading.Timer` timeout), `_check_disk_space`, service start + `_wait_for_service`, and the extra `pip`/`uv` installs are all synchronous. Decide the Textual worker pattern that drives them off the UI thread and streams progress into the gauges, plus clean cancellation semantics.
- **Packaging.** Promote `textual>=0.80` from the dev group (`pyproject.toml:63`) to a runtime dependency; decide whether headless users get an opt-out extra later.
- **Testing the decision layer as a unit.** Today decisions are tested *through* `input()` mocks and the CLI (`test_install_select_profile.py`, `test_wizard_e2e.py`). Add direct unit tests on the pure decision functions, plus Textual-app tests (see `learnings.md` guidance on racy pilot timers — assert reset invariants, not exact ticks).
- **Walking-skeleton scope.** First shippable slice: cockpit collects **one** decision (profile), hands the rest to existing plain prompts, drives a real install end to end. Confirm this as slice 0.
- **Docs to update in the shipping PR** (project rule: docs change in the same session): `UserManual/20_wizard.md` (all three front-ends, TTY detection, `--no-tui`), `Architecture/220_accessibility_and_internationalization.md` (the no-GUI/plain-text guarantees), `UserManual/10_installation.md` (extras/runtime dep), `quick_start.md`, `Backlog/03_world_class_roadmap.md` (add the item), `CHANGELOG.md`, `BREAKING.md`.

## Future Iterations
- Full-cockpit screens for the advanced branches (provider pickers, license acceptance) — the #2 "later" path.
- A headless/minimal install that omits the graphics engine for pure-container deployments.
- Optional "seamless return" to the cockpit after advanced prompts, if user feedback wants it.
- Richer first-run touches: a calibration/benchmark screen that suggests a profile from the detected hardware (the prototype's calibration screen already sketches this).

## References
- **Team plan:** [interactive-tui-setup-wizard-team-plan.md](./interactive-tui-setup-wizard-team-plan.md)
- [examples/textual_core_matrix.py](examples/textual_core_matrix.py) `[user+code-agent]` — prototype "core matrix" profile/corpus screen; holds selection locally, no decision-return path yet
- [examples/textual_calibration.py](examples/textual_calibration.py) `[user+code-agent]` — prototype calibration screen; emits navigation only
- [examples/textual_design.py](examples/textual_design.py) `[user+code-agent]` — shared cockpit design primitives (palette, gauges, spinners) and the two navigation messages
- [archon_search/install.py](archon_search/install.py) `[user+code-agent]` — `SearchInstaller.run` flow, `WizardFeatures`, all prompt helpers, prewarm/license/config-writer logic
- [archon_search/cli/install_cmd.py](archon_search/cli/install_cmd.py) `[user+code-agent]` — `wizard` command and `--non-interactive` flag; likely home of TTY detection + `--no-tui`
- [Documentation/UserManual/20_wizard.md](Documentation/UserManual/20_wizard.md) `[docs-agent]` — canonical wizard reference; primary doc to update
- [Documentation/UserManual/10_installation.md](Documentation/UserManual/10_installation.md) `[docs-agent]` — install paths and optional-extras table (no TUI extra yet)
- [Documentation/UserManual/00_index.md](Documentation/UserManual/00_index.md) `[docs-agent]` — first-run reading order framing the wizard as guided setup
- [Documentation/UserManual/30_configuration.md](Documentation/UserManual/30_configuration.md) `[docs-agent]` — TOML mapping produced by wizard answers
- [Documentation/Architecture/220_accessibility_and_internationalization.md](Documentation/Architecture/220_accessibility_and_internationalization.md) `[docs-agent]` — no-GUI/plain-text a11y guarantees + English-only i18n; conflicts with cockpit, must update
- [Documentation/quick_start.md](Documentation/quick_start.md) `[docs-agent]` — non-interactive install example and profiles
- [Documentation/product_guide.md](Documentation/product_guide.md) `[docs-agent]` — first-run onboarding narrative
- [Documentation/Completed/C14-wizard-ux-improvements-brief.md](Documentation/Completed/C14-wizard-ux-improvements-brief.md) `[docs-agent]` — prior decision to defer richer menus; this feature reverses it
- [Documentation/Completed/C14-wizard-ux-improvements-plan.md](Documentation/Completed/C14-wizard-ux-improvements-plan.md) `[docs-agent]` — out-of-scope list from that prior decision
- [Documentation/Completed/C8-wizard-optional-features-investigation.md](Documentation/Completed/C8-wizard-optional-features-investigation.md) `[docs-agent]` — optional-feature step design
- [Documentation/Completed/C8-wizard-optional-features-plan.md](Documentation/Completed/C8-wizard-optional-features-plan.md) `[docs-agent]` — plan for the optional-feature step
- [Documentation/Completed/C15-wizard-configurability-expansion-brief.md](Documentation/Completed/C15-wizard-configurability-expansion-brief.md) `[docs-agent]` — flag surface the decision layer must expose
- [Documentation/Completed/C0-tiered-install-profiles-brief.md](Documentation/Completed/C0-tiered-install-profiles-brief.md) `[docs-agent]` — profile tier foundation the cockpit selects
- [Documentation/Backlog/03_world_class_roadmap.md](Documentation/Backlog/03_world_class_roadmap.md) `[docs-agent]` — roadmap; no open TUI item yet (add this feature)
- [Documentation/archon-search-notes.md](Documentation/archon-search-notes.md) `[docs-agent]` — feature-idea backlog; profile/config context
- [CHANGELOG.md](CHANGELOG.md) `[docs-agent]` — only record of the existing Textual prototype (palette/gauge design handoff)
- [learnings.md](learnings.md) `[docs-agent]` — Textual test guidance (racy pilot timers; assert reset invariants) and `textual` extra notes
- [pyproject.toml](pyproject.toml) `[code-agent]` — `textual>=0.80` is dev-group only; must become a runtime dependency
- [tests/integration/test_wizard_e2e.py](tests/integration/test_wizard_e2e.py) `[code-agent]` — CliRunner e2e harness mocking the blocking work
- [tests/test_install_select_profile.py](tests/test_install_select_profile.py) `[code-agent]` — canonical `input()`-mock + non-interactive test pattern
- [tests/test_install_prewarm.py](tests/test_install_prewarm.py) `[code-agent]` — how the timeout/prewarm work is tested
- [tests/test_e2e_wizard_optional_features.py](tests/test_e2e_wizard_optional_features.py) `[code-agent]` — optional-feature decision-matrix coverage
- [tests/test_hyde_ragfusion_wizard.py](tests/test_hyde_ragfusion_wizard.py) `[code-agent]` — provider-selection coverage

## Recommendation
This is the right feature to build now, and the strangler approach makes it safe: keep the plain flow as the always-works foundation, add the cockpit as a face on top, ship it one screen at a time. The hardest part is **not the screens** — it's making the slow download/service-start steps run in the background with live progress and clean cancellation (#5); a cockpit that freezes for two minutes is worse than the plain prompts it replaces, so that is the piece that must not be compromised. The one thing to get right before any screen art: **prise the setup decisions apart from the question-asking so all three front-ends share one answer-sheet** — skip that and you maintain three copies of the setup rules forever.
