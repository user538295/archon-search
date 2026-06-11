# C8 — Wizard Optional Features Investigation

**Purpose**: Factual inventory of all optional features, settings, and capabilities in archon-search that require downloads, extra packages, or explicit enabling. Documents what the interactive wizard currently offers, what is NOT surfaced, and what would need to change for a user to enable any feature (including `[code]` tree-sitter code enrichment) through the wizard without knowing tricky CLI flags or config keys.

**Status**: Investigation complete (not a plan — no implementation tasks here)

---

## 1. The Wizard / Install Flow

### Entry Points

There are two distinct commands:

- **`archon-search wizard`** (`install_cmd.py:67`) — the interactive setup wizard. Runs the full install flow: profile selection, optional feature questions, model download, service registration and start.
- **`archon-search install`** (`install_cmd.py:85`) — register and start the service only. Requires `wizard` to have been run first; does not prompt for any options.

The wizard is implemented in `SearchInstaller.run()` (`install.py:884`). It accepts the following flags (all exposed via `_install_options` in `install_cmd.py:21`):

| CLI Flag | Type | Default | Purpose |
|---|---|---|---|
| `--profile` | choice: minimal/balanced/max | None (interactive) | Skip profile selection prompt |
| `--multilingual` | flag | False | Use multilingual embedding and reranker models |
| `--skip-preload` | flag | False | Skip model pre-download (download on first query instead) |
| `--force` | flag | False | Force reinstall (requires `--delete-db`) |
| `--delete-db` | flag | False | Delete existing index on reinstall |
| `--dry-run` | flag | False | Print actions without executing |
| `--non-interactive` | flag | False | Skip all interactive prompts, use defaults |
| `--accept-jina-license` | flag | False | Pre-accept Jina CC-BY-NC-4.0 license |
| `--accept-fasttext-license` | flag | False | Pre-accept fasttext CC-BY-SA 3.0 license |
| `--config` | path | None | Override config file path |

### Questions Asked in Interactive Mode

The wizard asks **exactly two** interactive questions (beyond the license gates):

1. **Profile selection** — displays a table of 3 profiles (minimal/balanced/max) with download size, quality stars, and speed estimates. The table also shows the exact model names. Prompts `Choice [1-3, default 1]`. (`install.py:609`)

2. **Confirmation** — after showing a summary of what will be installed (embedder, reranker, chunk size, GPU providers), prompts `Proceed? [Y/n]`. (`install.py:1014`)

**Conditional prompts** (triggered by specific flag combinations or conditions, not presented as "questions" to the user):

3. **Jina CC-BY-NC-4.0 license gate** — shown when the selected profile uses `jinaai/jina-reranker-v2-base-multilingual` (multilingual balanced or max). Prompts `Type 'accept' to continue`. (`install.py:459`)

4. **fasttext CC-BY-SA 3.0 license gate** — shown when `--multilingual` is passed and `--skip-preload` is not set. Prompts `Type 'accept' to continue`. (`install.py:493`)

5. **Force-delete confirmation** — shown when `--force --delete-db` is passed with an existing database. Prompts `Type 'yes' to confirm`. (`install.py:311`)

### What the Wizard Automatically Configures

- Embedding model (from profile)
- Reranker model (from profile; empty string = no reranker)
- Chunk size (from profile)
- `multilingual` boolean in config
- `profile` name in config (for record-keeping)
- GPU execution providers (detected automatically; `CoreMLExecutionProvider` for Apple Silicon if validated, `CUDAExecutionProvider` for NVIDIA)
- Service registration (launchd on macOS, systemd on Linux)
- Model file pre-download to fastembed cache
- fasttext `lid.176.ftz` download to `~/.archon-search/models/` (if multilingual)

### What the Wizard Does NOT Configure

Everything else in `archon-search.toml` is left at defaults after install. The wizard writes only the `[database]` section fields listed above.

---

## 2. All Optional Python Extras

Defined in `pyproject.toml` under `[project.optional-dependencies]`:

| Extra Group | Packages | What It Enables |
|---|---|---|
| `multilingual` | `fasttext-wheel>=0.9.2` | Language detection for multilingual corpora; the `LanguageDetector` class tags each chunk with an ISO language code at ingest time. Required before `multilingual = true` is useful in config. Download: `lid.176.ftz` model (~917 KB) fetched separately at install time. |
| `code` | `tree-sitter>=0.25.0,<0.26`, `tree-sitter-python>=0.25.0,<0.26`, `tree-sitter-typescript>=0.23.2,<0.24`, `tree-sitter-javascript>=0.25.0,<0.26`, `tree-sitter-go>=0.25.0,<0.26`, `tree-sitter-rust>=0.24.2,<0.25`, `tree-sitter-java>=0.23.5,<0.24`, `tree-sitter-bash>=0.25.1,<0.26` | AST-based code chunk enrichment (`CodeEnricher`). Adds five symbol-level metadata fields (`_symbol_type`, `_containing_function`, `_containing_class`, `_module_path`, `_symbol_subtype`) to chunks from `.py`, `.ts`, `.js`, `.go`, `.rs`, `.java`, `.sh` files at ingest time. Grammars are pre-compiled C extensions (wheels). |

**Note**: There is no `search`, `gpu`, `hyde`, or other extra group currently in `pyproject.toml`. The brief for C4 (HyDE) proposes a `hyde = ["anthropic>=0.40,<2.0"]` extra group, but it does not yet exist.

The base install (`uv sync` or `pip install archon-search` without extras) includes all core dependencies: lancedb, fastembed, docling, markitdown, trafilatura, chonkie, fastmcp, fastapi, uvicorn, watchdog, pydantic, tomlkit, click, httpx, python-json-logger.

---

## 3. Config Keys That Toggle Features

All keys with defaults are from `archon_search/config.py` (`SearchConfig` dataclass) and `archon-search.toml.example`.

### [database] section — feature toggles

| Config Key | Default | What It Controls |
|---|---|---|
| `db_path` | `"~/.archon-search/search"` | Path to the LanceDB vector store directory. Override to store the index on a different volume. |
| `embedding_model` | `"BAAI/bge-small-en-v1.5"` | Which fastembed model is used for dense embedding. Changing this without `--force --delete-db` aborts startup with a model-mismatch guard. |
| `reranker_model` | `"Xenova/ms-marco-MiniLM-L-6-v2"` | Cross-encoder reranker model. **Empty string disables reranking entirely** (`pipeline.py:1013`): `if cfg.reranker_model:` is the only guard. The minimal multilingual profile sets this to `None` (written as `""` in config), meaning multilingual minimal has no reranker. |
| `multilingual` | `false` | When `true`, constructs a `LanguageDetector` at pipeline creation time (`pipeline.py:1025`) and runs fasttext language detection on every ingested document. Requires `[multilingual]` extra and `lid.176.ftz` model file. |
| `language_detection_confidence_threshold` | `0.7` | Float in `(0.0, 1.0]`. Detections with fasttext confidence below this threshold are stored as `"unknown"` instead of a language code. Only active when `multilingual = true`. |
| `providers` | `[]` (CPU default) | ONNX Runtime execution providers list. Controls GPU acceleration for embedding and reranker. Values: `["CUDAExecutionProvider"]` (NVIDIA), `["CoreMLExecutionProvider"]` (Apple Silicon). Empty = CPU. Auto-configured by the wizard on detection. |
| `eager_load_embedders` | `false` | When `true`, pre-warms all distinct embedding model instances at server startup instead of lazy-loading on first query. Avoids the ~5–15s first-query latency spike but increases startup time. |
| `embedder_cache_size` | `3` | LRU cache size for per-collection embedder model instances. |
| `chunk_size` | `512` | Token target for chunking. Profile-dependent: `512` for minimal/balanced, `1024` for max. |
| `auto_reindex_on_chunk_size_change` | `true` | When the stored chunk size differs from the configured one, automatically re-index on startup. |
| `top_k_retrieve` | `15` | Candidate pool size from the retrieval stage (before reranking). |
| `top_k_return` | `5` | Final result count after reranking. |
| `centroid_incremental_enabled` | `true` | Use incremental centroid update on every ingest instead of full recompute. |
| `centroid_recompute_threshold` | `10000` | Trigger a full centroid recompute after this many mutations. |

### [search] section

| Config Key | Default | What It Controls |
|---|---|---|
| `max_fanout` | `8` | Maximum number of collections in one multi-collection request. |
| `fanout_leg_trim` | `40` | Per-leg candidate pool kept by local RRF score before global merge. |
| `fanout_timeout_seconds` | `30.0` | Fan-out wall-clock budget; exceeding it returns HTTP 504. |

### [routing] section

| Config Key | Default | What It Controls |
|---|---|---|
| `routing_shortlist_size` | `8` | Collections considered by the router before parallel search. |
| `routing_confidence_threshold` | `0.30` | Minimum router confidence to dispatch a query to a collection. |
| `max_parallel_collections` | `3` | Maximum collections searched concurrently per query. |
| `routing_strategy` | `"centroid"` | `"centroid"` = pure vector centroid similarity; `"hybrid"` = blends centroid with description-embedding cosine. |
| `routing_description_weight` | `0.3` | Weight for description-embedding cosine in hybrid routing; ignored when strategy = centroid. |

### [collections] section

| Config Key | Default | What It Controls |
|---|---|---|
| `collections` | `[]` | Static list of collection names to load at startup. Entries here are created/registered when the server starts. |
| `watch` | `false` | Enable watchdog-based filesystem watcher that triggers re-index on file changes in monitored directories. |
| `pinned_collections` | `[]` | Collections always included in every search, bypassing router. |

### [logging] section

| Config Key | Default | What It Controls |
|---|---|---|
| `level` | `"INFO"` | Log level (DEBUG/INFO/WARNING/ERROR/CRITICAL). |
| `log_file` | `"~/.archon-search/logs/archon-search.log"` | Set to `""` to disable file logging (stderr only). |
| `format` | `"text"` | `"text"` or `"json"` (structured for log aggregators). |
| `backup_count` | `7` | Rotated log files to retain. |

### [telemetry] section

| Config Key | Default | What It Controls |
|---|---|---|
| `enabled` | `false` | Opt-in local query telemetry. Appends one JSON line per call to `log_dir`. Raw query text is never logged; only structural metadata (collection names, latency, result counts). |
| `retention_days` | `30` | How many days of telemetry files to retain before pruning. |
| `log_dir` | `"~/.archon-search/search-logs"` | Directory for telemetry JSONL files. |
| `export_enabled` | `false` | Reserved for a future remote-export feature; setting to `true` logs a warning and is silently coerced to `false`. No external transmission in v1. |

### [observability] section

| Config Key | Default | What It Controls |
|---|---|---|
| `stage_timings_enabled` | `true` | Record per-stage latency (embed, route, vector, fts, fuse, rerank, etc.) in structured logs and `POST /explain` responses. |
| `request_id_header` | `"X-Request-ID"` | HTTP header for correlation ID propagation. |

### [namespaces] section

Freeform `key = value` mapping of namespace names to ACL expressions. Empty by default. Not a toggle — a data configuration section.

### [server] section

| Config Key | Default | What It Controls |
|---|---|---|
| `host` | `"127.0.0.1"` | Interface the server binds to. Change to `"0.0.0.0"` to expose on all interfaces (e.g., Docker or network deployments). |
| `port` | `8765` | TCP port the HTTP server listens on. |

---

## 4. Features With Download Requirements

### 4a. Embedding Models (fastembed)

**Trigger location**: `_prewarm_models()` in `install.py:216`, called at wizard step 14. Also lazy-loaded on first `embed()` call in the running server.

**Download destination**: fastembed's HuggingFace cache (typically `~/.cache/huggingface/hub/` or the path configured by `HF_HOME`/`FASTEMBED_CACHE_PATH` env vars).

All models are fetched as ONNX model weights via the fastembed registry:

| Profile | English Embedder | Download Size |
|---|---|---|
| minimal | `BAAI/bge-small-en-v1.5` | ~90 MB (part of 147 MB total) |
| balanced | `BAAI/bge-base-en-v1.5` | ~270 MB (part of 330 MB total) |
| max | `BAAI/bge-large-en-v1.5` | ~1.3 GB (part of 2.3 GB total) |

| Profile | Multilingual Embedder | Download Size |
|---|---|---|
| minimal | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | ~220 MB total |
| balanced | `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` | Part of 2.11 GB total |
| max | `intfloat/multilingual-e5-large` | Part of 3.35 GB total |

### 4b. Reranker Models (fastembed cross-encoder)

**Trigger location**: `_prewarm_models()` in `install.py:251`, called in same wizard step. Also lazy-loaded on first `predict()` call (`reranker.py:37`).

**Download destination**: same fastembed/HuggingFace cache as embedding models.

| Profile | English Reranker | Notes |
|---|---|---|
| minimal | `Xenova/ms-marco-MiniLM-L-6-v2` | Apache 2.0 |
| balanced | `Xenova/ms-marco-MiniLM-L-12-v2` | Apache 2.0 |
| max | `BAAI/bge-reranker-base` | MIT |

| Profile | Multilingual Reranker | Notes |
|---|---|---|
| minimal | None | No reranker for multilingual minimal |
| balanced | `jinaai/jina-reranker-v2-base-multilingual` | **CC-BY-NC-4.0** — license gate required |
| max | `jinaai/jina-reranker-v2-base-multilingual` | **CC-BY-NC-4.0** — license gate required |

### 4c. fasttext Language Identification Model

**Trigger location**: `_download_fasttext_model()` in `install.py:526`, called at wizard step 3b only when `--multilingual` is passed and `--skip-preload` is not set.

**Download destination**: `~/.archon-search/models/lid.176.ftz`

**Source URL**: `https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz`

**License**: CC-BY-SA 3.0 (Facebook Research). A license acceptance prompt is shown before download.

**Approximate size**: ~917 KB.

### 4d. tree-sitter Grammar Packages (`[code]` extra)

**Trigger location**: NOT downloaded at install time. The `[code]` extra packages are Python wheels (pre-compiled C extensions). They are installed via `pip install archon-search[code]` or `uv sync --extra code`. No runtime download occurs — grammars are embedded in the wheel files.

**What is installed**:
- `tree-sitter` core (~1 MB wheel)
- 7 grammar packages (`tree-sitter-python`, `tree-sitter-typescript`, `tree-sitter-javascript`, `tree-sitter-go`, `tree-sitter-rust`, `tree-sitter-java`, `tree-sitter-bash`) — each ~0.5–2 MB

**Loading behavior**: Grammar `Language` objects are lazy-loaded per extension in `_get_grammar()` (`code_enricher.py:134`) on the first ingest of a file with that extension. No network call is made; imports come from the installed wheels. If the wheel is not installed, `_get_grammar()` catches `ImportError`, caches `None`, and logs a one-time INFO message — ingest continues without symbol metadata.

**Note**: As of C3c Task 7.1 (commit c551132), the dispatch branch is wired: `pipeline.py` routes files with recognized code extensions through `CodeEnricher` and all other files through `MarkdownEnricher`. No pending implementation work remains for this feature.

---

## 5. Profiles

### Profile Definitions

Defined in `archon_search/profiles.py`. Three profiles exist in two variants (English and Multilingual).

#### English Profiles

| Profile | Embedder | Reranker | Chunk Size | Download | Quality | CPU Speed | Metal Speed | RAM |
|---|---|---|---|---|---|---|---|---|
| `minimal` | `BAAI/bge-small-en-v1.5` | `Xenova/ms-marco-MiniLM-L-6-v2` | 512 | ~147 MB | ★★☆☆☆ | ~40 ms/q | ~15 ms/q | 0.5 GB |
| `balanced` | `BAAI/bge-base-en-v1.5` | `Xenova/ms-marco-MiniLM-L-12-v2` | 512 | ~330 MB | ★★★☆☆ | ~150 ms/q | ~50 ms/q | 1.0 GB |
| `max` | `BAAI/bge-large-en-v1.5` | `BAAI/bge-reranker-base` | 1024 | ~2.3 GB | ★★★★☆ | ~400 ms/q | ~130 ms/q | 2.5 GB |

#### Multilingual Profiles

| Profile | Embedder | Reranker | Chunk Size | Download | Quality | CPU Speed | Metal Speed | RAM |
|---|---|---|---|---|---|---|---|---|
| `minimal` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | None | 512 | ~220 MB | ★☆☆☆☆ | ~60 ms/q | ~20 ms/q | 0.5 GB |
| `balanced` | `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` | `jinaai/jina-reranker-v2-base-multilingual` | 512 | ~2.1 GB | ★★★☆☆ | ~200 ms/q | ~65 ms/q | 1.5 GB |
| `max` | `intfloat/multilingual-e5-large` | `jinaai/jina-reranker-v2-base-multilingual` | 1024 | ~3.35 GB | ★★★★☆ | ~450 ms/q | ~150 ms/q | 3.0 GB |

#### Profile Selection Logic

- `_PROFILE_ORDER = ("minimal", "balanced", "max")` — the display order
- `VALID_PROFILE_NAMES = frozenset({"minimal", "balanced", "max"})` — the accepted set
- Default when `--non-interactive` and no `--profile` flag: `"minimal"`
- Profiles are selected at install time and written to `archon-search.toml` under `[database].profile` (as a label only) and the actual model/chunk settings are set explicitly. The profile label is informational.

#### What Profiles Do NOT Control

Profiles do not set: GPU providers (auto-detected), telemetry, watchdog, routing strategy, fan-out settings, top-k values, log format, or any optional extra packages. These remain at their defaults unless the user manually edits `archon-search.toml`.

---

## 6. Gaps — Features Not Surfaced in the Wizard

The following optional features or capabilities exist in the codebase but are not surfaced by the wizard and require either (a) manual `pip install`, (b) manual config file editing, or (c) both.

### Gap 1: Code Symbol Enrichment (`[code]` extra)

**Status**: Fully implemented. `CodeEnricher` is implemented and pipeline dispatch is wired (C3c Task 7.1, commit c551132).

**User impact**: Users who index code repositories get no symbol-level metadata (`_symbol_type`, `_containing_function`, etc.) unless they install the `[code]` extra. Once installed, enrichment activates automatically at first ingest of a code file. Existing code files already indexed without the extra will lack symbol metadata until re-ingested.

**What a user currently must do**:
1. Know to run `uv pip install archon-search[code]` (or `pip install archon-search[code]`) separately after install.
2. No config key is needed once installed — feature activates automatically at first ingest of a code file.

**Wizard gap**: The wizard does not ask "Do you plan to index code files?" and does not offer to install the `[code]` extra.

### Gap 2: Multilingual Language Detection

**Partial surface**: The wizard does ask `--multilingual` (implicitly via the profile table's "Add --multilingual to use multilingual models" footnote). However, this is presented as a CLI flag, not as an interactive question. A user in fully interactive mode who does not read the footnote will not be prompted about multilingual support.

**User impact**: An English-only user who adds documents in other languages later will not have language detection without re-running the wizard with `--multilingual`.

**What a user must do**: Re-run `archon-search wizard --multilingual --force --delete-db` (since switching embedding models requires re-indexing all data).

**Wizard gap**: No interactive question "Will your corpus include non-English documents?"

### Gap 3: Reranking On/Off

**Status**: All English profiles include a reranker by default. Multilingual minimal does not.

**User impact**: Users who want to disable the reranker for lower latency (e.g., interactive real-time search use cases) must manually set `reranker_model = ""` in `archon-search.toml`. No wizard path for this.

**Wizard gap**: No option to "use embedder only, no reranker" in the wizard profile flow.

### Gap 4: GPU / Execution Provider Configuration

**Partial surface**: GPU detection is automatic (`detect_gpu()` in `install.py:684`). On Apple Silicon, `CoreMLExecutionProvider` is validated and configured. On CUDA systems, `CUDAExecutionProvider` is configured.

**User impact**: If a user wants to force a specific provider (e.g., disable Metal acceleration on Apple Silicon, or use a custom ONNX provider), they must manually edit `[database].providers` in `archon-search.toml`.

**Wizard gap**: No interactive confirmation "Apple Silicon detected — enable Metal acceleration? [Y/n]". The wizard configures it automatically and only reports it in the summary. CoreML validation failure falls back silently to CPU with a warning.

### Gap 5: Filesystem Watcher (`watch`)

**Status**: The `watch = false` config key exists. Enabling it causes the `watcher.py` + `sync.py` watchdog to monitor collection source directories and re-index on change.

**User impact**: Users who want live sync must manually set `[collections].watch = true` in `archon-search.toml`.

**Wizard gap**: No question "Watch collection directories for changes and re-index automatically?"

### Gap 6: Telemetry

**Status**: Telemetry is opt-in and disabled by default. It logs structural metadata (no raw queries) to `~/.archon-search/search-logs/`.

**User impact**: Users who want local usage analytics must manually set `[telemetry].enabled = true`.

**Wizard gap**: No question "Enable local query telemetry?"

### Gap 7: Eager Embedder Loading

**Status**: `eager_load_embedders = false` by default. When `true`, all embedding models are pre-warmed at server startup, eliminating the ~5–15s first-query latency.

**User impact**: Users who care about predictable first-query latency (e.g., in automated workflows) must manually set this in config.

**Wizard gap**: No question "Pre-load embedding models at startup to avoid first-query delay?"

### Gap 8: HyDE Query Expansion (`[hyde]` extra — planned, C4)

**Status**: Not yet implemented. C4 brief specifies `hyde = ["anthropic>=0.40,<2.0"]` as a new optional extra group.

**User impact**: When shipped, users will need to install `archon-search[hyde]`, set `ANTHROPIC_API_KEY`, and enable `[hyde].enabled = true` in config. The wizard currently has no pathway for this.

**Wizard gap**: No question "Enable AI-powered query expansion (HyDE)? Requires an Anthropic API key."

### Gap 9: Routing Strategy

**Status**: `routing_strategy = "centroid"` by default. Changing to `"hybrid"` blends centroid scores with description-embedding cosine similarity and may improve routing for corpora with distinct domain boundaries.

**User impact**: Users must manually edit `[routing].routing_strategy = "hybrid"` and tune `routing_description_weight`.

**Wizard gap**: No advanced configuration step for routing strategy.

### Gap 10: Log Format (JSON vs Text)

**Status**: `format = "text"` by default. Container deployments or log aggregation pipelines typically want `"json"`.

**User impact**: Users deploying in Docker/Kubernetes or feeding logs to ELK/Splunk must manually set `[logging].format = "json"`.

**Wizard gap**: No question "Are you deploying in a container? Enable JSON log format?"

---

## Summary Table

| Feature | Extra Needed | Config Change Needed | Surfaced in Wizard | Requires Re-index |
|---|---|---|---|---|
| Code symbol enrichment (`[code]`) | Yes | No | **Yes** (`--code / --no-code`; installs `[code]` extra via uv/pip) | No (existing code files need re-ingest for symbol metadata) |
| Multilingual language detection | Yes (`multilingual`) | Yes (`multilingual = true`) | **Yes** (interactive question "Will your corpus include non-English documents?" before profile selection; also `--multilingual` flag) | Yes |
| Multilingual models | No | Via profile | **Yes** (multilingual question selects multilingual model stack) | Yes |
| Reranker on/off | No | Yes (`reranker_model = ""`) | **Yes** (`--no-reranker`; interactive question when profile includes a reranker) | No |
| GPU acceleration (Metal/CUDA) | No | Auto-configured | **Yes** (interactive confirmation after auto-detection: "Apple Silicon detected — enable Metal acceleration? [Y/n]"; `--disable-gpu` flag for non-interactive override) | No |
| Filesystem watcher | No | Yes (`watch = true`) | **Yes** (`--watch / --no-watch`; interactive question "Auto-watch directories and re-index on file changes?") | No |
| Telemetry | No | Yes (`enabled = true`) | **Yes** (`--telemetry / --no-telemetry`; interactive question "Enable local query telemetry?") | No |
| Eager embedder loading | No | Yes (`eager_load_embedders = true`) | **Yes** (`--eager-load / --no-eager-load`; interactive question "Pre-load embedding models at startup?") | No |
| HyDE query expansion (C4, planned) | Yes (`hyde`) | Yes (`[hyde].enabled = true`) | No (not yet implemented — depends on C4) | No |
| Routing strategy: hybrid | No | Yes (`routing_strategy = "hybrid"`) | **Yes** (`--routing-strategy {centroid,hybrid}`; interactive choice "Routing strategy (centroid/hybrid) [centroid]") | No |
| JSON log format | No | Yes (`format = "json"`) | **Yes** (`--log-format {text,json}`; interactive question "Log format (text/json) [text]") | No |

---

## Source References

- `archon_search/install.py` — wizard flow, lock, disk check, model download, profile selection, license gates
- `archon_search/cli/install_cmd.py` — CLI option definitions for `wizard` and `install` commands
- `archon_search/profiles.py` — `InstallProfile` dataclass, `ENGLISH_PROFILES`, `MULTILINGUAL_PROFILES`, `get_profile()`
- `archon_search/config.py` — `SearchConfig`, `TelemetryConfig`, `ObservabilityConfig`, `load_config()`
- `archon_search/constants.py` — `DEFAULT_ROUTING_DESCRIPTION_WEIGHT`, `DEFAULT_FAST_MODEL`, `DEFAULT_MODEL`
- `archon_search/code_enricher.py` — `CodeEnricher`, `CODE_EXTENSIONS`, `_get_grammar()`, `_GRAMMAR_CACHE`
- `archon_search/language_detector.py` — `LanguageDetector`, `FASTTEXT_MODEL_FILENAME`, `FASTTEXT_MODELS_DIR`
- `archon_search/reranker.py` — `ModelReranker`, `Reranker`, `make_reranker()`
- `archon_search/pipeline.py` — `create_pipeline()`, reranker wiring at lines 1013–1020, language detector wiring at lines 1025–1028
- `pyproject.toml` — `[project.optional-dependencies]` groups (`multilingual`, `code`)
- `archon-search.toml.example` — all config keys with defaults and comments
- `Documentation/Backlog/C3c-code-symbol-context-plan.md` — C3c plan including pipeline dispatch (Task 7.1, completed in commit c551132)
- `Documentation/Backlog/C4-hyde-query-expansion-brief.md` — HyDE feature brief including `[hyde]` optional extra group
