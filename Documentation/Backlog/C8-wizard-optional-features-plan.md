# C8 — Wizard Optional Features
**Purpose**: Implement all 8 wizard gaps identified in the C8 investigation (Gap 8 / HyDE excluded until C4 ships). Adds interactive questions for code enrichment, multilingual, reranker toggle, GPU confirmation, filesystem watcher, telemetry, eager loading, routing strategy, and log format.
**Audience**: archon-search contributors implementing C8; operators running `archon-search wizard`.
**Status**: To Do

---

## Background

The C8 investigation (`C8-wizard-optional-features-investigation.md`) found that 9 optional features exist in the codebase but are invisible to users running the interactive wizard. Eight are addressed in this plan; Gap 8 (HyDE query expansion) depends on C4 and is deferred. Users must know to pass undocumented CLI flags or manually edit `archon-search.toml`. This plan surfaces each feature as a first-class interactive question placed between profile selection and the install summary — the natural point where users commit to a configuration before downloading.

---

## Goal

After this plan ships: a user who runs `archon-search wizard` in interactive mode is asked about every optional feature that requires a package install or config toggle. Each question has a sensible default (shown in brackets), can be pre-answered via a new CLI flag, and is suppressed entirely under `--non-interactive`. The install summary reflects all choices. No feature changes are made to search behavior — only to the install flow.

---

## Scope

### In Scope
- `WizardFeatures` dataclass carrying all optional-feature choices
- `_prompt_multilingual()` — converts the `--multilingual` flag to an interactive yes/no question before profile selection
- `_prompt_optional_features()` — seven-question interactive block after profile selection (code, reranker, watcher, telemetry, eager loading, routing strategy, log format)
- `_prompt_gpu_confirm()` — optional GPU enable/disable after auto-detection
- `_apply_wizard_features_to_toml()` — writes `WizardFeatures` fields to the relevant TOML sections
- Extend `_write_profile_config()` and `_profile_toml()` to accept `WizardFeatures`
- `_install_code_extra()` — installs `archon-search[code]` via uv pip (with sys.executable -m pip fallback)
- Extend `_render_summary()` to display enabled optional features
- Eight new CLI flags on `archon-search wizard` (`--multilingual` already exists and gains interactive behavior but is not a new flag)
- Extend `SearchInstaller.run()` signature to accept all new flags
- `archon-search.toml.example` update with comments for each newly-surfaced key

### Out of Scope
- Gap 8 (HyDE query expansion) — depends on C4, which is not shipped
- Any changes to search, indexing, MCP, or REST API behavior
- The `archon-search install` (register-and-start) command — unchanged

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 4.1 — Final verification & documentation update].

---

## What does NOT change
- All existing wizard CLI flags (`--profile`, `--multilingual`, `--skip-preload`, `--force`, `--delete-db`, `--accept-jina-license`, `--accept-fasttext-license`, `--non-interactive`, `--config`) — existing behavior preserved
- `archon-search install` (register-and-start) command — not modified
- REST API, MCP tools, server, or any retrieval pipeline code
- Existing TOML section schemas — only existing optional keys are toggled; no new keys introduced
- The `_select_profile()` function signature and behavior — multilingual prompt is inserted BEFORE calling it in `run()`, not inside it

---

## Known limitations / accepted trade-offs
- The `[code]` extra install uses `uv pip install` (primary) with `sys.executable -m pip` fallback — consistent with existing `install_deps()` pattern; not tested against all pip variants
- Routing strategy (`centroid` / `hybrid`) and log format (`text` / `json`) questions accept free-text input with validation; a typo triggers one retry with a clear message; on the second invalid attempt, the default is used silently
- GPU confirmation (Gap 4) uses a simple `[Y/n]` prompt after auto-detection; if the user declines, `providers = []` (CPU) is written explicitly to config to override any previous Metal/CUDA setting
- The multilingual interactive question is skipped when `--multilingual` is already passed — the flag takes precedence
- Interactive "yes" to the multilingual question selects multilingual models but does NOT install the `[multilingual]` extra package (`fasttext-wheel`). Users who answer "yes" interactively must separately run `pip install archon-search[multilingual]` (or `uv pip install archon-search[multilingual]`) before language detection works. This matches the existing behavior when `--multilingual` CLI flag is passed. A future enhancement could add `_install_multilingual_extra()` analogous to `_install_code_extra()`.
- Wizard re-runs are not fully idempotent for optional features: `_apply_wizard_features_to_toml` only writes non-default values and does not clear previously-set feature keys. If a user ran the wizard with `--telemetry` and re-runs without it, `[telemetry].enabled` remains `true` in the config. To reset to defaults, users must manually edit `archon-search.toml`.

---

## Architecture

### New components

**`WizardFeatures` dataclass** (`archon_search/install.py`):
```python
@dataclass
class WizardFeatures:
    install_code_extra: bool = False
    disable_reranker: bool = False
    enable_watch: bool = False
    enable_telemetry: bool = False
    eager_load_embedders: bool = False
    routing_strategy: str = "centroid"
    log_format: str = "text"
```
Carries all optional feature choices from prompt functions to config writer. No behavior of its own. **Do NOT add `gpu_declined` to this dataclass** — GPU decline is not a feature choice persisted via `WizardFeatures`; it is handled as a direct TOML write in `run()` (see Task 3.1).

**`_prompt_multilingual(non_interactive, flag_value) -> bool`** (`install.py`):
Returns `flag_value` unchanged if True. In non-interactive mode returns False. Otherwise asks "Will your corpus include non-English documents? [y/N]".

**`_prompt_optional_features(non_interactive, profile, *, install_code, disable_reranker, enable_watch, enable_telemetry, eager_load, routing_strategy, log_format) -> WizardFeatures`** (`install.py`):
Asks 7 yes/no (or choice) questions. Each `*`-keyword argument pre-answers its question when not None; None triggers the interactive prompt. Non-interactive returns defaults.

**`_prompt_gpu_confirm(non_interactive, gpu) -> bool`** (`install.py`):
Called after `detect_gpu()`. Only prompts when `gpu` is Metal or CUDA. Non-interactive and `GpuType.NONE` always return True (enable). Returns False if user declines — caller writes `providers = []` to config.

**`_apply_wizard_features_to_toml(doc, features) -> None`** (`install.py`):
Writes non-default `WizardFeatures` fields to an in-memory tomlkit document. Only writes fields that differ from defaults to avoid TOML clutter. Sections created via `tomlkit.table()` if absent.

**`_install_code_extra(dry_run) -> None`** (`install.py`):
Runs `uv pip install --python {sys.executable} archon-search[code]`; on uv failure or absence falls back to `{sys.executable} -m pip install archon-search[code]`. Raises `InstallError` on failure. No-op when `dry_run=True`.

### Modified components

- `_write_profile_config(config_path, profile, profile_name, multilingual, features=None)` — adds optional `features: WizardFeatures | None` parameter; calls `_apply_wizard_features_to_toml()` when not None
- `_profile_toml(profile_name, multilingual, features=None)` — same extension; used for fresh installs
- `_render_summary(profile_name, prof, multilingual, providers, features=None)` — adds a "Optional features" section to the printed summary when features are non-default
- `SearchInstaller.run()` — adds 8 new keyword-only parameters; inserts multilingual prompt before profile selection, optional-features block after, GPU confirm after GPU detection, code install before pre-warm
- `archon_search/cli/install_cmd.py` — 8 new Click options wired to the new `run()` parameters

### Data flow

```
wizard CLI flags
      ↓
install_cmd.py  →  SearchInstaller.run(non_interactive, ..., install_code=None, ...)
                         │
                         ├─ _prompt_multilingual()  →  is_multilingual: bool
                         │
                         ├─ _select_profile(profile, is_multilingual, non_interactive)
                         │
                         ├─ _prompt_optional_features(...)  →  features: WizardFeatures
                         │
                         ├─ detect_gpu()  +  _prompt_gpu_confirm()  →  enable_gpu: bool
                         │
                         ├─ _write_profile_config(..., features)
                         │       └─ _apply_wizard_features_to_toml(doc, features)
                         │
                         ├─ _render_summary(..., features)
                         │
                         ├─ confirmation prompt (existing)
                         │
                         ├─ _install_code_extra()  (if features.install_code_extra)
                         │
                         └─ _prewarm_models()  →  service start
```

### New config keys written (existing keys, not new schema)

All keys are already defined in `SearchConfig` / `archon-search.toml.example`. The wizard now optionally writes them:

| TOML key | Default written | Trigger |
|---|---|---|
| `[database].reranker_model` | `""` | `disable_reranker=True` |
| `[database].eager_load_embedders` | `true` | `eager_load_embedders=True` |
| `[database].providers` | `[]` | `enable_gpu=False` (GPU declined) — written via direct TOML write in `run()`, NOT via `configure_providers()` |
| `[collections].watch` | `true` | `enable_watch=True` |
| `[telemetry].enabled` | `true` | `enable_telemetry=True` |
| `[routing].routing_strategy` | `"hybrid"` | `routing_strategy="hybrid"` |
| `[logging].format` | `"json"` | `log_format="json"` |

---

## Task breakdown

### Phase 1 — Data model and prompt functions
> **Releasable**: after this phase, all new prompt functions are testable in isolation; no observable change to the wizard yet.

#### Task 1.1 — `WizardFeatures` dataclass
- [x] **File**: `archon_search/install.py`
- **Depends on**: nothing
- **Description**:
  - Add `@dataclass class WizardFeatures` with fields: `install_code_extra: bool = False`, `disable_reranker: bool = False`, `enable_watch: bool = False`, `enable_telemetry: bool = False`, `eager_load_embedders: bool = False`, `routing_strategy: str = "centroid"`, `log_format: str = "text"`
  - Place immediately after the existing imports / module-level constants, before `_check_disk_space`
  - No behavior — pure data container
- **Releasable**: after this task, `WizardFeatures` is importable and constructable
- **Tests (TDD)** — `tests/test_install_wizard_features.py`:
  - Unit: `test_defaults` — `WizardFeatures()` has all expected defaults
  - Unit: `test_custom_values` — fields accept non-default values
  - Checkpoint: `uv run pytest tests/test_install_wizard_features.py -v`

#### Task 1.2 — `_prompt_multilingual()`
- [x] **File**: `archon_search/install.py`
- **Depends on**: nothing (no dependency on Task 1.1)
- **Description**:
  - `def _prompt_multilingual(non_interactive: bool, flag_value: bool) -> bool`
  - If `flag_value` is True, return True immediately (flag takes precedence, no prompt)
  - If `non_interactive` is True, return False (default: English)
  - Otherwise print "Will your corpus include non-English documents? [y/N]: " and read one line
  - Accept "y" / "yes" (case-insensitive) as True; anything else (including empty) as False
  - On EOFError: print "No input received (EOF). Using English." and return False
  - Place near `_select_profile()`, after it in file order
- **Releasable**: after this task, `_prompt_multilingual()` is callable and tested
- **Tests (TDD)** — `tests/test_install_wizard_features.py`:
  - Unit: `test_flag_true_skips_prompt` — `flag_value=True` returns True without reading input
  - Unit: `test_non_interactive_returns_false` — `non_interactive=True, flag_value=False` returns False
  - Unit: `test_interactive_yes` — input "y" returns True
  - Unit: `test_interactive_no` — input "" returns False
  - Unit: `test_interactive_eof` — EOFError returns False without raising
  - Unit: `test_interactive_yes_uppercase` — input "YES" returns True
  - Checkpoint: `uv run pytest tests/test_install_wizard_features.py -v`

#### Task 1.3 — `_prompt_optional_features()`
- [x] **File**: `archon_search/install.py`
- **Depends on**: Task 1.1
- **Description**:
  - ```python
    def _prompt_optional_features(
        non_interactive: bool,
        profile: InstallProfile,
        *,
        install_code: bool | None = None,
        disable_reranker: bool | None = None,
        enable_watch: bool | None = None,
        enable_telemetry: bool | None = None,
        eager_load: bool | None = None,
        routing_strategy: str | None = None,
        log_format: str | None = None,
    ) -> WizardFeatures:
    ```
  - When `non_interactive=True`, all `None` values use defaults (False / "centroid" / "text"); non-None values are used as-is
  - Interactive mode: for each `None` parameter, print a question and read input. For yes/no, default is No; for choices, default is shown in brackets
  - Questions in order:
    1. "Index code files (installs tree-sitter enrichment)? [y/N]: " → `install_code_extra`
    2. "Disable reranker for lower latency? [y/N]: " (shown only when `profile.reranker is not None`) → `disable_reranker`
    3. "Auto-watch directories and re-index on file changes? [y/N]: " → `enable_watch`
    4. "Enable local query telemetry? [y/N]: " → `enable_telemetry`
    5. "Pre-load embedding models at startup (eliminates first-query latency)? [y/N]: " → `eager_load_embedders`
    6. "Routing strategy (centroid/hybrid) [centroid]: " → `routing_strategy`; reject any value not in `{"centroid", "hybrid"}` with a clear message and retry once; second invalid attempt uses default
    7. "Log format (text/json) [text]: " → `log_format`; same validate-and-retry pattern for `{"text", "json"}`
  - On EOFError at any question: use that question's default, continue
  - Returns populated `WizardFeatures`
- **Releasable**: after this task, `_prompt_optional_features()` is callable and tested
- **Tests (TDD)** — `tests/test_install_wizard_features.py`:
  - Unit: `test_non_interactive_defaults` — non_interactive mode returns all defaults
  - Unit: `test_flag_overrides_respected` — non-None flag values written directly, no prompt
  - Unit: `test_interactive_all_yes` — all "y" inputs produce all-enabled features
  - Unit: `test_reranker_question_skipped_when_no_reranker` — when `profile.reranker is None`, `disable_reranker` stays False
  - Unit: `test_invalid_routing_strategy_retries` — first "bad" then "hybrid" → `routing_strategy="hybrid"`
  - Unit: `test_invalid_routing_strategy_twice_uses_default` — "bad" twice → `routing_strategy="centroid"`
  - Unit: `test_eof_uses_defaults` — EOFError on any question → defaults used, no raise
  - Unit: `test_invalid_log_format_retries` — first "bad" then "json" → `log_format="json"`
  - Unit: `test_invalid_log_format_twice_uses_default` — "bad" twice → `log_format="text"`
  - Unit: `test_partial_flag_override_interactive_rest` — some flags pre-answered, remaining questions prompted interactively; stdin mock called only for non-overridden questions
  - Unit: `test_eof_midway_preserves_prior_answers` — EOF after 2 questions answered → first 2 preserved, remaining use defaults
  - Checkpoint: `uv run pytest tests/test_install_wizard_features.py -v`

#### Task 1.4 — `_prompt_gpu_confirm()`
- [x] **File**: `archon_search/install.py`
- **Depends on**: nothing
- **Description**:
  - `def _prompt_gpu_confirm(non_interactive: bool, gpu: GpuType) -> bool`
  - Returns True immediately when `gpu == GpuType.NONE` (no GPU, nothing to confirm)
  - Returns True immediately when `non_interactive=True` (auto-enable)
  - For Metal: print "Apple Silicon detected — enable Metal acceleration? [Y/n]: "
  - For CUDA: print "NVIDIA GPU detected — enable CUDA acceleration? [Y/n]: "
  - Accept "n" / "no" (case-insensitive) as False; anything else (including empty) as True
  - On EOFError: return True (auto-enable)
- **Releasable**: after this task, `_prompt_gpu_confirm()` is callable and tested
- **Tests (TDD)** — `tests/test_install_wizard_features.py`:
  - Unit: `test_no_gpu_returns_true` — `GpuType.NONE` always returns True without prompt
  - Unit: `test_non_interactive_metal_returns_true` — `non_interactive=True, gpu=GpuType.METAL` returns True
  - Unit: `test_interactive_metal_accept` — input "" (default) returns True
  - Unit: `test_interactive_metal_decline` — input "n" returns False
  - Unit: `test_interactive_cuda_decline` — input "no" returns False
  - Unit: `test_interactive_cuda_accept` — CUDA GPU detected, input "" (default) → returns True
  - Unit: `test_eof_returns_true` — EOFError returns True
  - Checkpoint: `uv run pytest tests/test_install_wizard_features.py -v`

---

### Phase 2 — Config writing and code install
> **Releasable**: after this phase, all config-writing functions accept `WizardFeatures` and correctly write optional sections; `_install_code_extra()` is callable.

#### Task 2.1 — `_apply_wizard_features_to_toml()`
- [x] **File**: `archon_search/install.py`
- **Depends on**: Task 1.1
- **Description**:
  - `def _apply_wizard_features_to_toml(doc: tomlkit.TOMLDocument, features: WizardFeatures) -> None`
  - For each non-default field in `features`, write to the appropriate TOML section. Create section via `tomlkit.table()` if absent. Only writes non-default values (avoids TOML clutter):
    - `features.disable_reranker` → `doc["database"]["reranker_model"] = ""`
    - `features.eager_load_embedders` → `doc["database"]["eager_load_embedders"] = True`
    - `features.enable_watch` → `doc["collections"]["watch"] = True`
    - `features.enable_telemetry` → `doc["telemetry"]["enabled"] = True`
    - `features.routing_strategy != "centroid"` → `doc["routing"]["routing_strategy"] = features.routing_strategy`
    - `features.log_format != "text"` → `doc["logging"]["format"] = features.log_format`
  - `install_code_extra` is NOT written to TOML (it controls subprocess install, not config)
  - Uses `tomlkit` consistently — no string formatting
  - **Note**: The TOML key is `doc["logging"]["format"]` (not `"log_format"`). The config loader in `config.py` maps the TOML key `format` to the dataclass field `log_format`. Do not write `doc["logging"]["log_format"]`.
- **Releasable**: after this task, `_apply_wizard_features_to_toml()` is callable and tested in isolation
- **Tests (TDD)** — `tests/test_install_config_writer.py` (extend existing file):
  - Unit: `test_apply_defaults_writes_nothing` — `WizardFeatures()` leaves doc unchanged
  - Unit: `test_apply_disable_reranker` — `disable_reranker=True` → `doc["database"]["reranker_model"] == ""`
  - Unit: `test_apply_enable_watch` — `enable_watch=True` → `doc["collections"]["watch"] == True`
  - Unit: `test_apply_enable_telemetry` — `enable_telemetry=True` → `doc["telemetry"]["enabled"] == True`
  - Unit: `test_apply_eager_load` — `eager_load_embedders=True` → `doc["database"]["eager_load_embedders"] == True`
  - Unit: `test_apply_routing_hybrid` — `routing_strategy="hybrid"` → `doc["routing"]["routing_strategy"] == "hybrid"`
  - Unit: `test_apply_log_format_json` — `log_format="json"` → `doc["logging"]["format"] == "json"`
  - Unit: `test_apply_creates_missing_sections` — sections absent before call are created correctly
  - Unit: `test_apply_preserves_existing_sections` — other TOML content untouched
  - Unit: `test_apply_install_code_extra_not_written_to_toml` — `WizardFeatures(install_code_extra=True)` leaves doc unchanged (no `install_code_extra` key anywhere in doc)
  - Checkpoint: `uv run pytest tests/test_install_config_writer.py -v`

#### Task 2.2 — Extend `_write_profile_config()` and `_profile_toml()`
- [x] **File**: `archon_search/install.py`
- **Depends on**: Task 2.1
- **Description**:
  - Add `features: WizardFeatures | None = None` parameter to both `_write_profile_config()` and `_profile_toml()`
  - In `_write_profile_config()`: after writing profile fields, if `features is not None`, call `_apply_wizard_features_to_toml(doc, features)` before the atomic write
  - In `_profile_toml()`: after building the base TOML string, parse with tomlkit, call `_apply_wizard_features_to_toml(doc, features)` if not None, return `tomlkit.dumps(doc)`
  - All existing callers pass `features=None` implicitly — no behavior change for existing call sites
- **Releasable**: after this task, calling either function with `features` correctly writes optional sections; existing tests still pass
- **Tests (TDD)** — `tests/test_install_config_writer.py`:
  - Unit: `test_write_profile_with_features_telemetry` — `_write_profile_config(..., features=WizardFeatures(enable_telemetry=True))` writes `[telemetry].enabled = true`; other profile fields unchanged
  - Unit: `test_write_profile_no_features` — existing test `test_write_profile_config_fresh_file` continues to pass (backward-compatible)
  - Unit: `test_profile_toml_with_features_watch` — `_profile_toml("minimal", False, WizardFeatures(enable_watch=True))` contains `[collections]\nwatch = true`
  - Integration: `test_load_config_after_write_with_features` — write config with features, `load_config()` returns correct values for all toggled fields
  - Checkpoint: `uv run pytest tests/test_install_config_writer.py -v`

#### Task 2.3 — `_install_code_extra()`
- [x] **File**: `archon_search/install.py`
- **Depends on**: nothing (standalone function)
- **Description**:
  - `def _install_code_extra(dry_run: bool = False) -> None`
  - No-op when `dry_run=True` (print what would be run)
  - Primary path: `subprocess.run(["uv", "pip", "install", "--python", sys.executable, "archon-search[code]"], check=True, capture_output=True)`
  - On `FileNotFoundError` (uv not on PATH) or non-zero exit: fall back to `subprocess.run([sys.executable, "-m", "pip", "install", "archon-search[code]"], check=True, capture_output=True)`
  - On fallback failure: raise `InstallError("Failed to install archon-search[code]: {stderr}")`
  - Print "Installing code enrichment packages..." before the subprocess call
  - Print "Code enrichment packages installed." on success
- **Releasable**: after this task, `_install_code_extra()` is callable and tested
- **Tests (TDD)** — `tests/test_install_code_extra.py` (new file):
  - Unit: `test_dry_run_no_subprocess` — dry_run=True calls no subprocess
  - Unit: `test_uv_success` — mocked uv success → no fallback called
  - Unit: `test_uv_not_found_falls_back_to_pip` — `FileNotFoundError` from uv → pip called
  - Unit: `test_uv_failure_falls_back_to_pip` — CalledProcessError from uv → pip called
  - Unit: `test_pip_failure_raises_install_error` — both uv and pip fail → `InstallError` raised
  - Unit: `test_pip_success_after_uv_failure` — uv raises CalledProcessError, pip succeeds → no exception raised, success message printed
  - Checkpoint: `uv run pytest tests/test_install_code_extra.py -v`

#### Task 2.4 — Extend `_render_summary()` to display optional features
- [x] **File**: `archon_search/install.py`
- **Depends on**: Task 1.1
- **Description**:
  - Current signature: `_render_summary(profile_name, prof, multilingual, providers) -> str`
  - New signature: `_render_summary(profile_name, prof, multilingual, providers, features: WizardFeatures | None = None) -> str`
  - When `features is not None` and any non-default field is True (or non-default string): append an "Optional features:" section to the summary
  - Format each enabled feature as a bullet: e.g. "• Code enrichment (tree-sitter)", "• Telemetry enabled", "• Routing: hybrid", "• Log format: json"
  - When all features are defaults, the section is omitted entirely (no visual noise for basic installs)
  - `features=None` behaves identically to existing behavior (fully backward-compatible)
- **Releasable**: after this task, the summary displays enabled features when present
- **Tests (TDD)** — `tests/test_install_ui.py` (extend existing):
  - Unit: `test_render_summary_no_features` — `features=None` output matches previous (no "Optional features" section)
  - Unit: `test_render_summary_all_defaults` — `WizardFeatures()` also produces no optional section
  - Unit: `test_render_summary_with_code_and_telemetry` — `WizardFeatures(install_code_extra=True, enable_telemetry=True)` output contains "Code enrichment" and "Telemetry"
  - Unit: `test_render_summary_routing_hybrid` — `WizardFeatures(routing_strategy="hybrid")` output contains "Routing: hybrid"
  - Unit: `test_render_summary_disable_reranker` — `WizardFeatures(disable_reranker=True)` output contains "Reranker disabled" (or similar)
  - Unit: `test_render_summary_log_format_json` — `WizardFeatures(log_format="json")` output contains "Log format: json"
  - Unit: `test_render_summary_eager_load_and_watch` — `WizardFeatures(eager_load_embedders=True, enable_watch=True)` output mentions both
  - Checkpoint: `uv run pytest tests/test_install_ui.py -v`

---

### Phase 3 — Wiring
> **Releasable**: after Task 3.2, `archon-search wizard` surfaces all optional feature questions to end-users.

#### Task 3.1 — Extend `SearchInstaller.run()` with new parameters and wiring
- [x] **File**: `archon_search/install.py`
- **Depends on**: Tasks 1.1, 1.2, 1.3, 1.4, 2.2, 2.3, 2.4
- **Description**:
  - Add 8 new keyword-only parameters to `SearchInstaller.run()`:
    ```python
    install_code: bool | None = None,
    disable_reranker: bool | None = None,
    enable_watch: bool | None = None,
    enable_telemetry: bool | None = None,
    eager_load: bool | None = None,
    routing_strategy: str | None = None,
    log_format: str | None = None,
    disable_gpu: bool = False,
    ```
  - **Before Step 1** (profile selection): insert `is_multilingual = _prompt_multilingual(non_interactive, multilingual)`. Replace the existing `multilingual_flag` local in `_select_profile()` call with `is_multilingual`.
  - **After Step 2** (get profile data): insert `features = _prompt_optional_features(non_interactive, prof, install_code=install_code, disable_reranker=disable_reranker, enable_watch=enable_watch, enable_telemetry=enable_telemetry, eager_load=eager_load, routing_strategy=routing_strategy, log_format=log_format)`
  - **Step 9** (GPU detection): after `gpu = self.detect_gpu()`, add `enable_gpu = not disable_gpu and _prompt_gpu_confirm(non_interactive, gpu)`. When `enable_gpu is False`, do NOT rely on `configure_providers()` — instead, directly write `doc["database"]["providers"] = tomlkit.array()` to the config document after the profile config write. The `configure_providers()` call should only be made when `enable_gpu is True`.
  - **Config write branches**:
    - **Branch A (force reinstall)**: `_execute_force_reinstall()` internally calls `_write_profile_config()`. Add `features: WizardFeatures | None = None` parameter to `_execute_force_reinstall()` and forward it to its internal `_write_profile_config()` call. The call site in `run()` Branch A must pass `features=features`.
    - **Branch B (fresh install)**: `_profile_toml()` call (Task 2.2 already adds `features` parameter) — update the call to pass `features=features`. This branch does NOT call `_write_profile_config()`.
    - **Branch C (update/merge)**: pass `features=features` to `_write_profile_config()`.
  - **Step 12** (summary): pass `features=features` to `_render_summary()`
  - **Before Step 14** (pre-warm): if `features.install_code_extra`, call `_install_code_extra(dry_run=self.dry_run)` (runs regardless of `skip_preload`); on `InstallError`, print warning and continue (code install failure is non-fatal)
  - All existing parameters and their behavior are unchanged
- **Releasable**: after this task, the full interactive wizard flow exercises all new prompts end-to-end
- **Tests (TDD)** — `tests/test_install_run.py` (extend existing):
  - Integration: `test_run_prompts_multilingual_question` — wizard in interactive mode without `--multilingual` receives multilingual prompt
  - Integration: `test_run_multilingual_flag_skips_prompt` — `multilingual=True` skips `_prompt_multilingual` call
  - Integration: `test_run_optional_features_prompted` — wizard calls `_prompt_optional_features` after profile selection
  - Integration: `test_run_code_extra_installed_when_requested` — `install_code=True` triggers `_install_code_extra()`
  - Integration: `test_run_code_install_failure_is_non_fatal` — `_install_code_extra` raises `InstallError` → run() continues, returns 0
  - Integration: `test_run_gpu_confirm_decline_writes_cpu` — `disable_gpu=True` writes `providers = []` to config
  - Integration: `test_run_non_interactive_uses_defaults` — `non_interactive=True` skips all prompts, uses defaults
  - Integration: `test_run_disable_reranker_writes_empty_string` — `disable_reranker=True` → `load_config()` shows `reranker_model == ""`
  - Integration: `test_run_watch_written_to_config` — `enable_watch=True` → `load_config()` shows `watch == True`
  - Integration: `test_run_force_reinstall_preserves_features` — `force=True, delete_db=True, enable_watch=True` → config has `[collections].watch = true`
  - Integration: `test_run_interactive_gpu_decline_writes_cpu` — interactive mode, `detect_gpu()` returns METAL, user answers "n" → config has `database.providers = []`
  - Checkpoint: `uv run pytest tests/test_install_run.py -v`

#### Task 3.2 — New CLI flags in `install_cmd.py`
- [ ] **File**: `archon_search/cli/install_cmd.py`
- **Depends on**: Task 3.1
- **Description**:
  - Add 8 new options to the `wizard` command. These flags are wizard-only and must NOT be added to `_install_options` if that decorator is shared with `install`. The `install` command is register-and-start only; add these flags directly to the `wizard` command group:
    - `--code / --no-code` (`is_flag=True`, default=None, help="Install tree-sitter code enrichment packages")
    - `--watch / --no-watch` (`is_flag=True`, default=None, help="Enable filesystem watcher for auto-reindex")
    - `--telemetry / --no-telemetry` (`is_flag=True`, default=None, help="Enable local query telemetry")
    - `--eager-load / --no-eager-load` (`is_flag=True`, default=None, help="Pre-load embedding models at startup")
    - `--no-reranker` (`is_flag=True`, default=False, help="Disable reranker (lower latency, less precision)")
    - `--routing-strategy` (`type=click.Choice(["centroid", "hybrid"])`, default=None, help="Routing strategy")
    - `--log-format` (`type=click.Choice(["text", "json"])`, default=None, help="Log format")
    - `--disable-gpu` (`is_flag=True`, default=False, help="Force CPU execution; skip GPU acceleration")
  - Pass all new flags through to `installer.run()` with matching parameter names
  - Flags with `default=None` (three-state bool) must use `Click.option` with `is_flag` pairs; flags that are always False/True use simple `is_flag=True`
  - Note: `--no-reranker` is a one-sided flag (no `--reranker` pair) since the profile default already includes a reranker
- **Releasable**: after this task, `archon-search wizard --help` shows all 8 new flags; flags work in non-interactive mode
- **Tests (TDD)** — `tests/test_install_cmd.py` (extend existing):
  - Unit: `test_wizard_help_contains_new_flags` — all 8 new flags (`--code`, `--no-code`, `--watch`, `--no-watch`, `--telemetry`, `--no-telemetry`, `--eager-load`, `--no-eager-load`, `--no-reranker`, `--routing-strategy`, `--log-format`, `--disable-gpu`) appear in `--help` output
  - Unit: `test_wizard_non_interactive_with_code_flag` — `wizard --non-interactive --code --profile minimal` calls `run(install_code=True, non_interactive=True, profile="minimal")`
  - Unit: `test_wizard_routing_strategy_hybrid` — `wizard --non-interactive --routing-strategy hybrid --profile minimal` calls `run(routing_strategy="hybrid", ...)`
  - Unit: `test_install_command_does_not_have_new_flags` — `archon-search install --help` does NOT show `--code`, `--watch`, etc.
  - Checkpoint: `uv run pytest tests/test_install_cmd.py -v`

#### Task 3.3 — E2E tests for wizard CLI optional features
- [ ] **File**: `tests/test_e2e_wizard_optional_features.py` (new file)
- **Depends on**: Tasks 3.1, 3.2
- **Marker**: `@pytest.mark.integration`
- **Description**:
  - Use Click's `CliRunner` (with `mix_stderr=False`) and `tmp_path` to invoke the real `wizard` CLI command end-to-end, then parse the written TOML file with `tomlkit` to assert on config values.
  - All tests run `--non-interactive` with a `--config` pointing to `tmp_path / "archon-search.toml"` so no real services or model downloads are triggered. Patch `_prewarm_models`, `_check_disk_space`, `detect_gpu`, `configure_providers`, `write_service_file`, `load_service`, `_wait_for_service`, and `_is_service_running` at the module level via `patch.multiple`.
  - Interactive-mode tests additionally mock `builtins.input` with a queue of responses.
  - `_install_code_extra` is patched via `MagicMock()` to verify it is (or is not) called without actually running pip.
- **Use cases covered**:
  1. **All feature flags non-interactive** — `wizard --non-interactive --profile minimal --code --watch --telemetry --log-format json --routing-strategy hybrid --no-reranker --eager-load` → config has `collections.watch=true`, `telemetry.enabled=true`, `logging.format="json"`, `routing.routing_strategy="hybrid"`, `database.reranker_model=""`, `database.eager_load_embedders=true`
  2. **GPU decline flag** — `wizard --non-interactive --profile minimal --disable-gpu` → config has `database.providers=[]`
  3. **Defaults produce minimal config** — `wizard --non-interactive --profile minimal` with no feature flags → config does NOT contain optional keys (`watch`, `telemetry.enabled`, `routing_strategy`, `logging.format`)
  4. **Interactive: user enables telemetry and watch** — `wizard --profile minimal` with stdin responses "n" (multilingual), default profile selection, "n" (code), "n" (reranker), "y" (watch), "y" (telemetry), "n" (eager-load), "" (routing=default), "" (log-format=default) → config has `collections.watch=true`, `telemetry.enabled=true`
  5. **Interactive: user enables multilingual** — stdin "y" (multilingual) → `_select_profile` called with `multilingual=True` (verified via mock spy on `_select_profile`)
  6. **Interactive: invalid routing then valid** — stdin "badval" then "hybrid" for routing question → config has `routing.routing_strategy="hybrid"`
  7. **Code extra install triggered** — `wizard --non-interactive --profile minimal --code` → patched `_install_code_extra` called exactly once
  8. **Code extra install failure is non-fatal** — patched `_install_code_extra` raises `InstallError` → `wizard` exits with code 0 and does NOT leave an incomplete config
  9. **Re-run wizard adds features to existing config** — first run writes minimal config; second run with `--watch` → `collections.watch=true` added, other existing keys preserved
- **Tests (TDD)** — `tests/test_e2e_wizard_optional_features.py`:
  - Integration: `test_e2e_all_feature_flags` — use case 1 above
  - Integration: `test_e2e_disable_gpu` — use case 2 above
  - Integration: `test_e2e_defaults_produce_clean_config` — use case 3 above
  - Integration: `test_e2e_interactive_watch_and_telemetry` — use case 4 above
  - Integration: `test_e2e_interactive_multilingual_yes` — use case 5 above
  - Integration: `test_e2e_interactive_invalid_routing_retries` — use case 6 above
  - Integration: `test_e2e_code_extra_install_triggered` — use case 7 above
  - Integration: `test_e2e_code_extra_install_failure_non_fatal` — use case 8 above
  - Integration: `test_e2e_rerun_adds_features_to_existing_config` — use case 9 above
- **Checkpoint**: `uv run pytest tests/test_e2e_wizard_optional_features.py -m integration -v`

---

### Phase 4 — Verification & Documentation

#### Task 4.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: Tasks 1.1–1.4, 2.1–2.4, 3.1–3.3
- **Description**:
  - Spawn an agent to discover all documentation in the project (READMEs, ADRs, Architecture docs, User Manual, `archon-search.toml.example`, `CLAUDE.md`, `contributing.md`, `Documentation/Backlog/C8-wizard-optional-features-investigation.md`) and update every file whose content is affected by the changes delivered in this plan.
  - Specifically:
    - `archon-search.toml.example` — add or update comments for each newly-surfaced key (watch, telemetry.enabled, eager_load_embedders, routing_strategy, logging.format) explaining the wizard question that sets them
    - `Documentation/Architecture/600_api_reference_or_public_interface.md` — update CLI surface table to list new `wizard` flags
    - `Documentation/UserManual/` — update any wizard walkthrough docs
    - `Documentation/Backlog/C8-wizard-optional-features-investigation.md` — update "Surfaced in Wizard" column in summary table for all 8 implemented gaps to reflect "Yes" after implementation (Gap 8 / HyDE remains "No")
  - Verify all acceptance criteria below are met before marking complete.
- **Releasable**: after this task, the feature is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - `uv run pytest` passes with coverage ≥ 85% (default run, no new `--no-cov`)
  - `archon-search wizard --help` shows all 8 new flags: `--code`, `--no-code`, `--watch`, `--no-watch`, `--telemetry`, `--no-telemetry`, `--eager-load`, `--no-eager-load`, `--no-reranker`, `--routing-strategy`, `--log-format`, `--disable-gpu`
  - Running `archon-search wizard --non-interactive --profile minimal --code --watch --telemetry --log-format json` produces a config where `[collections].watch = true`, `[telemetry].enabled = true`, `[logging].format = "json"` and code extra install is attempted
  - Running `archon-search wizard --non-interactive --profile minimal --no-reranker` produces a config where `[database].reranker_model = ""`
  - Running `archon-search wizard --non-interactive --profile minimal --routing-strategy hybrid` produces a config where `[routing].routing_strategy = "hybrid"`
  - All 8 gaps implemented in this plan are marked "Yes" in the C8 investigation summary table's "Surfaced in Wizard" column (Gap 8 / HyDE remains "No" — pending C4)
  - No existing `test_install_*.py` tests are broken
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked; run `uv run pytest tests/test_install_*.py -v` and `uv run pytest tests/test_e2e_wizard_optional_features.py -m integration -v`.
