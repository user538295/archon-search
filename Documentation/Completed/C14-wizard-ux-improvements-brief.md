# Feature Brief: Wizard UX Improvements

## Problem
The `archon-search wizard` presents bare single-line prompts with no context, has a broken `--dry-run`, wrong prompt ordering, and leaves users without guidance after a successful install — causing silent mis-configuration and abandoned setups.

## Goal
A user with no prior archon-search knowledge can run `archon-search wizard`, understand every question they're asked, make informed choices, and know exactly what to do after the wizard completes — without consulting the docs.

## Users & Context
- **New users** setting up archon-search for the first time on a developer laptop; they encounter all prompts sequentially and have no prior context
- **Returning users** re-running the wizard after a config change; they need clear re-run semantics and no surprise overwrites
- **CI/automation users** using `--non-interactive` + flags; they rely on `--dry-run` being trustworthy

## Core Flow

1. User runs `archon-search wizard`
2. **Multilingual preamble** (moved first): wizard asks one question — "Do your documents include non-English text?" — before showing the profile table, so the table renders with the correct model set for the chosen language mode. Prompts `[y/N]`.
3. **Profile selection**: wizard shows the full comparison table with RAM/latency/quality tradeoffs and model names for the chosen language mode. User picks a number.
4. **GPU detection + confirmation**: wizard detects Metal/CUDA and asks to confirm, explaining what it means and how to disable later.
5. **License gates**: only shown for profiles that require Jina/fasttext models.
6. **Optional features** (7 prompts): each prompt is preceded by a 2–4 line explanation block showing what the feature does, what the tradeoff is, and what the default means. The "Disable reranker?" prompt is **skipped** when the resolved profile has `reranker=None`.
7. **Expanded summary**: shows profile, language, db path, host:port, API key file location, total estimated download size, and all non-default optional features. User can verify before committing.
8. Service registration + model download + pre-warm.
9. **Success + "Next steps" block**: prints the 4 most likely follow-up commands and the API key file location.

## In Scope

- **Fix `--dry-run`**: gate every filesystem write (`_write_profile_config`, `atomic_write_bytes`, `shutil.copy2`), `_download_fasttext_model`, and `_prewarm_models` on `self.dry_run`. This is a correctness bug, not polish. All three branches in `run()` must be covered:
  - **Branch B (fresh install)**: config write, license write, service register, model download, prewarm — all gated. When the config write is skipped, the subsequent `load_config(config_path)` call would return stale defaults that don't match the selected profile, since `load_config` returns `SearchConfig()` (all defaults) on `FileNotFoundError`. The fix must construct a `SearchConfig` from the profile data in-memory and assign it to `self.cfg`, bypassing `load_config` entirely in dry-run mode.

  **Note**: `_download_fasttext_model` is called branch-independently before the config write branches (Branch B/C), at approximately line 1249 in the current implementation. Its dry-run gate must be applied at that call site (before Branch B/C), not inside Branch B.
  - **Branch C (idempotent re-run)**: config write (overwrite), `.bak` creation, service re-register — all gated.
  - **`_execute_force_reinstall`**: same gates as Branch B plus the explicit service stop step (`stop()`).
- **Add explanation text to all 7 optional-feature prompts**: transplant the descriptions from `Documentation/UserManual/02_wizard.md` (section 3a–3g) directly into the terminal output as a `print()` block unconditionally before the non-interactive early-return check in each prompt function, so explanations are printed regardless of whether the prompt fires interactively or defaults silently. The semantic content is the same as the docs, but Markdown formatting (bold markers, inline code backticks, link syntax) must be stripped or converted to plain text for terminal output.
- **Add 1-line context to the profile selection prompt**: show RAM range, accuracy rating, and "Recommended for most users" annotation on the right default per audience (e.g., `minimal` is the default but should be annotated).
- **Reorder prompts**: multilingual preamble → profile → GPU → licenses → optional features → summary → confirm. Currently GPU is after config is written. Also, optional features prompts currently run BEFORE license prompts; the reorder moves them to AFTER license gates.

  **Design note on multilingual/profile ordering**: The profile table shows different model sets depending on the multilingual flag (e.g., multilingual `minimal` has `reranker=None` while English `minimal` has `Xenova/ms-marco-MiniLM-L-6-v2`). The recommended approach: ask multilingual first as a brief language-preamble step before showing the profile table, so the table renders with the correct model list for the chosen language mode. Profile selection then follows immediately with an accurate table. This avoids a re-render or re-confirmation step.

  Note: moving the GPU question earlier applies to the user-facing confirmation prompt only. The `configure_providers` call (which writes the GPU provider choice to the config file) must remain after the config file has been created — it cannot be moved before the config write step. The GPU provider choice is captured from the detection result and passed to the summary display directly; `configure_providers` applies it to the file in its current position.

- **Skip "Disable reranker?" for profiles where `reranker=None`**: asking to disable a reranker that is not present in the resolved profile is confusing noise. Gate on `profile.reranker is None`, not on profile name alone.
- **Expand the summary screen**: add db path, host:port, API key file path, and a download-size estimate derived from `InstallProfile.download_mb` (the field already exists — no hardcoded table needed; see resolved Open Question below).
- **Add "Next steps" block on success**:
  ```
  archon-search is running on http://127.0.0.1:8765

  Next steps:
    archon-search ingest <path>           # add documents to search
    archon-search status                  # check service health
    archon-search sync                    # sync watched directories
    archon-search stop                    # stop the service

  API key: <first-8>…<last-4>  (full key: <resolved from key manager>)
  Config:  ~/.archon-search/archon-search.toml
  ```
  Note: the API key file path must be resolved from the key manager at runtime (which respects the `ARCHON_SEARCH_KEY_FILE` env override), not hardcoded to `~/.archon-search/.search.env`.
- **Warn before overwriting hand-edited config on re-run**: when Branch C (idempotent re-run) detects existing non-profile-default values in any wizard-written section, prompt: `"Existing config has custom values. Overwrite with profile defaults? [y/N]"`. Always announce the `.bak` location in the summary.

  **Overwrite detection algorithm**: compare existing config keys against the *previous* profile's defaults — the profile currently stored in config, not the newly selected one. Note: for `_write_profile_config` keys (`embedding_model`, `reranker_model`, `chunk_size`), compare against the profile-dependent defaults of the previous profile. For `_apply_wizard_features_to_toml` keys (`eager_load_embedders`, watch, telemetry enabled, routing strategy, log format), compare against `WizardFeatures()` static defaults — these are constant regardless of profile. Since `_apply_wizard_features_to_toml` only writes non-default values, a key's *absence* in the config means the static default is in effect (not a deletion). Compare only the keys that both wizard functions actually write: `_write_profile_config` writes `[database]` keys (`embedding_model`, `reranker_model`, `chunk_size`, `profile`, `multilingual`); `_apply_wizard_features_to_toml` writes optional feature keys across `[database]` (`eager_load_embedders`), `[collections]`, `[telemetry]`, `[routing]`, and `[logging]`. The complete set of keys to compare is the union of what both functions write. Keys in sections the wizard never writes (e.g., `[server]`) are excluded — they are always user-managed. This approach detects user changes without false-positives on keys written by a different profile selection. Fallback: if the previous profile cannot be determined (e.g., no profile key in config), treat the config as hand-edited and always warn.

  **`.bak` timing**: The overwrite warning prompt must fire BEFORE any writes: if the user answers 'N', no `.bak` is created and no config is overwritten. Only on 'y' (or when no custom values are detected) does Branch C proceed to create the `.bak` and write the new config. The summary then announces the `.bak` path because the backup exists at that point.

- **Add `--no-multilingual` flag**: the current `--multilingual` flag (`is_flag=True, default=False`) must be converted to a Click flag-pair (`--multilingual/--no-multilingual`, `default=None`) with three-state semantics: `True` (explicit opt-in, skip prompt), `False` (explicit opt-out, skip prompt), `None` (not set, show interactive prompt). Without this change, `--no-multilingual` would be functionally identical to not passing the flag, since both would resolve to `False`.

  The `_prompt_multilingual` function must change its `flag_value` parameter type from `bool` to `bool | None`. Three-way behavior: `True` (force multilingual, skip prompt); `False` (force English, skip prompt); `None` (unset, show interactive prompt or default to English in non-interactive mode). The early return `if flag_value is not None: return flag_value` must be added as the first check in the function.

## Out of Scope

- **`questionary` / `rich.prompt` / arrow-key navigation**: no new deps; the line-mode flow is sufficient. Defer until user-research evidence says prompts are the bottleneck.
- **Edit/review loop (edit/proceed/abort)**: the expanded summary + "press Enter to proceed" achieves 80% of the value without a full interactive edit loop. Complex to implement correctly across terminals; defer.
- **Renaming `archon-search install` → `archon-search service install`**: separate naming change with its own BREAKING.md entry and deprecation window; don't bundle.
- **Preset system (`--preset docker`, `--preset claude-desktop`)**: a valuable follow-on feature that builds on this work, but requires curating and maintaining preset files — separate scope.
- **`archon-search doctor` command**: useful for Ctrl+C recovery, but a separate command, not part of the wizard prompt flow.
- **Progress bars for model downloads**: fastembed's download output is third-party; wrapping it requires HF-hub callback hooks. Defer to a dedicated polish sprint.
- **Profile renaming** (`lite/standard/pro`): a naming change that needs user-facing migration and doc updates — separate scope.

## Key Decisions

- **Explanation text before each prompt, not after**: consistent with how questionary/Poetry show context — the user reads the explanation, then answers. Showing it after would be useless.
- **Always show explanations (no `--verbose` gate)**: gating behind a flag adds complexity and means most users never see the help. The explanations are short; they don't meaningfully slow down expert users who already know what to pick.
- **Profile question moved after multilingual preamble**: multilingual is asked first as a one-question language preamble, then the profile table is rendered with the correct model list for the chosen language mode. This ensures the profile table is accurate without a re-render step.
- **Fix `--dry-run` as a bug, not a UX improvement**: it goes into this sprint because it blocks trust in the tool. An untrusted dry-run means ops users can't safely test install commands.
- **"Next steps" always shown (no suppression flag)**: CI users redirect stdout; interactive users need the guidance. There is no case where suppressing it is the right default.

## Testing

- **`--dry-run` across all 3 install branches**: assert no files are written to disk (check that config, `.bak`, license files, and service files are absent), and assert the expected dry-run output is printed for each skipped action.
- **Prompt ordering**: capture stdout and assert the multilingual preamble prompt appears before the profile selection table; assert GPU prompt appears after profile selection and before license gates.
- **Skip reranker prompt**: assert the "Disable reranker?" prompt is skipped when `profile.reranker is None`; assert it is shown when `profile.reranker` is set.
- **Overwrite warning**: re-run with a hand-edited config triggers the overwrite prompt; re-run with a wizard-default (unmodified) config does not.
- **`--no-multilingual` flag tri-state**: `--multilingual` skips the prompt and sets multilingual=True; `--no-multilingual` skips the prompt and sets multilingual=False; neither flag shows the interactive prompt.
- **Explanation text**: assert each optional-feature prompt is preceded by its explanation string in stdout; assert no Markdown syntax (`**`, `` ` ``, `[text](url)`) appears in terminal output.
- **Next steps block**: assert all 4 commands (`ingest`, `status`, `sync`, `stop`) appear in stdout after a successful install.
- **`--dry-run` on fresh install**: assert wizard exits with code 0, no config file created, no model downloads triggered (assert `_prewarm_models` and `_download_fasttext_model` are not called — use mocking).
- **`--non-interactive` explanation text**: assert explanation text blocks are present in stdout when `--non-interactive` is used; assert the final config/key output lines appear at their expected positions relative to each other (not positionally absolute, but that key precedes config path line).
- **Expanded summary fields**: assert summary output contains db path, host:port, download size estimate (numeric value in MB), and API key file path.
- **Overwrite detection fallback**: assert that a config with no `profile` key triggers the overwrite warning prompt (fallback: unknown previous profile = always warn).
- **Overwrite detection on profile switch**: assert that re-running with a different profile selected does NOT trigger the overwrite warning (switching profiles is expected, not a hand-edit).
- **`--dry-run` service registration**: assert `_prewarm_models` is not called under `--dry-run`; assert `write_service_file` / `load_service` complete without filesystem side-effects (they already respect `dry_run` internally).
- **`--dry-run` Branch B self.cfg**: after a dry-run on a fresh install (no existing config), assert the summary output reflects the selected profile's model names and settings, not `SearchConfig()` defaults.
- **`--dry-run --force`**: assert the full force-reinstall branch (uninstall + fresh install gates) produces no filesystem writes.
- **`ARCHON_SEARCH_KEY_FILE` override**: set the env var to a custom path, run the wizard, assert the custom path appears in both the expanded summary and the Next Steps block.
- **Overwrite warning negative case**: answer 'N' to the overwrite warning prompt; assert no `.bak` file is created and the original config is unchanged.
- **`--non-interactive` + overwrite warning**: with an existing hand-edited config and `--non-interactive`, assert the wizard proceeds without showing the overwrite prompt, `.bak` is created, and the new profile config is written.
- **`.bak` content integrity**: after answering 'y' to the overwrite warning, assert the `.bak` file content matches the original config file byte-for-byte (not the newly written config).
- **Optional feature prompt count**: in interactive mode with a reranker-enabled profile, assert exactly 7 optional-feature prompts are shown; with a reranker=None profile, assert exactly 6 (reranker prompt skipped).

## Edge Cases & Constraints

- **Profiles where `reranker=None` + "Disable reranker?" prompt**: skip the prompt entirely (treated as `False`/keep default). Showing it would confuse users because the resolved profile has no reranker model configured. Note: English `minimal` has a reranker (`Xenova/ms-marco-MiniLM-L-6-v2`); only the multilingual `minimal` variant has `reranker=None`. Gate on `profile.reranker is None`, not on profile name.
- **`--non-interactive` + explanation text**: explanation blocks are still printed in non-interactive mode (they're informational output, not prompts). This preserves the audit trail of what defaults were applied. **Breaking change note**: automation scripts that parse wizard stdout for specific markers (e.g., the API key line) may be affected by the additional output. Since these are informational `print()` calls added before prompts, they do not change the final config or key output lines — but scripts using line-count assumptions or positional parsing will break. This is an intentional, documented behavior change.
- **`--non-interactive` + overwrite warning**: in non-interactive mode, the overwrite warning prompt auto-accepts (proceeds with the overwrite). CI/automation users pass explicit profile flags; silently blocking them would be worse than overwriting. The `.bak` is still created so the previous config is preserved.
- **`--dry-run` + service registration**: the service registration methods (`write_service_file` / `load_service`), which already propagate `dry_run` internally — the gate needed at `run()` level is for `_prewarm_models` and the steps that don't already propagate `dry_run`. Full chain: config write → license write → fasttext download → service register → model download → prewarm — all gated. See also the `load_config` stale-defaults issue in Branch B described in In Scope.
- **Ctrl+C during wizard (partial state)**: out of scope for this brief, but the re-run warning and `.bak` behavior added here reduce the pain of a mid-wizard abort.
- **Windows terminal + multi-line print before input()**: `print()` + `input()` works correctly on Windows ConHost and modern Terminal. No risk here — we're not adding questionary.
- **`--no-multilingual` flag tri-state**: the converted Click flag-pair (`--multilingual/--no-multilingual`, `default=None`) has three states: explicit True (skip prompt, multilingual=True), explicit False (skip prompt, multilingual=False), None (show interactive prompt). Scripts relying on the old `--multilingual` presence-only behavior are unaffected; scripts that previously relied on absence of `--multilingual` equaling multilingual=False now need `--no-multilingual` for explicit opt-out in non-interactive mode.

## Open Questions

- **Explanation text verbosity**: the 02_wizard.md explanations are 3–6 lines each. Should any be condensed for the terminal (e.g., 2 lines max with a "see docs for details" link), or shown in full? Full text is more helpful; condensed is less cluttered.
- **Re-run overwrite prompt behavior**: should it block and require explicit `y` (safer, prevents accidental overwrites), or default `y` with a 3-second countdown (faster for experienced users)? A blocking `[y/N]` defaulting to `N` is the safest choice but could be friction for scripted re-runs.
- **Profile "Recommended" annotation**: which profile should carry the "Recommended for most users" badge? `minimal` is the default (fastest, least RAM) but users may interpret it as low quality. `balanced` may be the better "recommended" choice for users who don't know their constraints.
- **~~Download size estimate in summary~~** *(resolved)*: use `InstallProfile.download_mb` — the field already exists in the codebase. No hardcoded table needed, no HF hub query required.
- **`--dry-run` output format**: after the fix, should dry-run print `[DRY RUN] Would write: ~/.archon-search/archon-search.toml` for each skipped action, or just proceed silently? Explicit output is better for ops users; a log-style format is most useful.
- **Summary screen "proceed" UX**: after the expanded summary, should the wizard ask `"Proceed? [Y/n]"` explicitly, or proceed automatically after displaying it (current behavior)? An explicit confirm aligns with Poetry/gh and prevents accidental installs.

## Future Iterations

- **Preset system**: `--preset docker`, `--preset claude-desktop`, `--preset ci-test` — loads a bundled TOML preset as defaults, reduces 10 prompts to 1 confirm for common deployments.
- **Sectioned optional features**: collapse the 7 optional prompts behind `"Configure optional features? [y/N]"` — users who say no get all defaults; users who say yes get the 7 prompts. Reduces wizard length for 80% of users.
- **Edit/review loop**: show full config summary and allow `[e]dit / [p]roceed / [a]bort` before any writes. Currently deferred because expanded summary + explicit confirm covers most of the value.
- **`archon-search doctor`**: diagnose and clean up partial-install state from Ctrl+C or failed prewarms.
- **Progress phases**: `[3/5] Downloading models (~2.3 GB, ~3 min on typical connection)` wrapper around fastembed output. Needs HF-hub download callback hooks.

## Recommendation

Build this sprint. The dry-run fix is a correctness bug that must ship regardless. The explanation text is a near-zero-cost change that turns a confusing wizard into an understandable one — the text already exists in `02_wizard.md`, it just needs to be `print()`-ed before the non-interactive early-return check in each prompt function (with Markdown syntax stripped). The prompt reordering and "next steps" block together represent about 1 dev-day and have the highest perception-to-effort ratio of any change in this brief. The hardest part is the re-run overwrite warning — getting the conditional detection right without false positives on profiles that intentionally override defaults. Do the bug fix and explanation text first; those are independent of everything else.
