# C14 — Wizard UX Improvements
**Purpose**: Fix a broken `--dry-run` (correctness bug), reorder wizard prompts for coherence, add explanation text before each optional-feature prompt, add a "Next steps" block after install, expand the summary screen, and guard against silent config overwrites on re-run.
**Audience**: archon-search contributors implementing C14; operators and new users running `archon-search wizard`.
**Status**: To Do

---

## Background

The wizard has four concrete problems:
1. **Broken `--dry-run`**: Branch B and Branch C of `run()` write config files, `.bak` files, and download models unconditionally — `self.dry_run` is not checked. Ops users cannot trust `--dry-run` to simulate an install without side effects.
2. **Wrong prompt order**: optional-feature prompts fire before license gates; GPU confirmation fires after config is written. New users are asked questions whose meaning depends on profile/language context they haven't been given yet.
3. **No explanation text**: each of the 7 optional-feature prompts is a bare one-line question with no context. Users cannot make informed choices.
4. **No post-install guidance**: the success message is a single line. New users have no idea what to do next.

Secondarily, `--multilingual` is an `is_flag=True, default=False` option — there is no `--no-multilingual` equivalent, making it impossible for non-interactive scripts to explicitly opt out while still showing "non-multilingual was explicitly chosen" in automation logs.

The full brief is in `Documentation/Backlog/C14-wizard-ux-improvements-brief.md`.

---

## Goal

After this plan ships: `--dry-run` makes zero filesystem writes across all three install branches; wizard prompts appear in a coherent order (multilingual preamble → profile → GPU confirm → licenses → optional features → summary → confirm); every optional-feature prompt is preceded by a 2–4 line plain-text explanation; the summary shows db path, host:port, API key path, and estimated download size; a "Next steps" block prints after a successful install; re-running the wizard on a config with hand-edited values warns the user before overwriting.

---

## Scope

### In Scope
- `--dry-run` gating: Branch B and C filesystem writes, `_download_fasttext_model` call, `_prewarm_models` call, and `.bak` creation in `_execute_force_reinstall`
- In-memory `SearchConfig` construction for Branch B dry-run (so `self.cfg` reflects the selected profile, not stale defaults)
- `--multilingual/--no-multilingual` Click flag-pair with `default=None` tri-state; `_prompt_multilingual` signature change to `bool | None`
- `run()` prompt reorder: GPU user prompt moves before license gates; optional-feature prompts move to after license gates; `configure_providers` stays after config write
- Explanation print blocks added to all 7 prompts in `_prompt_optional_features`, printed unconditionally before each prompt's non-interactive early-return check; Markdown stripped
- 1-line RAM/quality/Recommended annotation added to profile selection prompt
- `_render_summary` expanded with db path, host:port, API key file path, download size
- "Next steps" print block added to `run()` after service starts
- `_detect_config_hand_edits(config_path, prev_profile_name, multilingual) -> bool` — new function; Branch C integration with overwrite warning prompt and correct `.bak` sequencing (warning fires before writes)

### Out of Scope
- All items listed in the brief's "Out of Scope" section: `questionary`, preset system, `archon-search doctor`, progress bars, profile renaming, edit/review loop, renaming `install` → `service install`
- Any changes to search, indexing, MCP, REST API, or server code
- The `archon-search install` (register-and-start) command — unchanged

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 6.1 — Final verification & documentation update].

---

## What does NOT change
- All existing wizard CLI flags except `--multilingual` (which gains `--no-multilingual` counterpart and `default=None`); existing `--multilingual` behavior for users who pass it explicitly is unchanged
- `archon-search install` (register-and-start) command
- REST API, MCP tools, server, retrieval pipeline
- `write_service_file()` and `load_service()` — they already propagate `self.dry_run` internally; no changes needed
- `_execute_force_reinstall` dry-run gates for service stop, DB delete, and config write — already correct; only the `.bak` creation at the top of that function is un-gated

---

## Known limitations / accepted trade-offs
- `_prompt_multilingual` currently defaults to English in non-interactive mode regardless of the `--no-multilingual` flag; with the tri-state change, `None` still defaults to English in non-interactive mode — this behavior is preserved
- The in-memory `SearchConfig` construction for Branch B dry-run uses a temporary file (write `_profile_toml()` output to a `tempfile.NamedTemporaryFile`, call `load_config()` on it, delete it). This keeps the fix in `run()` without adding a new entrypoint to `config.py`.
- Explanation text is transplanted from `Documentation/UserManual/02_wizard.md` sections 3a–3g with Markdown stripped (bold markers, backticks, links removed). The text has been reviewed as terminal-appropriate; no semantic editing.
- Overwrite detection compares only wizard-written keys (union of `_write_profile_config` and `_apply_wizard_features_to_toml` output keys) against the previous profile's defaults for `[database]` keys and `WizardFeatures()` static defaults for optional-feature keys. `[server]` and other never-written sections are excluded. False positives can occur if a new release changes a profile's default model — this is documented as an accepted trade-off.
- The 6 Open Questions in the brief are deferred to implementation decisions: explanation verbosity (use full text from docs), `[y/N]` as the overwrite prompt (blocking, default N), `balanced` as the "Recommended" profile annotation, `[DRY RUN]` prefix for dry-run output, explicit `Proceed? [Y/n]` summary confirmation (keep existing behavior), and `InstallProfile.download_mb` for size estimate.

---

## Architecture

### Modified functions (`archon_search/install.py`)

**`_prompt_multilingual(non_interactive: bool, flag_value: bool | None) -> bool`**
- Signature changes from `flag_value: bool` to `flag_value: bool | None`
- New first check: `if flag_value is not None: return flag_value`
- Remaining logic (non-interactive defaults to False; interactive prompt) unchanged

**`_prompt_optional_features(...)`**
- 7 explanation `print()` blocks added, each unconditionally before its prompt's flag/non-interactive check
- Explanation text: plain-text adaptation of `02_wizard.md` sections 3a–3g (Markdown stripped)
- No signature changes; no behavior changes in non-interactive or flag-pre-answered paths

**`_render_summary(profile_name, profile, multilingual, providers, features, *, db_path, host, port, api_key_file, download_mb) -> str`**
- New keyword-only parameters: `db_path: str`, `host: str`, `port: int`, `api_key_file: str`, `download_mb: int`
- Adds lines for these fields in the output block
- Backward-compatible: keyword-only, no defaults — all callers must be updated

**`_detect_config_hand_edits(config_path: Path, prev_profile_name: str, prev_multilingual: bool) -> bool`**
- New function: reads existing config, reconstructs expected defaults for `prev_profile_name`, compares wizard-written keys
- Returns `True` if any wizard-written key differs from expected defaults (hand-edit detected)
- Returns `True` (always warn) if `prev_profile_name` is not a valid profile name (fallback)
- Compares `[database]` keys against `InstallProfile` fields; compares optional-feature keys against `WizardFeatures()` static defaults (absent key = static default in effect)

**`SearchInstaller.run()`**
- `multilingual: bool = False` parameter becomes `multilingual: bool | None = None`
- Prompt order refactored: GPU `detect_gpu()` + `_prompt_gpu_confirm()` moved before license gates; `_prompt_optional_features()` moved after license gates; `configure_providers` and GPU file writes remain after config write
- Branch B: `atomic_write_bytes` and `shutil.copy2` gated on `not self.dry_run`; after gates, when `self.dry_run and branch == "fresh"`, constructs `SearchConfig` in-memory via temp file
- Branch C: `shutil.copy2` (.bak) and `_write_profile_config` gated on `not self.dry_run`; overwrite warning added before writes
- `_download_fasttext_model` call at line ~1249 gated on `not self.dry_run`
- `_prewarm_models` call gated on `not self.dry_run`
- `_render_summary` call updated with new keyword arguments
- "Next steps" block printed after service starts (before completion message)

**`_execute_force_reinstall(...)`**
- `.bak` creation at line 371 gated on `not dry_run`

### Modified CLI (`archon_search/cli/install_cmd.py`)
- `--multilingual` option changes from `is_flag=True, default=False` to a flag-pair `--multilingual/--no-multilingual, default=None`
- `wizard()` handler parameter changes from `multilingual: bool` to `multilingual: bool | None`

### New config keys or env vars
- None — all changes are to wizard UX and dry-run gating; no config schema changes

---

## Task breakdown

### Phase 1 — `--dry-run` correctness bug fix
> **Releasable**: after Task 1.3; `--dry-run` is trustworthy across all install branches.

#### Task 1.1 — Gate Branch B filesystem writes and fix self.cfg stale-defaults
- [x] **File**: `archon_search/install.py`
- **Depends on**: nothing
- **Description**:
  - In `run()`, Branch B (fresh install, lines 1281–1288): wrap `atomic_write_bytes` and `shutil.copy2(.toml.bak)` calls with `if not self.dry_run:`.
  - After the branch block (line 1296), handle the dry-run/fresh case before the unconditional `load_config(config_path)`:
    ```python
    if self.dry_run and branch == "fresh":
        import tempfile, os as _os
        fd, tmp = tempfile.mkstemp(suffix=".toml")
        try:
            _os.write(fd, _profile_toml(profile_name, is_multilingual, features).encode())
            _os.close(fd)
            self.cfg = cfg = load_config(Path(tmp))
        finally:
            _os.unlink(tmp)
    else:
        self.cfg = cfg = load_config(config_path)
    ```
  - `print(f"[DRY RUN] Would write config: {config_path}")` before each gated write so ops users see what would happen.
  - Edge case: the `config_path.parent.mkdir(parents=True, exist_ok=True)` call at line 1286 is safe to keep (non-destructive, idempotent); do not gate it.
- **Releasable**: after this task, Branch B `--dry-run` writes nothing to disk and `self.cfg` reflects the selected profile.
- **Tests (TDD)** — `tests/test_install_run.py` (extend existing) and `tests/test_install_dry_run.py` (new):
  - Unit: `test_dry_run_branch_b_no_config_written` — run with `--dry-run` on fresh install (no existing config file); assert `config_path.exists()` is False after run.
  - Unit: `test_dry_run_branch_b_no_bak_written` — same setup; assert `.toml.bak` does not exist.
  - Unit: `test_dry_run_branch_b_cfg_reflects_profile` — run with `--dry-run`, profile="balanced"; assert `self.cfg.embedding_model` matches `get_profile("balanced", False).embedder`, not `SearchConfig().embedding_model`.
  - Unit: `test_dry_run_branch_b_prints_dry_run_prefix` — capture stdout; assert `"[DRY RUN]"` appears in output.
  - Unit: `test_dry_run_branch_b_exits_zero` — assert return code is 0 on a clean dry-run fresh install.
  - Checkpoint: `uv run pytest tests/test_install_dry_run.py -n0 -x`

#### Task 1.2 — Gate Branch C filesystem writes
- [x] **File**: `archon_search/install.py`
- **Depends on**: Task 1.1
- **Description**:
  - In `run()`, Branch C (idempotent re-run, lines 1291–1293): wrap both `shutil.copy2(config_path, config_path.with_suffix(".toml.bak"))` and `_write_profile_config(...)` calls with `if not self.dry_run:`.
  - `print(f"[DRY RUN] Would write .bak: {config_path.with_suffix('.toml.bak')}")` and `print(f"[DRY RUN] Would overwrite config: {config_path}")` in the else branches.
  - In Branch C dry-run, `load_config(config_path)` still reads the existing (unmodified) file — this is correct behavior; no temp-file workaround needed.
- **Releasable**: after this task, Branch C `--dry-run` makes no filesystem changes.
- **Tests (TDD)** — `tests/test_install_dry_run.py`:
  - Unit: `test_dry_run_branch_c_no_bak_overwrite` — set up an existing config (idempotent case); run with `--dry-run`; assert `.toml.bak` modification time is unchanged.
  - Unit: `test_dry_run_branch_c_config_unchanged` — same setup; assert `config_path.read_text()` is unchanged after run.
  - Unit: `test_dry_run_branch_c_prints_dry_run_prefix` — assert `"[DRY RUN]"` in stdout.
  - Checkpoint: `uv run pytest tests/test_install_dry_run.py -n0 -x`

#### Task 1.3 — Gate `_download_fasttext_model`, `_prewarm_models`, and `_execute_force_reinstall` `.bak`
- [x] **File**: `archon_search/install.py`
- **Depends on**: Task 1.2
- **Description**:
  - In `run()`, at the `_download_fasttext_model` call site (~line 1249, inside the `if is_multilingual and not skip_preload:` block): add `and not self.dry_run` to the outer condition, or wrap the call with `if not self.dry_run:` inside. Print `[DRY RUN] Would download fasttext model.` when gated.
  - In `run()`, at the `_prewarm_models` call site (~line 1355, inside `if not skip_preload:`): add `and not self.dry_run` to the condition. Print `[DRY RUN] Would download models (~{prof.download_mb} MB).` when gated.
  - In `_execute_force_reinstall()`, line 371: gate `shutil.copy2(config_path, bak_path)` with `if not dry_run:`. Print `[dry-run] Would create backup at {bak_path}.` when gated. The subsequent `has_backup = True` assignment must move inside the `else` branch.
- **Releasable**: after this task, `--dry-run` makes no filesystem writes or model downloads across all three install branches. The dry-run bug fix is complete.
- **Tests (TDD)** — `tests/test_install_dry_run.py`:
  - Unit: `test_dry_run_no_fasttext_download` — multilingual profile, dry-run; mock `_download_fasttext_model`; assert mock not called.
  - Unit: `test_dry_run_no_prewarm` — dry-run; mock `_prewarm_models`; assert mock not called.
  - Unit: `test_dry_run_force_no_bak` — force reinstall + dry-run; assert `.toml.bak` not created.
  - Unit: `test_dry_run_force_no_service_stop` — force + dry-run; mock service `stop()`; assert not called (already gated — regression guard).
  - Unit: `test_dry_run_all_three_branches_no_files` — parameterized: fresh/idempotent/force; assert filesystem state unchanged after each dry-run.
  - Checkpoint: `uv run pytest tests/test_install_dry_run.py -n0 -x`

---

### Phase 2 — `--no-multilingual` flag and `_prompt_multilingual` tri-state
> **Releasable**: after Task 2.1; `--no-multilingual` is a usable CLI flag.

#### Task 2.1 — Convert `--multilingual` to flag-pair and update `_prompt_multilingual`
- [x] **File**: `archon_search/cli/install_cmd.py`, `archon_search/install.py`
- **Depends on**: nothing (independent of Phase 1)
- **Description**:
  - In `install_cmd.py` line 24: change `click.option("--multilingual", is_flag=True, default=False, ...)` to `click.option("--multilingual/--no-multilingual", default=None, help="Use multilingual models (--no-multilingual forces English)")`.
  - In `install_cmd.py` `wizard()` handler: change parameter from `multilingual: bool` to `multilingual: bool | None`. Pass through to `installer.run(multilingual=multilingual)`.
  - In `install.py` `SearchInstaller.run()`: change `multilingual: bool = False` to `multilingual: bool | None = None`.
  - In `install.py` `_prompt_multilingual`: change signature to `(non_interactive: bool, flag_value: bool | None) -> bool`. Add as first statement: `if flag_value is not None: return flag_value`. Remove the `if flag_value: return True` check (now handled by the not-None guard). Remaining logic (non-interactive → False; interactive prompt) is unchanged.
  - Backward compatibility: existing callers passing `multilingual=True` still work (not-None path returns True immediately); callers passing `multilingual=False` explicitly now force English without prompting.
- **Releasable**: after this task, `--no-multilingual` skips the multilingual prompt and forces English; `--multilingual` forces multilingual; neither flag shows the interactive prompt.
- **Tests (TDD)** — `tests/test_install_wizard_features.py` (extend) and `tests/test_install_cmd.py` (extend):
  - Unit: `test_prompt_multilingual_flag_true` — `_prompt_multilingual(False, True)` returns True without calling `input()`.
  - Unit: `test_prompt_multilingual_flag_false` — `_prompt_multilingual(False, False)` returns False without calling `input()`.
  - Unit: `test_prompt_multilingual_flag_none_interactive` — `_prompt_multilingual(False, None)` with mocked `input` returning "y" returns True.
  - Unit: `test_prompt_multilingual_flag_none_non_interactive` — `_prompt_multilingual(True, None)` returns False without calling `input()`.
  - Unit: `test_no_multilingual_cli_flag` — invoke `wizard` via `CliRunner` with `["--no-multilingual", "--non-interactive", "--dry-run"]`; assert run receives `multilingual=False`.
  - Unit: `test_multilingual_cli_flag` — same but `["--multilingual", "--non-interactive", "--dry-run"]`; assert `multilingual=True`.
  - Unit: `test_no_multilingual_flag_no_prompt_shown` — `--no-multilingual` + interactive mode: assert multilingual prompt string absent from stdout.
  - Checkpoint: `uv run pytest tests/test_install_wizard_features.py tests/test_install_cmd.py -n0 -x -k multilingual`

---

### Phase 3 — Prompt reordering
> **Releasable**: after Task 3.1; wizard prompts appear in the correct coherent order.

#### Task 3.1 — Reorder prompts in `SearchInstaller.run()`
- [x] **File**: `archon_search/install.py`
- **Depends on**: Task 2.1 (multilingual flag tri-state must be in place before reorder)
- **Description**:
  - Move `gpu = self.detect_gpu()` and `enable_gpu = not disable_gpu and _prompt_gpu_confirm(non_interactive, gpu)` from Step 9 (post-config-write) to immediately after `get_profile()` (after line 1220, before the license gates). Store `gpu` and `enable_gpu` as locals for later use.
  - Move `features = _prompt_optional_features(...)` from its current position (after profile, before licenses, lines 1222–1233) to after the fasttext license gate (after line 1251). Optional features prompts now fire after all license prompts.
  - The GPU config file writes (`configure_providers`, inline `[database].providers` writes) remain in their current position (Step 9, after `load_config`). Only the user-facing prompt moves.
  - New ordering in `run()`:
    1. `_prompt_multilingual` → `_select_profile` → `get_profile`
    2. `detect_gpu()` + `_prompt_gpu_confirm()` (user prompt only — moved here)
    3. Jina license gate
    4. Fasttext license gate + `_download_fasttext_model` (gated by dry_run, Task 1.3)
    5. `_prompt_optional_features` (moved here)
    6. Config write branches (Branch B / C / force — all gated by dry_run, Task 1.1-1.2)
    7. `load_config` / in-memory cfg (Task 1.1)
    8. GPU file writes: `configure_providers` + inline providers write (stays here)
    9. `create_data_dir`, disk space check
    10. `_render_summary`, confirmation, code install, prewarm, service start, Next steps
- **Releasable**: after this task, wizard prompt sequence is: multilingual → profile → GPU confirm → licenses → optional features → summary → service install.
- **Tests (TDD)** — `tests/test_install_run.py` (extend):
  - Unit: `test_prompt_order_gpu_before_license` — capture stdout of a non-interactive run; assert GPU confirmation text appears before Jina license text in output sequence.
  - Unit: `test_prompt_order_optional_features_after_license` — capture stdout; assert optional-feature prompt text appears after license gate text.
  - Unit: `test_gpu_prompt_before_config_write` — mock `_prompt_gpu_confirm` and `_write_profile_config`; assert `_prompt_gpu_confirm` call precedes `_write_profile_config` call in execution order (use `call_args_list` or side-effect ordering).
  - Unit: `test_configure_providers_after_config_write` — mock `_write_profile_config` and `configure_providers`; assert `_write_profile_config` call precedes `configure_providers` call.
  - Unit: `test_reorder_non_interactive_still_succeeds` — full non-interactive run with all flags; assert return code 0.
  - Checkpoint: `uv run pytest tests/test_install_run.py -n0 -x -k "order or reorder or prompt_order"`

---

### Phase 4 — Explanation text for optional-feature prompts
> **Releasable**: after Task 4.1; every optional-feature prompt is preceded by a plain-text explanation block.

#### Task 4.1 — Add explanation print blocks to `_prompt_optional_features`
- [x] **File**: `archon_search/install.py`
- **Depends on**: nothing (independent; can be done in parallel with Phase 2/3)
- **Description**:
  - For each of the 7 prompts in `_prompt_optional_features`, add a `print()` block unconditionally **before** the `if install_code is not None:` / `elif non_interactive:` check. This ensures the explanation prints even in non-interactive mode (audit trail per the brief).
  - Explanation text is adapted from `Documentation/UserManual/02_wizard.md` sections 3a–3g with Markdown stripped (remove `**`, backticks, link syntax `[text](url)`). Preserve line breaks; use 2–4 lines per prompt.
  - Exact text per prompt (Markdown stripped, line-wrapped for 80 columns):
    - **3a. Code enrichment**: `"\nCode enrichment (tree-sitter):\n  Parses and indexes code files structurally — functions, classes, docstrings.\n  Installs tree-sitter and language parsers (~50 MB). Recommended if your corpus\n  includes source code. Default: disabled."`
    - **3b. Reranker toggle**: `"\nReranker:\n  A second-stage cross-encoder model that re-scores results for better precision.\n  Disabling it reduces latency and RAM but lowers recall quality.\n  Default: enabled (for profiles that include a reranker)."` (printed only when `profile.reranker is not None` — i.e., when the prompt will actually fire)
    - **3c. Filesystem watcher**: `"\nFilesystem watcher:\n  Monitors watched directories and automatically re-indexes files when they change.\n  Uses watchdog. Increases background CPU usage slightly.\n  Default: disabled."`
    - **3d. Telemetry**: `"\nLocal telemetry:\n  Logs per-query metadata (collection, result count, latency) to\n  ~/.archon-search/search-logs/. No query text is stored. Opt-in.\n  Default: disabled."`
    - **3e. Eager load**: `"\nEager embedder loading:\n  Pre-loads the embedding model at server startup instead of on the first query.\n  Eliminates first-query latency (~5-15s on first search without this).\n  Default: disabled."`
    - **3f. Routing strategy**: `"\nRouting strategy:\n  centroid: routes queries to collections using centroid similarity (fast, default).\n  hybrid: combines centroid with keyword scoring (slightly slower, more accurate\n  for mixed corpora with distinct topic clusters).\n  Default: centroid."`
    - **3g. Log format**: `"\nLog format:\n  text: human-readable log lines (default).\n  json: structured JSON logs, suitable for log aggregation pipelines.\n  Default: text."`
  - For prompt 3b, wrap the explanation print in `if profile.reranker is not None:` so it only appears when the prompt will fire. This avoids confusing output when the reranker prompt is skipped.
- **Releasable**: after this task, all 7 optional-feature prompts are self-documenting in both interactive and non-interactive mode.
- **Tests (TDD)** — `tests/test_install_wizard_features.py` (extend):
  - Unit: `test_explanation_printed_in_interactive_mode` — mock `input` returning defaults; capture stdout; assert explanation text for at least 3 prompts appears.
  - Unit: `test_explanation_printed_in_non_interactive_mode` — non-interactive run; capture stdout; assert explanation text appears (non-interactive still prints explanations).
  - Unit: `test_no_markdown_in_explanation_output` — capture stdout of full `_prompt_optional_features` call; assert `"**"`, `"``"` (backtick pair), and `"](http"` do not appear in output.
  - Unit: `test_reranker_explanation_skipped_when_no_reranker` — profile with `reranker=None`; capture stdout; assert reranker explanation text does NOT appear.
  - Unit: `test_reranker_explanation_shown_when_reranker_present` — profile with `reranker` set; capture stdout; assert reranker explanation text appears.
  - Unit: `test_prompt_count_7_with_reranker` — interactive mode, profile with reranker; count `input()` calls; assert exactly 7.
  - Unit: `test_prompt_count_6_without_reranker` — profile with `reranker=None`; count `input()` calls; assert exactly 6.
  - Checkpoint: `uv run pytest tests/test_install_wizard_features.py -n0 -x -k "explanation or prompt_count"`

---

### Phase 5 — UX additions
> **Releasable**: after Task 5.4; summary is expanded, Next steps block appears, profile table has annotations. After Task 5.6, overwrite warning is active.

#### Task 5.1 — Add 1-line context to profile selection prompt
- [x] **File**: `archon_search/install.py`
- **Depends on**: nothing
- **Description**:
  - In `_render_profile_table()`, add a "Recommended" annotation to the `balanced` profile row. Per the brief's resolved open question: `balanced` is the "Recommended for most users" profile.
  - For the wide terminal path (width >= 80), append `  ← Recommended` to the `balanced` row line.
  - For the narrow path, adapt as space allows (add `*` suffix and a footnote line `  * Recommended for most users`).
  - No changes to the profile selection prompt text itself — the table already shows RAM and quality implicitly via download size and `quality_stars`. The annotation is sufficient 1-line context.
- **Releasable**: after this task, the profile table guides users toward `balanced` as the default choice for most use cases.
- **Tests (TDD)** — `tests/test_install_select_profile.py` (extend):
  - Unit: `test_profile_table_balanced_recommended_annotation` — call `_render_profile_table(multilingual=False, width=80)`; assert `"Recommended"` appears in the `balanced` row.
  - Unit: `test_profile_table_minimal_no_recommended` — assert `"Recommended"` does NOT appear in the `minimal` row.
  - Unit: `test_profile_table_narrow_has_footnote` — `width=60`; assert `"Recommended"` or `"*"` annotation is present for the balanced row.
  - Checkpoint: `uv run pytest tests/test_install_select_profile.py -n0 -x -k recommended`

#### Task 5.2 — Expand `_render_summary` with new fields
- [x] **File**: `archon_search/install.py`
- **Depends on**: nothing
- **Description**:
  - Add keyword-only parameters to `_render_summary`:
    ```python
    def _render_summary(
        profile_name: str,
        profile: InstallProfile,
        multilingual: bool,
        providers: list[str],
        features: WizardFeatures | None = None,
        *,
        db_path: str = "",
        host: str = "127.0.0.1",
        port: int = 8765,
        api_key_file: str = "",
        download_mb: int = 0,
    ) -> str:
    ```
  - Add lines to the output block (after existing `Providers` line):
    ```
      Database:   <db_path>
      Server:     http://<host>:<port>
      API key:    <first-8>…<last-4>  (full key: <api_key_file>)
      Download:   ~<download_mb> MB
    ```
  - For the API key display: read `api_key_file` path, load the key value (format `ARCHON_SEARCH_API_KEY=<value>`), mask it as `<first-8>…<last-4>`. If the file doesn't exist or the key can't be read, show `(not yet generated)` instead of the masked key.
  - Update the single call site in `run()` (line 1334) to pass `db_path=str(cfg.db_path)`, `host=cfg.host`, `port=cfg.port`, `api_key_file=str(key_manager.KEY_FILE)`, `download_mb=prof.download_mb`.
- **Releasable**: after this task, the summary screen shows db path, server URL, API key hint, and download size.
- **Tests (TDD)** — `tests/test_install_ui.py` (extend):
  - Unit: `test_summary_contains_db_path` — call `_render_summary(..., db_path="/home/user/.archon-search/lancedb")`; assert `"lancedb"` in output.
  - Unit: `test_summary_contains_server_url` — assert `"http://127.0.0.1:8765"` in output.
  - Unit: `test_summary_contains_download_size` — assert `"~300 MB"` (or similar) in output.
  - Unit: `test_summary_api_key_masked` — create a tmp key file with `ARCHON_SEARCH_API_KEY=abcdefghijklmnopqrst`; assert `"abcdefgh…nopqrst"` in output.
  - Unit: `test_summary_api_key_file_missing` — `api_key_file` pointing to non-existent path; assert `"not yet generated"` in output.
  - Checkpoint: `uv run pytest tests/test_install_ui.py -n0 -x -k summary`

#### Task 5.3 — Add "Next steps" block on successful install
- [x] **File**: `archon_search/install.py`
- **Depends on**: Task 5.2 (so `key_manager.KEY_FILE` and cfg are in scope)
- **Description**:
  - In `run()`, between the "Step 16: wait for readiness" block and "Step 17: completion message", add a `_print_next_steps(host: str, port: int, api_key_file: str) -> None` helper function call.
  - Extract as a module-level function:
    ```python
    def _print_next_steps(host: str, port: int, api_key_file: str) -> None:
        """Print post-install guidance to stdout."""
        from archon_search import key_manager
        key_path = Path(api_key_file) if api_key_file else key_manager.KEY_FILE
        key_display = f"(full key: {key_path})"
        print(f"\narchon-search is running on http://{host}:{port}\n")
        print("Next steps:")
        print("  archon-search ingest <path>           # add documents to search")
        print("  archon-search status                  # check service health")
        print("  archon-search sync                    # sync watched directories")
        print("  archon-search stop                    # stop the service")
        print(f"\nAPI key: {key_display}")
        print(f"Config:  {get_default_config_path()}")
    ```
  - Call: `_print_next_steps(cfg.host, cfg.port, str(key_manager.KEY_FILE))` before the completion message.
  - The API key display in Next Steps shows only the file path (not the masked key value) — the summary already shows the masked key; duplication is avoided.
  - `_print_next_steps` is only called when `not self.dry_run` (wrap the call).
- **Releasable**: after this task, every successful non-dry-run install prints the 4 follow-up commands and key/config paths.
- **Tests (TDD)** — `tests/test_install_ui.py` (extend):
  - Unit: `test_next_steps_all_commands_present` — call `_print_next_steps("127.0.0.1", 8765, "/tmp/test.env")`; capture stdout; assert `"ingest"`, `"status"`, `"sync"`, `"stop"` all appear.
  - Unit: `test_next_steps_shows_correct_host_port` — custom host/port; assert `"http://0.0.0.0:9000"` in output.
  - Unit: `test_next_steps_shows_key_file_path` — assert the api_key_file path appears in output.
  - Unit: `test_next_steps_not_printed_in_dry_run` — full dry-run wizard run; capture stdout; assert `"Next steps"` does NOT appear.
  - Unit: `test_next_steps_key_file_from_key_manager` — no `api_key_file` override; assert `key_manager.KEY_FILE` path in output.
  - Checkpoint: `uv run pytest tests/test_install_ui.py -n0 -x -k next_steps`

#### Task 5.4 — `_detect_config_hand_edits` — hand-edit detection function
- [x] **File**: `archon_search/install.py`
- **Depends on**: nothing
- **Description**:
  - New module-level function:
    ```python
    def _detect_config_hand_edits(
        config_path: Path,
        prev_profile_name: str,
        prev_multilingual: bool,
    ) -> bool:
        """Return True if the on-disk config has values that differ from wizard defaults.

        Compares only wizard-written keys (union of _write_profile_config and
        _apply_wizard_features_to_toml output). Returns True (always warn) if
        prev_profile_name is not a recognized profile name.
        """
    ```
  - Implementation:
    1. Attempt `get_profile(prev_profile_name, prev_multilingual)` — if `ValueError`, return `True` (unknown profile → always warn).
    2. Build expected `[database]` values from `InstallProfile` fields: `embedding_model`, `reranker_model` (empty string when `None`), `chunk_size`, `profile`, `multilingual`.
    3. Build expected optional-feature defaults from `WizardFeatures()` (all default values): `eager_load_embedders=False` (absent), `collections.watch=False` (absent), `telemetry.enabled=False` (absent), `routing.routing_strategy="centroid"` (absent), `logging.format="text"` (absent). Note: `_apply_wizard_features_to_toml` only writes non-default values, so **absent keys mean the static default is in effect** — they are NOT hand-edits.
    4. Read `config_path` via `tomlkit.parse()`. For each wizard-written key, compare actual value to expected. Return `True` on first mismatch.
    5. For optional-feature keys: if the key is PRESENT in the config and differs from the `WizardFeatures()` static default, it's a hand-edit. If the key is ABSENT, it is not a hand-edit (static default in effect).
  - Return `False` if all wizard-written keys match expected defaults.
- **Releasable**: after this task, the hand-edit detection function is callable and tested; Branch C integration follows in Task 5.5.
- **Tests (TDD)** — `tests/test_install_config_writer.py` (extend) and new `tests/test_install_overwrite_detection.py`:
  - Unit: `test_detect_no_edits_returns_false` — config written by wizard with `balanced` profile + defaults; assert returns `False`.
  - Unit: `test_detect_changed_chunk_size_returns_true` — `chunk_size` modified from profile default; assert returns `True`.
  - Unit: `test_detect_changed_embedding_model_returns_true` — `embedding_model` changed; assert `True`.
  - Unit: `test_detect_absent_telemetry_not_hand_edit` — `[telemetry].enabled` absent; assert `False` (absence = static default in effect).
  - Unit: `test_detect_present_telemetry_true_is_hand_edit` — `[telemetry].enabled = true` present but WizardFeatures default is False; assert `True`.
  - Unit: `test_detect_unknown_profile_always_warns` — `prev_profile_name="unknown"`; assert `True`.
  - Unit: `test_detect_profile_switch_no_false_positive` — config has `balanced` profile values, detection run against `minimal` profile; assert does NOT return `True` for the profile-dependent key mismatch (switching profiles is not a hand-edit — detection should compare against the PREVIOUS stored profile, which is `balanced` in this case, so values match → returns `False`).
  - Checkpoint: `uv run pytest tests/test_install_overwrite_detection.py -n0 -x`

#### Task 5.5 — Integrate overwrite warning into Branch C
- [ ] **File**: `archon_search/install.py`
- **Depends on**: Task 5.4
- **Description**:
  - In `run()`, Branch C (idempotent re-run), before the `.bak` and config write:
    1. Read `prev_profile_name = existing_cfg.profile` and `prev_multilingual = existing_cfg.multilingual` from the already-loaded `existing_cfg` (read at line 1260).
    2. Call `has_edits = _detect_config_hand_edits(config_path, prev_profile_name, prev_multilingual)`.
    3. If `has_edits`:
       - Non-interactive mode: log a warning `"[warn] Existing config has custom values; overwriting with profile defaults."` and proceed (auto-accept).
       - Interactive mode: `answer = input("Existing config has custom values. Overwrite with profile defaults? [y/N]: ").strip().lower()`. If `answer not in ("y", "yes")`: print `"Installation aborted."` and return 1.
    4. Only AFTER the user accepts (or auto-accepts in non-interactive mode): proceed to the existing `.bak` and `_write_profile_config` calls (which are now gated by `if not self.dry_run:` from Task 1.2).
  - In dry-run mode, the detection still runs and the would-be prompt is described in output (`[DRY RUN] Would prompt: Existing config has custom values...`), but no actual prompt is shown and no writes occur.
  - Always announce `.bak` location in the summary: pass `.bak` path to `_render_summary` or print separately. Use a simple `print(f"  Backup:     {config_path.with_suffix('.toml.bak')}")` in the summary output (add as a new field in `_render_summary` or as a standalone print before the summary).
- **Releasable**: after this task, re-running the wizard on a hand-edited config prompts before overwriting; re-running on a clean wizard-default config proceeds silently.
- **Tests (TDD)** — `tests/test_install_run.py` (extend) and `tests/test_e2e_wizard_optional_features.py` (extend):
  - Unit: `test_overwrite_warning_triggers_on_hand_edit` — Branch C with hand-edited config; interactive mode; mock `input` returning "y"; assert `_write_profile_config` called.
  - Unit: `test_overwrite_warning_aborts_on_n` — Branch C, hand-edit detected; mock `input` returning "n"; assert return code 1, `.bak` not created.
  - Unit: `test_overwrite_warning_bak_not_created_on_n` — same; assert `.toml.bak` does not exist after abort.
  - Unit: `test_overwrite_no_warning_on_clean_config` — Branch C, no hand-edits; assert `input()` not called (no prompt shown).
  - Unit: `test_overwrite_non_interactive_auto_accepts` — Branch C, hand-edit detected, `--non-interactive`; assert `_write_profile_config` called (auto-accepted, no prompt).
  - Unit: `test_overwrite_non_interactive_bak_still_created` — same; assert `.toml.bak` created in non-interactive auto-accept path.
  - Unit: `test_overwrite_dry_run_no_prompt_no_writes` — `--dry-run` + hand-edit detected; assert no prompt shown, no writes made.
  - Unit: `test_bak_content_integrity` — Branch C with overwrite accepted; assert `.toml.bak` contains original config content, not newly written config.
  - Integration: `test_e2e_rerun_with_hand_edited_config` — full e2e: install, hand-edit config, re-run wizard interactively with "y"; assert new profile is active.
  - Checkpoint: `uv run pytest tests/test_install_run.py tests/test_e2e_wizard_optional_features.py -n0 -x -k overwrite`

---

### Phase 6 — Final Verification & Documentation

#### Task 6.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: Tasks 1.1, 1.2, 1.3, 2.1, 3.1, 4.1, 5.1, 5.2, 5.3, 5.4, 5.5
- **Description**:
  - Spawn an agent to discover and update every affected documentation file:
    - `Documentation/UserManual/02_wizard.md` — update prompt order diagram, CLI flags table (add `--no-multilingual`), non-interactive defaults table, summary section, add Next steps section
    - `Documentation/Architecture/600_api_reference_or_public_interface.md` — update wizard CLI surface with new flag
    - `Documentation/Architecture/110_component_catalog_and_layer_breakdown.md` — update if `_detect_config_hand_edits` or `_print_next_steps` warrant mention
    - `ONBOARDING.md` — update if wizard section references prompt order
    - `archon-search.toml.example` — no changes needed (no new config keys)
  - Verify all acceptance criteria below are met before marking complete.
- **Releasable**: after this task, the feature is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - `uv run pytest` passes with coverage >= 85%
  - `uv run pytest tests/test_install_dry_run.py -n0` — all 12+ dry-run tests pass
  - `--dry-run` on a fresh install (no existing config): zero files created in `~/.archon-search/`, exit code 0
  - `--dry-run` on an existing config (Branch C): config unchanged, no `.bak` created, exit code 0
  - `--dry-run --force --delete-db`: no filesystem writes, exit code 0
  - `archon-search wizard --no-multilingual --non-interactive --dry-run`: wizard runs and prints `[DRY RUN]` output; no `"multilingual"` prompt text in stdout
  - `archon-search wizard --multilingual --non-interactive --dry-run`: same; no prompt text; dry run reports multilingual profile
  - Wizard summary output contains `"Database:"`, `"Server:"`, `"API key:"`, `"Download:"` labels
  - Wizard success output (non-dry-run) contains `"Next steps:"` and all four commands: `ingest`, `status`, `sync`, `stop`
  - Optional-feature prompts in interactive mode preceded by explanation text; stdout contains no `"**"` or literal backtick pairs from Markdown
  - Profile table contains `"Recommended"` annotation on the `balanced` row
  - Prompt order in interactive stdout: multilingual question before profile table, GPU prompt before Jina license text, optional-feature prompts after license text
  - Re-running wizard on a hand-edited config: overwrite warning prompt appears; answering "N" leaves config unchanged and creates no `.bak`
  - `Documentation/UserManual/02_wizard.md` reflects the new prompt order and `--no-multilingual` flag
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked.
