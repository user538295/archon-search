# C15 — Wizard Configurability Expansion
**Purpose**: Add 7 deployment-time flags to the wizard (host, port, db_path, log_level, log_to_stderr, top_k, telemetry_retention_days), print full API key in success output, and add Tier 2 interactive HyDE/RAG Fusion toggle and `--server-key` flag.
**Audience**: archon-search contributors implementing C15; operators running `archon-search wizard`.
**Status**: To Do

---

## Background

Common deployment scenarios require hand-editing `~/.archon-search/archon-search.toml` after the wizard runs because the wizard doesn't expose these settings. Homelab/remote users need `--host`/`--port`, container users need `--log-to-stderr` (to complete the `--log-format json` container combo from C9), power users want more than 5 results via `--top-k`, and everyone is confused about where their API key lives. Tier 2 adds detection-gated HyDE/RAG Fusion and `--server-key` for users with Anthropic API keys and production deployments.

## Goal

A user deploying archon-search to any common environment (developer laptop, homelab server, Docker container, CI, Claude Desktop) can complete their full configuration through the wizard with zero manual TOML editing for the settings they need. Success: all 7 Tier 1 flags write their values to TOML, the success block always prints the full API key with source, HyDE/RAG Fusion is enabled with one prompt when `ANTHROPIC_API_KEY` is set, and `--server-key` lets users set a custom Bearer token.

---

## Scope

### In Scope
- 7 Tier 1 flags: `--host`, `--port`, `--db-path`, `--log-level`, `--log-to-stderr`, `--top-k`, `--telemetry-retention-days`
- `--log-to-stderr` additionally auto-prompted as a conditional follow-up when `json` log format is selected interactively
- API key visibility: full key + source + "keep private" note in success output (no flag)
- `--top-k` hint line added to the "Next steps" block (flags-only; no interactive prompt)
- Tier 2 HyDE + RAG Fusion: detection-gated interactive prompt + `--enable-hyde`/`--enable-rag-fusion` flags
- `_install_extra(package, label)` helper extracted from `_install_code_extra()`
- Tier 2 `--server-key TEXT`: lowercase hex, min 32 chars, writes `.search.env` mode 600

### Out of Scope
- Routing confidence knobs (deferred to `archon-search tune routing`)
- Collection seeding in wizard
- TLS/HTTPS
- `namespaces` configuration
- `--server-key-file PATH` (covered by `ARCHON_SEARCH_KEY_FILE` env var)
- Individual HyDE/RAG Fusion sub-knobs in wizard (TOML only)
- Interactive prompts for `--db-path` and `--top-k` (flags-only by design; `--top-k` surfaced via next-steps hint only)
- Filesystem-type check on `--db-path` (writability check only; local-vs-network validation would reject intentional container volume mounts)

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 5.1 — Final verification & documentation update].

---

## What does NOT change
- `key_manager._validate_key()` — no minimum length added; key manager accepts any valid hex string
- `config.py` validation for `top_k_return` — no upper bound added; 100-ceiling is wizard-only
- `_prompt_optional_features()` interactive questions count for existing features
- Existing `WizardFeatures` fields and their defaults
- `_install_code_extra()` public interface (kept as a thin wrapper after refactor)

---

## Known limitations / accepted trade-offs
- `--port` conflict not detected at wizard time; OS error at service start is the signal
- `--db-path` tilde not expanded before writing; `config.py` expands at use sites
- `top_k_retrieve = max(15, 3 * top_k_return)` coupling enforced only at wizard write time; hand-edited TOML bypasses the ratio
- `ANTHROPIC_API_KEY` not validated at wizard time; invalid key produces errors on first query
- Shell history warning for `--server-key` is informational only; cannot suppress shell history programmatically
- The 4 open questions from the brief are resolved as follows: **Q2** (`--top-k` prompt placement) — flags-only; discoverability covered by a hint line in the "Next steps" block (`archon-search wizard --top-k 20  # increase results per query (default: 5)`). **Q5** (API key format) — full key shown once with "keep this key private" note; masking is security theatre for a locally-generated key already in a plaintext file the user owns. **Q6** (`--log-to-stderr` prompt) — auto-prompted as a conditional follow-up when the user picks `json` log format interactively (same pattern as the reranker prompt skip already in C14); flags-only in non-interactive mode. **Q9** (`--db-path` validation depth) — writability only (`os.access W_OK`); filesystem-type detection would reject intentional container volume mounts, which is the primary use case for `--db-path`.

---

## Architecture

### Modified files
- `archon_search/install.py` — `WizardFeatures` (new fields), `_apply_wizard_features_to_toml()` (new write logic), `_install_code_extra()` (refactored to call `_install_extra()`), new `_install_extra()` helper, `_prompt_optional_features()` (HyDE/RAG Fusion prompt), `SearchInstaller.run()` (new params + success output)
- `archon_search/cli/install_cmd.py` — `wizard` Click command (new options + validation)

### New `WizardFeatures` fields (all optional, default off/None)
```python
host: str | None = None              # [server].host
port: int | None = None              # [server].port
db_path: str | None = None           # [database].db_path
log_level: str | None = None         # [logging].level
log_to_stderr: bool = False          # [logging].log_file = ""
top_k: int | None = None             # [database].top_k_return + top_k_retrieve
telemetry_retention_days: int | None = None  # [telemetry].retention_days
enable_hyde: bool = False            # [hyde].enabled
enable_rag_fusion: bool = False      # [rag_fusion].enabled
```

### `_prompt_optional_features()` — `--log-to-stderr` conditional follow-up
When the user interactively selects `json` log format, a conditional follow-up prompt fires immediately after:
```
Log to stderr only? [y/N]:
  Routes all log output to stderr instead of a file.
  Canonical container combo: --log-format json --log-to-stderr.
```
This mirrors the existing reranker-skip pattern (C14): the prompt only appears when contextually relevant. In non-interactive mode the follow-up is skipped; `--log-to-stderr` flag still works independently.

### `_install_extra(package: str, label: str, dry_run: bool = False) -> None`
Shared helper: `uv pip install` with pip fallback, echo label, raise `InstallError` on failure.
`_install_code_extra(dry_run)` becomes: `_install_extra("archon-search[code]", "code enrichment", dry_run)`.

### TOML write logic for new fields (in `_apply_wizard_features_to_toml`)
- `host` (not None) → `doc["server"]["host"] = features.host`
- `port` (not None) → `doc["server"]["port"] = features.port`
- `db_path` (not None) → `doc["database"]["db_path"] = features.db_path`
- `log_level` (not None) → `doc["logging"]["level"] = features.log_level`
- `log_to_stderr` (True) → `doc["logging"]["log_file"] = ""`
- `top_k` (not None) → `doc["database"]["top_k_return"] = features.top_k`; `doc["database"]["top_k_retrieve"] = max(15, 3 * features.top_k)`
- `telemetry_retention_days` (not None) → `doc["telemetry"]["retention_days"] = features.telemetry_retention_days` (only when `enable_telemetry=True`)
- `enable_hyde` (True) → `doc["hyde"]["enabled"] = True`
- `enable_rag_fusion` (True) → `doc["rag_fusion"]["enabled"] = True`

### Click validation
- `--host`: reject empty string via `callback`; accept any non-empty string
- `--port`: `type=click.IntRange(1, 65535)` or manual validation
- `--log-level`: `type=click.Choice(["DEBUG","INFO","WARNING","ERROR","CRITICAL"])`
- `--top-k`: manual validation 1–100; >100 error: `"top_k > 100 is likely to cause performance problems; edit archon-search.toml directly if you need a higher value."`
- `--telemetry-retention-days`: `type=click.IntRange(min=1)`
- `--server-key`: custom `click.ParamType` that validates `^[0-9a-f]+$` and len >= 32
- `--enable-hyde`/`--enable-rag-fusion`: pre-run check that `ANTHROPIC_API_KEY` is set; fail with `"Error: --enable-hyde/--enable-rag-fusion requires ANTHROPIC_API_KEY to be set in the environment"`

### API key visibility (in `SearchInstaller.run()` success block)
Full key shown once with a "keep private" note. Masking is omitted — the key already lives in a plaintext file the user owns; terminal output is not a meaningful additional exposure.
```python
key, source = load_or_generate_key()
if source == "env var":
    click.echo(f"  API key: {key}  (source: $ARCHON_SEARCH_API_KEY env var — keep this key private)")
elif source == "auto-generated":
    click.echo(f"  API key: {key}  (generated fresh — keep this key private; also stored at: {KEY_FILE})")
else:  # source.startswith("file:")
    click.echo(f"  API key: {key}  (keep this key private; also stored at: {KEY_FILE})")
```

### `--db-path` special handling in `SearchInstaller.run()`
- `Path(features.db_path).expanduser().mkdir(parents=True, exist_ok=True)` — writable check via `os.access`
- If config exists with different `db_path`: print migration note
- Warn if path is inside any configured collection path

### `--server-key` write in `SearchInstaller.run()`
- Validate at Click param level (hex + length >= 32)
- Write `ARCHON_SEARCH_API_KEY={key}\n` to `KEY_FILE` via `atomic_write_bytes` with mode 600
- Print: `"Note: your server key may appear in shell history. Consider using ARCHON_SEARCH_API_KEY env var instead."`
- If `ARCHON_SEARCH_API_KEY` is set: print additional warning that env var takes priority
- Print restart note: `"Server key updated. Restart the service to apply: archon-search restart."`

---

## Task breakdown

### Phase 1 — Tier 1 data + TOML writer

> **Releasable**: after Task 1.3 — all Tier 1 settings can be programmatically passed to `SearchInstaller.run()` and written to TOML; no CLI flags yet

#### Task 1.1 — `WizardFeatures` — 7 new Tier 1 fields
- [x] **File**: `archon_search/install.py`
- **Depends on**: nothing
- **Description**:
  - Add 9 new optional fields to `WizardFeatures` dataclass (7 Tier 1 + 2 Tier 2 for HyDE/RAG Fusion, since all live in the same dataclass): `host: str | None = None`, `port: int | None = None`, `db_path: str | None = None`, `log_level: str | None = None`, `log_to_stderr: bool = False`, `top_k: int | None = None`, `telemetry_retention_days: int | None = None`, `enable_hyde: bool = False`, `enable_rag_fusion: bool = False`
  - All new fields default to None / False so existing code constructing `WizardFeatures()` without these fields continues to work
  - No write logic yet — only the dataclass definition
- **Releasable**: after this task, `WizardFeatures` can carry the new settings; no behavior change yet
- **Tests (TDD)** — `tests/test_install_wizard_features.py`:
  - Unit: `test_wizard_features_new_fields_default_to_none_or_false` — verify all 9 new fields have correct defaults
  - Unit: `test_wizard_features_new_fields_accept_values` — construct with all new fields set to non-default values; assert values stored correctly
  - Checkpoint: `uv run pytest tests/test_install_wizard_features.py -v --no-cov`

#### Task 1.2 — `_apply_wizard_features_to_toml()` — write logic for 9 new fields
- [x] **File**: `archon_search/install.py`
- **Depends on**: Task 1.1
- **Description**:
  - `host` (not None): `_ensure_section("server")`, `doc["server"]["host"] = features.host`
  - `port` (not None): `_ensure_section("server")`, `doc["server"]["port"] = features.port`
  - `db_path` (not None): `_ensure_section("database")`, `doc["database"]["db_path"] = features.db_path`
  - `log_level` (not None): `_ensure_section("logging")`, `doc["logging"]["level"] = features.log_level`
  - `log_to_stderr` (True): `_ensure_section("logging")`, `doc["logging"]["log_file"] = ""`
  - `top_k` (not None): `_ensure_section("database")`, `doc["database"]["top_k_return"] = features.top_k`, `doc["database"]["top_k_retrieve"] = max(15, 3 * features.top_k)`
  - `telemetry_retention_days` (not None AND `features.enable_telemetry` True): `_ensure_section("telemetry")`, `doc["telemetry"]["retention_days"] = features.telemetry_retention_days`
  - `enable_hyde` (True): `_ensure_section("hyde")`, `doc["hyde"]["enabled"] = True`
  - `enable_rag_fusion` (True): `_ensure_section("rag_fusion")`, `doc["rag_fusion"]["enabled"] = True`
  - When `telemetry_retention_days` is set but `enable_telemetry` is False: skip the write (the warning is printed at the CLI layer, not here)
  - All 9 writes follow the existing if-guard pattern (only write when not default)
- **Releasable**: after this task, `_apply_wizard_features_to_toml()` writes all new fields to TOML; testable in isolation
- **Tests (TDD)** — `tests/test_install_config_writer.py`:
  - Unit: `test_apply_host_writes_server_section` — `WizardFeatures(host="0.0.0.0")` → TOML has `[server].host = "0.0.0.0"`
  - Unit: `test_apply_port_writes_server_section` — `WizardFeatures(port=9000)` → TOML has `[server].port = 9000`
  - Unit: `test_apply_db_path_writes_database_section` — `WizardFeatures(db_path="~/custom")` → TOML has `[database].db_path = "~/custom"`
  - Unit: `test_apply_log_level_writes_logging_section` — `WizardFeatures(log_level="DEBUG")` → TOML has `[logging].level = "DEBUG"`
  - Unit: `test_apply_log_to_stderr_writes_log_file_empty` — `WizardFeatures(log_to_stderr=True)` → TOML has `[logging].log_file = ""`
  - Unit: `test_apply_log_to_stderr_false_does_not_write_log_file` — `WizardFeatures(log_to_stderr=False)` → no `log_file` key written
  - Unit: `test_apply_top_k_writes_both_keys` — `WizardFeatures(top_k=10)` → `top_k_return=10`, `top_k_retrieve=30`
  - Unit: `test_apply_top_k_1_sets_retrieve_to_15` — `WizardFeatures(top_k=1)` → `top_k_retrieve=15` (max guard)
  - Unit: `test_apply_top_k_33_sets_retrieve_to_99` — `WizardFeatures(top_k=33)` → `top_k_retrieve=99`
  - Unit: `test_apply_top_k_none_does_not_write` — `WizardFeatures(top_k=None)` → no top_k keys written
  - Unit: `test_apply_telemetry_retention_with_telemetry_enabled` — `WizardFeatures(enable_telemetry=True, telemetry_retention_days=7)` → `[telemetry].retention_days=7`
  - Unit: `test_apply_telemetry_retention_without_telemetry_skipped` — `WizardFeatures(enable_telemetry=False, telemetry_retention_days=7)` → no `retention_days` key written
  - Unit: `test_apply_enable_hyde` — `WizardFeatures(enable_hyde=True)` → `[hyde].enabled = true`
  - Unit: `test_apply_enable_rag_fusion` — `WizardFeatures(enable_rag_fusion=True)` → `[rag_fusion].enabled = true`
  - Unit: `test_apply_all_new_fields_together` — all new non-default fields set; assert all expected keys present in doc
  - Checkpoint: `uv run pytest tests/test_install_config_writer.py -v --no-cov`

#### Task 1.3 — `SearchInstaller.run()` — 9 new keyword params
- [x] **File**: `archon_search/install.py`
- **Depends on**: Task 1.1
- **Description**:
  - Add to `SearchInstaller.run()` signature (keyword-only, all default to None/False): `host: str | None = None`, `port: int | None = None`, `db_path: str | None = None`, `log_level: str | None = None`, `log_to_stderr: bool = False`, `top_k: int | None = None`, `telemetry_retention_days: int | None = None`, `enable_hyde: bool = False`, `enable_rag_fusion: bool = False`, `server_key: str | None = None`
  - Pass them into `WizardFeatures` when constructing it inside `_prompt_optional_features()` call (or directly when building the `features` object; `_prompt_optional_features()` is the right place for the HyDE/RAG Fusion ones since they may also involve interactive prompts; the Tier 1 ones are flags-only, so pass directly into `WizardFeatures` fields after `_prompt_optional_features()` returns)
  - For `db_path` (when not None): `Path(db_path).expanduser().mkdir(parents=True, exist_ok=True)`, check writability via `os.access(Path(db_path).expanduser(), os.W_OK)`, raise `SystemExit(1)` if not writable; warn if `db_path` differs from existing config's `db_path`; warn if path is inside a configured collection path
  - `server_key` write: if not None, write `ARCHON_SEARCH_API_KEY={server_key}\n` to `KEY_FILE` via `atomic_write_bytes`, set file mode to 0o600 via `os.chmod`; print shell history warning; if `os.environ.get("ARCHON_SEARCH_API_KEY")`, print env-var-takes-priority warning; print restart note
  - Tier 1 fields set on features object after `_prompt_optional_features()` returns (overriding the prompt values since they're flags-only): `if host is not None: features.host = host` etc.
- **Releasable**: after this task, `SearchInstaller.run(host="0.0.0.0", port=9000, ...)` works programmatically
- **Tests (TDD)** — `tests/test_install_run.py`:
  - Unit: `test_run_passes_host_to_features` — mock `_apply_wizard_features_to_toml`; call `run(host="0.0.0.0", ...)`; assert `features.host == "0.0.0.0"` passed to writer
  - Unit: `test_run_passes_port_to_features` — same pattern for `port=9000`
  - Unit: `test_run_passes_top_k_to_features` — `top_k=20` passed through
  - Unit: `test_run_server_key_writes_key_file` — `server_key="abcd"*8` (32 chars); assert `KEY_FILE` written with correct content, mode 0o600
  - Unit: `test_run_server_key_prints_history_warning` — assert warning printed to stdout
  - Unit: `test_run_server_key_with_env_var_prints_priority_warning` — set `ARCHON_SEARCH_API_KEY` in env; assert priority warning printed
  - Unit: `test_run_db_path_creates_directory` — `db_path="/tmp/test_archon_db"`; assert dir created
  - Unit: `test_run_db_path_not_writable_exits` — mock `os.access` to return False; assert `SystemExit` raised
  - Unit: `test_run_db_path_migration_note_when_different` — existing config has different db_path; assert migration note printed
  - Checkpoint: `uv run pytest tests/test_install_run.py -v --no-cov -k "test_run_passes_host or test_run_passes_port or test_run_passes_top_k or test_run_server_key or test_run_db_path"`

---

### Phase 2 — Tier 1 Click options

> **Releasable**: after this phase — all 7 Tier 1 flags are fully functional from the CLI

#### Task 2.1 — `install_cmd.py wizard` — 7 new Click options with validation
- [x] **File**: `archon_search/cli/install_cmd.py`
- **Depends on**: Task 1.3
- **Description**:
  - Add to `_install_options()` decorator list (or directly to `wizard` via `@click.option`):
    - `--host TEXT`: callback validates non-empty; help: `"Bind address (default: 127.0.0.1); use 0.0.0.0 for remote access"`
    - `--port INTEGER`: `type=click.IntRange(1, 65535)`, default=None, help: `"HTTP port (default: 8765; valid: 1–65535)"`
    - `--db-path PATH`: `type=click.Path()`, default=None, help: `"Database directory (default: ~/.archon-search/search); write path as-is"`
    - `--log-level [DEBUG|INFO|WARNING|ERROR|CRITICAL]`: `type=click.Choice([...], case_sensitive=True)`, default=None
    - `--log-to-stderr`: `is_flag=True`, default=False, help: `"Log to stderr only (sets log_file=''); canonical container combo: --log-format json --log-to-stderr"`
    - `--top-k INTEGER`: default=None; manual validation in callback: `1 <= value <= 100`, else `raise click.BadParameter("top_k > 100 is likely to cause performance problems; edit archon-search.toml directly if you need a higher value.")`; error for 0 or < 1
    - `--telemetry-retention-days INTEGER`: `type=click.IntRange(min=1)`, default=None; if provided without `--telemetry`, print warning to stderr: `"Warning: --telemetry-retention-days has no effect because telemetry is not enabled. Pass --telemetry to enable it."` (warning printed in the `wizard()` function body after params are resolved, not in a click callback)
  - Use `click.Context.get_parameter_source()` inside the `wizard()` body to detect DEFAULT vs COMMAND_LINE for the 7 new params; only pass non-DEFAULT values through to `run()`
  - Add 7 new params to `wizard()` function signature and pass them to `SearchInstaller.run()`
  - `--host` (non-loopback): if `features.host and features.host != "127.0.0.1"`, print security note in success block: `"Note: binding to {features.host} exposes the service on all interfaces. Ensure a firewall or reverse proxy is in place if this host is reachable externally."` This covers `0.0.0.0`, `::`, and any explicit LAN/public IP — not just `0.0.0.0`.
  - **`--log-to-stderr` conditional follow-up**: add to `_prompt_optional_features()` — after resolving `_log_format_val`, if `_log_format_val == "json"` and not `non_interactive` and `log_to_stderr is None` (not pre-answered by flag): print the explanation block and prompt `"Log to stderr only? [y/N]: "`. Set `_log_to_stderr_val` accordingly. If `log_to_stderr` was pre-answered by flag, skip the prompt. If non-interactive, skip the prompt and use `log_to_stderr or False`. Add `log_to_stderr: bool | None = None` to `_prompt_optional_features()` signature; pass from `SearchInstaller.run()`.
  - **`--top-k` next-steps hint**: add to the success output block: `"  archon-search wizard --top-k 20        # increase results per query (default: 5)"`
- **Releasable**: after this task, all 7 Tier 1 flags are available from `archon-search wizard`
- **Tests (TDD)** — `tests/test_e2e_wizard_optional_features.py`:
  - Integration: `test_wizard_host_writes_toml` — invoke wizard `--host 0.0.0.0 --non-interactive`; assert `[server].host = "0.0.0.0"` in written TOML
  - Integration: `test_wizard_host_non_loopback_prints_security_note` — `--host 0.0.0.0`; assert security note in stdout
  - Integration: `test_wizard_host_lan_ip_prints_security_note` — `--host 192.168.1.100`; assert security note in stdout
  - Integration: `test_wizard_host_loopback_no_security_note` — `--host 127.0.0.1`; assert security note NOT in stdout
  - Integration: `test_wizard_port_writes_toml` — `--port 9000`; assert `[server].port = 9000`
  - Integration: `test_wizard_port_invalid_rejects` — `--port 0`; assert non-zero exit code
  - Integration: `test_wizard_port_65536_rejects` — `--port 65536`; assert non-zero exit
  - Integration: `test_wizard_db_path_writes_toml` — `--db-path ~/custom`; assert `[database].db_path = "~/custom"` (tilde not expanded)
  - Integration: `test_wizard_log_level_writes_toml` — `--log-level DEBUG`; assert `[logging].level = "DEBUG"`
  - Integration: `test_wizard_log_level_invalid_rejects` — `--log-level VERBOSE`; assert non-zero exit
  - Integration: `test_wizard_log_to_stderr_writes_empty_log_file` — `--log-to-stderr`; assert `[logging].log_file = ""`
  - Integration: `test_wizard_top_k_writes_both_keys` — `--top-k 20`; assert `top_k_return=20`, `top_k_retrieve=60`
  - Integration: `test_wizard_top_k_1_sets_retrieve_to_15` — `--top-k 1`; assert `top_k_retrieve=15`
  - Integration: `test_wizard_top_k_0_rejects` — `--top-k 0`; non-zero exit
  - Integration: `test_wizard_top_k_101_rejects` — `--top-k 101`; non-zero exit with message about performance
  - Integration: `test_wizard_telemetry_retention_without_telemetry_warns` — `--telemetry-retention-days 7` without `--telemetry`; assert warning on stderr; assert `retention_days` NOT in TOML
  - Integration: `test_wizard_telemetry_retention_with_telemetry_writes_toml` — `--telemetry --telemetry-retention-days 7`; assert `[telemetry].retention_days = 7`
  - Integration: `test_wizard_host_empty_string_rejects` — `--host ""`; assert non-zero exit
  - Integration: `test_wizard_not_passed_flags_do_not_write_toml` — run without new flags; assert `[server].host` not present in written TOML
  - Integration: `test_wizard_explicit_default_value_writes_to_toml` — `--port 8765` (same as default); assert `[server].port = 8765` IS written (idempotency behavior)
  - Integration: `test_wizard_log_format_json_prompts_log_to_stderr` — interactive mode, mock inputs: `json` for log format, `y` for stderr follow-up; assert `[logging].log_file = ""`
  - Integration: `test_wizard_log_format_text_does_not_prompt_log_to_stderr` — interactive mode, mock input: `text` for log format; assert log-to-stderr prompt string not in stdout
  - Integration: `test_wizard_non_interactive_json_does_not_prompt_log_to_stderr` — `--log-format json --non-interactive`; assert no stderr prompt, `log_file` not written (flag not passed)
  - Integration: `test_wizard_log_to_stderr_flag_bypasses_conditional_prompt` — `--log-format json --log-to-stderr --non-interactive`; assert `[logging].log_file = ""`
  - Integration: `test_wizard_success_output_contains_top_k_hint` — after successful install; assert `"--top-k"` appears in stdout next-steps block
  - Checkpoint: `uv run pytest tests/test_e2e_wizard_optional_features.py -m integration -v --no-cov -k "test_wizard_host or test_wizard_port or test_wizard_db_path or test_wizard_log or test_wizard_top_k or test_wizard_telemetry_retention"`

---

### Phase 3 — API key visibility

> **Releasable**: after this task — success block always shows full API key + source + "keep private" note

#### Task 3.1 — `SearchInstaller.run()` success output — full API key + source
- [ ] **File**: `archon_search/install.py`
- **Depends on**: nothing (independent output change)
- **Description**:
  - In `SearchInstaller.run()` Step 17 (completion message), call `load_or_generate_key()` to get `(key, source)`
  - Print full key with "keep private" note based on source (no masking — the key is already in a plaintext file the user owns):
    - `source == "env var"`: `f"  API key: {key}  (source: $ARCHON_SEARCH_API_KEY env var — keep this key private)"`
    - `source == "auto-generated"`: `f"  API key: {key}  (generated fresh — keep this key private; also stored at: {KEY_FILE})"`
    - `source.startswith("file:")`: `f"  API key: {key}  (keep this key private; also stored at: {KEY_FILE})"`
  - Import `KEY_FILE` from `archon_search.key_manager` at the import block top
  - Print happens inside `if not self.dry_run` guard, before the final "archon-search installed" line
- **Releasable**: after this task, success output includes the full API key with source label
- **Tests (TDD)** — `tests/test_install_run.py`:
  - Unit: `test_run_success_prints_full_key_env_var` — patch `load_or_generate_key` to return `("abcd1234efgh5678abcd1234efgh5678abcd1234efgh5678abcd1234efgh5678", "env var")`; assert output contains the full key and `"$ARCHON_SEARCH_API_KEY env var"` and `"keep this key private"`
  - Unit: `test_run_success_prints_full_key_auto_generated` — source `"auto-generated"`; assert output contains the full key, `"generated fresh"`, and key file path
  - Unit: `test_run_success_prints_full_key_file` — source `"file: /home/user/.archon-search/.search.env"`; assert output contains the full key and `"also stored at:"` and key file path; does NOT contain `"$ARCHON_SEARCH_API_KEY"`
  - Unit: `test_run_success_key_not_printed_in_dry_run` — dry-run mode; assert key NOT in output
  - Checkpoint: `uv run pytest tests/test_install_run.py -v --no-cov -k "test_run_success_prints_full_key or test_run_success_key_not"`

---

### Phase 4 — Tier 2 HyDE + RAG Fusion

> **Releasable**: after Task 4.3 — HyDE/RAG Fusion can be enabled interactively and via flags

#### Task 4.1 — `_install_extra()` helper extracted from `_install_code_extra()`
- [ ] **File**: `archon_search/install.py`
- **Depends on**: nothing
- **Description**:
  - Extract the subprocess install logic from `_install_code_extra()` into `_install_extra(package: str, label: str, dry_run: bool = False) -> None`
  - `_install_extra()` prints `f"Installing {label}..."`, runs uv/pip with `package`, prints `f"{label.capitalize()} installed."` on success; raises `InstallError` on failure
  - `_install_code_extra(dry_run: bool = False) -> None` becomes a thin wrapper: `_install_extra("archon-search[code]", "code enrichment", dry_run)`
  - Public interface of `_install_code_extra` unchanged (backward compatible)
- **Releasable**: after this task, `_install_extra()` is callable for any extra package
- **Tests (TDD)** — `tests/test_install_code_extra.py`:
  - Unit: `test_install_extra_dry_run_echoes_package` — `dry_run=True`; assert echo message contains package name; no subprocess call
  - Unit: `test_install_extra_calls_uv_pip_install` — mock subprocess; assert uv command called with correct package
  - Unit: `test_install_extra_falls_back_to_pip_when_uv_absent` — mock uv to raise `FileNotFoundError`; assert pip fallback called
  - Unit: `test_install_extra_raises_install_error_on_pip_failure` — both uv and pip fail; assert `InstallError` raised with package name
  - Unit: `test_install_code_extra_delegates_to_install_extra` — patch `_install_extra`; call `_install_code_extra()`; assert called with `"archon-search[code]"` and `"code enrichment"`
  - Checkpoint: `uv run pytest tests/test_install_code_extra.py -v --no-cov`

#### Task 4.2 — HyDE/RAG Fusion `WizardFeatures` fields are already in Task 1.1. TOML writer for `enable_hyde`/`enable_rag_fusion` is already in Task 1.2. This task: add HyDE/RAG Fusion to `_prompt_optional_features()`.
- [ ] **File**: `archon_search/install.py`
- **Depends on**: Task 4.1, Task 1.1
- **Description**:
  - Add keyword params to `_prompt_optional_features()`: `enable_hyde: bool | None = None`, `enable_rag_fusion: bool | None = None`
  - At the end of the function (after existing prompts), add HyDE/RAG Fusion block:
    - If `enable_hyde is True` or `enable_rag_fusion is True` (flag-forced): set `_enable_hyde_val = enable_hyde or False`, `_enable_rag_fusion_val = enable_rag_fusion or False` (no prompt needed)
    - If `non_interactive` and neither flag set: `_enable_hyde_val = _enable_rag_fusion_val = False`
    - If `os.environ.get("ANTHROPIC_API_KEY")` and not `non_interactive` and neither flag pre-set: print explanation block from brief; prompt `"Enable AI query expansion? [y/N]: "`; if yes: `_enable_hyde_val = _enable_rag_fusion_val = True`; check extras installed via `importlib.util.find_spec("anthropic")`-equivalent; if not installed: offer `_install_extra("archon-search[hyde,rag_fusion]", "AI query expansion (HyDE + RAG Fusion)", dry_run=False)`; if user declines install: skip both silently
    - If no `ANTHROPIC_API_KEY` and not pre-set: `_enable_hyde_val = _enable_rag_fusion_val = False`; post-install hint stored as a string to be printed after success (return it via a separate mechanism or just print inside `run()` after checking env)
  - Add post-install hint logic in `SearchInstaller.run()`: after success block, if `ANTHROPIC_API_KEY` not set and neither `enable_hyde` nor `enable_rag_fusion` were requested: print `"Tip: Set $ANTHROPIC_API_KEY to enable AI query expansion (HyDE + RAG Fusion) next run."`
  - Return updated `WizardFeatures` with `enable_hyde` and `enable_rag_fusion` set
- **Releasable**: after this task, interactive HyDE/RAG Fusion prompt works with `ANTHROPIC_API_KEY` detection
- **Tests (TDD)** — `tests/test_install_wizard_features.py`:
  - Unit: `test_prompt_optional_features_hyde_rag_fusion_skipped_when_no_api_key` — no `ANTHROPIC_API_KEY` in env; `non_interactive=False`; assert `features.enable_hyde is False` and `features.enable_rag_fusion is False` without prompting
  - Unit: `test_prompt_optional_features_hyde_rag_fusion_prompted_when_api_key_present` — set `ANTHROPIC_API_KEY`; mock input to return `"y"`; assert `features.enable_hyde is True` and `features.enable_rag_fusion is True`
  - Unit: `test_prompt_optional_features_hyde_rag_fusion_declined` — set `ANTHROPIC_API_KEY`; mock input to return `"n"`; assert both False
  - Unit: `test_prompt_optional_features_hyde_skipped_non_interactive_even_with_key` — `non_interactive=True`, `ANTHROPIC_API_KEY` set; assert both False (no prompt)
  - Unit: `test_prompt_optional_features_enable_hyde_flag_bypasses_prompt` — `enable_hyde=True`; no `ANTHROPIC_API_KEY`; but since these flags are validated at CLI layer before reaching this, just assert `features.enable_hyde is True`
- **Tests (TDD)** — `tests/test_e2e_wizard_optional_features.py`:
  - Integration: `test_wizard_enable_hyde_requires_anthropic_key` — `--enable-hyde` without `ANTHROPIC_API_KEY`; assert non-zero exit with error message
  - Integration: `test_wizard_enable_rag_fusion_requires_anthropic_key` — `--enable-rag-fusion` without `ANTHROPIC_API_KEY`; assert non-zero exit
  - Integration: `test_wizard_non_interactive_skips_hyde_prompt_even_with_key` — set `ANTHROPIC_API_KEY`, `--non-interactive`; assert neither `[hyde]` nor `[rag_fusion]` in TOML
  - Integration: `test_wizard_enable_hyde_and_rag_fusion_writes_toml` — set `ANTHROPIC_API_KEY`, `--enable-hyde --enable-rag-fusion --non-interactive`; assert both `[hyde].enabled = true` and `[rag_fusion].enabled = true` in TOML
  - Checkpoint: `uv run pytest tests/test_e2e_wizard_optional_features.py -m integration -v --no-cov -k "test_wizard_enable_hyde or test_wizard_enable_rag or test_wizard_non_interactive_skips_hyde"`

#### Task 4.3 — `install_cmd.py wizard` — `--enable-hyde` and `--enable-rag-fusion` Click flags
- [ ] **File**: `archon_search/cli/install_cmd.py`
- **Depends on**: Task 4.2
- **Description**:
  - Add `@click.option("--enable-hyde", is_flag=True, default=False, help="Enable HyDE query expansion (requires ANTHROPIC_API_KEY)")`
  - Add `@click.option("--enable-rag-fusion", is_flag=True, default=False, help="Enable RAG Fusion query expansion (requires ANTHROPIC_API_KEY)")`
  - In the `wizard()` function body, before calling `SearchInstaller.run()`: if `enable_hyde or enable_rag_fusion` and not `os.environ.get("ANTHROPIC_API_KEY")`: `raise click.UsageError("--enable-hyde/--enable-rag-fusion requires ANTHROPIC_API_KEY to be set in the environment")`
  - Pass `enable_hyde=enable_hyde, enable_rag_fusion=enable_rag_fusion` to `SearchInstaller.run()`
  - Add `enable_hyde` and `enable_rag_fusion` keyword params to `SearchInstaller.run()` if not already done; pass them to `_prompt_optional_features()`
- **Releasable**: after this task, `--enable-hyde` and `--enable-rag-fusion` are fully functional CLI flags
- **Tests (TDD)** — `tests/test_install_cmd.py`:
  - Unit: `test_wizard_help_contains_enable_hyde` — `CliRunner().invoke(main, ["wizard", "--help"])`; assert `"--enable-hyde"` in output
  - Unit: `test_wizard_help_contains_enable_rag_fusion` — same for `"--enable-rag-fusion"`
  - Checkpoint: `uv run pytest tests/test_install_cmd.py -v --no-cov -k "enable_hyde or enable_rag_fusion"`

---

### Phase 5 — Tier 2 `--server-key`

> **Releasable**: after Task 5.2 — `--server-key` fully functional

#### Task 5.1 — `--server-key` custom Click param type with hex + length validation
- [ ] **File**: `archon_search/cli/install_cmd.py`
- **Depends on**: nothing
- **Description**:
  - Add `class _HexKeyParamType(click.ParamType)` in `install_cmd.py`:
    - `name = "HEX_KEY"`
    - `convert(value, param, ctx)`: if not `_HEX_RE.fullmatch(value)`: `self.fail(f"--server-key must be a lowercase hex string (e.g., generated with: python -c \"import secrets; print(secrets.token_hex(32))\")", param, ctx)`; if `len(value) < 32`: `self.fail("--server-key must be at least 32 hex characters for adequate security.", param, ctx)`; return `value`
    - Import `_HEX_RE` from `archon_search.key_manager` (it's a module-level constant)
  - Add `@click.option("--server-key", type=_HexKeyParamType(), default=None, help="Custom server API key (lowercase hex, min 32 chars). Sets the archon-search Bearer token.")`
  - Add `server_key: str | None` to `wizard()` signature
  - Pass `server_key=server_key` to `SearchInstaller.run()`
- **Releasable**: after this task, `--server-key` validates input at parse time; write logic in Task 5.2
- **Tests (TDD)** — `tests/test_install_cmd.py`:
  - Unit: `test_server_key_valid_hex_32_chars_accepted` — invoke wizard with valid 32-char hex key (with all other infrastructure patched); assert exit code 0 (or that `run()` received the key)
  - Unit: `test_server_key_non_hex_rejected_at_parse` — `--server-key sk-abc123`; assert non-zero exit with error about hex format
  - Unit: `test_server_key_empty_string_rejected` — `--server-key ""`; assert non-zero exit
  - Unit: `test_server_key_31_chars_rejected` — 31-char hex string; assert non-zero exit with length error
  - Unit: `test_server_key_32_chars_accepted` — 32-char hex string; assert no parse error
  - Unit: `test_server_key_uppercase_rejected` — `"ABCD" * 8`; assert non-zero exit (lowercase only)
  - Checkpoint: `uv run pytest tests/test_install_cmd.py -v --no-cov -k "server_key"`

#### Task 5.2 — `SearchInstaller.run()` — `--server-key` write logic (already scaffolded in Task 1.3; this task adds the key file write)
- [ ] **File**: `archon_search/install.py`
- **Depends on**: Task 5.1, Task 1.3
- **Description**:
  - The `server_key` param is already on `run()` from Task 1.3. This task implements the actual write:
    - If `server_key is not None`: write `f"ARCHON_SEARCH_API_KEY={server_key}\n".encode()` to `KEY_FILE` using `atomic_write_bytes(KEY_FILE, ...)`, then `os.chmod(KEY_FILE, 0o600)`
    - Print: `"Note: your server key may appear in shell history. Consider using ARCHON_SEARCH_API_KEY env var instead."`
    - If `os.environ.get("ARCHON_SEARCH_API_KEY")`: print: `"Warning: ARCHON_SEARCH_API_KEY env var is set and takes priority over the key file. Your --server-key value was written to disk but will not be used while ARCHON_SEARCH_API_KEY is set."`
    - Print: `"Server key updated. Restart the service to apply: archon-search restart."`
    - Write happens before the service start (after config write, before Step 14)
- **Releasable**: after this task, `--server-key` fully writes the key file with mode 600 and all required warnings
- **Tests (TDD)** — `tests/test_e2e_wizard_optional_features.py`:
  - Integration: `test_wizard_server_key_writes_key_file` — invoke wizard with `--server-key <32-hex>` (patch file I/O and services); assert `KEY_FILE` written with `ARCHON_SEARCH_API_KEY=<key>` and mode 0o600
  - Integration: `test_wizard_server_key_prints_history_warning` — assert history warning in output
  - Integration: `test_wizard_server_key_prints_restart_note` — assert restart note in output
  - Integration: `test_wizard_server_key_with_env_var_set_prints_priority_warning` — set `ARCHON_SEARCH_API_KEY` env; assert priority warning in output
  - Integration: `test_wizard_server_key_with_env_var_set_still_writes_file` — set `ANTHROPIC_API_KEY` (wait, this is `ARCHON_SEARCH_API_KEY`) env; assert key IS still written to file (not rejected)
  - Checkpoint: `uv run pytest tests/test_e2e_wizard_optional_features.py -m integration -v --no-cov -k "server_key"`

---

### Phase 6 — Verification & Documentation

#### Task 6.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to discover all documentation in the project (READMEs, ADRs, Architecture docs, user guides, UserManual, `02_wizard.md`, `Documentation/UserManual/`, `Documentation/Architecture/`, CLAUDE.md, `archon-search.toml.example`) and update every file whose content is affected by the changes delivered in this plan. The agent must not update docs that are unrelated.
  - Specifically update:
    - `Documentation/UserManual/02_wizard.md` — add all 7 Tier 1 flags, HyDE/RAG Fusion toggle, `--server-key`, API key visibility; add "What the wizard does not configure" section noting TOML-only knobs; document idempotency behavior of explicit flags
    - `Documentation/Architecture/600_api_reference_or_public_interface.md` — update CLI surface section for wizard
    - `archon-search.toml.example` — add commented examples for new config keys if not already present
  - Verify all acceptance criteria below are met before marking this task complete.
- **Releasable**: after this task, the feature is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - **Tier 1 — each flag writes its value**: `--host 0.0.0.0` → `[server].host = "0.0.0.0"` in TOML; same for all 7 flags
  - **Tier 1 — not-passed flag leaves TOML unchanged**: run wizard without `--port`; existing `port = 9000` not overwritten
  - **Tier 1 — explicit default writes**: `--port 8765` → `[server].port = 8765` written (even though 8765 is default)
  - `--port 0` → non-zero exit; `--port 65536` → non-zero exit
  - `--log-level VERBOSE` → non-zero exit
  - `--top-k 0` → non-zero exit; `--top-k 101` → non-zero exit with performance message; `--top-k 1` → `top_k_return=1`, `top_k_retrieve=15`; `--top-k 100` → `top_k_retrieve=300`
  - `--telemetry-retention-days 7` without `--telemetry` → warning on stderr; `retention_days` NOT in TOML
  - `--host ""` → non-zero exit
  - `--db-path ~/custom` → TOML has `db_path = "~/custom"` (tilde preserved)
  - `--log-to-stderr` → `[logging].log_file = ""`; omitted → no `log_file` key
  - Success output includes the full API key + correct source label + "keep this key private" note (env var or file); key NOT printed in dry-run mode
  - Success "next steps" block contains `"--top-k"` hint
  - Interactive `--log-format json` triggers `"Log to stderr only?"` follow-up; `y` → `[logging].log_file = ""`; `--non-interactive` skips the follow-up
  - `--enable-hyde` without `ANTHROPIC_API_KEY` → non-zero exit with clear error
  - `--enable-rag-fusion` without `ANTHROPIC_API_KEY` → non-zero exit with clear error
  - `--non-interactive` with `ANTHROPIC_API_KEY` set and no `--enable-hyde`/`--enable-rag-fusion` → neither `[hyde]` nor `[rag_fusion]` in TOML
  - `--enable-hyde --enable-rag-fusion` with `ANTHROPIC_API_KEY` set → both `[hyde].enabled = true` and `[rag_fusion].enabled = true` in TOML
  - `--server-key "sk-abc123"` → non-zero exit (non-hex)
  - `--server-key` with 31 hex chars → non-zero exit (length)
  - `--server-key` with 32 hex chars → key written to `KEY_FILE` with mode 0o600; shell history warning printed; restart note printed
  - `--server-key` with `ARCHON_SEARCH_API_KEY` set → key written AND env-var priority warning printed
  - Full test suite passes: `uv run pytest` (no `--no-cov`; coverage gate ≥ 85%)
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked; run `uv run pytest` and verify exit code 0.
