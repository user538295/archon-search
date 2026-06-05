# C2 — Multilingual Retrieval
**Purpose**: Unlock language detection, the blocked `language` filter, and multilingual embedding support so operators on non-English corpora get filterable, quality-ranked results.
**Audience**: archon-search contributors implementing C2; reviewers of the resulting PRs.
**Status**: To Do

---

## Background

Language detection has been blocked since the initial schema landed. Three code sites reference "C2" as the forward pointer:

- `filters.py:_validate_language` — raises `ValueError` on any non-null `language` value
- `store_filters.py:build_where` — hard-asserts `language is None` before generating SQL
- `_types.py:ChunkRecord.language` docstring — "reserved; populated by C2"

The `language` column exists in LanceDB with type `pa.utf8()`. The write path already stores `c.language or ""`. The read path collapses it back to `None` via `row.get("language") or None`, erasing the three-state contract. The schema plumbing is done; what remains is: language detection, the install flow, the filter unlock, a startup guard, and the eval gate.

The full design and all key decisions are in `Documentation/Backlog/multilingual-retrieval-brief.md`.

---

## Goal

After C2 ships: operators ingesting non-English corpora get per-document language tags populated automatically. The `language=<code>` filter returns only matching chunks on single-collection queries. Server startup fails clearly when `multilingual=true` but the required package or model file is absent. The eval harness passes with multilingual fixtures and a documented before/after recall@5 on non-English content.

---

## Scope

### In Scope
- `[multilingual]` optional extra in `pyproject.toml` with `fasttext-wheel`
- `language_detection_confidence_threshold: float = 0.7` config key in `SearchConfig`
- `LanguageDetector` module: loads `lid.176.ftz`, detects per-document language in thread pool, strips `__label__`, normalizes to ISO 639-1 (2-letter) or ISO 639-3 (3-letter), returns `"unknown"` below threshold
- `_prompt_fasttext_license()` + `_download_fasttext_model()` in `install.py`, consistent with Jina license gate pattern
- `--accept-fasttext-license` CLI flag on `archon-search install`
- Startup check: fail with clear error distinguishing "package missing" from "model file missing" when `multilingual=true`
- `DocumentChunker.chunk()` gains keyword-only `language: str = ""` parameter
- `pipeline.py:ingest_file` calls `LanguageDetector.detect()` after parse, before chunk; propagates tag to all records
- Store read path fix: preserve `""` as a distinct three-state value (not coerced to `None`)
- `filters.py`: replace hard-block validator with passthrough validation accepting ISO codes + `"unknown"`
- `store_filters.py:build_where`: add `language = '<code>'` SQL clause; remove hard-block assertion
- `FilterFlags.language_filter_used: bool` in `telemetry/entry.py`
- `/status` warning when `multilingual=true` and a collection contains chunks with `language=""`
- MCP `search` and `search_with_context` tool description updates to reflect `language` as valid filterable parameter (single-collection only)
- Multilingual eval fixtures (minimum one non-English language), updated `thresholds.toml`, documented before/after recall@5 comparison
- LanceDB FTS language tokenizer spike (gated: if Python API does not support it, dropped without workaround)

### Out of Scope
- Per-chunk language detection
- Per-query model routing or chunk-level model routing
- Operator UI language / i18n
- Language-based collection routing
- Automatic reindex trigger on profile switch
- Language filter for multi-collection fan-out queries (v1 limitation inherited)

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task F.1 — Final verification & documentation update].

---

## What does NOT change
- Multi-collection fan-out query path — language filter has no effect there (v1 limitation; not new behavior)
- `--force --delete-db` requirement for profile switches — existing reinstall guard in `install.py` is unchanged
- FTS index rebuild logic — only modified if Task 1.1 spike confirms LanceDB Python API supports language tokenizer config
- `store.py` write path — `c.language or ""` is already correct; only the read path changes
- `SearchConfig.multilingual` type and TOML key — already exists at `config.py:66`

---

## Known limitations / accepted trade-offs
- Mixed-language documents: single detected language assigned to all chunks; minority-language chunks tagged with majority language
- Language filter for multi-collection fan-out: not supported in v1; documented in operator-facing output and MCP tool descriptions
- Profile switch is destructive: switching multilingual profile requires `--force --delete-db`; all data must be re-ingested; this is pre-existing behavior, not new
- Air-gapped deployments: `lid.176.ftz` must be copied manually after install
- fasttext outputs `__label__xx` codes; the normalization table covers common codes but rare/regional codes fall through to the raw fasttext code (still a valid, filterable value)

---

## Architecture

### New modules
- `archon_search/language_detector.py` — `LanguageDetector(model_path: Path)` class; `async detect(text: str, *, confidence_threshold: float) -> str`; `_FASTTEXT_ISO_MAP: dict[str, str]` for ISO 639-3 → 639-1 normalization; runs fasttext in thread pool via `asyncio.to_thread()`, same pattern as `Embedder` and `Reranker`

### Modified modules
- `pyproject.toml` — new `[project.optional-dependencies] multilingual = ["fasttext-wheel"]`
- `archon_search/config.py` — `SearchConfig.language_detection_confidence_threshold: float = 0.7`; TOML key `language_detection_confidence_threshold` under `[database]`
- `archon_search/_types.py` — `ChunkRecord.language`: `str | None = None` → `str = ""`; `SearchResult.language`: `str | None = None` → `str = ""`
- `archon_search/store.py` (read path, 3 sites) — `row.get("language") or None` → `row.get("language") or ""` to preserve three-state (handles both Arrow NULL and empty string)
- `archon_search/_diagnostics.py` — `ScoredSearchCandidate.language`: `str | None = None` → `str = ""`
- `archon_search/server/routes_search.py` — `SearchResultSchema.language`: `str | None = None` → `str = ""`; OpenAPI snapshot updated
- `archon_search/server/routes_explain.py` — `ExplainResult.language` and `ExplainNearMiss.language`: `str | None = None` → `str = ""`
- `archon_search/chunker.py` — `chunk()` gains keyword-only `language: str = ""`; assigns to each `ChunkRecord.language`
- `archon_search/pipeline.py` — `ingest_file()`: after parse, before chunk call, detect language if `config.multilingual`; pass result to `self._chunker.chunk(..., language=lang)`; propagation via chunker is sufficient (no post-chunk loop needed)
- `archon_search/install.py` — `_prompt_fasttext_license(non_interactive, accept_fasttext_license)`, `_download_fasttext_model(models_dir: Path)`, wired into the install flow after Jina gate when `multilingual=true`
- `archon_search/cli/install_cmd.py` — `--accept-fasttext-license` flag added to `_install_options`
- `archon_search/server/app.py` — `_check_multilingual_deps(config)` called in `lifespan` startup
- `archon_search/filters.py` — `_validate_language` replaced: allow `None`, `"unknown"`, and `[a-z]{2,3}` pattern; reject empty string and codes > 3 chars
- `archon_search/store_filters.py` — `build_where`: remove hard-block on `language`; add `language = '<code>'` clause when non-null
- `archon_search/telemetry/entry.py` — `FilterFlags.language_filter_used: StrictBool = False`; `from_search_filters` updated
- `archon_search/server/routes_status.py` — per-collection warning when `multilingual=true` and untagged chunks detected
- `archon_search/server/mcp.py` — `search` and `search_with_context` tool descriptions updated

### New config keys
- `language_detection_confidence_threshold` (float, default `0.7`) under `[database]` section in `archon-search.toml`

### New model path
- `~/.archon-search/models/lid.176.ftz` — downloaded at install when `multilingual=true`

### Three-state language contract (invariant)
- `""` — never processed (legacy, pre-C2 chunks)
- `"unknown"` — processed, confidence below threshold
- `"<code>"` — detected ISO 639-1 or ISO 639-3 code
- `language=fr` filter: returns `fr`-tagged only; excludes `""` and `"unknown"`
- `language=unknown` filter: returns `"unknown"`-tagged only
- No filter: returns all three states

---

## Task breakdown

### Phase 1 — Spike: LanceDB FTS Language Tokenizer
> **Releasable**: after Task 1.1; decision recorded gates whether Phase 12 is executed.

#### Task 1.1 — Spike: confirm LanceDB FTS language tokenizer support
- [x] **File**: `Documentation/Architecture/spikes/C2-fts-tokenizer-spike.md` (new, not committed to main — result drives Phase 12 decision)
- **Depends on**: nothing
- **Description**:
  - Read `lancedb` Python API source and/or docs to determine if `lancedb.index.FTS()` or any `create_index` variant accepts a `language` / `tokenizer` parameter
  - Current usage in `store.py:rebuild_fts_index`: `await table.create_index("text", config=FTS(), replace=True)` — check if `FTS(language="french")` or equivalent is valid
  - If supported: document the exact API call, parameter name, and how language codes map (e.g., `"english"`, `"french"` vs ISO codes)
  - **Secondary spike**: verify whether LanceDB/DataFusion SQL supports `GROUP BY` / `ORDER BY` / aggregate queries (`count(*)`) on table columns — needed by `get_dominant_language` in Task 12.1. Document the answer alongside the FTS finding. If `GROUP BY` is not supported, `get_dominant_language` must be implemented as a Python-side scan and tally.
  - **Tertiary spike**: verify `WHERE column = ''` (equality with empty string) returns correct counts in LanceDB/DataFusion — needed by `count_untagged_language_chunks` in Task 9.1. Standard SQL distinguishes `''` from `NULL`; DataFusion should too, but confirm explicitly.
  - If FTS language tokenizer not supported: record the finding; Phase 12 is dropped; add a one-line note to `BREAKING.md` as future work
  - This is a research task — no production code changes
- **Releasable**: after this task, the Phase 12 decision is settled and documented.
- **Tests (TDD)**: N/A — spike only; result determines whether Phase 12 tasks are executed
- **Checkpoint**: manual review of spike document

---

### Phase 2 — Foundation: Package, Config, and Store Read Path
> **Releasable**: after Task 2.3; legacy read path is fixed and the three-state invariant is enforced.

#### Task 2.1 — Add `[multilingual]` optional extra to `pyproject.toml`
- [x] **File**: `pyproject.toml`
- **Depends on**: nothing
- **Description**:
  - Add under `[project.optional-dependencies]`: `multilingual = ["fasttext-wheel"]`
  - `fasttext-wheel` provides pre-built wheels; avoids C++ compilation on all platforms
  - Do not add to `dev` or `all` extras — must remain opt-in
- **Releasable**: after this task, `pip install archon-search[multilingual]` installs fasttext-wheel.
- **Tests (TDD)** — `tests/test_pyproject.py` (new file) or `tests/config/test_extras.py`:
  - Unit: `test_multilingual_extra_declared` — parse `pyproject.toml`, assert `multilingual` key exists under `optional-dependencies` and contains a string matching `fasttext-wheel`
  - Checkpoint: `uv run pytest tests/test_pyproject.py -x`

#### Task 2.2 — Add `language_detection_confidence_threshold` to `SearchConfig`
- [ ] **File**: `archon_search/config.py`
- **Depends on**: nothing
- **Description**:
  - Add `language_detection_confidence_threshold: float = 0.7` to `SearchConfig` dataclass
  - In `load_config()`: parse from `database["language_detection_confidence_threshold"]` with `_coerce_float` (or equivalent); validate range `0.0 < value <= 1.0`; raise `ConfigError` if out of range
  - Update `archon-search.toml.example` to include the key with a comment
- **Releasable**: after this task, the threshold is configurable via TOML.
- **Tests (TDD)** — `tests/test_config.py`:
  - Unit: `test_default_confidence_threshold` — load config with no `language_detection_confidence_threshold` key; assert value is `0.7`
  - Unit: `test_custom_confidence_threshold` — load config with `language_detection_confidence_threshold = 0.5`; assert value is `0.5`
  - Unit: `test_confidence_threshold_out_of_range` — value `1.5` raises `ConfigError`
  - Unit: `test_confidence_threshold_zero` — value `0.0` raises `ConfigError`
  - Unit: `test_confidence_threshold_negative` — value `-0.1` raises `ConfigError`
  - Unit: `test_confidence_threshold_upper_bound` — value `1.0` succeeds (exact upper boundary is valid)
  - Checkpoint: `uv run pytest tests/test_config.py -x -k "threshold"`

#### Task 2.3 — Fix `store.py` read path: preserve three-state language value
- [ ] **File**: `archon_search/store.py`
- **Depends on**: nothing
- **Description**:
  - Three sites to update (lines 1464, 1639, 1889 approx): `language=row.get("language") or None` → `language=row.get("language") or ""`
  - The change: `or None` collapses `""` to `None`, erasing the legacy-vs-missing distinction. `or ""` maps both `None` (Arrow NULL, e.g. from external tools) and `""` (legacy sentinel) to `""` — both are valid representations of "never processed." Using `row.get("language", "")` would NOT protect against Arrow NULL values (LanceDB returns `{"language": None}` for NULL, not a missing key); `or ""` is the correct form.
  - `SearchResult.language` type is `str | None` — update to `str = ""` in `_types.py` to match the three-state contract; `None` is no longer a valid output from the read path
  - `ChunkRecord.language` in `_types.py` (line 70): change from `str | None = None` to `str = ""` — aligns with three-state; `None` was only a schema placeholder
  - The write path `"language": c.language or ""` in `store.py` is already correct — no change needed there
  - Update `ScoredSearchCandidate.language` in `archon_search/_diagnostics.py` (line ~80): `str | None = None` → `str = ""`; this ensures the explain/hybrid-search-with-trace path uses the same three-state contract
  - Update `SearchResultSchema.language` in `archon_search/server/routes_search.py`: `str | None = None` → `str = ""`; keeps the OpenAPI schema aligned; update the OpenAPI snapshot in `tests/` as part of this task
  - Update `ExplainResult.language` in `archon_search/server/routes_explain.py` (line ~92): `str | None = None` → `str = ""`
  - Update `ExplainNearMiss.language` in `archon_search/server/routes_explain.py` (line ~129): `str | None = None` → `str = ""`
  - Audit all `if result.language is not None:` / `if r.language is not None:` checks across the codebase for `SearchResult`, `ScoredSearchCandidate`, `SearchResultSchema`, `ExplainResult`, and `ExplainNearMiss` — update any `is None` guard to `== ""` or `!= ""` as appropriate; known sites include `tests/test_types.py:193`, `tests/test_store.py:3143,3180`, `tests/test_metadata_schema.py:316`, `tests/test_store_trace.py:172,177`, `tests/test_diagnostics.py:61`
  - Also delete `test_openapi_schema_language_description_says_reserved_c2` in `tests/server/test_routes_search.py:604` (asserts the old "reserved"/"C2" description which will change)
  - Add entry to `BREAKING.md`: (a) Python: `SearchResult.language`, `ScoredSearchCandidate.language`, `ExplainResult.language`, `ExplainNearMiss.language` now return `""` for legacy/untagged chunks instead of `None` — update `is None` guards to `== ""`; (b) REST/JSON: the `language` field in search and explain responses now serializes as `""` (empty string) instead of `null` — OpenAPI clients must update their type stubs accordingly (`nullable: false`)
- **Releasable**: after this task, legacy chunks return `language=""` from queries instead of `None`.
- **Tests (TDD)** — `tests/test_store.py` (add to existing):
  - Unit: `test_read_empty_language_preserved` — ingest chunk with `language=""`, search, assert result `language == ""`
  - Unit: `test_read_specific_language_preserved` — ingest chunk with `language="fr"`, assert result `language == "fr"`
  - Unit: `test_read_unknown_language_preserved` — ingest chunk with `language="unknown"`, assert result `language == "unknown"`
  - Unit: `test_scored_search_candidate_language_empty_not_none` — construct `ScoredSearchCandidate` without language; assert `language == ""`
  - Unit: `test_openapi_language_field_not_nullable` — the OpenAPI JSON schema for `SearchResultSchema.language` must not list `"null"` as a valid type after the change
  - Unit: `test_explain_result_language_field_three_state` — run explain pipeline with a chunk tagged `language="fr"`; assert `ScoredSearchCandidate.language == "fr"`; run with untagged chunk; assert `language == ""`
  - Integration: `test_explain_http_language_empty_not_null` — POST to `/explain` with a legacy chunk; assert JSON response `top_results[0].language == ""` (not `null`); assert `near_misses[0].language == ""` (not `null`)
  - Checkpoint: `uv run pytest -x -k "language"` (all test files, not just `test_store.py`, to surface any `language is None` assertions in other files)

---

### Phase 3 — Language Detector Module
> **Releasable**: after Task 3.1; language detection is callable from anywhere in the codebase.

#### Task 3.1 — `LanguageDetector` class in `archon_search/language_detector.py`
- [ ] **File**: `archon_search/language_detector.py`
- **Depends on**: Task 2.2 (confidence threshold config key is defined)
- **Description**:
  - `_FASTTEXT_ISO_MAP: dict[str, str]` — mapping from fasttext 3-letter codes to ISO 639-1 2-letter codes for languages that have one (e.g. `"deu" → "de"`, `"fra" → "fr"`); fasttext codes are already mostly ISO 639-1, this map handles the exceptions
  - `class LanguageDetector`: `__init__(self, model_path: Path) -> None` — lazy-loads fasttext model; raises `RuntimeError("fasttext-wheel not installed")` if import fails; raises `FileNotFoundError` if `model_path` does not exist
  - `async def detect(self, text: str, *, confidence_threshold: float) -> str` — strips newlines (`text[:2000].replace("\n", " ")`) before calling `self._model.predict(cleaned, k=1)` — fasttext predict expects single-line input; runs in thread via `asyncio.to_thread(self._model.predict, cleaned, 1)` to avoid blocking; strips `__label__` prefix from returned label; applies `_normalize_lang_code`; returns `"unknown"` if confidence (top-1 probability) < `confidence_threshold`; returns `"unknown"` if `text` is empty or whitespace-only
  - `def _normalize_lang_code(code: str) -> str` — looks up in `_FASTTEXT_ISO_MAP`; returns 2-letter if found; returns raw code otherwise (ISO 639-3 passthrough)
  - Module-level `FASTTEXT_MODEL_FILENAME = "lid.176.ftz"` and `FASTTEXT_MODELS_DIR = Path.home() / ".archon-search" / "models"` constants
- **Releasable**: after this task, callers can `await detector.detect(text, confidence_threshold=0.7)` and receive a normalized ISO code or `"unknown"`.
- **Tests (TDD)** — `tests/test_language_detector.py`:
  - Unit: `test_detect_english_text` — mock fasttext predict returning `(["__label__en"], [0.99])`; assert result is `"en"`
  - Unit: `test_detect_french_text` — mock predict returning `(["__label__fr"], [0.95])`; assert result is `"fr"`
  - Unit: `test_detect_below_threshold` — mock predict returning `(["__label__de"], [0.4])`; threshold `0.7`; assert result is `"unknown"`
  - Unit: `test_detect_empty_text` — pass `""` or `"   "`; assert result is `"unknown"` without calling fasttext
  - Unit: `test_normalize_lang_code_passthrough` — `_normalize_lang_code("fr")` returns `"fr"` (already 2-letter)
  - Unit: `test_detect_model_not_installed` — importing fasttext raises `ImportError`; `LanguageDetector.__init__` raises `RuntimeError`
  - Unit: `test_detect_model_file_missing` — `FileNotFoundError` raised for missing model path
  - Unit: `test_detect_runs_in_thread` — confirm `asyncio.to_thread` is called (mock it)
  - Unit: `test_detect_strips_newlines` — input `"bonjour\nmonde"` is cleaned to single line before predict
  - Unit: `test_normalize_lang_code_3_to_2` — `_normalize_lang_code("fra")` returns `"fr"` (3-letter to 2-letter)
  - Unit: `test_normalize_lang_code_unknown_3letter` — `_normalize_lang_code("xxx")` returns `"xxx"` (passthrough)
  - Unit: `test_detect_truncates_long_text` — pass 5000-char string; verify predict receives at most 2000 chars
  - Checkpoint: `uv run pytest tests/test_language_detector.py -x`

---

### Phase 4 — Install: fasttext Model Download + License Gate
> **Releasable**: after Task 4.3; `archon-search install --multilingual` downloads the fasttext model with license gate.

#### Task 4.1 — `_prompt_fasttext_license()` in `install.py`
- [ ] **File**: `archon_search/install.py`
- **Depends on**: nothing
- **Description**:
  - `def _prompt_fasttext_license(non_interactive: bool, accept_fasttext_license: bool = False) -> None`
  - Prints: `"WARNING: lid.176.ftz (fasttext language identification model) is licensed CC-BY-SA 3.0.\nThis model was created by Facebook Research and redistributed under CC-BY-SA 3.0.\nYou must comply with its terms for any use."`
  - If `accept_fasttext_license=True`: return immediately (non-interactive acceptance)
  - If `non_interactive=True`: print `"Non-interactive mode: fasttext license automatically declined."` and `raise SystemExit(1)`
  - Otherwise: prompt with `input("Type 'accept' to confirm license acceptance and continue, or anything else to abort: ")`; return on `"accept"`, else print `"License not accepted. Aborting."` and `raise SystemExit(1)`
  - Pattern mirrors `_prompt_jina_license` exactly
- **Releasable**: after this task, the license gate is callable from the install flow.
- **Tests (TDD)** — `tests/test_install.py` (add to existing):
  - Unit: `test_fasttext_license_accepted_flag` — `accept_fasttext_license=True` returns without printing or prompting
  - Unit: `test_fasttext_license_non_interactive_declines` — `non_interactive=True` raises `SystemExit(1)`
  - Unit: `test_fasttext_license_interactive_accept` — mock `input` returning `"accept"`; returns without raising
  - Unit: `test_fasttext_license_interactive_decline` — mock `input` returning `"no"`; raises `SystemExit(1)`
  - Checkpoint: `uv run pytest tests/test_install.py -x -k "fasttext_license"`

#### Task 4.2 — `_download_fasttext_model()` in `install.py`
- [ ] **File**: `archon_search/install.py`
- **Depends on**: Task 4.1
- **Description**:
  - `FASTTEXT_MODEL_URL = "https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz"` — module-level constant
  - `def _download_fasttext_model(models_dir: Path) -> None` — creates `models_dir` (mode `0o700`) if absent; target path is `models_dir / "lid.176.ftz"`; if file already exists, log at DEBUG and return; downloads via `urllib.request.urlretrieve` with progress output to stderr; raises `InstallError` on network failure
  - `urlretrieve` is synchronous; install runs in a blocking subprocess (not async), so no `asyncio.to_thread()` wrapping needed. However, `urlretrieve` has no socket timeout and can hang indefinitely. Use `urllib.request.urlopen(url, timeout=120)` + chunked `shutil.copyfileobj` instead of bare `urlretrieve` — this provides an explicit 120-second socket timeout and progress logging
  - After download: assert file size > 0; attempt to open with `fasttext.load_model(str(path))` in a subprocess or try-read to detect corruption; if the file is corrupt (size=0 or load fails), delete it and raise `InstallError("fasttext model download appears corrupt; re-run install")`
  - Wire into the install flow: after Jina license gate, if `is_multilingual=True` and not `skip_preload`: call `_prompt_fasttext_license(non_interactive, accept_fasttext_license)` then `_download_fasttext_model(Path.home() / ".archon-search" / "models")`
  - Print step: `"[4b/5] Downloading fasttext language model..."` (consistent with existing step printing pattern)
- **Releasable**: after this task, `archon-search install --multilingual` downloads `lid.176.ftz`.
- **Tests (TDD)** — `tests/test_install.py`:
  - Unit: `test_download_fasttext_model_skips_if_exists` — file already present; assert `urlretrieve` not called
  - Unit: `test_download_fasttext_model_creates_dir` — `models_dir` does not exist; assert directory created with `0o700`
  - Unit: `test_download_fasttext_model_network_error` — mock `urlretrieve` raising `URLError`; assert `InstallError` raised
  - Checkpoint: `uv run pytest tests/test_install.py -x -k "download_fasttext"`

#### Task 4.3 — `--accept-fasttext-license` CLI flag
- [ ] **File**: `archon_search/cli/install_cmd.py`
- **Depends on**: Task 4.1, Task 4.2
- **Description**:
  - Add to `_install_options` decorator: `click.option("--accept-fasttext-license", is_flag=True, default=False, help="Accept fasttext lid.176.ftz CC-BY-SA 3.0 license")`
  - Add `accept_fasttext_license: bool` parameter to `_run_installer()` and thread it through to `install.py` install function call
  - The `install.py` install function (`_run_install` or equivalent) must accept and forward `accept_fasttext_license` to `_prompt_fasttext_license`
- **Releasable**: after this task, CI and non-interactive deployments can use `--accept-fasttext-license`.
- **Tests (TDD)** — `tests/cli/test_install_cmd.py` (add):
  - Unit: `test_accept_fasttext_license_flag_present` — invoke `install --help`; assert `--accept-fasttext-license` appears in output
  - Integration: `test_install_multilingual_non_interactive_with_flag` — mock install flow; assert `_prompt_fasttext_license` called with `accept_fasttext_license=True` when flag is set
  - Checkpoint: `uv run pytest tests/cli/test_install_cmd.py -x -k "fasttext"`

---

### Phase 5 — Startup Guard
> **Releasable**: after Task 5.1; server fails clearly at startup when multilingual deps are absent.

#### Task 5.1 — `_check_multilingual_deps()` startup check in `app.py`
- [ ] **File**: `archon_search/server/app.py`
- **Depends on**: Task 3.1 (LanguageDetector module, provides model path constant)
- **Description**:
  - `def _check_multilingual_deps(config: SearchConfig) -> None` — called in the synchronous `create_app()` body, BEFORE `SearchPipeline` is constructed (line ~164 in `app.py`). Note: `SearchPipeline` is constructed outside the lifespan function, so the check cannot be deferred to lifespan startup.
  - If `config.multilingual` is `False`: return immediately
  - Check 1 — package: `import fasttext` inside try/except `ImportError`; if fails, raise `RuntimeError("multilingual=true but fasttext-wheel is not installed; run: pip install archon-search[multilingual]")`
  - Check 2 — model file: check `Path.home() / ".archon-search" / "models" / "lid.176.ftz"` exists; if not, raise `RuntimeError("multilingual=true but lid.176.ftz model is missing; run: archon-search install --multilingual")`
  - Both errors must be distinct and actionable; do not combine into one message
  - Ordering constraint: `_check_multilingual_deps(config)` must be called in the synchronous `create_app()` body (before `lifespan` yields), or immediately at the top of `lifespan` before any `await` — specifically before `SearchPipeline` is instantiated with `language_detector`. If the pipeline is constructed at app creation time (outside lifespan), the check must also run before that construction point.
- **Releasable**: after this task, server startup with `multilingual=true` and missing deps raises a clear `RuntimeError` before accepting requests.
- **Tests (TDD)** — `tests/server/test_app.py` (add):
  - Unit: `test_check_multilingual_deps_disabled` — `multilingual=False`; no import attempted; returns without error
  - Unit: `test_check_multilingual_deps_package_missing` — `multilingual=True`; mock `fasttext` import to raise `ImportError`; assert `RuntimeError` with "fasttext-wheel" in message
  - Unit: `test_check_multilingual_deps_model_missing` — `multilingual=True`; mock import succeeds; model file absent; assert `RuntimeError` with "lid.176.ftz" in message
  - Unit: `test_check_multilingual_deps_all_present` — both import and file present; returns without error
  - Checkpoint: `uv run pytest tests/server/test_app.py -x -k "multilingual_deps"`

---

### Phase 6 — Ingest Pipeline Integration
> **Releasable**: after Task 6.2; ingested documents receive language tags on all chunks.

#### Task 6.1 — Add `language` parameter to `DocumentChunker.chunk()`
- [ ] **File**: `archon_search/chunker.py`
- **Depends on**: Task 2.3 (ChunkRecord.language is `str = ""`)
- **Description**:
  - Add keyword-only parameter `language: str = ""` to `chunk()` signature: `def chunk(self, text, doc_id, source_path, *, file_type, updated_at, ingested_by, language: str = "") -> list[ChunkRecord]:`
  - Assign `language=language` in the `ChunkRecord(...)` constructor in the list comprehension
  - Default `""` means: not detected (legacy behavior when caller does not pass language)
  - All existing callers continue to work without change since it is keyword-only with a default
- **Releasable**: after this task, callers can pass `language="fr"` and all chunks receive the tag.
- **Tests (TDD)** — `tests/test_chunker.py`:
  - Unit: `test_chunk_language_propagated` — call `chunk(..., language="fr")`; assert every returned `ChunkRecord.language == "fr"`
  - Unit: `test_chunk_language_defaults_to_empty` — call without `language` kwarg; assert every `ChunkRecord.language == ""`
  - Unit: `test_chunk_language_unknown` — call with `language="unknown"`; assert every `ChunkRecord.language == "unknown"`
  - Checkpoint: `uv run pytest tests/test_chunker.py -x`

#### Task 6.2 — Wire `LanguageDetector` into `pipeline.py:ingest_file`
- [ ] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 3.1, Task 5.1, Task 6.1
- **Description**:
  - Import `LanguageDetector` lazily (inside `ingest_file` or as a `TYPE_CHECKING` import to avoid circular import)
  - `SearchPipeline.__init__` gains two new optional parameters: `language_detector: LanguageDetector | None = None` and `language_detection_confidence_threshold: float = 0.7` — both injected by callers; `language_detector=None` disables detection (multilingual off)
  - Store both as `self._language_detector` and `self._language_detection_confidence_threshold`; do NOT add a `config` parameter to `SearchPipeline.__init__` (the existing constructor does not take config — only individual values)
  - In `ingest_file`, after the parse block and before the `self._chunker.chunk(...)` call: if `self._language_detector is not None`, call `lang = await self._language_detector.detect(markdown, confidence_threshold=self._language_detection_confidence_threshold)` else `lang = ""`
  - Pass `language=lang` to `self._chunker.chunk(...)`
  - `create_pipeline()` factory in `pipeline.py`: when `config.multilingual=True`, instantiate `LanguageDetector(Path.home() / ".archon-search" / "models" / "lid.176.ftz")` and pass `language_detector=detector, language_detection_confidence_threshold=config.language_detection_confidence_threshold` to `SearchPipeline(...)`
  - **`app.py` production path (critical)**: `app.py` constructs `SearchPipeline` directly (line ~164) OUTSIDE the lifespan, NOT via `create_pipeline()`. This is the production path. It MUST also be updated: after `_check_multilingual_deps(config)` passes, conditionally instantiate `LanguageDetector(model_path)` and pass `language_detector` and `language_detection_confidence_threshold` to the inline `SearchPipeline(...)` constructor in `app.py`. Do not rely on `create_pipeline()` for the production path.
  - Language detection runs inside `record_stage("language_detect")` (consistent with existing `record_stage("parse")` / `record_stage("persist")` pattern in `pipeline.py`)
- **Releasable**: after this task, ingesting files with `multilingual=true` produces language-tagged chunks.
- **Tests (TDD)** — `tests/test_pipeline.py` (add):
  - Unit: `test_ingest_file_with_language_detection` — mock `LanguageDetector.detect` returning `"fr"`; assert all chunks have `language="fr"`
  - Unit: `test_ingest_file_language_detection_disabled` — `language_detector=None`; assert all chunks have `language=""`
  - Unit: `test_ingest_file_language_unknown` — mock `detect` returning `"unknown"`; assert all chunks have `language="unknown"`
  - Integration (`-m integration`): `test_ingest_and_search_with_language_tag` — full ingest → search; assert returned results carry expected language field
  - Integration (`-m integration`): `test_ingest_directory_language_detection` — ingest a directory of non-English files; assert all chunks for each file have the correct language tag; confirms `ingest_directory` correctly propagates detection through `ingest_file` batch calls
  - Checkpoint: `uv run pytest tests/test_pipeline.py -x -k "language"`

---

### Phase 7 — Filter Unlocking
> **Releasable**: after Task 7.2; `language=fr` queries return filtered results from single-collection search.

#### Task 7.1 — Unlock `SearchFilters.language` in `filters.py`
- [ ] **File**: `archon_search/filters.py`
- **Depends on**: Task 2.3 (three-state read path is fixed)
- **Description**:
  - Replace `_validate_language` validator that raises `ValueError("language filtering not yet supported (see C2)")` with a passthrough validator that:
    - Returns `None` for `None` input
    - Returns `None` for `""` (empty string treated as no filter)
    - Lowercases `v` before validation: `v = v.lower()`
    - Validates lowercased `v` matches `^[a-z]{2,3}$` or equals `"unknown"` — raises `ValueError("language must be a 2–3 letter ISO code or 'unknown'")` otherwise
    - Returns the lowercased code (normalizes `"FR"` → `"fr"`, `"De"` → `"de"`)
  - Update `Field` description for `language`: `"ISO 639-1 / ISO 639-3 language code to filter by (single-collection queries only; 'unknown' is a valid value)"`
- **Releasable**: after this task, valid language values pass validation without raising.
- **Tests (TDD)** — `tests/test_filters.py`:
  - Unit: `test_language_filter_none_passthrough` — `SearchFilters(language=None)` succeeds
  - Unit: `test_language_filter_valid_iso2` — `SearchFilters(language="fr")` sets `language="fr"`
  - Unit: `test_language_filter_valid_iso3` — `SearchFilters(language="fra")` sets `language="fra"`
  - Unit: `test_language_filter_unknown` — `SearchFilters(language="unknown")` succeeds
  - Unit: `test_language_filter_empty_string_coerces_to_none` — `SearchFilters(language="")` results in `language=None`
  - Unit: `test_language_filter_invalid_code_too_long` — `SearchFilters(language="english")` raises `ValueError`
  - Unit: `test_language_filter_uppercase_normalized` — `SearchFilters(language="FR")` succeeds and returns `language="fr"` (lowercased, not rejected)
  - NOTE: existing tests `test_language_filter_raises_for_non_null_value` (or similar) in `tests/test_filters.py` / `tests/test_search_filters.py` that assert language filtering raises `ValueError` must be deleted as part of this task
  - Unit: `test_language_filter_openapi_description_updated` — assert `SearchFilters.language` `Field` description no longer contains "reserved" or "C2"
  - Checkpoint: `uv run pytest tests/test_filters.py -x -k "language"`

#### Task 7.2 — Add `language` SQL clause to `store_filters.py:build_where`
- [ ] **File**: `archon_search/store_filters.py`
- **Depends on**: Task 7.1
- **Description**:
  - Remove the `if filters.language is not None: raise ValueError(...)` hard-block assertion
  - Add after existing `indexed_before` clause: `if filters.language is not None: clauses.append("language = " + _sql_quote_str(filters.language))`
  - Update docstring: add `language` to the "Fields handled" list; remove it from "Fields deliberately NOT emitted as SQL"
  - The `_sql_quote_str` call ensures proper SQL injection protection (consistent with existing pattern)
- **Releasable**: after this task, `language=fr` in `SearchFilters` generates `language = 'fr'` SQL clause.
- **Tests (TDD)** — `tests/test_store_filters.py` (add):
  - Unit: `test_build_where_language_fr` — `SearchFilters(language="fr")`; assert clause `"language = 'fr'"` in result
  - Unit: `test_build_where_language_unknown` — `SearchFilters(language="unknown")`; assert clause `"language = 'unknown'"` in result
  - Unit: `test_build_where_language_none_omitted` — `SearchFilters(language=None)`; assert `"language"` not in result
  - Unit: `test_build_where_language_with_other_filters` — `SearchFilters(language="de", file_type="pdf")`; assert both clauses present and joined with `" AND "`
  - Unit: `test_build_where_language_sql_safe` — `SearchFilters(language="fr")` does not raise; result uses `_sql_quote_str` (not f-string)
  - Integration (`-m integration`): `test_search_language_filter_excludes_unknown_and_empty` — collection with chunks tagged `fr`, `unknown`, and `""`; search with `language=fr`; assert result count equals only `fr`-tagged chunks; assert no `unknown` or `""` chunks in results
  - Integration (`-m integration`): `test_search_language_unknown_filter_returns_only_unknown` — collection with `fr` and `unknown` chunks; search with `language=unknown`; assert only `unknown`-tagged results
  - Integration (`-m integration`): `test_search_language_filter_on_legacy_collection` — collection where all chunks have `language=""`; search with `language=fr`; assert `results == []` with status 200 (not 500)
  - Integration (`-m integration`): `test_search_language_and_glob_combined` — search with `language=fr` and `source_path_glob="*.md"`; verify both SQL-side `language` clause and Python-side glob post-filter are applied; result must satisfy both constraints
  - Unit (HTTP via `TestClient`): `test_post_search_language_filter_http_response` — POST to `/search` with `{"collection": "col", "query": "q", "filters": {"language": "fr"}}`; assert HTTP 200; assert `response.json()["results"][0]["language"] == "fr"` (not `null`, not missing); verifies the full `SearchResult → SearchResultSchema.from_result() → JSON` serialization chain
  - NOTE: delete or update `test_post_search_invalid_filter_returns_422_with_validator_message` in `tests/server/test_routes_search.py` (line ~525) — it currently sends `language="en"` expecting 422; after Task 7.1, `"en"` is a valid filter; update to use `language="english"` (too long) to keep the 422 assertion valid
  - Checkpoint: `uv run pytest tests/test_store_filters.py -x -k "language"`

---

### Phase 8 — Telemetry
> **Releasable**: after Task 8.1; telemetry entries record whether the language filter was used.

#### Task 8.1 — Add `language_filter_used` to `FilterFlags`
- [ ] **File**: `archon_search/telemetry/entry.py`
- **Depends on**: Task 7.1
- **Description**:
  - Add `language_filter_used: StrictBool = False` to `FilterFlags` model after `include_metadata`
  - Update `from_search_filters` factory: add `language_filter_used=filters.language is not None`
  - Update docstring on `FilterFlags`: remove `"language is deliberately omitted"` note; replace with `"language_filter_used: True when a language filter was applied"`
  - Telemetry no-raw-query invariant is preserved: only a boolean is stored, never the actual language code value
- **Releasable**: after this task, search telemetry entries carry `language_filter_used=true/false`.
- **Tests (TDD)** — `tests/telemetry/test_entry.py` (add):
  - Unit: `test_filter_flags_language_filter_used_true` — `SearchFilters(language="fr")`; `FilterFlags.from_search_filters(...)` returns `language_filter_used=True`
  - Unit: `test_filter_flags_language_filter_used_false` — `SearchFilters(language=None)`; returns `language_filter_used=False`
  - Unit: `test_filter_flags_no_raw_language_value` — assert `FilterFlags` has no field that stores the actual language code string (only the boolean)
  - Checkpoint: `uv run pytest tests/telemetry/test_entry.py -x -k "language"`

---

### Phase 9 — Status Warning
> **Releasable**: after Task 9.1; operators get a warning when multilingual mode is on but data has not been re-ingested.

#### Task 9.1 — `/status` warning for legacy untagged chunks
- [ ] **File**: `archon_search/server/routes_status.py`
- **Depends on**: Task 2.3 (read path returns `""` for legacy)
- **Description**:
  - In the `status()` handler, after building per-collection info: if `config.multilingual is True` and a collection's `chunk_count > 0`, query the store for whether any chunks have `language = ""` (count query: `SELECT count(*) WHERE language = ''` via LanceDB)
  - If untagged chunks exist: add a `"warning"` key to that collection's status dict: `"warning": "multilingual=true but collection contains untagged chunks; re-ingest required"`
  - Only run this check when `config.multilingual=True` — zero overhead for English-only installs
  - Use a new store method `SearchStore.count_untagged_language_chunks(collection: str) -> int` — queries LanceDB for rows where `language = ''` using the `_sql_quote_str` helper (consistent with `build_where`), not an f-string; the CI guard in `tests/test_no_fstring_sql.py` covers `store.py` for f-string SQL regression
- **Releasable**: after this task, `GET /status` surfaces a per-collection warning for re-ingest-needed collections.
- **Tests (TDD)** — `tests/server/test_routes_status.py` (add):
  - Unit: `test_status_warning_when_multilingual_and_untagged` — mock `count_untagged_language_chunks` returning `5`; assert warning in response
  - Unit: `test_status_no_warning_when_multilingual_false` — `config.multilingual=False`; assert no warning regardless of chunk state
  - Unit: `test_status_no_warning_when_all_tagged` — mock returns `0`; assert no warning
  - Unit: `test_count_untagged_language_chunks_store_method` — unit test for the new store method directly
  - Checkpoint: `uv run pytest tests/server/test_routes_status.py -x -k "multilingual"`

---

### Phase 10 — MCP Tool Description Updates
> **Releasable**: after Task 10.1; MCP clients see accurate `language` parameter docs.

#### Task 10.1 — Update MCP tool descriptions for `search` and `search_with_context`
- [ ] **File**: `archon_search/server/mcp.py`
- **Depends on**: Task 7.1
- **Description**:
  - For the `search` MCP tool: find the `language` parameter description and update from "reserved — not yet implemented (C2)" to `"ISO 639-1 or ISO 639-3 language code to filter results (e.g. 'fr', 'de', 'unknown'). Single-collection queries only — multi-collection fan-out rejects this filter with a validation error."`
  - Same update for `search_with_context` MCP tool
  - If the parameter is currently absent from MCP tool schemas, add it with the above description
  - Do not change the parameter name — must be `language` for consistency with REST API
- **Releasable**: after this task, MCP clients receive accurate documentation for language filtering.
- **Tests (TDD)** — `tests/server/test_mcp.py` (add):
  - Unit: `test_search_tool_language_param_described` — introspect registered MCP tools; assert `search` tool has `language` in its input schema with description containing `"ISO 639"`
  - Unit: `test_search_with_context_tool_language_param_described` — same for `search_with_context`
  - Unit: `test_mcp_search_invalid_language_returns_error` — call MCP `search` tool with `language="english"` (7 chars, fails `^[a-z]{2,3}$`); assert response is an MCP error with `code="validation_error"` (not an unhandled exception)
  - Checkpoint: `uv run pytest tests/server/test_mcp.py -x -k "language"`

---

### Phase 11 — Eval Fixtures
> **Releasable**: after Task 11.1; the eval harness can run against non-English content and report before/after recall@5.

#### Task 11.1 — Add multilingual eval fixtures and update `thresholds.toml`
- [ ] **File**: `tests/eval/documents.jsonl`, `tests/eval/queries.jsonl`, `tests/eval/labels.jsonl`, `tests/eval/corpus/` (new entries), `tests/eval/thresholds.toml`
- **Depends on**: Task 6.2 (language tags are populated during ingest)
- **Description**:
  - Add at minimum 5 French (or German) documents to `corpus/` and corresponding entries in `documents.jsonl`
  - Add at minimum 5 queries in the same language with relevance labels in `labels.jsonl`
  - Use the deterministic eval backend (no real model weights needed — see `tests/eval/README.md`)
  - Add threshold entries to `thresholds.toml` under a `[multilingual]` table: `recall_at_5_fr` (or `_de`) with value derived from a run of the eval suite on the new fixtures; document the English-only baseline value and the multilingual model value in a comment
  - The before/after recall@5 comparison must be captured in `tests/eval/baselines/` as `baseline-multilingual.md` with a table of: English-only model recall@5 on non-English fixtures vs multilingual model recall@5 on same fixtures
  - Read `tests/eval/README.md` before touching fixtures or thresholds — the maintenance policy and schema are documented there
- **Releasable**: after this task, `uv run pytest -m eval` reports multilingual recall@5 and fails if below threshold.
- **Tests (TDD)** — `tests/eval/test_eval_suite.py` (existing, extended by new fixtures):
  - Eval: `test_recall_at_5_multilingual_fr` (or `_de`) — eval harness runs on new fixtures; asserts recall@5 ≥ threshold from `thresholds.toml`
  - Checkpoint: `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py -x -k "multilingual"`

---

### Phase 12 — FTS Tokenization (conditional on Phase 1 spike)
> **Releasable**: after Task 12.1 **only if** Task 1.1 spike confirmed LanceDB Python API supports language tokenizer config. If the spike found no support, skip this phase entirely and document in `BREAKING.md`.

#### Task 12.1 — Wire language-aware FTS tokenization in `store.py`
- [ ] **File**: `archon_search/store.py`
- **Depends on**: Task 1.1 (spike confirmed support), Task 6.2
- **Description**:
  - **Prerequisite**: Task 1.1 confirmed the exact `FTS(language=...)` API call. Key findings from the spike:
    - Parameter is `language`, value must be a **capitalized full English name** (e.g., `"French"`, `"German"` — NOT `"french"`/`"german"`). LanceDB raises `ValueError` for unrecognized values.
    - LanceDB's internal keys use non-standard codes for Dutch (`"du"` not `"nl"`) and Greek (`"gr"` not `"el"`). The map must bridge from fasttext/ISO 639-1 output to LanceDB's keys.
    - `GROUP BY` SQL is **not supported** in LanceDB 0.30.2 — `get_dominant_language` must use Python-side aggregation (see below).
  - `rebuild_fts_index(collection: str)` gains optional `language: str = ""` parameter
  - When `language` is a non-empty, recognized code: pass language tokenizer config to `FTS()` constructor
  - Add `_LANCEDB_TOKENIZER_MAP: dict[str, str]` — mapping from fasttext/ISO output codes to LanceDB tokenizer names (capitalized). Must include bridge entries: `{"fr": "French", "de": "German", "en": "English", "nl": "Dutch", "el": "Greek", "nb": "Norwegian", "nn": "Norwegian", ...}` — see spike for full list
  - Languages not in the map fall back to `FTS()` default (no regression for unsupported languages)
  - The collection-level language is determined from the majority tag across all chunks using a **Python-side scan** (LanceDB does not support `GROUP BY` SQL): `await tbl.query().select(["language"]).to_arrow()` then `Counter` on non-empty values
  - New store method: `async def get_dominant_language(collection: str) -> str` — fetches the `language` column, counts non-empty values with `Counter`, returns the most common code or `""` if all chunks are untagged
- **Releasable**: after this task, FTS tokenization uses the correct Tantivy stemmer for the collection's dominant language.
- **Tests (TDD)** — `tests/test_store.py` (add):
  - Unit: `test_rebuild_fts_index_with_language` — mock LanceDB; assert `FTS` constructed with language param when supported
  - Unit: `test_rebuild_fts_index_unknown_language_uses_default` — language not in map; assert default FTS used
  - Unit: `test_get_dominant_language` — collection with mixed tags; assert most-common non-empty code returned
  - Checkpoint: `uv run pytest tests/test_store.py -x -k "fts_language or dominant_language"`

---

### Final Phase — Verification & Documentation

#### Task F.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to discover all documentation in the project (Architecture docs, ADRs, UserManual, OperatorGuide, CHANGELOG, `archon-search.toml.example`, `BREAKING.md`) and update every file whose content is affected by C2:
    - `Documentation/Architecture/100_system_architecture_overview.md` — add language detection to ingest pipeline description
    - `Documentation/Architecture/110_component_catalog_and_layer_breakdown.md` — add `language_detector.py` module entry
    - `Documentation/Architecture/130_data_architecture_and_persistence.md` — update `language` field three-state contract
    - `Documentation/Architecture/150_security_and_privacy_architecture.md` — confirm no raw language code in telemetry (already safe by design)
    - `Documentation/Architecture/600_api_reference_or_public_interface.md` — update `SearchFilters.language` from "reserved (C2)" to active; document three-state contract, single-collection limitation
    - `Documentation/UserManual/` — add multilingual installation and filtering guide
    - `Documentation/OperatorGuide/` — document `--multilingual` install flag, confidence threshold config, re-ingest requirement for profile switches
    - `archon-search.toml.example` — add `language_detection_confidence_threshold` key with comment
    - `CHANGELOG.md` — add C2 entry
    - `BREAKING.md` — note if FTS language tokenization was dropped (if Phase 12 skipped)
    - Remove `# C2` roadmap references from `filters.py` docstrings and `store_filters.py` comments
  - Verify all acceptance criteria below are met before marking this task complete.
- **Releasable**: after this task, the feature is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - [ ] `archon-search install --multilingual` downloads `lid.176.ftz` after CC-BY-SA 3.0 license prompt
  - [ ] `archon-search install --multilingual --accept-fasttext-license` skips the interactive prompt
  - [ ] Server startup with `multilingual=true` and `fasttext-wheel` not installed raises `RuntimeError` with "fasttext-wheel" in message
  - [ ] Server startup with `multilingual=true` and `lid.176.ftz` absent raises `RuntimeError` with "lid.176.ftz" in message
  - [ ] Ingesting a French document produces `language="fr"` on all chunks
  - [ ] Ingesting a document whose fasttext confidence is below threshold produces `language="unknown"` on all chunks
  - [ ] `GET /search` with `language=fr` returns only `fr`-tagged chunks; excludes `"unknown"` and `""` chunks
  - [ ] `GET /search` with `language=unknown` returns only `"unknown"`-tagged chunks
  - [ ] `GET /search` with `language=fr` on a legacy collection with no language tags returns 0 results (not an error)
  - [ ] Legacy chunks (pre-C2 ingest) return `language=""` from the read path (not `None`)
  - [ ] `GET /status` with `multilingual=true` shows a per-collection warning for collections containing `language=""` chunks
  - [ ] `FilterFlags.language_filter_used=true` in telemetry entries when `language` filter was set
  - [ ] `uv run pytest -m eval` passes with multilingual fixtures; `baselines/baseline-multilingual.md` documents before/after recall@5 on non-English content
  - [ ] `uv run pytest` (default, no markers) passes with ≥85% coverage
  - [ ] All "C2" roadmap references removed from `filters.py` and `store_filters.py` comments/docstrings
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked.
