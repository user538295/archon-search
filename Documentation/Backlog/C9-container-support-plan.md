# C9 — Container Support
**Purpose**: Ship a `docker run`-ready archon-search image that is fully configurable via env vars, persists data on a mounted volume, and integrates cleanly into the existing GitHub Actions release pipeline.
**Audience**: Developers, operators, and CI pipelines that need portable, reproducible archon-search deployments.
**Status**: To Do

---

## Background

archon-search currently requires host-level service installation (launchd/systemd) and manual path management. There is no portable deployment unit — making reproducible dev/test/prod environments or operator handoffs difficult. Two config-layer prerequisites (ARCH-2 and ARCH-3) must be solved first: host/port env var overrides and a relocatable path root. Without them a Docker image cannot change its port without a mounted config file, and `ARCHON_SEARCH_DATA_DIR` silently fails to redirect key files and model caches.

## Goal

A working `docker run` and `docker compose up` that starts archon-search fully configured via env vars, persists all runtime state on a single mounted volume, and emits logs to stderr. Two image variants — CPU (`:latest`) and NVIDIA GPU (`:gpu`) — are published to GHCR on every tag push via the existing release workflow. Operators can reach the server immediately; no TOML file is required for basic usage.

---

## Scope

### In Scope
- ARCH-2: `ARCHON_SEARCH_HOST` and `ARCHON_SEARCH_PORT` env var overrides in `load_config()`
- ARCH-3: `ARCHON_SEARCH_DATA_DIR` env var that redirects all runtime-relevant paths (db, logs, telemetry, key file, jobs file, fasttext models, ingest history)
- `archon-search serve` CLI subcommand — foreground uvicorn start, no platform service management; defaults host to `0.0.0.0` unless explicitly overridden
- `ARCHON_SEARCH_CONTAINER=1` — adds `StreamHandler(sys.stderr)` to the `archon_search` logger
- `Dockerfile` with `--build-arg VARIANT=cpu|gpu` (single file)
- `docker-compose.yml` with dev/test/prod services (separate named volumes, `stop_grace_period: 30s`)
- `.dockerignore`
- CI docker smoke test (`@pytest.mark.docker`)
- Extend `archon-search-release.yml` to build and push CPU + GPU images to GHCR on tag push

### Out of Scope
- Apple Silicon (Metal/MPS) GPU support
- Remote inference / embedding microservice split
- Kubernetes manifests, Helm charts
- Multi-instance / horizontal scaling
- Windows container support (pre-existing PLT-1)
- Service/install modules (`install.py`, `platform/*.py`) — contain `Path.home()` refs but never execute in a container

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 5.2 — Final verification & documentation update].

---

## What does NOT change
- Existing `start` / `stop` / `install` / `uninstall` subcommands and platform service management
- TOML-based configuration for operators not using Docker
- `ARCHON_SEARCH_KEY_FILE` env var (still overrides DATA_DIR for key file path)
- TOML config file discovery: `get_default_config_path()` resolves to `~/.archon-search/archon-search.toml` or `$ARCHON_SEARCH_CONFIG` — NOT to `$ARCHON_SEARCH_DATA_DIR/archon-search.toml`. To use a config file inside the container, set `ARCHON_SEARCH_CONFIG=/data/archon-search.toml`. If both `ARCHON_SEARCH_CONFIG` and `ARCHON_SEARCH_DATA_DIR` are set, the config file is read from `ARCHON_SEARCH_CONFIG` but all path fields inside it are overridden by `DATA_DIR`-derived paths.
- Telemetry invariant: no `query` parameter in telemetry entry constructors
- The `--cov-fail-under=85` coverage gate

---

## Known limitations / accepted trade-offs
- `GET /ready` does not verify model weight availability; a model warmup step or separate probe is needed if operators require search readiness before container is marked ready.
- In-flight ingest jobs are not awaited on SIGTERM — existing behavior, not introduced here.
- Container serves plaintext HTTP; TLS termination is the operator's responsibility.
- LanceDB single-writer: running two containers against the same `ARCHON_SEARCH_DATA_DIR` volume is undefined behavior — documented, not guarded.
- If no persistent volume and no `ARCHON_SEARCH_API_KEY` is provided, the key regenerates on every start — documented prominently.
- `ARCHON_SEARCH_CONTAINER` will be droppable once ARCH-3 delivers full per-field env overrides; retained now as an explicit, operator-friendly toggle.
- `archon-search collection add/remove` commands write to the TOML config file at `get_default_config_path()`, which resolves to `~/.archon-search/archon-search.toml` — NOT under `ARCHON_SEARCH_DATA_DIR`. These commands will fail inside the container (non-root user, HOME may be read-only). Operators who need to modify collection config must set `ARCHON_SEARCH_CONFIG` to a path inside the `/data` volume, or edit config outside the container.

---

## Architecture

### New modules / files
- `archon_search/paths.py` — `get_data_dir() -> Path`: single source of truth for the base data directory, reads `ARCHON_SEARCH_DATA_DIR` env var or falls back to `Path.home() / ".archon-search"`.
- `archon_search/cli/serve.py` — `serve` Click command: loads config (with `0.0.0.0` host default), calls `run_server()`, never touches platform service management.
- `Dockerfile` — single file with `ARG VARIANT=cpu|gpu` that selects the base image; often called "multi-stage" but technically a conditional base image selection (no COPY --from between stages). CPU and GPU builds share no layers.
- `docker-compose.yml` — dev/test/prod services with separate named volumes.
- `.dockerignore`

### Modified modules
- `archon_search/config.py` — `load_config()` gains env var overrides for `ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_PORT`, and `ARCHON_SEARCH_DATA_DIR`-derived paths. Accepts `serve: bool = False` kwarg to default host to `0.0.0.0` before TOML/env processing.
- `archon_search/logging_setup.py` — `configure_logging()` adds `StreamHandler(sys.stderr)` when `ARCHON_SEARCH_CONTAINER=1`.
- `archon_search/key_manager.py` — module-level `KEY_FILE` constant replaced with `_get_key_file() -> Path` (lazy, honours `ARCHON_SEARCH_KEY_FILE` first, then `get_data_dir()`).
- `archon_search/jobs/model.py` + `jobs/store.py` — `JOBS_FILE` constant replaced with `get_jobs_file() -> Path`; `JobStore.__init__` default parameter changed to `None`.
- `archon_search/language_detector.py` + `server/app.py` + `pipeline.py` — `FASTTEXT_MODELS_DIR` constant replaced with `get_fasttext_models_dir() -> Path`; module-level `_MULTILINGUAL_MODEL_PATH` in `app.py` made lazy.
- `archon_search/cli/ingest.py` — history sessions default path uses `get_data_dir()`.
- `archon_search/cli/main.py` — registers `serve` command.
- `.github/workflows/archon-search-release.yml` — new job builds and pushes CPU + GPU images to GHCR.

### Env var / config additions

| Name | Type | Default | Precedence |
|---|---|---|---|
| `ARCHON_SEARCH_HOST` | `str` | `"127.0.0.1"` (`"0.0.0.0"` in serve mode) | env > TOML > default |
| `ARCHON_SEARCH_PORT` | `int` 1–65535 | `8765` | env > TOML > default |
| `ARCHON_SEARCH_DATA_DIR` | `Path` | `~/.archon-search` | env > TOML path values > default |
| `ARCHON_SEARCH_CONTAINER` | `"1"` | unset | presence check only |

### Path derivations from `ARCHON_SEARCH_DATA_DIR`

| Config field / constant | Derived path |
|---|---|
| `config.db_path` | `$DATA_DIR/search` |
| `config.log_file` | `$DATA_DIR/logs/archon-search.log` |
| `config.telemetry.log_dir` | `$DATA_DIR/search-logs` |
| `key_manager._get_key_file()` | `$DATA_DIR/.search.env` (unless `ARCHON_SEARCH_KEY_FILE` set) |
| `jobs.get_jobs_file()` | `$DATA_DIR/archon-search-jobs.json` |
| `language_detector.get_fasttext_models_dir()` | `$DATA_DIR/models` |
| `cli/ingest.py` history default | `$DATA_DIR/history/sessions` |

---

## Task breakdown

### Phase 1 — ARCH-2: Env var overrides for host and port
> **Releasable**: after Task 1.2 — `load_config()` accepts host and port from env vars, and TOML-only setups are regression-tested.

#### Task 1.1 — ARCHON_SEARCH_HOST + ARCHON_SEARCH_PORT env var overrides
- [ ] **File**: `archon_search/config.py`
- **Depends on**: nothing
- **Description**:
  - Add an env var application block at the end of `load_config()`, after all TOML parsing.
  - `ARCHON_SEARCH_HOST`: read with `os.environ.get("ARCHON_SEARCH_HOST")`; if present and non-empty, set `config.host`. No further validation (any string is a valid host string at config time).
  - `ARCHON_SEARCH_PORT`: read with `os.environ.get("ARCHON_SEARCH_PORT")`; if present, parse as int (raise `ConfigError("ARCHON_SEARCH_PORT must be an integer, got {value!r}")` on non-int); validate 1–65535 (raise `ConfigError("ARCHON_SEARCH_PORT must be between 1 and 65535, got {n}")` on out-of-range); set `config.port`.
  - Empty string for `ARCHON_SEARCH_PORT` is treated as "not set" (skip override).
  - `load_config()` also gains `serve: bool = False` kwarg — when `True`, host is initialised to `"0.0.0.0"` on the freshly-constructed `SearchConfig()` object BEFORE TOML and env var processing, so TOML and env var can still override it.
  - Design note: adding `serve` as a kwarg to `load_config()` introduces a CLI-layer concern into the config layer. This is an intentional short-cut for v1. Log it as tech-debt in `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md` after implementation. The alternative (caller applies the `0.0.0.0` default after `load_config()` returns, using a sentinel for "was this value explicitly set?") is deferred to a future refactor.
- **Releasable**: after this task, `load_config()` reads host and port from env vars with correct precedence.
- **Tests (TDD)** — `tests/test_config_env_overrides.py`:
  - Unit: `test_host_env_overrides_default` — `ARCHON_SEARCH_HOST="0.0.0.0"` → `config.host == "0.0.0.0"`.
  - Unit: `test_port_env_overrides_default` — `ARCHON_SEARCH_PORT="9000"` → `config.port == 9000`.
  - Unit: `test_port_env_overrides_toml` — TOML sets port 8000, env sets 9000 → port is 9000.
  - Unit: `test_port_env_invalid_non_int` — `ARCHON_SEARCH_PORT="abc"` → `ConfigError` mentioning "integer".
  - Unit: `test_port_env_invalid_out_of_range` — `ARCHON_SEARCH_PORT="0"` → `ConfigError` mentioning "1 and 65535"; `ARCHON_SEARCH_PORT="65536"` → `ConfigError` mentioning "1 and 65535"; `ARCHON_SEARCH_PORT="-1"` → `ConfigError` (after int parse, fails range check).
  - Unit: `test_port_env_empty_string_ignored` — `ARCHON_SEARCH_PORT=""` → port stays at default.
  - Unit: `test_host_env_empty_string_ignored` — `ARCHON_SEARCH_HOST=""` → host stays at default (empty string is treated as "not set").
  - Unit: `test_serve_kwarg_sets_default_host` — `load_config(serve=True)` with no env/TOML → `config.host == "0.0.0.0"`.
  - Unit: `test_serve_kwarg_overridable_by_env` — `load_config(serve=True)` with `ARCHON_SEARCH_HOST="192.168.1.1"` → host is `"192.168.1.1"`.
  - Unit: `test_serve_kwarg_overridable_by_toml` — `load_config(serve=True, config_path=toml_with_host)` → TOML host wins.
  - Checkpoint: `uv run pytest tests/test_config_env_overrides.py -v`
- **conftest.py requirement**: Add an `autouse=True` function-scoped fixture to `tests/conftest.py` that calls `monkeypatch.delenv` (with `raising=False`) for these six env vars before each test: `ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_PORT`, `ARCHON_SEARCH_DATA_DIR`, `ARCHON_SEARCH_CONTAINER`, `ARCHON_SEARCH_KEY_FILE`, `ARCHON_SEARCH_CONFIG`. This prevents env var leakage between tests. IMPORTANT: do NOT include `ARCHON_SEARCH_API_KEY` in this list — it is set globally at module level (`tests/conftest.py` line 25) for auth test infrastructure and must remain set for all tests.

#### Task 1.2 — Config regression baseline
- [ ] **File**: `tests/test_config_defaults.py`
- **Depends on**: Task 1.1
- **Description**:
  - Test that `load_config()` with zero env vars set and no TOML produces a `SearchConfig` whose every field matches the expected dataclass default.
  - Assert each field explicitly (not just `config is not None`). Cover: `host`, `port`, `db_path`, `log_file`, `level`, `telemetry.enabled`, `telemetry.log_dir`, `telemetry.retention_days`, `hyde.enabled`, `observability.stage_timings_enabled`, and any other top-level fields.
  - The test must patch out `ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_PORT`, and `ARCHON_SEARCH_DATA_DIR` from the environment (use `monkeypatch.delenv(..., raising=False)`) to guarantee isolation.
- **Releasable**: after this task, any future change that accidentally shifts a default will be caught immediately.
- **Tests (TDD)** — `tests/test_config_defaults.py`:
  - Unit: `test_default_host` — `config.host == "127.0.0.1"`.
  - Unit: `test_default_port` — `config.port == 8765`.
  - Unit: `test_default_db_path` — `config.db_path == "~/.archon-search/search"`.
  - Unit: `test_default_log_file` — `config.log_file == "~/.archon-search/logs/archon-search.log"`.
  - Unit: `test_default_telemetry_disabled` — `config.telemetry.enabled == False`.
  - Unit: `test_default_telemetry_log_dir` — `config.telemetry.log_dir == "~/.archon-search/search-logs"`.
  - Unit: `test_all_defaults_snapshot` — single parameterised assertion covering every named field so new fields are not silently skipped.
  - Checkpoint: `uv run pytest tests/test_config_defaults.py -v`

---

### Phase 2 — ARCH-3: Relocatable path root
> **Releasable**: after Task 2.6 — `ARCHON_SEARCH_DATA_DIR` is a single env var knob that correctly redirects all seven runtime-state paths; no path is computed at module import time.

#### Task 2.1 — archon_search/paths.py: get_data_dir()
- [ ] **File**: `archon_search/paths.py`
- **Depends on**: nothing
- **Description**:
  - New module. Single public function: `get_data_dir() -> Path`.
  - Reads `os.environ.get("ARCHON_SEARCH_DATA_DIR")`; if set and non-empty after `.strip()`, returns `Path(value).expanduser()`.
  - If the env var is set but empty/whitespace-only, raises `ValueError("ARCHON_SEARCH_DATA_DIR must not be empty")` — uses `ValueError`, NOT `ConfigError`, to avoid a circular import (`config.py` will import `get_data_dir` from `paths.py`).
  - If the env var is absent, returns `Path.home() / ".archon-search"`.
  - Edge case: if `Path.home()` raises `RuntimeError` (e.g., HOME is unset in the container), catch it and raise `ValueError("ARCHON_SEARCH_DATA_DIR must be set: HOME is not set and no data directory can be determined")`. In the standard container image `ARCHON_SEARCH_DATA_DIR=/data` is set via ENV, so this only fires on misconfiguration.
  - `Path("/")` and trailing slashes are valid — `Path` handles normalisation.
  - No side effects (no directory creation).
- **Releasable**: after this task, all lazy path accessors can import `get_data_dir` from `archon_search.paths`.
- **Tests (TDD)** — `tests/test_paths.py`:
  - Unit: `test_default_returns_home_archon` — no env var → `Path.home() / ".archon-search"`.
  - Unit: `test_env_var_overrides_default` — `ARCHON_SEARCH_DATA_DIR="/data"` → `Path("/data")`.
  - Unit: `test_env_var_tilde_expanded` — `ARCHON_SEARCH_DATA_DIR="~/mydata"` → expanded path.
  - Unit: `test_empty_env_var_raises` — `ARCHON_SEARCH_DATA_DIR=""` → `ValueError`.
  - Unit: `test_whitespace_env_var_raises` — `ARCHON_SEARCH_DATA_DIR="   "` → `ValueError`.
  - Unit: `test_root_path_is_valid` — `ARCHON_SEARCH_DATA_DIR="/"` → `Path("/")`, no error.
  - Unit: `test_trailing_slash_normalised` — `ARCHON_SEARCH_DATA_DIR="/data/"` → `Path("/data")`.
  - Unit: `test_home_unset_raises_valueerror` — monkeypatch `Path.home` to raise `RuntimeError("HOME is not set")`; env var unset → `ValueError` is raised with message mentioning "HOME is not set".
  - Checkpoint: `uv run pytest tests/test_paths.py -v`

#### Task 2.2 — config.py: ARCHON_SEARCH_DATA_DIR overrides for config paths
- [ ] **File**: `archon_search/config.py`
- **Depends on**: Task 2.1
- **Description**:
  - In the env var application block added in Task 1.1, after applying `ARCHON_SEARCH_HOST` and `ARCHON_SEARCH_PORT`: if `ARCHON_SEARCH_DATA_DIR` is set (non-empty), call `get_data_dir()` and override:
    - `config.db_path = str(data_dir / "search")`
    - `config.log_file = str(data_dir / "logs" / "archon-search.log")`
    - `config.telemetry.log_dir = str(data_dir / "search-logs")`
  - Precedence: env var `ARCHON_SEARCH_DATA_DIR` overrides any TOML-sourced value for these three fields. Explicit env var `ARCHON_SEARCH_DATA_DIR` + explicit TOML `db_path` → DATA_DIR wins.
  - If `ARCHON_SEARCH_DATA_DIR` is set but empty → catch the `ValueError` from `get_data_dir()` in `load_config()` and re-raise as `ConfigError`. Implementation: wrap the `get_data_dir()` call in a try/except: `try: data_dir = get_data_dir() except ValueError as exc: raise ConfigError(str(exc)) from exc`. This ensures callers of `load_config()` only need to catch `ConfigError`, not `ValueError`.
- **Releasable**: after this task, `config.db_path`, `config.log_file`, and `config.telemetry.log_dir` are all driven by `ARCHON_SEARCH_DATA_DIR`.
- **Tests (TDD)** — `tests/test_config_env_overrides.py` (extend existing file):
  - Unit: `test_data_dir_overrides_db_path` — `ARCHON_SEARCH_DATA_DIR="/data"` → `config.db_path == "/data/search"`.
  - Unit: `test_data_dir_overrides_log_file` — `ARCHON_SEARCH_DATA_DIR="/data"` → `config.log_file == "/data/logs/archon-search.log"`.
  - Unit: `test_data_dir_overrides_telemetry_log_dir` — `ARCHON_SEARCH_DATA_DIR="/data"` → `config.telemetry.log_dir == "/data/search-logs"`.
  - Unit: `test_data_dir_overrides_toml_db_path` — TOML sets `db_path = "/toml/db"`, env sets `/data` → `config.db_path == "/data/search"`.
  - Unit: `test_data_dir_empty_raises` — `ARCHON_SEARCH_DATA_DIR=""` → `ConfigError`.
  - Unit: `test_serve_kwarg_with_data_dir` — `load_config(serve=True)` with `ARCHON_SEARCH_DATA_DIR="/data"` and no TOML → `config.host == "0.0.0.0"` AND `config.db_path == "/data/search"` AND `config.log_file == "/data/logs/archon-search.log"`. (Tests the full production container path combining Phase 1 and Phase 2 features. Placed here because DATA_DIR override logic is implemented in this task.)
  - Checkpoint: `uv run pytest tests/test_config_env_overrides.py -v`

#### Task 2.3 — key_manager.py: lazy key file path
- [ ] **File**: `archon_search/key_manager.py`
- **Depends on**: Task 2.1
- **Description**:
  - Remove the module-level `KEY_FILE: Path = ...` constant (lines 16–21).
  - Add `_get_key_file() -> Path`: if `ARCHON_SEARCH_KEY_FILE` env var is set and non-empty, return `Path(env).expanduser()`; otherwise return `get_data_dir() / ".search.env"`. Import `get_data_dir` from `archon_search.paths`.
  - Replace all six internal usages of `KEY_FILE` with `_get_key_file()`: `load_or_generate_key()` (source string), `_load_from_file()` (exists check, stat, read), `_generate_and_write()` (makedirs, payload construction, `.with_suffix()`).
  - `ARCHON_SEARCH_KEY_FILE` still takes precedence over `ARCHON_SEARCH_DATA_DIR` (evaluated inside `_get_key_file()`).
- **Releasable**: after this task, key file path is resolved lazily at call time, not at import time.
- **Tests (TDD)** — `tests/test_key_manager.py` (extend existing):
  - Unit: `test_get_key_file_default` — no env vars → `Path.home() / ".archon-search" / ".search.env"`.
  - Unit: `test_get_key_file_key_file_env` — `ARCHON_SEARCH_KEY_FILE="/custom/.env"` → `Path("/custom/.env")`.
  - Unit: `test_get_key_file_data_dir_env` — `ARCHON_SEARCH_DATA_DIR="/data"` → `Path("/data/.search.env")`.
  - Unit: `test_key_file_env_overrides_data_dir` — both set → `ARCHON_SEARCH_KEY_FILE` wins.
  - Unit: `test_no_module_level_key_file_constant` — `import inspect, archon_search.key_manager as km; src = inspect.getsource(km); assert "Path.home()" not in src` — verifies no import-time `Path.home()` evaluation remains, regardless of constant naming.
  - Checkpoint: `uv run pytest tests/test_key_manager.py -v`
- **Migration of existing tests (required)**: Every test that uses `monkeypatch.setattr(km, "KEY_FILE", ...)` must be updated. This includes tests in `tests/test_key_manager.py` (~20 occurrences) AND `tests/server/test_middleware_auth.py` (at least 1 occurrence — verify by grepping for `KEY_FILE` across all test files). Update all to use `monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", ...)` or `monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", ...)`. Do not leave any `setattr` patching of `KEY_FILE` — after removal, those patches silently become no-ops and the tests test nothing.

#### Task 2.4 — jobs/model.py + jobs/store.py: lazy jobs file path
- [ ] **Files**: `archon_search/jobs/model.py`, `archon_search/jobs/store.py`, `archon_search/jobs/__init__.py`
- **Depends on**: Task 2.1
- **Description**:
  - `jobs/model.py`: remove module-level `JOBS_FILE: Path = ...` constant (line 8). Add `get_jobs_file() -> Path` function that returns `get_data_dir() / "archon-search-jobs.json"`. Import `get_data_dir` from `archon_search.paths`. Update `__all__` to export `get_jobs_file` instead of (or alongside) `JOBS_FILE`.
  - `jobs/__init__.py`: replace `JOBS_FILE` import/re-export with `get_jobs_file`. Update `__all__`.
  - `jobs/store.py`: change `JobStore.__init__(self, path: Path = JOBS_FILE)` to `JobStore.__init__(self, path: Path | None = None)`. Inside `__init__`, resolve `self._path = path if path is not None else get_jobs_file()`. Remove `from archon_search.jobs.model import JOBS_FILE` import; import `get_jobs_file` instead.
- **Releasable**: after this task, jobs file path is resolved lazily; `JobStore()` with no argument picks up `ARCHON_SEARCH_DATA_DIR`.
- **Tests (TDD)** — `tests/test_jobs_paths.py`:
  - Unit: `test_get_jobs_file_default` — no env vars → `Path.home() / ".archon-search" / "archon-search-jobs.json"`.
  - Unit: `test_get_jobs_file_data_dir` — `ARCHON_SEARCH_DATA_DIR="/data"` → `Path("/data/archon-search-jobs.json")`.
  - Unit: `test_job_store_default_path_is_lazy` — instantiate `JobStore()` with `ARCHON_SEARCH_DATA_DIR="/data"` set → `store._path == Path("/data/archon-search-jobs.json")`.
  - Unit: `test_job_store_explicit_path_overrides` — `JobStore(path=Path("/custom/jobs.json"))._path == Path("/custom/jobs.json")`.
  - Checkpoint: `uv run pytest tests/test_jobs_paths.py -v`
- **Migration of existing tests (required)**: If any existing tests use `monkeypatch.setattr` on `JOBS_FILE`, update them to use `monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", ...)` instead.

#### Task 2.5 — language_detector.py + server/app.py + pipeline.py: lazy fasttext models dir
- [ ] **Files**: `archon_search/language_detector.py`, `archon_search/server/app.py`, `archon_search/pipeline.py`
- **Depends on**: Task 2.1
- **Description**:
  - `language_detector.py`: replace module-level `FASTTEXT_MODELS_DIR = Path.home() / ".archon-search" / "models"` with `get_fasttext_models_dir() -> Path` function that returns `get_data_dir() / "models"`. Keep `FASTTEXT_MODEL_FILENAME` constant as-is (it is a filename, not a path).
  - `server/app.py`: remove module-level `_MULTILINGUAL_MODEL_PATH: Path = FASTTEXT_MODELS_DIR / FASTTEXT_MODEL_FILENAME` (line 53). Replace usages (lines 86, 212) with a lazy call `get_fasttext_models_dir() / FASTTEXT_MODEL_FILENAME` at the call site. Update import to replace `FASTTEXT_MODELS_DIR` with `get_fasttext_models_dir`.
  - `pipeline.py`: update the two `FASTTEXT_MODELS_DIR` usages (lines 1008, 1030 — specifically inside `create_pipeline()`) to call `get_fasttext_models_dir()` at runtime. Remove the `from archon_search.language_detector import FASTTEXT_MODELS_DIR` import; add `from archon_search.language_detector import get_fasttext_models_dir`. This import MUST be updated — removing `FASTTEXT_MODELS_DIR` from `language_detector.py` without updating this callsite causes `ImportError`.
- **Releasable**: after this task, fasttext models dir is resolved lazily at call time; `ARCHON_SEARCH_DATA_DIR` correctly redirects model downloads.
- **Tests (TDD)** — `tests/test_language_detector_paths.py`:
  - Unit: `test_get_fasttext_models_dir_default` — no env vars → `Path.home() / ".archon-search" / "models"`.
  - Unit: `test_get_fasttext_models_dir_data_dir` — `ARCHON_SEARCH_DATA_DIR="/data"` → `Path("/data/models")`.
  - Unit: `test_no_module_level_fasttext_models_dir` — `import inspect, archon_search.language_detector as ld; assert "Path.home()" not in inspect.getsource(ld)`.
  - Unit: `test_no_module_level_multilingual_model_path` — `import inspect, archon_search.server.app as app; assert "Path.home()" not in inspect.getsource(app)` and `assert not isinstance(getattr(app, "_MULTILINGUAL_MODEL_PATH", None), Path)`.
  - Checkpoint: `uv run pytest tests/test_language_detector_paths.py -v`
- **Migration of existing tests (required)**: If any existing tests use `monkeypatch.setattr` on `FASTTEXT_MODELS_DIR`, update them to use `monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", ...)` instead.

#### Task 2.6 — cli/ingest.py: lazy history sessions path
- [ ] **File**: `archon_search/cli/ingest.py`
- **Depends on**: Task 2.1
- **Description**:
  - Line 25: replace `Path.home() / ".archon-search" / "history" / "sessions"` with `get_data_dir() / "history" / "sessions"`. Import `get_data_dir` from `archon_search.paths`.
  - This is inside a function body so it is already evaluated at call time; only the source of the base directory changes.
- **Releasable**: after this task, all seven runtime-state paths are driven by `ARCHON_SEARCH_DATA_DIR`.
- **Tests (TDD)** — `tests/test_cli_ingest_paths.py`:
  - Unit: `test_default_history_path` — with no env var, invokes `ingest` with no `--path` and captures the resolved path (`click.testing.CliRunner`); assert it ends with `.archon-search/history/sessions`.
  - Unit: `test_history_path_uses_data_dir` — `ARCHON_SEARCH_DATA_DIR="/data"` → resolved path ends with `/data/history/sessions`.
  - Checkpoint: `uv run pytest tests/test_cli_ingest_paths.py -v`

---

### Phase 3 — serve subcommand + container mode
> **Releasable**: after Task 3.2 — `archon-search serve` starts uvicorn in the foreground with `0.0.0.0` default and stderr logging when `ARCHON_SEARCH_CONTAINER=1` is set.

#### Task 3.1 — archon_search/cli/serve.py: serve subcommand
- [ ] **Files**: `archon_search/cli/serve.py` (new), `archon_search/cli/main.py`
- **Depends on**: Task 1.1
- **Description**:
  - `serve.py`: `@click.command()` named `serve`. Accepts `--config` option (same signature as `start.py`). Calls `load_config(config_path, serve=True)` then `run_server(config)`. Imports `run_server` from `archon_search.server.app`.
  - `serve` does NOT call `_get_service()`, `service.start()`, `launchd`, or `systemd`. It is a pure foreground-blocking call.
  - `main.py`: add `from archon_search.cli.serve import serve` and `main.add_command(serve)`.
  - Docstring on `serve`: `"""Start the archon-search server in the foreground (container / direct-run mode)."""`
  - The `serve=True` kwarg to `load_config()` sets the host default to `0.0.0.0` before TOML and env var processing (established in Task 1.1). An explicit `ARCHON_SEARCH_HOST` env var or TOML `host` key still overrides it.
- **Releasable**: after this task, `archon-search serve` is the `CMD` for the Docker container.
- **Tests (TDD)** — `tests/test_cli_serve.py`:
  - Unit: `test_serve_calls_run_server` — mock `run_server` and `load_config`; invoke `serve` via `CliRunner`; assert `run_server` was called once.
  - Unit: `test_serve_uses_serve_load_config` — assert `load_config` was called with `serve=True`.
  - Unit: `test_serve_host_defaults_to_0000` — with no env/TOML, `load_config` returns a config with `host == "0.0.0.0"`.
  - Unit: `test_serve_respects_host_env_var` — `ARCHON_SEARCH_HOST="192.168.1.1"` → config host is `"192.168.1.1"`.
  - Unit: `test_serve_does_not_call_service_management` — mock `_get_service`; invoke `serve`; assert `_get_service` was never called.
  - Integration: `test_serve_registered_in_cli` — `from archon_search.cli.main import main; assert "serve" in main.commands`.
  - Unit: `test_start_still_registered_in_cli` — `from archon_search.cli.main import main; assert "start" in main.commands` — verifies that adding `serve` did not accidentally remove or shadow the existing `start` command.
  - Checkpoint: `uv run pytest tests/test_cli_serve.py -v`

#### Task 3.2 — logging_setup.py: ARCHON_SEARCH_CONTAINER stderr handler
- [ ] **File**: `archon_search/logging_setup.py`
- **Depends on**: nothing (independent of Phase 2)
- **Description**:
  - In `configure_logging(config: SearchConfig)`, after the existing handler setup (whether or not a file handler was added), check `os.environ.get("ARCHON_SEARCH_CONTAINER") == "1"`.
  - If true: create a `logging.StreamHandler(sys.stderr)`, apply the same formatter (text or JSON per `config.log_format`), attach `CorrelationIdFilter`, set `logger.propagate = False`, and add the handler to the `archon_search` logger.
  - If `log_file` is empty (early-return branch), the check still runs — refactor the early return to a conditional skip of file-handler creation only, then fall through to the container handler check.
  - Implementation detail: when `log_file` is empty and `ARCHON_SEARCH_CONTAINER=1`, `logger.propagate` MUST be explicitly set to `False` after adding the stderr StreamHandler. Without this, logs go through BOTH the new StreamHandler AND the root logger's default stderr handler — producing duplicate log lines. The current code sets `propagate=False` only in the file-handler branch; the refactored code must set it in the container-handler branch too.
  - Precedence: explicit `log_file` in TOML + `ARCHON_SEARCH_CONTAINER=1` → both file handler AND stderr handler are active.
  - `ARCHON_SEARCH_CONTAINER` unset or `"0"` → no change to existing behaviour.
  - Import `os`, `sys` (already present or add).
- **Releasable**: after this task, container logs flow to stderr and are visible via `docker logs`.
- **Tests (TDD)** — `tests/test_logging_setup.py` (extend existing):
  - Unit: `test_container_env_adds_stderr_handler` — `ARCHON_SEARCH_CONTAINER=1`, call `configure_logging(config)` → `archon_search` logger has a `StreamHandler` targeting `sys.stderr`.
  - Unit: `test_container_env_unset_no_stderr_handler` — no env var, `log_file` set → no `StreamHandler` on `archon_search` logger.
  - Unit: `test_container_env_with_empty_log_file` — `ARCHON_SEARCH_CONTAINER=1`, `config.log_file=""` → stderr handler is present (the empty-log_file path still reaches the container check).
  - Unit: `test_container_env_with_log_file_adds_both` — `ARCHON_SEARCH_CONTAINER=1`, valid `log_file` → both file handler and stderr handler present.
  - Unit: `test_container_env_zero_does_not_add_handler` — `ARCHON_SEARCH_CONTAINER=0` → no stderr handler.
  - Unit: `test_container_env_with_empty_log_file_propagate_false` — `ARCHON_SEARCH_CONTAINER=1`, `config.log_file=""` → `logger.propagate` is `False` (no duplicate log lines through root logger).
  - Checkpoint: `uv run pytest tests/test_logging_setup.py -v`

---

### Phase 4 — Docker packaging
> **Releasable**: after Task 4.3 — a correctly built CPU image passes the automated smoke test.

#### Task 4.1 — Dockerfile + .dockerignore
- [ ] **Files**: `Dockerfile`, `.dockerignore`
- **Depends on**: Task 3.1 (serve subcommand must exist as the CMD) and Task 3.2 (ARCHON_SEARCH_CONTAINER stderr handler must exist before the Dockerfile sets ARCHON_SEARCH_CONTAINER=1)
- **Description**:
  - **Before writing**: verify the NVIDIA CUDA 12.1.1-cudnn8-runtime-ubuntu22.04 tag exists in the NVIDIA Container Registry (`nvcr.io` or Docker Hub `nvidia/cuda`). Verify whether `fastembed>=0.8.0` ships a `[gpu]` extra (`pip index versions fastembed` + inspect extras); if it does not, document the `pip uninstall onnxruntime && pip install onnxruntime-gpu` pattern instead.
  - `Dockerfile`:
    - The CPU/GPU variant selection uses the build-arg-as-base-image pattern (not traditional multi-stage with COPY --from). Specify these lines at the top of the Dockerfile:
      ```
      ARG BASE_IMAGE=python:3.12-slim
      FROM ${BASE_IMAGE}
      ```
    - The CI workflow passes `--build-arg BASE_IMAGE=python:3.12-slim` for CPU and `--build-arg BASE_IMAGE=nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04` (or ubuntu24.04 per verification) for GPU. This eliminates the need for conditional FROM logic. Update the release workflow steps (Task 5.1) to pass `BASE_IMAGE` instead of `VARIANT`.
    - GPU base image setup: when `BASE_IMAGE` is the NVIDIA CUDA image, the Dockerfile MUST also install Python 3.12 (see GPU Python 3.12 installation notes above). Consider using a FROM-AS multi-stage pattern as an alternative if the base-image-ARG approach becomes unwieldy:
      ```
      FROM python:3.12-slim AS cpu
      FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04 AS gpu
      ARG VARIANT=cpu
      FROM ${VARIANT} AS final
      ```
    - Either pattern is valid — the implementer must choose one and be consistent with the release workflow. Verify the chosen pattern with a local docker build before committing.
    - GPU Python 3.12 installation: IMPORTANT: Ubuntu 22.04 ships Python 3.10, not 3.12. Python 3.12 requires the deadsnakes PPA: `apt-get install -y software-properties-common && add-apt-repository ppa:deadsnakes/ppa && apt-get update && apt-get install -y python3.12 python3.12-venv python3.12-dev python3.12-distutils`. Alternatively, use `nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu24.04` (Ubuntu 24.04 ships Python 3.12) — verify this tag exists before deciding. Install `fastembed[gpu]` or `onnxruntime-gpu` (per verification above).
    - Both stages: install `tini` (via apt/pip), `uv`, then `pip install archon-search` (from PyPI, or `COPY . && pip install .` for local builds — use `COPY` for the plan).
    - Create non-root user and own the data dir: `RUN useradd --uid 1000 --no-create-home appuser && mkdir -p /data && chown appuser:appuser /data`.
    - `WORKDIR /app`, `USER appuser`.
    - Note: The `chown` is required: `VOLUME /data` creates an anonymous volume owned by root; without pre-creating `/data` with correct ownership, UID 1000 cannot write the key file on anonymous-volume runs (e.g., `docker run` without `-v`).
    - `ENV ARCHON_SEARCH_DATA_DIR=/data ARCHON_SEARCH_CONTAINER=1`.
    - `VOLUME /data`.
    - `EXPOSE 8765`.
    - `HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 CMD python -c "import urllib.request, sys; urllib.request.urlopen('http://localhost:8765/ready')" || exit 1` — use Python urllib (no curl dependency; `python:3.12-slim` does not include curl and installing it adds ~5MB plus apt dependency maintenance).
    - `ENTRYPOINT ["tini", "--"]`.
    - `CMD ["archon-search", "serve"]`.
  - `.dockerignore`: exclude `.git/`, `__pycache__/`, `*.pyc`, `*.pyo`, `.venv/`, `dist/`, `*.egg-info/`, `tests/`, `Documentation/`, `.github/`, `.pytest_cache/`, `.coverage`, `*.jsonl`.
- **Releasable**: after this task, `docker build .` (CPU, uses default `BASE_IMAGE`) and `docker build --build-arg BASE_IMAGE=nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04 .` (GPU) both succeed.
- **Tests (TDD)** — `tests/test_dockerfile_lint.py`:
  - Unit: `test_dockerfile_exists` — `Path("Dockerfile").exists()`.
  - Unit: `test_dockerignore_exists` — `Path(".dockerignore").exists()`.
  - Unit: `test_dockerfile_has_non_root_user` — `"appuser"` appears in `Dockerfile`.
  - Unit: `test_dockerfile_has_tini_entrypoint` — `ENTRYPOINT ["tini", "--"]` in `Dockerfile`.
  - Unit: `test_dockerfile_has_healthcheck` — `HEALTHCHECK` line present; also verify the line does NOT reference `curl`.
  - Unit: `test_dockerfile_has_data_dir_env` — `ARCHON_SEARCH_DATA_DIR=/data` in `ENV` line.
  - Unit: `test_dockerfile_has_container_env` — `ARCHON_SEARCH_CONTAINER=1` in `ENV` line.
  - Unit: `test_dockerignore_excludes_git` — `.git/` or `.git` in `.dockerignore`.
  - Checkpoint: `uv run pytest tests/test_dockerfile_lint.py -v`

#### Task 4.2 — docker-compose.yml
- [ ] **Files**: `docker-compose.yml`, `.env.example`
- **Depends on**: Task 4.1
- **Description**:
  - Three services: `archon-dev`, `archon-test`, `archon-prod`. Each has:
    - `image: archon-search:latest` (or `build: .` for local builds).
    - `environment:` block with `ARCHON_SEARCH_API_KEY: ${ARCHON_SEARCH_API_KEY:-}` (variable substitution — never a hardcoded value; without a persistent volume, the server auto-generates a key) and `ARCHON_SEARCH_DATA_DIR=/data`.
    - Note: Add a `.env.example` file alongside `docker-compose.yml` with `ARCHON_SEARCH_API_KEY=your-key-here` so operators know how to configure it.
    - Unique named volume mount: `archon-dev-data:/data`, `archon-test-data:/data`, `archon-prod-data:/data`.
    - Unique port mapping: dev `18765:8765`, test `18766:8765`, prod `8765:8765`.
    - `stop_grace_period: 30s`.
    - Comment: `# TLS termination is the operator's responsibility — reverse-proxy in front of this service.`
  - Named volumes section: declares `archon-dev-data`, `archon-test-data`, `archon-prod-data`.
  - Commented-out `archon-model-cache` named volume with a comment explaining: mount at `$HOME/.cache/fastembed` inside the container (or the fastembed cache path) to avoid re-downloading model weights on every container recreate.
  - Comment: `# LanceDB single-writer: do not mount the same named volume to more than one running container.`
  - Comment: `# Without a persistent volume, the API key regenerates on every start.`
- **Releasable**: after this task, `docker compose up archon-dev` starts a dev instance.
- **Tests (TDD)** — `tests/test_compose_lint.py`:
  - Unit: `test_compose_file_exists` — `Path("docker-compose.yml").exists()`.
  - Unit: `test_compose_has_three_services` — parse YAML, assert services `archon-dev`, `archon-test`, `archon-prod` present.
  - Unit: `test_compose_separate_named_volumes` — each service mounts a unique volume name.
  - Unit: `test_compose_stop_grace_period` — each service has `stop_grace_period: 30s`.
  - Unit: `test_compose_volumes_declared` — all three named volumes declared in top-level `volumes:`.
  - Unit: `test_compose_api_key_uses_variable_substitution` — read `docker-compose.yml` as raw text (not parsed YAML, since YAML parsing resolves variables); assert the string `${ARCHON_SEARCH_API_KEY` appears in the file and no literal hardcoded key value is present for the API_KEY field.
  - Checkpoint: `uv run pytest tests/test_compose_lint.py -v`

#### Task 4.3 — CI docker smoke test
- [ ] **File**: `tests/test_docker_smoke.py`
- **Depends on**: Task 4.1
- **Description**:
  - `@pytest.mark.docker` — excluded from default test run; run explicitly in CI with `-m docker`.
  - `@pytest.mark.skipif(not shutil.which("docker"), reason="docker not available")`.
  - Test `test_cpu_image_starts_and_serves_ready`:
    - `subprocess.run(["docker", "build", "-t", "archon-search:smoke-test", "."], check=True, timeout=300)`.
    - `container_id = subprocess.run(["docker", "run", "-d", "-e", "ARCHON_SEARCH_API_KEY=smoketest", "-p", "18765:8765", "archon-search:smoke-test"], capture_output=True, text=True, check=True).stdout.strip()` (note: do NOT use `--rm` with `-d`; the container must remain for cleanup; `Popen` does not capture stdout and must not be used here).
    - Poll `http://localhost:18765/ready` with `urllib.request` up to 30s (1s sleep between attempts).
    - Assert HTTP 200.
    - Cleanup: `docker rm -f <cid>` in `finally`.
  - Test `test_uid_1000_can_write_data_dir`:
    - `container_id = subprocess.run(["docker", "run", "-d", "--user", "1000", "-v", f"{tmp_dir}:/data", "-e", "ARCHON_SEARCH_API_KEY=smoketest", "archon-search:smoke-test"], capture_output=True, text=True, check=True).stdout.strip()` where `tmp_dir = tempfile.mkdtemp()` followed immediately by `os.chmod(tmp_dir, 0o777)` (created in the test body). The chmod is required: `tempfile.mkdtemp()` creates the directory owned by the current process UID (not 1000), so without chmod, UID 1000 inside the container cannot write to it. Use a `try/finally` block to ensure `subprocess.run(["docker", "rm", "-f", container_id])` and `shutil.rmtree(tmp_dir, ignore_errors=True)` are called in teardown.
    - Poll `/ready`; on success, verify `tmp_dir` contains `.search.env` (key file created by UID 1000).
  - Note: the `docker` marker MUST be added to `pyproject.toml` under `[tool.pytest.ini_options].markers` (not just conftest.py) because `addopts` uses `--strict-markers`. Example: `"docker: tests that require a running Docker daemon and perform a real image build"`.
- **Releasable**: after this task, CI has an automated gate that the CPU image builds and serves traffic.
- **Tests (TDD)**: the tests in this file ARE the deliverable — no separate test file needed.
  - Unit: `test_docker_marker_in_pyproject` — read `pyproject.toml` and assert the `[tool.pytest.ini_options].markers` list contains an entry starting with `"docker"`. This guards against `--strict-markers` rejecting the docker tests if the marker registration is omitted.
  - Checkpoint: `uv run pytest tests/test_docker_smoke.py -v -m docker`

---

### Phase 5 — Release pipeline + final verification
> **Releasable**: after Task 5.1 — every tag push also publishes CPU and GPU images to GHCR. After Task 5.2 — all documentation reflects the delivered implementation.

#### Task 5.1 — Extend archon-search-release.yml: build and push images to GHCR
- [ ] **File**: `.github/workflows/archon-search-release.yml`
- **Depends on**: Task 4.1
- **Description**:
  - Add a new job `docker` that runs after the existing `test` job (add `needs: test`).
  - Steps:
    1. `actions/checkout@v4` with `fetch-depth: 0`.
    2. `docker/setup-buildx-action@v3`.
    3. `docker/login-action@v3` with `registry: ghcr.io`, `username: ${{ github.actor }}`, `password: ${{ secrets.GITHUB_TOKEN }}`.
    4. Extract image tag from the git tag: `TAG=${GITHUB_REF_NAME}` (the tag name, e.g. `26.6.42`). Also tag as `latest`.
    5. Build and push CPU image:
       ```
       # Pass BASE_IMAGE (or VARIANT for multi-stage) matching the pattern chosen in the Dockerfile.
       docker buildx build --push \
         --build-arg BASE_IMAGE=python:3.12-slim \
         -t ghcr.io/${{ github.repository_owner }}/archon-search:$TAG \
         -t ghcr.io/${{ github.repository_owner }}/archon-search:latest \
         .
       ```
    6. Build and push GPU image:
       ```
       # Pass BASE_IMAGE (or VARIANT for multi-stage) matching the pattern chosen in the Dockerfile.
       docker buildx build --push \
         --build-arg BASE_IMAGE=nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04 \
         -t ghcr.io/${{ github.repository_owner }}/archon-search:$TAG-gpu \
         -t ghcr.io/${{ github.repository_owner }}/archon-search:gpu \
         .
       ```
    7. Add `continue-on-error: true` to the GPU build step. IMPORTANT: also add a post-build step that adds a GitHub Actions job summary annotation (`echo "⚠️ GPU image build failed — :gpu tag not pushed" >> $GITHUB_STEP_SUMMARY`) when the GPU build step exits non-zero. Without this, a failed GPU build passes silently and operators who pull `:gpu` or `:TAG-gpu` get "image not found" with no warning.
  - The docker smoke test (Task 4.3) does NOT run in this job — it is a developer tool, not a release gate (GPU runner not available in standard CI). Document this decision with a comment in the workflow.
- **Releasable**: after this task, `release.sh` publishes both image variants to GHCR automatically.
- **Tests (TDD)**: no automated test — verify by reading the workflow YAML.
  - Checkpoint: manually review YAML syntax with `python -c "import yaml; yaml.safe_load(open('.github/workflows/archon-search-release.yml'))"`.

#### Task 5.2 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to discover all documentation in the project (READMEs, ADRs, API docs, architecture docs, user guides, BREAKING.md) and update every file whose content is affected by the changes delivered in this plan. The agent must not update docs that are unrelated.
  - Files that will likely need updates: `README.md` (add "Running with Docker" section), `Documentation/Architecture/100_system_architecture_overview.md` (container deployment unit), `Documentation/Architecture/150_security_and_privacy_architecture.md` (DATA_DIR, container logging), `Documentation/Architecture/160_operational_readiness_monitoring_and_reliability.md` (Docker HEALTHCHECK, serve command), `Documentation/Architecture/600_api_reference_or_public_interface.md` (serve subcommand), `Documentation/UserManual/` (operator guide for Docker), `CLAUDE.md` (serve subcommand in common commands section).
  - Verify all acceptance criteria below are met before marking complete.
- **Releasable**: after this task, the feature is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - `docker build -t archon-search:test .` exits 0 (CPU default).
  - `docker build --build-arg BASE_IMAGE=nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04 -t archon-search:test-gpu .` exits 0 (use the actual verified tag).
  - `docker run --rm -e ARCHON_SEARCH_API_KEY=test -p 18765:8765 archon-search:test archon-search serve &` → `curl http://localhost:18765/ready` returns HTTP 200 within 30s.
  - `docker run --rm --user 1000 -v /tmp/test-data:/data -e ARCHON_SEARCH_API_KEY=test archon-search:test archon-search serve &` → `/tmp/test-data/.search.env` is created.
  - `uv run pytest tests/test_config_env_overrides.py tests/test_config_defaults.py tests/test_paths.py tests/test_key_manager.py tests/test_jobs_paths.py tests/test_language_detector_paths.py tests/test_cli_ingest_paths.py tests/test_cli_serve.py tests/test_logging_setup.py tests/test_dockerfile_lint.py tests/test_compose_lint.py -v` — all pass.
  - `uv run pytest` (default suite) exits 0 with coverage ≥ 85%.
  - `ARCHON_SEARCH_HOST=0.0.0.0 ARCHON_SEARCH_PORT=9000 uv run archon-search serve` starts and `curl http://localhost:9000/ready` returns HTTP 200.
  - `ARCHON_SEARCH_DATA_DIR=/tmp/archon-test uv run archon-search serve &` → `/tmp/archon-test/.search.env` is created on startup.
  - No `grep -rn "Path.home()" archon_search/key_manager.py archon_search/jobs/model.py archon_search/language_detector.py` matches (import-time paths removed).
  - README contains a "Running with Docker" section with at minimum the `docker run` one-liner and the persistent-volume warning.
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked.
