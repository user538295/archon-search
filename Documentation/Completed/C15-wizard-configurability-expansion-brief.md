# Feature Brief: Wizard Configurability Expansion

## Problem
Common deployment scenarios — remote access, custom storage paths, container logging, result-count tuning, AI-powered recall boosters — require hand-editing `~/.archon-search/archon-search.toml` after the wizard runs, because the wizard doesn't expose these settings. Users discover this only after install.

## Goal
A user deploying archon-search to any common environment (developer laptop, homelab server, Docker container, CI, Claude Desktop) can complete their full configuration through the wizard with zero manual TOML editing for the settings they need.

## Users & Context
- **Homelab / remote users**: bind to `0.0.0.0`, custom port, need to reach the server from another machine
- **Container users**: want JSON log format + stderr-only logging, custom `db_path` on a mounted volume. Note: `--log-format json` already exists as a wizard flag (shipped in a prior release). The Tier 1 addition of `--log-to-stderr` completes the canonical container combo: `archon-search wizard --log-format json --log-to-stderr`.
- **Quality-focused users**: want to enable HyDE or RAG Fusion once they have an Anthropic API key
- **Production users**: want to set a custom server key (`--server-key`) and know where it lives
- **Power users**: want more than 5 results per query (`top_k_return = 5` surprises them)

## Core Flow

### Tier 1 — Quick-win flags (no interactive prompt by default; flags-only)

The 7 new flags follow the same 4-step pattern as existing `WizardFeatures` fields:
1. Add field to `WizardFeatures` dataclass
2. Add Click option to `install_cmd.py`
3. Add write logic to `_apply_wizard_features_to_toml()`
4. Add keyword param to `SearchInstaller.run()`

The API key visibility change (item 8) is an output-only change — no WizardFeatures field, no Click option, no TOML write.

The settings are:

| Flag | Config key written | Default |
|---|---|---|
| `--host TEXT` | `[server].host` | `127.0.0.1` |
| `--port INT` | `[server].port` | `8765` |
| `--db-path PATH` | `[database].db_path` | `~/.archon-search/search` |
| `--log-level LEVEL` | `[logging].level` | `INFO` |
| `--log-to-stderr` | `[logging].log_file = ""` | (default: file logging) |
| `--top-k INT` | `[database].top_k_return` + auto-scale `top_k_retrieve` | `5` / `15` |
| `--telemetry-retention-days INT` | `[telemetry].retention_days` | `30` |

**API key visibility** (no flag required — output change only): at end of install, always print the masked key. The display must respect the key source returned by `load_or_generate_key()`:
- If the key came from `ARCHON_SEARCH_API_KEY` env var: `API key: <first-8>…<last-4>  (source: $ARCHON_SEARCH_API_KEY env var)`
- If the key came from file: `API key: <first-8>…<last-4>  (full key: ~/.archon-search/.search.env)`
- If the key was auto-generated and written to file: `API key: <first-8>…<last-4>  (generated fresh — full key: ~/.archon-search/.search.env)`

Do not print a file path when the key source is the env var.

**Implementation note**: `load_or_generate_key()` returns source as a string — `'env var'`, `'file: {full_path}'`, or `'auto-generated'`. Use `source == 'env var'` for the env var case, `source == 'auto-generated'` for the auto-generated case, and `source.startswith('file:')` for the file case. Do not use exact string matching for the file case.

### Tier 2 — HyDE + RAG Fusion (detection-gated interactive + flags)

**Mutual exclusion note**: Both `hyde.enabled` and `rag_fusion.enabled` are set to `true` in the config. At query time they are mutually exclusive — the caller selects one per request via the `hyde=true` or `rag_fusion=true` request field (`routes_search.py` enforces this: `rag_fusion` wins if both are sent). Enabling both in config means both features are *available*; they do not run simultaneously.

1. At optional-features stage, wizard checks for `ANTHROPIC_API_KEY` in env.
2. If key found: prompt with explanation block:
   ```
   Enable AI query expansion? [y/N]:
     Requires an Anthropic API key (detected in $ANTHROPIC_API_KEY).
     HyDE generates a hypothetical answer to improve recall for vague queries.
     RAG Fusion rewrites the query multiple ways and merges result sets.
     Both features send query text to Anthropic's API — do not enable if queries are sensitive.
     Selected per-query via the API (hyde=true or rag_fusion=true request field); they are
     mutually exclusive at request time. If both are sent in a single request, RAG Fusion
     takes precedence and HyDE is silently skipped.
     Each feature has a configurable rate limit (default: 60 req/min per feature, adjustable
     in archon-search.toml).
   ```
3. If key not found: skip prompt silently; show a post-install hint:
   ```
   Tip: Set $ANTHROPIC_API_KEY to enable AI query expansion (HyDE + RAG Fusion) next run.
   ```
4. If user says yes: enable both `hyde.enabled = true` and `rag_fusion.enabled = true`; check that the `archon-search[hyde,rag_fusion]` extras are installed. Refactor `_install_code_extra()` by extracting a shared `_install_extra(package: str, label: str)` helper that accepts both the pip package specifier and a human-readable label for echo messages. Keep `_install_code_extra()` as a thin wrapper calling `_install_extra("archon-search[code]", "code enrichment")` for backward compatibility. Call `_install_extra("archon-search[hyde,rag_fusion]", "AI query expansion (HyDE + RAG Fusion)")` for the HyDE/RAG Fusion extras installation.
5. Flags: `--enable-hyde`, `--enable-rag-fusion` (refuse with clear error if `ANTHROPIC_API_KEY` is unset: `"Error: --enable-hyde/--enable-rag-fusion requires ANTHROPIC_API_KEY to be set in the environment"`).
6. When `--non-interactive` is set without `--enable-hyde`/`--enable-rag-fusion`, skip the HyDE/RAG Fusion prompt regardless of `ANTHROPIC_API_KEY` presence (consistent with the non-interactive defaults for all other optional features).

### Tier 2 — Custom API key (`--server-key`)

Named `--server-key` (not `--api-key`) to disambiguate from `ANTHROPIC_API_KEY`. The flag sets the archon-search Bearer token used to authenticate API/MCP calls, NOT the Anthropic API key.

1. Flag: `--server-key TEXT` writes the provided key to `~/.archon-search/.search.env` with mode 600, overwriting the auto-generated one.
2. Format constraint: `--server-key` must be a valid lowercase hex string (same regex used by `key_manager._validate_key()`: `^[0-9a-f]+$`). The wizard validates the format at flag-parse time and rejects non-hex values with a clear error message (e.g., `"Error: --server-key must be a lowercase hex string (e.g., generated with: python -c \"import secrets; print(secrets.token_hex(32))\")")`). A value like `sk-abc123` is rejected at parse time and never written to disk.
3. If passed on the CLI, warn: `"Note: your server key may appear in shell history. Consider using ARCHON_SEARCH_API_KEY env var instead."`
4. No interactive prompt — flags-only.
5. The server reads the key once at startup. If the service is already running when `--server-key` is used, print in the success block: `"Server key updated. Restart the service to apply: archon-search restart."`

## In Scope

**Tier 1 (all 8 quick wins):**
- `--host TEXT` (validate at Click layer: reject empty string; accept any non-empty string — full IP/hostname validation is not required since the server will fail to bind on start with a clear OS error)
- `--port INT` (validate 1–65535, mirror `config.py:195-197`)
- `--db-path PATH` (mkdir at install; validate writable; write the path as provided — do NOT expand `~` before writing; `config.py` calls `Path(cfg.db_path).expanduser()` at use sites, so the literal `~` in TOML is correct and preserves portability)
- `--log-level [DEBUG|INFO|WARNING|ERROR|CRITICAL]` (mirror `config.py:316-324` validation)
- `--log-to-stderr` (sets `log_file = ""`; note: `--log-to-stderr` is a boolean flag that writes `[logging].log_file = ""` — the TOML writer for this field differs from the boolean pattern: `if features.log_to_stderr: toml["logging"]["log_file"] = ""`)
- `--top-k INT` (sets `top_k_return`; auto-sets `top_k_retrieve = max(15, 3 * top_k)`; validate 1–100; values above 100 are rejected with a clear error message)
- `--telemetry-retention-days INT` (validate >= 1, mirror `config.py:342-346`; only written if telemetry is enabled)
- Print masked API key + source in the success output block (file path or env var, depending on key source)

**Tier 2:**
- HyDE + RAG Fusion toggle with `ANTHROPIC_API_KEY` detection and extras installation (`archon-search[hyde,rag_fusion]`)
- `--server-key TEXT` custom server key flag (lowercase hex only, minimum 32 hex characters / 16 bytes of entropy). This minimum length is enforced only at the wizard CLI layer. Do NOT add minimum length to `key_manager._validate_key()` — the key manager must accept any valid hex string to preserve compatibility with `ARCHON_SEARCH_API_KEY` env var values.

## Acceptance Criteria

**Tier 1 — all flags:**
- Each new flag writes its value to the expected TOML key when passed explicitly, even if the value matches the default.
- Each flag leaves its TOML key unchanged when not passed (no write if not explicitly provided).
- `--port`: validates 1–65535; rejects 0, 65536, and non-integer values.
- `--log-level`: validates against `[DEBUG|INFO|WARNING|ERROR|CRITICAL]`; rejects arbitrary strings.
- `--top-k`: validates 1–100; rejects 0, 101, and non-integer values. Auto-sets `top_k_retrieve = max(15, 3 * top_k_return)`. When `--top-k N` is passed, both `[database].top_k_return = N` and `[database].top_k_retrieve = max(15, 3 * N)` are present in the written TOML.
- `--telemetry-retention-days`: validates >= 1; not written if `--telemetry` is not enabled; prints warning to stderr when dropped.
- `--host`: rejects empty string; accepts `127.0.0.1`, `0.0.0.0`, `::1`.
- `--db-path`: creates parent directories; validates writable; writes path as-is (no tilde expansion); warns if path differs from existing config's `db_path`.
- `--log-to-stderr`: writes `[logging].log_file = ""` when passed; no write when not passed.

**API key visibility:**
- Success output always includes masked key + correct source label (env var or file).
- "Auto-generated" source shows the file path.

**Tier 2 — HyDE/RAG Fusion:**
- `--enable-hyde` or `--enable-rag-fusion` without `ANTHROPIC_API_KEY` set fails with a clear error message at wizard start.
- `--non-interactive` without `--enable-hyde`/`--enable-rag-fusion` skips the HyDE/RAG Fusion prompt even when `ANTHROPIC_API_KEY` is set.
- When user accepts the HyDE/RAG Fusion prompt: both `hyde.enabled = true` and `rag_fusion.enabled = true` are written to TOML.
- When `archon-search[hyde,rag_fusion]` is not installed and user accepts the prompt: install offer is presented (using `_install_extra("archon-search[hyde,rag_fusion]", "AI query expansion (HyDE + RAG Fusion)")`).

**Tier 2 — `--server-key`:**
- Non-hex input (e.g., `sk-abc123`, empty string) is rejected at parse time with a clear error message.
- `--server-key` of length 31 is rejected; length 32 is accepted.
- Valid lowercase hex input is written to `.search.env` with mode 600.
- Shell history warning is printed when `--server-key` is passed.
- Service restart note is printed when `--server-key` is passed.
- When `ARCHON_SEARCH_API_KEY` is set and `--server-key` is passed: key is written to disk AND warning is printed (not rejected at parse time).

## Out of Scope

- **`--routing-confidence FLOAT`**: routing ensemble knobs interact with each other (`threshold`, `description_weight`, `shortlist_size`). Exposing one in isolation misleads. Defer to a `archon-search tune routing` subcommand.
- **Collection seeding in the wizard**: collections are managed via `archon-search collection` subcommands. Adding collection prompts to the install wizard significantly extends scope; needs its own design.
- **TLS / HTTPS**: not supported in archon-search today. `--host 0.0.0.0` + docs recommending nginx/caddy in front is the right answer.
- **`namespaces` configuration**: multi-tenant; needs a `archon-search namespace add` subcommand, not a wizard prompt.
- **`--server-key-file PATH`**: `ARCHON_SEARCH_KEY_FILE` env var already covers this. No wizard exposure needed.
- **TOML-only knobs** (centroid tuning, fanout params, embedder cache size, language detection threshold, observability headers): too advanced, no useful interactive prompt design; document-only.
- **Individual HyDE/RAG Fusion sub-knobs** (`hyde.model`, `rag_fusion.num_queries`, timeout/rate-limit values): defaults are sane; exposing them in the wizard is feature creep. TOML only.
- **`--db-path` interactive prompt in basic mode**: `--db-path` is flags-only. Adding it to the interactive flow extends wizard length without clear benefit for the majority of users who use the default path.
- **`--top-k` interactive prompt in basic mode**: flags-only for the same reason; 5 results is a conservative default that surprises only power users who will use flags.

## Key Decisions

- **Tier 1 is flags-only, not interactive prompts**: adding 8 more questions to the interactive wizard makes it 50% longer. These are deployment-time knobs, not "which profile do I want?" questions. Flags are the right surface; docs and the "What the wizard does not configure" section in `02_wizard.md` is the discovery path.
- **`--top-k` auto-scales `top_k_retrieve`**: coupling prevents the silent failure mode where `top_k_return > top_k_retrieve`. Formula: `top_k_retrieve = max(15, 3 * top_k_return)`. This matches the relationship already in the config comments. The `top_k_retrieve = max(15, 3 * top_k_return)` coupling is enforced only at wizard write time, NOT enforced by `config.py` at load time. Users who hand-edit `top_k_return` post-install bypass the ratio. This is documented as a manual edit responsibility.
- **HyDE + RAG Fusion as a single interactive decision**: asking "enable HyDE?" and "enable RAG Fusion?" as separate prompts adds friction for a two-feature combo that is almost always toggled together. One prompt; both enabled. Advanced users can disable one via TOML.
- **Skip HyDE/RAG Fusion silently when no API key**: prompting users who don't have an Anthropic key to "go get one" is onboarding noise. Show a post-install hint instead.
- **API key visibility is output-only (no flag required for basic case)**: the auto-generated key already exists; users just don't know where to find it. Printing it in the success block costs nothing and eliminates the most common "where is my API key?" support question.
- **`--telemetry-retention-days` without `--telemetry`**: print a warning — `"Warning: --telemetry-retention-days has no effect because telemetry is not enabled. Pass --telemetry to enable it."` — and drop the value. Silent no-op was considered but a warning is more discoverable and does not break scripted use (it writes to stderr).
- **`--server-key` named to disambiguate from `ANTHROPIC_API_KEY`**: the flag sets the archon-search Bearer token, not the Anthropic key. Using `--api-key` was ambiguous in the presence of both key types in the same wizard session.
- **Explicit CLI flags always win**: when a Tier 1 flag is explicitly passed, write it regardless of whether it matches the default. This allows resetting to default without TOML editing. Use `click.Context.get_parameter_source()` to distinguish `DEFAULT` (not passed) from `COMMAND_LINE` (explicitly passed).

## Edge Cases & Constraints

- **`--port` conflict**: wizard does not detect port conflicts at install time (`config.py` validates range only). If port 8765 is taken, the service start fails after config is written. Not blocking for this brief — document it.
- **`--db-path` pointing inside a source corpus**: warn if the path is a subdirectory of any collection path. Cannot prevent, but should print a warning.
- **`--db-path` on a different filesystem from the default**: mkdir must handle cross-device paths. `Path.mkdir(parents=True, exist_ok=True)` already handles this; no special casing needed.
- **`--telemetry-retention-days` without `--telemetry`**: only write `retention_days` if telemetry is enabled. If `--telemetry-retention-days 7` is passed without `--telemetry`, print: `"Warning: --telemetry-retention-days has no effect because telemetry is not enabled. Pass --telemetry to enable it."` and drop the value.
- **`--server-key TEXT` in shell history**: print the warning regardless of shell; cannot programmatically suppress history. This is a documentation responsibility.
- **HyDE/RAG Fusion extras not installed**: mirror the `_install_code_extra()` pattern. If extras are absent and the user wants HyDE/RAG Fusion, offer to install (`pip install archon-search[hyde,rag_fusion]`); if they decline, skip both features silently.
- **`ANTHROPIC_API_KEY` detected but invalid**: wizard does not validate the key at install time (that would require a live API call). Document: "the key is used at search time; an invalid key will produce errors on first query."
- **`--log-level` + `--log-to-stderr` interaction**: both can be set independently. `log_format = json` + `log_file = ""` is the canonical container combo; the success block should note this when both are set. Note: `--log-format json` already exists as a wizard flag (shipped in a prior release). The addition of `--log-to-stderr` completes the canonical container combo: `archon-search wizard --log-format json --log-to-stderr`.
- **`config.py` validation at load time**: all new wizard-written values must pass the existing `load_config()` validation rules. Port (1–65535), log level (frozenset), retention_days (>=1) are already validated. `host` is not validated by `config.py` today — reject empty string at wizard time with clear error; IPv6 addresses (`::1`, `[::]`) are accepted as-is, with format validation delegated to the OS bind call at server start. `db_path` is not validated by `config.py` today — no silent write of an empty string.
- **`--db-path` changed from previous install**: if a config file already exists with a different `db_path`, print: `"Note: changing db_path from <old> to <new>. Existing data at <old> will NOT be moved. To migrate, stop the service and copy the directory manually."`
- **`--db-path` tilde expansion**: write the path as provided — do NOT expand `~` before writing. `config.py` calls `Path(cfg.db_path).expanduser()` at use sites, so the literal `~` in TOML is correct and preserves portability.
- **`--top-k` ceiling**: reject values > 100 at the CLI layer with message `"top_k > 100 is likely to cause performance problems; edit archon-search.toml directly if you need a higher value."` Note: the 100-ceiling is enforced only at the wizard layer. `config.py` validates `top_k_return > 0` with no upper bound. Users who hand-edit `top_k_return > 100` in TOML will not be stopped — the error message ('edit archon-search.toml directly if you need a higher value') is intentional documentation of this escape hatch. Boundary test cases: `--top-k 0` → error; `--top-k 1` → valid, `top_k_retrieve = 15`; `--top-k 5` → valid (default), `top_k_retrieve = 15`; `--top-k 33` → valid, `top_k_retrieve = 99`; `--top-k 100` → valid, `top_k_retrieve = 300`; `--top-k 101` → error.
- **`--host` validation**: reject empty string at wizard time with clear error. IPv6 addresses (`::1`, `[::]`) are accepted as-is; the format validation responsibility belongs to the OS bind call at server start.
- **Idempotency on re-run**: use `click.Context.get_parameter_source()` to distinguish explicitly-passed flags from Click defaults. If a flag is explicitly passed, always write it to TOML (even if it matches the default — this allows resetting a value back to default). If a flag is not passed at all (not present in the CLI invocation), do not write it (preserving any existing custom value). This means `--port 8765` explicitly resets a prior `port = 9000` back to 8765, while running the wizard without `--port` leaves the existing `port = 9000` intact. Document this explicitly in `02_wizard.md`.
- **`--server-key` hex-only validation**: `key_manager._validate_key()` uses `_HEX_RE = re.compile(r"^[0-9a-f]+$")`. The wizard validates `--server-key` at flag-parse time and rejects any non-lowercase-hex value immediately with a clear error message, before writing to disk. Minimum key length: 32 hex characters (16 bytes of entropy, same as `secrets.token_hex(16)`). Keys shorter than 32 chars are rejected with: `"Error: --server-key must be at least 32 hex characters for adequate security."` This minimum length is enforced only at the wizard CLI layer. Do NOT add minimum length to `key_manager._validate_key()` — the key manager must accept any valid hex string to preserve compatibility with `ARCHON_SEARCH_API_KEY` env var values.
- **`--server-key` with `ARCHON_SEARCH_API_KEY` env var set**: `key_manager` reads the env var first and the file is never consulted. If `ARCHON_SEARCH_API_KEY` is set when `--server-key` is passed, the key IS written to disk (mode 600) regardless; the warning is printed AFTER writing, not as a pre-write rejection. This ensures the key is available if the env var is later removed. Warning text: `"Warning: ARCHON_SEARCH_API_KEY env var is set and takes priority over the key file. Your --server-key value was written to disk but will not be used while ARCHON_SEARCH_API_KEY is set."`

## Open Questions

- **Should `--host 0.0.0.0` trigger a security note in the summary?** Binding to all interfaces without TLS is a real risk in shared environments. Options: always print a reminder when host != 127.0.0.1; or only when the server is reachable externally (hard to detect). A single-line note in the summary is the safest, lowest-friction choice.
- **`--top-k` interactive prompt placement**: flags-only keeps the wizard short, but power users who want more than 5 results won't know to use the flag. Should `top_k_return` appear as the *last* optional-features prompt (after routing strategy, before summary), shown only with its explanation? Or remain flags-only?
- **HyDE + RAG Fusion as one toggle vs two separate prompts**: the brief recommends a single combined prompt. If there is a use case for HyDE-without-RAG-Fusion (or vice versa), two prompts are needed. What do the majority of users want?
- **`--enable-hyde`/`--enable-rag-fusion` flags when `ANTHROPIC_API_KEY` is absent**: ~~should the wizard fail with a clear error, or proceed and write the config anyway?~~ **RESOLVED**: refuse with clear error if `ANTHROPIC_API_KEY` is unset. See Tier 2 step 5.
- **Masked key format in success output**: `<first-8>…<last-4>` is the current proposal. Is this enough to identify the right key without exposing it? Some users may want full key printed once (the `fly.io` / `railway` pattern — shown once, copy it now). The brief recommends masked; this is worth confirming.
- **`--log-to-stderr` interactive prompt**: should it be shown automatically when `--log-format json` is selected ("Log to stderr only for container use? [y/N]"), or always flags-only? Auto-suggesting it when json is selected would help container users discover the option without reading the docs.
- **HyDE/RAG Fusion extras package names**: ~~the brief assumes `archon-search[hyde]` as the extras name~~ **RESOLVED**: both extras are `[hyde]` and `[rag_fusion]` in `pyproject.toml`; install as `archon-search[hyde,rag_fusion]`.
- **`--telemetry-retention-days` with no active telemetry**: ~~if the user passes `--telemetry-retention-days 7` but did not pass `--telemetry`, should it silently no-op, warn, or error?~~ **RESOLVED**: print a warning (see Key Decisions and Edge Cases).
- **`db_path` validation — writable check at wizard time vs service-start time**: writing to a path that becomes unmounted after install would be a bad experience. Should the wizard validate the path is writable AND on a local filesystem, or just validate writability and let service start catch the rest?

## Future Iterations

- **`archon-search tune routing` subcommand**: expose `routing_confidence_threshold`, `routing_description_weight`, `max_parallel_collections` as a dedicated tuning command for users with multi-collection setups — not a wizard prompt.
- **`archon-search namespace add` subcommand**: multi-tenant namespace configuration deserves its own command.
- **`--preset server`, `--preset container`, `--preset claude-desktop`**: presets could bundle `--host 0.0.0.0 --log-format json --log-to-stderr` as a single `--preset container` flag, eliminating the need to remember individual flags for common deployment patterns.
- **Collection seeding in the wizard**: "Do you have a directory to index now?" as the final optional step, wiring into `archon-search collection add`. Needs separate design; would significantly extend wizard length.
- **API key rotation command**: `archon-search key rotate` — regenerates `~/.archon-search/.search.env`, restarts service. Outside wizard scope but related to the API key visibility work here.

## Recommendation

Tier 1 is straightforward execution work: the 7 new flags follow the existing `WizardFeatures` + `_apply_wizard_features_to_toml` + Click decorator pattern established in C8 (the API key visibility change is output-only). The implementation risk is low. Ship `--host`, `--port`, `--db-path`, and the API key visibility change first — those close the most common "I installed it and now I can't reach it / find my key" failure mode. Tier 2 (HyDE/RAG Fusion) is higher-value but higher-complexity: the `ANTHROPIC_API_KEY` detection + extras installation flow needs careful design to avoid confusing users who don't have a key. The hardest part of this entire brief is the HyDE/RAG Fusion prompt — getting the gating logic, extras check, and data-egress warning right without making the happy path awkward for the majority of users who won't need it. Do Tier 1 first; gate Tier 2 on HyDE shipping (C4 in the roadmap).
