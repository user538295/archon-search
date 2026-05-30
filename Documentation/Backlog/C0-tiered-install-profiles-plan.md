# C1 — Tiered Install Profiles

**Purpose**: Present first-time users with a clear profile selection (Minimal / Balanced / Max) at install time, pre-warm model files before the first query, and guard against accidental index corruption on reinstall with different models.
**Audience**: archon-search contributors implementing C1; reviewers of the resulting PRs.
**Status**: Draft

---

## Background

`archon-search install` currently creates a config with hard-coded defaults and downloads model files silently on first query. Users have no visibility into which models will run, what quality/speed tradeoff they are making, or what hardware is required. The default model (`BAAI/bge-small-en-v1.5`) is the fastest/smallest option but is never explained as such. Switching models later requires manual config editing followed by a full re-index, which is undiscoverable.

The brief at `Documentation/Backlog/C1-tiered-install-profiles-brief.md` resolved the key decisions around profile names, model selection, CLI flags, config write path, pre-warm mechanics, reinstall guard, and the `install_cmd.py` → `SearchInstaller` consolidation.

**Note**: The P0 bug (`config.py:35` default reranker model name) was already fixed in commit `83935d6`.

---

## Goal

After C1 ships: `archon-search install` either prompts the user to choose a profile (interactive) or accepts `--profile minimal|balanced|max` and `--multilingual` flags (scripted). Before any files are written or daemons started, the user sees exactly which models will be downloaded and what hardware the choice requires. Model files are downloaded during install with a progress display. The server starts with cached model files, eliminating the silent first-query latency. A reinstall guard prevents silent index corruption when switching models. All install logic lives in `SearchInstaller`; `install_cmd.py` is a thin Click shim.

---

## Scope

### In Scope
- `archon_search/profiles.py` — profile registry (data model + English/multilingual profile definitions)
- `profile: str` and `multilingual: bool` added to `SearchConfig` and `load_config()`
- Durable profile config writer using `_durable_io.atomic_write_bytes` + fix `configure_providers()` to use the same
- Advisory install lock (`~/.archon-search/.install.lock`, PID-based stale-lock detection)
- Disk space check before model download
- Model pre-warm via `TextEmbedding(model, lazy_load=True)` + `TextCrossEncoder(model, lazy_load=True)`
- Reinstall guard: detect conflicting `embedding_model` or `chunk_size`; require `--force --delete-db` to override
- `--force --delete-db` 5-step rollback function (with steps 6–7 handled by the `run()` caller) with config backup and restore-on-failure
- Jina CC-BY-NC-4.0 license gate for multilingual profiles 2 and 3
- Profile table display with narrow-terminal fallback; summary confirmation screen
- Interactive profile selection + non-interactive path (`--profile`, `--multilingual`, `--skip-preload` flags)
- `--non-interactive` defaults to `minimal` (English) when `--profile` is omitted
- `SearchInstaller.run()` rewrite incorporating all of the above
- `install_cmd.py` consolidation: thin Click shim; `uninstall` stays in `install_cmd.py`
- `_profile_toml(profile_name, multilingual)` factory superseding the bare `_default_toml()` path for fresh installs

### Out of Scope
- Runtime profile switching without reinstall
- Per-collection model overrides
- Profile migration command (background re-embedding job)
- GPU provider selection by user at install time (auto-detection unchanged)
- Chunk size as a separate install-time option
- ColBERT / SPLADE tiers
- Windows service management

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 4.1 — Final verification & documentation update].

---

## What does NOT change
- REST/MCP API contract — no new endpoints, no schema changes
- `uninstall` command location (`install_cmd.py`)
- `save_config()` behavior (separate tech-debt item)
- LanceDB schema logic (schema is recreated by the normal store init path when DB is deleted)
- `--cov-fail-under=85` threshold
- `BREAKING.md` — no breaking changes

---

## Known limitations / accepted trade-offs
- Pre-warm progress is tqdm/HF progress bars on stderr (option a from the brief) — not a custom rich display; acceptable for v1.
- ONNX session initialization still occurs server-side on first query (~5–15s for large models). This is explained to the user in the summary screen.
- Crash-injection tests for `--force --delete-db` rollback are not included in this plan — the rollback relies on the already-tested `_durable_io` primitives.
- Timeout formula uses a 100 KB/s floor; actual download speed varies. Timeout fires with a warning and the service starts without a cached model — same as today.
- **Pre-warm timeout is non-preemptive**: the cancellation flag is checked between the embedder and reranker download phases, but not during an individual download call. If the embedder download hangs (network issue, DNS failure), the timeout fires but the function continues blocking until the download completes or fails. This is a limitation of the `threading.Timer` + flag design and is accepted for v1.
- Jina license prompt is required but the check is process-level only (no cryptographic assurance); downstream commercial misuse is not archon-search's enforcement problem.
- `_default_toml()` in `config_cmd.py` remains for the `config` subcommand; only the `install` path switches to `_profile_toml()`.
- `--force --delete-db` always deletes the database when both flags are provided, even if the new profile is identical to the existing one. This is by design — the flags express an explicit destructive intent. The confirmation prompt (in interactive mode) provides the final safety gate.

---

## Architecture

### New module
`archon_search/profiles.py`:
```python
@dataclass
class InstallProfile:
    name: str               # "minimal" | "balanced" | "max"
    embedder: str           # fastembed model name
    reranker: str | None    # fastembed cross-encoder name; None for Minimal multilingual
    chunk_size: int         # tokens
    download_mb: int        # approximate total download
    quality_stars: str      # e.g. "★★☆☆☆"
    cpu_ms: int             # approximate p50 latency ms on CPU
    metal_ms: int           # approximate p50 latency ms on Apple Silicon
    memory_gb: float        # approximate RAM required

ENGLISH_PROFILES: dict[str, InstallProfile]   # keys: "minimal", "balanced", "max"
MULTILINGUAL_PROFILES: dict[str, InstallProfile]

JINA_RERANKER_MODEL = "jinaai/jina-reranker-v2-base-multilingual"

def get_profile(name: str, multilingual: bool) -> InstallProfile: ...
```

### Config additions (existing `archon_search/config.py`)
```python
@dataclass
class SearchConfig:
    ...
    profile: str = ""           # empty string means "unset / legacy install"
    multilingual: bool = False
```
Persisted in `[database]` section. Read by `load_config()`. Written only by `_write_profile_config()` (install path), never by `save_config()`.

### New helpers in `archon_search/install.py`
- `_profile_toml(profile_name: str, multilingual: bool) -> str` — full TOML string for a fresh config
- `_write_profile_config(config_path: Path, profile: InstallProfile, profile_name: str, multilingual: bool) -> None` — updates `[database]` section in existing or new TOML durably
- `_acquire_install_lock() -> contextlib.AbstractContextManager` — PID-based advisory lock
- `_check_disk_space(profile: InstallProfile) -> None` — raises `InstallError` if insufficient
- `_prewarm_models(profile: InstallProfile, timeout: int) -> None` — lazy_load download with timeout
- `_check_reinstall_guard(existing_cfg: SearchConfig, new_profile: InstallProfile) -> None` — raises `NeedsForceDeleteError` on conflict
- `_execute_force_reinstall(config_path, db_path, profile, profile_name, multilingual, non_interactive, dry_run=False)` — 5-step rollback function (steps 6–7 delegated to `run()` caller)
- `_render_profile_table(multilingual: bool, width: int) -> str` — profile table or narrow fallback
- `_render_summary(profile_name, profile, multilingual, providers) -> str`
- `_maybe_prompt_jina_license(profile, multilingual, non_interactive) -> None` — raises `SystemExit(1)` on decline
- `_select_profile(profile_flag, multilingual_flag, non_interactive) -> tuple[str, bool]`

### Config write path
All profile writes use `_durable_io.atomic_write_bytes(path, tomlkit.dumps(doc).encode())`.
`configure_providers()` in `SearchInstaller` is patched to use the same.

### CLI additions (existing `archon_search/cli/install_cmd.py`)
New flags on the `install` Click command:
- `--profile [minimal|balanced|max]`
- `--multilingual / --no-multilingual`
- `--skip-preload`
- `--force`
- `--delete-db`

`install_cmd.py`'s `install` command body becomes:
```python
# NOTE: SearchInstaller.__init__ takes config_file: str | None, not config_path: Path.
# Pass config_file=str(config_path) if config_path else None,
# OR rename config_file -> config_path (type Path | str | None) throughout install.py.
SearchInstaller(config_file=str(config_path) if config_path else None, dry_run=dry_run).run(
    profile=profile, multilingual=multilingual,
    non_interactive=non_interactive, skip_preload=skip_preload,
    force=force, delete_db=delete_db,
    accept_jina_license=accept_jina_license,
)
```

---

## Task breakdown

### Phase 1 — Config & Profile Foundation
> **Releasable**: after this phase, the profile registry and config fields are in place and independently testable; no user-visible behavior change yet.

#### Task 1.1 — Profile registry
- [x] **File**: `archon_search/profiles.py` (new file)
- **Depends on**: nothing
- **Description**:
  - `@dataclass InstallProfile(name, embedder, reranker, chunk_size, download_mb, quality_stars, cpu_ms, metal_ms, memory_gb)` — all fields required; `reranker: str | None` (None = Minimal multilingual, no reranker)
  - `ENGLISH_PROFILES: dict[str, InstallProfile]` with keys `"minimal"`, `"balanced"`, `"max"`:
    - minimal: embedder=`BAAI/bge-small-en-v1.5`, reranker=`Xenova/ms-marco-MiniLM-L-6-v2`, chunk_size=512, download_mb=147, cpu_ms=40, metal_ms=15, memory_gb=0.5
    - balanced: embedder=`BAAI/bge-base-en-v1.5`, reranker=`Xenova/ms-marco-MiniLM-L-12-v2`, chunk_size=512, download_mb=330, cpu_ms=150, metal_ms=50, memory_gb=1.0
    - max: embedder=`BAAI/bge-large-en-v1.5`, reranker=`BAAI/bge-reranker-base`, chunk_size=1024, download_mb=2300, cpu_ms=400, metal_ms=130, memory_gb=2.5
  - `MULTILINGUAL_PROFILES: dict[str, InstallProfile]` with keys `"minimal"`, `"balanced"`, `"max"`:
    - minimal: embedder=`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, reranker=None, chunk_size=512, download_mb=220
    - balanced: embedder=`sentence-transformers/paraphrase-multilingual-mpnet-base-v2`, reranker=`jinaai/jina-reranker-v2-base-multilingual`, chunk_size=512, download_mb=2110
    - max: embedder=`intfloat/multilingual-e5-large`, reranker=`jinaai/jina-reranker-v2-base-multilingual`, chunk_size=1024, download_mb=3350
  - `JINA_RERANKER_MODEL = "jinaai/jina-reranker-v2-base-multilingual"` constant
  - `VALID_PROFILE_NAMES: frozenset[str] = frozenset({"minimal", "balanced", "max"})`
  - `get_profile(name: str, multilingual: bool) -> InstallProfile` — raises `ValueError` if `name` not in `VALID_PROFILE_NAMES`
  - **Releasable**: after this task, `get_profile()` is callable and returns typed profile data.
- **Tests (TDD)** — `tests/test_profiles.py`:
  - Unit: `test_get_profile_english_minimal` — returns correct embedder, reranker, chunk_size
  - Unit: `test_get_profile_multilingual_max` — returns correct multilingual model names
  - Unit: `test_get_profile_minimal_multilingual_has_no_reranker` — `reranker` is `None`
  - Unit: `test_get_profile_invalid_name_raises_valueerror` — `get_profile("ultra", False)` raises `ValueError`
  - Unit: `test_all_english_profiles_have_reranker` — all English profiles have non-None reranker
  - Unit: `test_multilingual_balanced_and_max_use_jina_reranker` — reranker == `JINA_RERANKER_MODEL`
  - Checkpoint: `pytest tests/test_profiles.py -v`

---

#### Task 1.2 — `SearchConfig` profile fields + `load_config()` extension
- [x] **File**: `archon_search/config.py`
- **Depends on**: nothing (independent of Task 1.1)
- **Description**:
  - Add `profile: str = ""` and `multilingual: bool = False` to `SearchConfig` dataclass, after the existing `[database]` fields.
  - In `load_config()`, inside the `database = doc.get("database", {})` block, add:
    ```python
    if "profile" in database:
        config.profile = str(database["profile"])
    if "multilingual" in database:
        config.multilingual = _coerce_bool(database["multilingual"], "multilingual")
    ```
  - No changes to `save_config()` — it only writes `collections` and `pinned_collections`.
  - **Releasable**: after this task, `load_config()` surfaces `profile` and `multilingual` from existing TOML files.
- **Tests (TDD)** — `tests/test_config.py` (extend existing file):
  - Unit: `test_load_config_profile_and_multilingual_defaults` — missing keys → `profile=""`, `multilingual=False`
  - Unit: `test_load_config_reads_profile_balanced` — TOML with `profile = "balanced"` → `config.profile == "balanced"`
  - Unit: `test_load_config_reads_multilingual_true` — TOML with `multilingual = true` → `config.multilingual is True`
  - Unit: `test_load_config_multilingual_wrong_type_raises` — `multilingual = "yes"` raises `ConfigError`
  - Unit: `test_save_config_round_trip_preserves_profile_and_multilingual` — write a config with `profile = "balanced"` and `multilingual = true` via `_write_profile_config`; call `save_config()`; call `load_config()`; assert `profile == "balanced"` and `multilingual == True`. (Guards against a future `save_config()` refactor silently dropping these fields.)
  - Checkpoint: `pytest tests/test_config.py -v`

---

#### Task 1.3 — Durable profile config writer + `configure_providers()` durable-write fix
- [x] **File**: `archon_search/install.py`
- **Depends on**: Task 1.1 (InstallProfile type), Task 1.2 (config fields exist)
- **Description**:
  - Add `_write_profile_config(config_path: Path, profile: InstallProfile, profile_name: str, multilingual: bool) -> None`:
    - If `config_path` exists, parse with `tomlkit.parse(config_path.read_text())`; else create `tomlkit.document()`.
    - Ensure `[database]` section exists.
    - Write these keys into `[database]`: `embedding_model`, `reranker_model` (skipped / set to `""` if `profile.reranker is None`), `chunk_size`, `profile`, `multilingual`.
    - Persist with `_durable_io.atomic_write_bytes(config_path, tomlkit.dumps(doc).encode())`.
    - **IMPORTANT**: this function MUST use tomlkit round-trip editing (parse-modify-serialize), NOT a raw TOML string write. The `[database]` section is updated in-place; all other sections (`[server]`, `[logging]`, `[telemetry]`, `[collections]`) must be preserved unchanged.
  - Add `_profile_toml(profile_name: str, multilingual: bool) -> str`:
    - Returns a minimal valid TOML string that includes the `[database]` section with the profile's model keys and `[server]` defaults. Used for fresh installs (replaces bare `_default_toml()` call in the install path).
    - Delegates model selection to `get_profile(profile_name, multilingual)`.
    - **`_profile_toml()` MUST generate all config sections that `_default_toml()` generates** (not just `[database]` and `[server]`). To prevent drift from `_default_toml()` in `config_cmd.py`, `_profile_toml()` should either (a) call `_default_toml()` and use tomlkit to overlay the profile-specific `[database]` values, or (b) share a common config-generation helper. The implementation choice must be documented in a comment.
  - Fix `configure_providers()`: replace `config_path.write_text(tomlkit.dumps(doc))  # noqa: durable-write` with `_durable_io.atomic_write_bytes(config_path, tomlkit.dumps(doc).encode())` and remove the `# noqa` comment.
  - **Releasable**: after this task, profile config can be written durably and `configure_providers()` no longer bypasses the fsync contract.
- **Tests (TDD)** — `tests/test_install_config_writer.py` (new file):
  - Unit: `test_write_profile_config_fresh_file` — creates file; `load_config()` returns correct embedding_model, chunk_size, profile, multilingual
  - Unit: `test_write_profile_config_updates_existing` — existing TOML with other sections preserved; only `[database]` updated
  - Unit: `test_write_profile_config_preserves_server_and_logging_sections` — create a TOML file with `[server]`, `[logging]`, `[telemetry]`, and `[collections]` sections alongside `[database]`; call `_write_profile_config()`; parse result; assert all non-`[database]` sections are unchanged (no key removed or mutated)
  - Unit: `test_write_profile_config_no_reranker_writes_empty_string` — multilingual minimal sets `reranker_model = ""`
  - Unit: `test_write_profile_config_is_atomic` — file has correct content even if process is interrupted after write (use `atomic_write_bytes` contract)
  - Unit: `test_write_profile_config_cleans_stale_tmp_before_write` — create a stale `<config_path>.tmp` file; verify write succeeds (stale tmp removed before calling `atomic_write_bytes`)
  - Unit: `test_profile_toml_fresh_minimal` — `_profile_toml("minimal", False)` → write to temp file → call `load_config(temp_file)` → assert `config.embedding_model == "BAAI/bge-small-en-v1.5"`, `config.profile == "minimal"`, `config.multilingual == False`
  - Unit: `test_profile_toml_fresh_max_multilingual` — `_profile_toml("max", True)` → write to temp file → call `load_config(temp_file)` → assert correct multilingual max embedder, `config.profile == "max"`, `config.multilingual == True`
  - Unit: `test_configure_providers_uses_durable_write` — mock `_durable_io.atomic_write_bytes`; assert it is called (not `write_text`)
  - Unit: `test_configure_providers_tests_use_atomic_write_bytes_mock` — note: update existing `TestConfigureProviders` tests in `tests/test_install.py` to mock `atomic_write_bytes` instead of `write_text` (tests that assert on file content remain valid; only tests that mock `write_text` directly need updating)
  - Checkpoint: `pytest tests/test_install_config_writer.py -v`

> **Implementation note for `_write_profile_config`**: Before calling `atomic_write_bytes`, check for and remove any stale `<config_path>.tmp` file. This is safe because `atomic_write_bytes` only creates a sibling `.tmp` as a staging file. A stale `.tmp` from a prior crash would otherwise cause `FileExistsError` on the next run.

---

#### Task 1.4 — Runtime support for optional reranker
- [x] **Files**: `archon_search/pipeline.py`, `archon_search/server/app.py`, `archon_search/config.py`
- **Depends on**: Task 1.2 (SearchConfig)
- **Description**:
  - **Problem**: `reranker_model = ""` is written when `profile.reranker is None` (Multilingual Minimal). The current `SearchPipeline.__init__` declares `reranker: Reranker` as non-optional, and both `search()` and `explain()` call `self._reranker.rerank()` / `self._reranker._rerank_with_trace()` unconditionally. An empty string causes `AttributeError` on the first query.
  - In `config.py`: clarify that `SearchConfig.reranker_model: str` accepts `""` as a sentinel meaning "no reranker". The existing default `"Xenova/ms-marco-MiniLM-L-6-v2"` is unchanged for all non-empty cases.
  - In `pipeline.py` — make `reranker` optional in `SearchPipeline`. The following `self._reranker.*` access sites ALL require guarding (enumerate and guard every one):
    - Change `SearchPipeline.__init__` signature: `reranker: Reranker` → `reranker: Reranker | None = None`.
    - In `SearchPipeline.search()`: add `if self._reranker is not None:` guard around the `self._reranker.rerank()` call. When `reranker` is `None`, apply `top_k_return` trimming manually (`candidates = candidates[:self._top_k_return]`) and return the RRF-sorted candidates as-is. The score for each result uses the existing `reranker_score if reranker_score is not None else rrf_score` fallback logic in `_candidate_to_search_result()`. API response fields `reranker_score` and `reranker_trace` must be `null` / omitted when reranker is `None`.
    - In `SearchPipeline.search_many()` (multi-collection path): add the same `if self._reranker is not None:` guard around any `self._reranker.*` calls. When `reranker` is `None`, return candidates without a reranking pass. Set `rerank_time_ms = 0.0` in `FanoutTimings` (no time is spent in reranking when the reranker is absent).
    - In `_candidate_to_search_result()`: remove the `assert score is not None` at line 701. Replace with: use `reranker_score` if not None, else fall back to `rrf_score`. This method is called from `search_many()` and must not assert when reranker is absent.
    - In `SearchPipeline.explain()`: add `if self._reranker is not None:` guard around the `self._reranker._rerank_with_trace()` call. When `reranker` is `None`, set `reranker_trace = None` in the explain output (omit the reranker section entirely rather than crashing). This applies to both single-collection and multi-collection `explain()` paths. For multi-collection `explain()` with `rerank=True` and `reranker=None`: treat as equivalent to `rerank=False` — skip the reranker call, sort by RRF score across collections, and set `reranker_trace=None`. Do NOT raise `ExplainMultiCollectionNoRerankError`; that exception is reserved for when the user explicitly passes `rerank=False`, not for when the pipeline has no reranker.
    - In `SearchPipeline.reranker_is_warm` property (called by the readiness endpoint): guard against `self._reranker is None` — return `False` when reranker is `None` instead of raising `AttributeError`.
    - In `create_pipeline()`: if `cfg.reranker_model == ""`, skip `ModelReranker` and `Reranker` construction entirely; pass `reranker=None` to `SearchPipeline`.
    - **Eval harness impact (accepted limitation)**: `eval/_tracing.py` directly accesses `pipeline._reranker` (line 93) and calls `reranker.rerank_candidates()` (line 107). This will raise `AttributeError` if the eval suite is run against a no-reranker profile (Multilingual Minimal). Add a guard to `eval/_tracing.py`: before line 93, check `if pipeline._reranker is None: raise RuntimeError('Eval harness requires a reranker; cannot run with Multilingual Minimal profile.')`. This converts a confusing `AttributeError` into a clear error message. The eval harness is designed for reranker-equipped pipelines and must not be run with `reranker=None` in v1.
  - In `app.py` — address BOTH construction sites:
    - `create_pipeline()` in `pipeline.py` handles the `cfg.reranker_model == ""` case (above).
    - The inline construction in `app.py:create_app()` (where `Reranker(ModelReranker(config.reranker_model, ...))` is currently constructed unconditionally): if `config.reranker_model == ""`, do NOT construct `ModelReranker` or `Reranker`; pass `reranker=None` directly.
  - **Releasable**: after this task, installing with a profile that has `reranker=None` (Multilingual Minimal) does not crash on first search query or explain call.
- **Tests (TDD)** — `tests/test_pipeline_optional_reranker.py` (new file):
  - Unit: `test_pipeline_skips_reranker_when_model_is_empty` — `SearchConfig` with `reranker_model=""` → `create_pipeline()` returns pipeline with `reranker=None`; no `ModelReranker` instantiated
  - Unit: `test_app_skips_reranker_when_config_model_empty` — mock `config.reranker_model = ""`; assert `ModelReranker` is NOT constructed during app startup
  - Unit: `test_search_without_reranker_returns_results` — Construct a `SearchPipeline` directly with `reranker=None`; mock the store's `hybrid_search` to return candidate results; call `pipeline.search(query='test')` directly; assert results list is returned without `AttributeError` (verifies the `if self._reranker is not None:` guard in `search()`); also assert `reranker_score` and `reranker_trace` fields are `None`/omitted in results; assert `len(results) <= top_k_return`
  - Unit: `test_candidate_to_search_result_uses_rrf_score_when_reranker_none` — construct a `ScoredSearchCandidate` with `reranker_score=None` and `rrf_score=0.75`; call `_candidate_to_search_result()`; assert `result.score == 0.75` (the assert at line 701 has been replaced with a fallback)
  - Unit: `test_search_many_without_reranker_returns_results` — Construct a `SearchPipeline` directly with `reranker=None`; mock the store's multi-collection search path to return candidate results; call `pipeline.search_many(query='test', collections=['col1','col2'])` directly; assert results list is returned without `AttributeError`
  - Unit: `test_explain_without_reranker_returns_results` — Construct a `SearchPipeline` directly with `reranker=None`; call `pipeline.explain(query='test', collection='col1', rerank=True)` (correct signature: no `doc_id` parameter; use `rerank=True` to exercise the guard path); assert a valid explain response is returned with `reranker_trace=None` and no `AttributeError` (verifies the `if self._reranker is not None:` guard in `explain()`)
  - Unit: `test_explain_multi_collection_without_reranker` — Construct a `SearchPipeline` directly with `reranker=None`; call `pipeline.explain(query='test', collections=['col1','col2'], rerank=True)`; assert no `AttributeError`, `reranker_trace=None`
  - Unit: `test_pipeline_reranker_is_warm_returns_false_when_none` — Construct a `SearchPipeline` with `reranker=None`; assert `pipeline.reranker_is_warm is False` (no `AttributeError`; readiness endpoint safe)
  - Checkpoint: `pytest tests/test_pipeline_optional_reranker.py -v`

---

### Phase 2 — Install Guards & Utilities
> **Releasable**: after this phase, all safety gates (lock, disk space, reinstall guard, force-delete rollback, pre-warm) are independently testable functions; still no user-visible UX change.

#### Task 2.1 — Advisory install lock
- [x] **File**: `archon_search/install.py`
- **Depends on**: nothing (independent utility)
- **Description**:
  - `_install_lock_path() -> Path` returns `Path.home() / ".archon-search" / ".install.lock"`.
  - `class InstallLockError(Exception)`: raised when lock is already held.
  - `@contextlib.contextmanager _acquire_install_lock() -> Iterator[None]`:
    - On entry: use `os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)` to create the lock file atomically (`O_EXCL` guarantees create-or-fail; no TOCTOU window). Write `"<pid>:<timestamp>"` (e.g., `f"{os.getpid()}:{int(time.time())}"`) to the lock file. The timestamp mitigates PID reuse on busy systems.
    - If `os.open` raises `FileExistsError`: the lock file already exists. Read its contents and parse the PID from `<pid>:<timestamp>` format. If PID parsing fails (`ValueError` or malformed content), treat as stale — unlink the file and retry `O_EXCL` creation.
    - Stale lock detection (after `FileExistsError`): check if the recorded PID is still alive using a platform-safe check:
      - **POSIX** (`sys.platform != "win32"`): use `os.kill(pid, 0)`:
        - If it raises `ProcessLookupError` (ESRCH): the process is dead — treat as stale.
        - If it raises `PermissionError` (EPERM): the process **exists** but is owned by a different user — treat as **live** and raise `InstallLockError`. Do NOT treat EPERM as stale.
        - If it returns `None`: the process is alive — raise `InstallLockError`.
      - **Windows** (`sys.platform == "win32"`): use `psutil.pid_exists(pid)` — **do NOT use `os.kill` on Windows**, as it calls `TerminateProcess` even for signal 0 and will kill the target process.
    - If PID is dead: unlink the stale lock file and retry `O_EXCL` creation **at most once**. If the second `O_EXCL` attempt fails (another concurrent process took the lock between the unlink and the retry), raise `InstallLockError` immediately — do not retry again. If PID is alive (or EPERM on POSIX): raise `InstallLockError("Install already in progress.")`.
    - On exit (finally): remove lock file.
  - **`psutil` dependency**: add `psutil >= 5.0; sys_platform == "win32"` as a platform-conditional dependency in `pyproject.toml`. Alternatively, use `try/except ImportError` with a fallback: if `psutil` is unavailable on Windows, treat any lock file as stale (conservative: always proceed rather than risk failing to detect a live process). **Never use `os.kill(pid, sig)` on Windows for liveness checks** — it calls `TerminateProcess` even for signal 0.
  - **Releasable**: after this task, concurrent install protection is in place.
- **Tests (TDD)** — `tests/test_install_lock.py` (new file):
  - Unit: `test_lock_creates_pid_file` — lock file exists and contains current PID (with timestamp) during context
  - Unit: `test_lock_removes_file_on_exit` — lock file absent after context exits normally
  - Unit: `test_lock_removes_file_on_exception` — lock file absent after context raises
  - Unit: `test_lock_raises_if_live_pid_holds_lock` — write a live PID (current PID) to lock file; entering context raises `InstallLockError`
  - Unit: `test_lock_removes_stale_dead_pid_and_proceeds` — write a dead PID to lock file; entering context succeeds and overwrites the file; also covers the scenario where two concurrent processes race: mock `os.open` to succeed on the first stale-check unlink, then raise `FileExistsError` on the single retry attempt; assert `InstallLockError` is raised (not an infinite loop)
  - Unit: `test_lock_treats_permission_error_from_kill_as_live_process` — write a valid-format PID to lock file; mock `os.kill(pid, 0)` to raise `PermissionError`; entering context raises `InstallLockError` (process is alive but owned by another user)
  - Unit: `test_lock_uses_o_excl_for_atomic_creation` — mock `os.open`; verify `O_EXCL | O_CREAT` flags used; confirm no separate read-then-write sequence (no TOCTOU)
  - Unit: `test_lock_uses_platform_safe_pid_check` — mock `sys.platform` as `"win32"`; verify `os.kill` is NOT called; verify `psutil.pid_exists` IS called
  - Unit: `test_lock_handles_corrupted_pid_file` — write `"not-a-pid"` to lock file; entering context treats it as stale and succeeds (no `ValueError` raised, no `InstallLockError`)
  - Unit: `test_lock_concurrent_acquisition_blocks_second_caller` — use `threading.Thread` to hold lock in one thread; assert that a second concurrent `_acquire_install_lock()` call in another thread raises `InstallLockError`
  - Checkpoint: `pytest tests/test_install_lock.py -v`

---

#### Task 2.2 — Disk space check
- [x] **File**: `archon_search/install.py`
- **Depends on**: Task 1.1 (InstallProfile)
- **Description**:
  - `class InstallError(Exception)`: generic install-abort error (add near top of `install.py` if not already present).
  - `_check_disk_space(profile: InstallProfile, base_path: Path | None = None) -> None`:
    - `base_path` defaults to `Path.home() / ".archon-search"` (where fastembed caches models).
    - Walk up the path hierarchy to find an existing ancestor: `check_path = base_path; while not check_path.exists(): check_path = check_path.parent`. Use `shutil.disk_usage(check_path)`. (Using `.parent` alone is insufficient: on a first install neither `~/.archon-search` nor its parent may exist in edge cases.)
    - Required bytes = `profile.download_mb * 1024 * 1024 * 2` (2× for partial download + decompression).
    - If `usage.free < required_bytes`, raise `InstallError(f"Insufficient disk space. This profile requires ~{profile.download_mb * 2} MB free; only {usage.free // 1_000_000} MB available.")`.
  - **Releasable**: after this task, disk space is checked before any download starts.
- **Tests (TDD)** — `tests/test_install_disk_space.py` (new file):
  - Unit: `test_disk_space_sufficient_does_not_raise` — mock `shutil.disk_usage` with ample free space
  - Unit: `test_disk_space_insufficient_raises_install_error` — mock with tight free space; assert `InstallError` raised with expected message
  - Unit: `test_disk_space_walks_up_to_existing_ancestor` — base path and its parent do not exist; walks up until finding an existing ancestor and uses it for `shutil.disk_usage`
  - Unit: `test_disk_space_usage_raises_is_propagated` — mock `shutil.disk_usage` to raise `PermissionError`; assert the exception propagates (not swallowed). The implementation may optionally wrap it in `InstallError` with a user-friendly message; either way the error must not be silently swallowed.
  - Checkpoint: `pytest tests/test_install_disk_space.py -v`

---

#### Task 2.3 — Model pre-warm downloader
- [ ] **File**: `archon_search/install.py`
- **Depends on**: Task 1.1 (InstallProfile), Task 1.4 (optional reranker runtime support)
- **Description**:
  - `_prewarm_timeout(profile: InstallProfile) -> int`:
    - `estimated_bytes = profile.download_mb * 1_000_000`
    - `return min(1800, max(300, estimated_bytes // 100_000))` (seconds)
  - `_prewarm_models(profile: InstallProfile, timeout: int | None = None) -> None`:
    - If `timeout is None`, compute via `_prewarm_timeout(profile)`.
    - **Timeout implementation**: use `threading.Timer` with a cancellation flag — **do NOT use `signal.alarm`**. `signal.alarm` is absent on Windows (`AttributeError`), is process-global (can interrupt unrelated file I/O mid-operation, leaving corrupted cache files), and is unsafe in library code. Implementation: create a `threading.Event` flag; start `threading.Timer(timeout, lambda: flag.set())`; check the flag between download phases (after embedder download, before reranker download). If the flag is set: log a warning (`"Model pre-warm timed out after {timeout}s. Service will start without cached model files, same as default behavior."`) and return without raising. On success: cancel the timer.
    - **Note (non-preemptive timeout)**: the cancellation flag is checked between the embedder and reranker phases, but NOT during an individual download call. If the embedder download hangs, the timeout fires but the function continues blocking until the download completes or fails. This is an accepted v1 limitation (see Known Limitations).
    - `TextEmbedding(profile.embedder, lazy_load=True)` — this call triggers the download to the fastembed cache without creating an ONNX session.
    - Check cancellation flag. If set, warn and return early.
    - If `profile.reranker` is not None: `TextCrossEncoder(profile.reranker, lazy_load=True)`.
    - fastembed / huggingface_hub print progress to stderr; do not suppress (option a from brief).
    - Wrap in try/except to surface a clear `InstallError` if download fails, including which model failed.
  - **Releasable**: after this task, model files can be pre-warmed with a cross-platform timeout and clear error handling.
- **Tests (TDD)** — `tests/test_install_prewarm.py` (new file):
  - Unit: `test_prewarm_timeout_minimal` — 147 MB profile → between 300 and 1800
  - Unit: `test_prewarm_timeout_max` — 2300 MB profile → capped at 1800
  - Unit: `test_prewarm_calls_text_embedding_lazy` — mock `fastembed.TextEmbedding`; assert `lazy_load=True` passed
  - Unit: `test_prewarm_calls_cross_encoder_when_reranker_set` — mock `fastembed.TextCrossEncoder`; assert called with reranker model
  - Unit: `test_prewarm_skips_cross_encoder_when_reranker_none` — multilingual minimal profile; assert `TextCrossEncoder` not called
  - Unit: `test_prewarm_raises_install_error_on_download_failure` — mock `TextEmbedding` to raise; assert `InstallError` raised with model name in message
  - Unit: `test_prewarm_raises_install_error_on_cross_encoder_failure` — mock `TextEmbedding` to succeed; mock `TextCrossEncoder` to raise; assert `InstallError` is raised with the reranker model name in the message
  - Unit: `test_prewarm_timeout_fires_and_warns` — set a very short timeout (e.g., 0.01s); mock `TextEmbedding` to sleep past the timeout; verify timeout fires, a warning is logged, and the function returns without raising; also assert `TextCrossEncoder` is NOT called when the cancellation flag fires between the embedder and reranker phases
  - Unit: `test_prewarm_cancels_timer_on_success` — mock `threading.Timer`; verify `cancel()` is called after successful download
  - Checkpoint: `pytest tests/test_install_prewarm.py -v`

---

#### Task 2.4 — Reinstall guard
- [ ] **File**: `archon_search/install.py`
- **Depends on**: Task 1.1 (InstallProfile), Task 1.2 (SearchConfig.profile, .multilingual)
- **Description**:
  - `class NeedsForceDeleteError(InstallError)`: raised when model or chunk_size conflict requires `--force --delete-db`.
  - `_check_reinstall_guard(existing_cfg: SearchConfig, new_profile: InstallProfile, new_profile_name: str, new_multilingual: bool) -> None`:
    - If `existing_cfg.profile == ""`: legacy install (no profile recorded). Compare `existing_cfg.embedding_model` and `existing_cfg.chunk_size` directly.
    - If `existing_cfg.embedding_model == new_profile.embedder` and `existing_cfg.chunk_size == new_profile.chunk_size`: idempotent — return silently. (Reranker change alone does NOT trigger the guard.)
    - Otherwise: raise `NeedsForceDeleteError` with message: `"Existing index uses {existing_cfg.embedding_model} (chunk_size={existing_cfg.chunk_size}). Switching to {new_profile.embedder} (chunk_size={new_profile.chunk_size}) requires re-indexing all documents. Run with --force --delete-db to proceed."`.
  - Caller is responsible for checking whether a DB directory and config file actually exist before calling this guard (guard only runs when config file exists).
  - **Releasable**: after this task, conflicting reinstalls are blocked with a clear message.
- **Tests (TDD)** — `tests/test_install_guard.py` (new file):
  - Unit: `test_guard_idempotent_same_model_and_chunk` — same embedder + chunk_size → no exception
  - Unit: `test_guard_different_embedder_raises` — different embedder → `NeedsForceDeleteError`
  - Unit: `test_guard_different_chunk_size_raises` — same embedder, different chunk_size → raises
  - Unit: `test_guard_reranker_only_change_does_not_raise` — same embedder + chunk_size, different reranker → no exception
  - Unit: `test_guard_legacy_install_compares_raw_embedding_model` — `existing_cfg.profile = ""`; different model → raises
  - Checkpoint: `pytest tests/test_install_guard.py -v`

---

#### Task 2.5 — Force-delete-db rollback sequence
- [ ] **File**: `archon_search/install.py`
- **Depends on**: Task 1.3 (_write_profile_config), Task 2.4 (NeedsForceDeleteError)
- **Description**:
  - `_execute_force_reinstall(config_path: Path, db_path: Path, profile: InstallProfile, profile_name: str, multilingual: bool, non_interactive: bool, dry_run: bool = False) -> None`:
    - Step 1: Back up config. If `config_path` exists, copy to `config_path.with_suffix(".toml.bak")`.
    - Step 2: Confirmation gate. If `not non_interactive`: print `"WARNING: This will permanently delete all indexed data. Type 'yes' to confirm: "`. If input is not `"yes"`, restore backup (if any), print `"Aborted."`, raise `SystemExit(1)`.
    - Step 3: Stop service. If `dry_run`: print `"[dry-run] Would stop service."` and skip the actual stop call. Otherwise: call `get_search_service().stop()`. On error, restore backup and re-raise. Note: a "service not running" error from `stop()` (e.g., on Linux where `systemctl stop` returns non-zero for a stopped unit) should NOT abort the reinstall. Catch `RuntimeError` from `stop()` — if the error message indicates the service was not running (or if `_is_service_running()` returns False before calling `stop()`), treat as a no-op and proceed.
    - Step 4: Delete DB directory. If `dry_run`: print `"[dry-run] Would delete database at <db_path>."` and skip rmtree. Otherwise: call `shutil.rmtree(db_path)`. If `db_path` does not exist, skip rmtree silently and proceed (no database to delete is not an error). On rmtree error: the database may be partially deleted — do NOT restore the config backup (it would reference a corrupted DB). Leave the backup in place as a recovery artifact. Print: `"Install failed during database deletion. Your previous config has been preserved at <config_path>.bak for reference. Run archon-search install to create a fresh install from scratch."` Then raise `SystemExit(1)`. **After step 4 succeeds, if anything subsequently fails**: do NOT restore the config backup (the old index no longer exists; restoring old config would reference a deleted database). Leave the backup in place as a recovery artifact. Print: `"Install failed after database deletion. Your previous config has been preserved at <config_path>.bak for reference. Run archon-search install to create a fresh install from scratch."` Then raise `SystemExit(1)`.
    - Step 5: Write new profile config. If `dry_run`: print `"[dry-run] Would write profile config for <profile_name>."` and skip the write. Otherwise: call `_write_profile_config(config_path, profile, profile_name, multilingual)`. Before calling `_write_profile_config`, check for and remove any stale `<config_path>.tmp` file (same as `_write_profile_config`'s own implementation note). On error, follow the post-DB-deletion failure path above (leave backup, print message, `SystemExit(1)`).
    - Step 6: Pre-warm and service start are handled by the main `run()` flow after this function returns.
    - Step 7 (caller's responsibility): start service.
  - **Releasable**: after this task, `--force --delete-db` can safely reinitialize the install.
- **Tests (TDD)** — `tests/test_install_force_delete.py` (new file):
  - Unit: `test_force_reinstall_backs_up_config` — assert `.toml.bak` created before DB deletion
  - Unit: `test_force_reinstall_confirms_before_delete` — non-interactive=False; mock input returning `"no"` → exits 1, backup restored, rmtree not called
  - Unit: `test_force_reinstall_yes_confirmation_proceeds` — non-interactive=False; mock input returning `"yes"` → rmtree called, `_write_profile_config` called, no `SystemExit`
  - Unit: `test_force_reinstall_skips_confirm_when_non_interactive` — non-interactive=True; no input prompt, proceeds
  - Unit: `test_force_reinstall_deletes_db_directory` — mock rmtree; assert called with db_path
  - Unit: `test_force_reinstall_restores_backup_on_stop_failure` — service stop raises; backup file restored, rmtree not called
  - Unit: `test_force_reinstall_prints_post_db_deletion_message_on_write_failure` — `_write_profile_config` raises after DB deleted; verify: config backup is preserved (NOT deleted), the post-DB-deletion message is printed to stderr and references the backup path, `SystemExit(1)` is raised
  - Unit: `test_force_reinstall_handles_missing_db_directory` — `db_path` does not exist; verify function proceeds without error (no `FileNotFoundError`), `_write_profile_config` is still called
  - Unit: `test_force_reinstall_dry_run_skips_all_destructive_ops` — `dry_run=True`; verify `shutil.rmtree` is NOT called, `get_search_service().stop()` is NOT called, `_write_profile_config` is NOT called, but the function returns without raising `SystemExit`
  - Checkpoint: `pytest tests/test_install_force_delete.py -v`

---

### Phase 3 — UI & CLI Integration
> **Releasable**: after this phase, the full install UX is live; `archon-search install` presents profiles and installs the chosen one. Each task in this phase is releasable individually after Task 3.4 integrates them.

#### Task 3.1 — Profile table and summary screen
- [ ] **File**: `archon_search/install.py`
- **Depends on**: Task 1.1 (profiles)
- **Description**:
  - `_render_profile_table(multilingual: bool, width: int = 80) -> str`:
    - If `width >= 80`: render the full table as shown in the brief (profile/download/quality/speed columns + model names + best-for rows).
    - If `width < 80`: render a compact list format — one profile per line: `"1) Minimal: {embedder} + {reranker} (~{mb} MB)"`.
    - Always appends: `"  Add --multilingual to use multilingual models instead."` if `not multilingual`, else `"  (Showing multilingual models)"`.
    - Returns a string (not prints directly); caller prints it.
  - `_render_summary(profile_name: str, profile: InstallProfile, multilingual: bool, providers: list[str]) -> str`:
    - Returns the summary block shown in the brief: profile label, embedder, reranker, chunk_size, providers detected, and the ONNX session initialization note.
  - **Releasable**: after this task, display helpers are callable and testable in isolation.
- **Tests (TDD)** — `tests/test_install_ui.py` (new file):
  - Unit: `test_render_table_wide_contains_all_profiles` — width=80; output contains "Minimal", "Balanced", "Max"
  - Unit: `test_render_table_narrow_uses_list_format` — width=60; output does NOT contain the full table separator `─────`; contains "1)", "2)", "3)"
  - Unit: `test_render_table_multilingual_shows_multilingual_note` — multilingual=True; output contains "multilingual"
  - Unit: `test_render_summary_balanced_english` — contains "BAAI/bge-base-en-v1.5", "512", "Balanced"
  - Unit: `test_render_summary_shows_providers` — providers=["CoreMLExecutionProvider"]; output contains "CoreML"
  - Checkpoint: `pytest tests/test_install_ui.py -v`

---

#### Task 3.2 — Jina license gate
- [ ] **File**: `archon_search/install.py`
- **Depends on**: Task 1.1 (JINA_RERANKER_MODEL constant)
- **Description**:
  - `_requires_jina_license(profile: InstallProfile) -> bool`:
    - Returns `profile.reranker == JINA_RERANKER_MODEL`.
  - `_prompt_jina_license(non_interactive: bool, accept_jina_license: bool = False) -> None`:
    - Prints: the CC-BY-NC-4.0 warning block from the brief (model name, license, commercial use prohibition).
    - If `accept_jina_license` is `True`: skip the prompt entirely and proceed without raising `SystemExit(1)`, even when `non_interactive=True`. This enables CI/CD automation for multilingual deployments.
    - Else if `non_interactive`: print `"Non-interactive mode: Jina license automatically declined. Use an English profile for commercial installs."` and raise `SystemExit(1)`.
    - Otherwise: prompt `"Type 'accept' to confirm license acceptance and continue, or anything else to abort: "`.
    - If input is not `"accept"` (case-insensitive strip): print `"License not accepted. Aborting."` and raise `SystemExit(1)`.
  - `run()` must accept and forward `accept_jina_license: bool = False` to `_prompt_jina_license()`. The `--accept-jina-license` CLI flag (defined in Task 3.5) passes this value in.
  - **Releasable**: after this task, Jina multilingual profiles cannot be installed without explicit license acknowledgment (but can be automated via `--accept-jina-license`).
- **Tests (TDD)** — `tests/test_install_jina_gate.py` (new file):
  - Unit: `test_requires_jina_license_true_for_multilingual_balanced` — balanced multilingual → True
  - Unit: `test_requires_jina_license_false_for_english` — any English profile → False
  - Unit: `test_requires_jina_license_false_for_multilingual_minimal` — no reranker → False
  - Unit: `test_prompt_jina_non_interactive_raises_systemexit` — raises `SystemExit(1)` without prompting
  - Unit: `test_prompt_jina_accept_does_not_raise` — mock input returning `"accept"` → no exception
  - Unit: `test_prompt_jina_accept_uppercase_does_not_raise` — mock input returning `"ACCEPT"` → no exception (case-insensitive)
  - Unit: `test_prompt_jina_accept_with_whitespace_does_not_raise` — mock input returning `" accept "` → no exception (strip + case-insensitive)
  - Unit: `test_prompt_jina_decline_raises_systemexit` — mock input returning `"no"` → `SystemExit(1)`
  - Unit: `test_prompt_jina_accept_jina_license_flag_skips_prompt` — `accept_jina_license=True, non_interactive=True`; assert no `SystemExit`, no `input()` call (prompt is bypassed entirely)
  - Checkpoint: `pytest tests/test_install_jina_gate.py -v`

---

#### Task 3.3 — Profile selection logic
- [ ] **File**: `archon_search/install.py`
- **Depends on**: Task 1.1, Task 3.1 (_render_profile_table)
- **Description**:
  - `_select_profile(profile_flag: str | None, multilingual_flag: bool, non_interactive: bool) -> tuple[str, bool]`:
    - Returns `(profile_name, multilingual)`.
    - If `non_interactive and profile_flag is None and not multilingual_flag`: return `("minimal", False)`; log defaults for both profile and multilingual.
    - If `non_interactive and profile_flag is None and multilingual_flag is True`: return `("minimal", True)` — respect the explicit `--multilingual` flag; log default only for profile (`"Profile defaulted to minimal"`).
    - If `profile_flag is not None`: validate against `VALID_PROFILE_NAMES`; raise `click.BadParameter` if invalid. Return `(profile_flag, multilingual_flag)`.
    - Interactive path (profile_flag is None, not non_interactive):
      - Detect terminal width via `shutil.get_terminal_size(fallback=(80, 24)).columns`.
      - Print `_render_profile_table(multilingual=multilingual_flag, width=terminal_width)`.
      - Prompt `"Choice [1-3, default 1]: "`. Map `"1"` → `"minimal"`, `"2"` → `"balanced"`, `"3"` → `"max"`. Empty input → `"minimal"`. Invalid input → re-prompt (up to 3 attempts then `SystemExit(1)`).
      - In the prompt loop, catch `EOFError` from `input()` (raised when stdin is a closed pipe): print `"No input received (EOF). Aborting."` and raise `SystemExit(1)`.
      - Return `(selected_name, multilingual_flag)`.
  - **Releasable**: after this task, profile selection logic is independently callable from `SearchInstaller.run()`.
- **Tests (TDD)** — `tests/test_install_select_profile.py` (new file):
  - Unit: `test_non_interactive_no_flag_defaults_minimal_english` — `profile_flag=None, multilingual_flag=False, non_interactive=True` → returns `("minimal", False)`
  - Unit: `test_non_interactive_with_multilingual_flag_returns_minimal_multilingual` — `profile_flag=None, multilingual_flag=True, non_interactive=True` → returns `("minimal", True)`
  - Unit: `test_explicit_profile_flag_returned_as_is` — `profile_flag="max", multilingual_flag=False` → `("max", False)`
  - Unit: `test_explicit_profile_flag_with_multilingual_true` — `profile_flag="balanced", multilingual_flag=True` → `("balanced", True)`
  - Unit: `test_explicit_invalid_profile_raises` — `profile_flag="ultra"` → `click.BadParameter`
  - Unit: `test_interactive_choice_1_returns_minimal` — mock input `"1"`, `multilingual_flag=False` → `("minimal", False)`
  - Unit: `test_interactive_empty_defaults_to_minimal` — mock input `""` → `("minimal", False)`
  - Unit: `test_interactive_invalid_then_valid_retries` — mock inputs `["x", "2"]` → `("balanced", False)`
  - Unit: `test_interactive_three_invalid_inputs_exits` — three invalid inputs → `SystemExit(1)`
  - Unit: `test_interactive_eof_on_input_exits` — mock `input()` to raise `EOFError`; assert `SystemExit(1)` raised with the "No input received (EOF). Aborting." message
  - Unit: `test_interactive_choice_returns_multilingual_flag_as_given` — mock input `"2"`, `multilingual_flag=True` → `("balanced", True)`
  - Checkpoint: `pytest tests/test_install_select_profile.py -v`

---

#### Task 3.4 — `SearchInstaller.run()` full profile-aware rewrite
- [ ] **File**: `archon_search/install.py`
- **Depends on**: Task 1.4, Task 2.1–2.5, Task 3.1–3.3
- **Description**:
  - Update `SearchInstaller.__init__`: keep `self.cfg = load_config(path)` in `__init__` as it is today (for backwards compatibility with `run_uninstall()`, `_is_service_running()`, `_wait_for_service()`, and `create_data_dir()`, all of which read `self.cfg`). Do NOT remove this assignment. Note: `_bootstrap_collections()` also reads `self.cfg` — it is dead code (never called in production) but has tests in `tests/test_install.py`. Keeping `self.cfg` in `__init__` means those tests continue to pass without modification.
  - Rewrite `SearchInstaller.run(non_interactive, profile, multilingual, skip_preload, force, delete_db) -> int`:
    - Validate `--force` requires `--delete-db`: if `force and not delete_db`, print error message from brief and return 1.
    - Within `_acquire_install_lock()` context:
      - **Step 0** (before lock acquisition in practice, but inside the method): Run `_remove_legacy_service()` if a legacy service file exists (macOS launchd plist from pre-C1 installs). Create log directory `~/.archon-search/logs/` if absent.
      1. Call `_select_profile(profile, multilingual, non_interactive)` → `(profile_name, is_multilingual)`.
      2. `prof = get_profile(profile_name, is_multilingual)`.
      3. If `_requires_jina_license(prof)`: call `_prompt_jina_license(non_interactive, accept_jina_license=accept_jina_license)`.
      4. Config path: `Path(self.config_file) if self.config_file else get_default_config_path()`.
      5. Reinstall check: if `config_path.exists()`: `existing_cfg = load_config(config_path)`. Call `_check_reinstall_guard(existing_cfg, prof, profile_name, is_multilingual)`. Catch `NeedsForceDeleteError`: print error message and return 1 if `not (force and delete_db)`.
      5b. Compute `db_path`: `db_path = Path(self.cfg.db_path)` from the existing config (or from `get_default_config_path()` if config does not exist). This must be computed before the branch at step 6 — `_execute_force_reinstall` requires a concrete `db_path` argument.
      6. **`if force and delete_db`** (branch A): call `_execute_force_reinstall(config_path, db_path, prof, profile_name, is_multilingual, non_interactive, dry_run=self.dry_run)`. After this call returns, proceed directly to step 8b — do NOT execute steps 7 or 8. **Design decision**: `--force --delete-db` is unconditionally destructive when both flags are set. If the reinstall guard passes silently (no conflict), the force-delete flow still runs. This is intentional — the user explicitly requested database deletion. The confirmation prompt at step 13 (if not `non_interactive`) provides the final safety gate.
      7. **`elif config_path` does not exist** (branch B — fresh install): check for and remove any stale `<config_path>.tmp` file, then create parent dirs and write `_profile_toml(profile_name, is_multilingual)` to `config_path` durably via `_durable_io.atomic_write_bytes`. Create a backup of the config file (`config_path.with_suffix(".toml.bak")`) immediately after writing.
      8. **`elif`** (branch C — reinstall, same profile — idempotent): Create a backup of the config file first: copy `config_path` to `config_path.with_suffix(".toml.bak")` if `config_path` exists. Then call `_write_profile_config(config_path, prof, profile_name, is_multilingual)`. (Backup is created BEFORE the write, so restoring it reverts the profile update.)
      - **Note**: steps 6, 7, and 8 are mutually exclusive branches (`if/elif/elif`). Only one branch executes per run.
      8b. After the applicable branch (config written): reassign `self.cfg = cfg = load_config(config_path)`. This overwrites the stale `self.cfg` set in `__init__` with the freshly-written config, ensuring `configure_providers()`, `validate_providers()`, and all subsequent steps use the correct profile models.
      9. GPU detection + `configure_providers()` and `validate_providers()` — both continue to read `self.cfg` (no parameter change needed, since `self.cfg` was updated at step 8b).
      10. Create data directory.
      11. Disk space check: `_check_disk_space(prof)`. On `InstallError`: print and return 1.
      12. Print summary: `_render_summary(profile_name, prof, is_multilingual, providers)`.
      13. Confirmation (if `not non_interactive`): prompt `"Proceed? [Y/n]: "`. Abort if not `y`. In non-interactive mode, skip this prompt entirely.
      14. Pre-warm: if `not skip_preload`: print `[4/5] Downloading models...` then call `_prewarm_models(prof)`. Track which branch was taken via a local variable (e.g., `branch = "force" | "fresh" | "idempotent"`) set at steps 6/7/8 respectively. On `InstallError` from pre-warm, apply branch-aware recovery: if branch is `"fresh"` (step 7): delete both the config file and its `.bak` backup to leave a clean state (no prior config existed; both files must be removed to avoid leaving a config referencing a model that failed to download). If branch is `"idempotent"` (step 8): restore the `.bak` backup to the config path (reverts the profile update; the old DB is still intact). If branch is `"force"` (step 6): do NOT restore or delete the backup; the DB is gone, the new config is valid, and the `.bak` is a recovery artifact. Print error and return 1 — the user can re-run install to retry.
      15. Register and start service (existing `write_service_file()` + `load_service()`).
      16. Wait for service readiness (`_wait_for_service()`).
      17. Print: `"archon-search installed and running. Profile: {profile_name.capitalize()} · {'Multilingual' if is_multilingual else 'English'}."`.
    - Return 0 on success.
  - **Note**: Confirmation screen (step 13) must be shown AFTER disk-space check (step 11) and summary display (step 12) but BEFORE model download (step 14). This ensures the user confirms before any long download begins. In non-interactive mode, no confirmation prompt; proceed automatically.
  - **Releasable**: after this task, the full install flow with profile selection is functional end-to-end.
- **Tests (TDD)** — `tests/test_install_run.py` (new file):
  - Integration: `test_run_non_interactive_minimal_skips_preload` — `non_interactive=True, profile="minimal", skip_preload=True`; mocks service start; assert config written with correct model; assert no confirmation prompt is shown
  - Integration: `test_run_force_without_delete_db_returns_1` — `force=True, delete_db=False` → returns 1, no service start
  - Integration: `test_run_reinstall_same_profile_is_idempotent` — existing config matches; no `NeedsForceDeleteError`
  - Integration: `test_run_reinstall_different_profile_no_force_returns_1` — different embedding_model; returns 1 with error message
  - Integration: `test_run_jina_multilingual_non_interactive_returns_1` — multilingual balanced, non_interactive → returns 1 (license declined)
  - Integration: `test_run_disk_space_failure_returns_1` — mock disk_usage with insufficient space; returns 1 before service start
  - Integration: `test_run_prewarm_failure_returns_1` — idempotent reinstall path (branch C); set up an existing config with `embedding_model='BAAI/bge-small-en-v1.5'` and `profile='minimal'`; run with the same profile (idempotent); mock pre-warm to raise `InstallError`; assert return 1; assert config file content still contains `embedding_model='BAAI/bge-small-en-v1.5'` (original config was restored from the pre-write backup, proving the rollback was meaningful and not a no-op)
  - Integration: `test_run_fresh_install_prewarm_failure_cleans_up_config` — fresh install path (branch B, no existing config); mock pre-warm to raise `InstallError`; verify both the config file and its `.bak` backup are absent after the failure (fresh install means there was no prior config; both must be deleted to avoid leaving a config referencing a model that failed to download); assert return 1
  - Integration: `test_run_force_reinstall_prewarm_failure_does_not_restore_old_backup` — force-reinstall path (branch A); mock pre-warm to raise `InstallError`; verify: config file contains the NEW profile (not the old one), `.bak` backup is still present (not deleted), return 1
  - Integration: `test_run_force_delete_db_different_profile_succeeds` — existing config has minimal model; `force=True, delete_db=True, profile="balanced", non_interactive=True, skip_preload=True`; mock rmtree, service stop, service start; assert config written with balanced model, rmtree called, return 0
  - Integration: `test_run_creates_log_directory` — assert `~/.archon-search/logs/` is created during run
  - Integration: `test_run_calls_legacy_service_cleanup` — mock `_remove_legacy_service`; assert it is called during run
  - Checkpoint: `pytest tests/test_install_run.py -v`

---

#### Task 3.5 — `install_cmd.py` consolidation
- [ ] **File**: `archon_search/cli/install_cmd.py`
- **Depends on**: Task 3.4 (SearchInstaller.run() complete API)
- **Description**:
  - Keep the existing `uninstall` Click command unchanged.
  - Move `_legacy_service_path()` and `_remove_legacy_service()` from `install_cmd.py` into `install.py` so they can be called from `SearchInstaller.run()` (step 0). Remove from `install_cmd.py`.
  - Remove `_default_toml()` call from the install path — it is superseded by `_profile_toml()`. The `_default_toml()` function itself remains in `config_cmd.py` for the `config` subcommand; it is only removed from the `install` path.
  - Log directory creation (`~/.archon-search/logs/`) moves from `install_cmd.py` to `SearchInstaller.run()` step 0. Remove from `install_cmd.py`.
  - Keep `_wait_for_health()` and `_get_db_path()` removed from `install_cmd.py` — these are now in `SearchInstaller`.
  - Replace the `install` Click command body with new flags and a single `SearchInstaller(...).run(...)` call:
    ```python
    @click.command()
    @click.option("--profile", type=click.Choice(["minimal","balanced","max"]), default=None)
    @click.option("--multilingual", is_flag=True, default=False)
    @click.option("--skip-preload", is_flag=True, default=False)
    @click.option("--force", is_flag=True, default=False)
    @click.option("--delete-db", is_flag=True, default=False)
    @click.option("--dry-run", is_flag=True, default=False)
    @click.option("--non-interactive", is_flag=True, default=False)
    @click.option("--accept-jina-license", is_flag=True, default=False)
    @click.option("--config", "config_path", default=None, type=click.Path(path_type=Path))
    def install(...) -> None:
        # NOTE: SearchInstaller.__init__ takes config_file: str | None (not config_path: Path).
        # Either pass config_file=str(config_path) if config_path else None here,
        # OR rename config_file -> config_path throughout install.py.
        sys.exit(SearchInstaller(config_file=str(config_path) if config_path else None, dry_run=dry_run).run(
            non_interactive=non_interactive, profile=profile, multilingual=multilingual,
            skip_preload=skip_preload, force=force, delete_db=delete_db,
            accept_jina_license=accept_jina_license,
        ))
    ```
  - **Releasable**: after this task, `archon-search install --profile balanced` works end-to-end.
- **Tests (TDD)** — `tests/test_install_cmd.py` (extend or new file):
  - Integration: `test_install_cmd_non_interactive_minimal_skip_preload` — invoke via Click test runner; assert `SearchInstaller.run` called with correct kwargs; mock `run` to return 0
  - Integration: `test_install_cmd_profile_choice_validation` — `--profile ultra` → Click error, exit != 0
  - Integration: `test_install_cmd_force_without_delete_db` — passes `force=True, delete_db=False` to `run()`; `run()` returns 1 → `sys.exit(1)`
  - Integration: `test_uninstall_cmd_unchanged` — `archon-search uninstall --delete-db` still works; service.stop called
  - Checkpoint: `pytest tests/test_install_cmd.py -v`

---

### Phase 4 — Verification & Documentation

#### Task 4.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to discover all documentation in the project (READMEs, ADRs, API docs, Architecture docs, user guides, `CHANGELOG`, `BREAKING.md`) and update every file whose content is affected by the changes delivered in this plan. The agent must not update docs that are unrelated.
  - Files expected to need updates: `Documentation/Architecture/100_system_architecture_overview.md`, `Documentation/Architecture/110_component_catalog_and_layer_breakdown.md`, `Documentation/Architecture/600_api_reference_or_public_interface.md`, `Documentation/UserManual/` (install/onboarding docs), `Documentation/quick_start.md`, `Documentation/roadmap.md` (mark C1 complete), `archon-search.toml.example` (add `profile` and `multilingual` example keys).
  - Verify all acceptance criteria below before marking this task complete.
- **Releasable**: after this task, the feature is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - `archon-search install --non-interactive --profile minimal --skip-preload` exits 0 with a config containing `embedding_model = "BAAI/bge-small-en-v1.5"`, `profile = "minimal"`, `multilingual = false`.
  - `archon-search install --non-interactive --profile balanced --multilingual --skip-preload` exits 1 (Jina license declined in non-interactive mode).
  - `archon-search install --non-interactive --force` exits 1 with the `--force requires --delete-db` message.
  - After installing minimal, running `archon-search install --non-interactive --profile balanced --skip-preload` exits 1 with the "requires re-indexing" message.
  - `load_config()` on a config written by the installer returns `profile="minimal"` and `multilingual=False`.
  - `uv run pytest` (default suite, coverage ≥ 85%) passes without warnings.
  - `uv run pytest -m integration` passes.
  - All documentation files listed above are updated to describe the profile selection flow.
  - `archon-search.toml.example` documents `profile` and `multilingual` keys under `[database]`.
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked.
