**Purpose**: Comprehensive guide to the `archon-search wizard` command — what it does, every prompt it asks, all CLI flags, what it configures, and what it does not.
**Audience**: End users and operators setting up archon-search for the first time or reconfiguring an existing install.
**Status**: Stable
**Last reviewed**: 2026-06-11 / **Next review**: 2027-06-11

# The archon-search Wizard

## Overview

`archon-search wizard` is the interactive setup command for archon-search. Running it takes you through a guided flow that:

1. Asks about your corpus (English or multilingual)
2. Lets you choose an install profile (model quality tier)
3. Asks about optional features (code enrichment, reranker, filesystem watcher, telemetry, etc.)
4. Handles license acceptance for any third-party models that require it
5. Detects GPU hardware and asks whether to enable acceleration
6. Downloads model weights from Hugging Face
7. Writes `~/.archon-search/archon-search.toml` with your choices
8. Registers and starts the background service

**When to use the wizard vs `archon-search install`:**

| Command | Use when |
|---|---|
| `archon-search wizard` | First-time setup, or changing your profile or optional features. Downloads models and configures everything. |
| `archon-search install` | You have already run the wizard and just want to re-register and start the service (e.g., after a system restart where auto-start is not configured). Requires the wizard to have been run first. |

---

## Step-by-Step Wizard Flow

This section walks through every prompt in the order they appear.

### Step 0 — Legacy service cleanup

Before asking any questions, the wizard silently checks for and removes any legacy service file from a previous installation:
- macOS: `~/Library/LaunchAgents/com.archon.search.plist`
- Linux: `~/.config/systemd/user/archon-search.service`

If a legacy file is found, the wizard unloads and removes it automatically. You will see a message like:

```
Removed legacy service file: ~/Library/LaunchAgents/com.archon.search.plist
```

### Step 1 — Multilingual corpus question

```
Will your corpus include non-English documents? [y/N]:
```

**Default**: No (English models).

**What it means**: If you answer `y`, the wizard selects multilingual model variants for every profile. Multilingual models support documents in many languages and enable the `language=<code>` filter on searches. They are larger and somewhat slower than their English counterparts.

If you are unsure, answer `n`. You can always reinstall with `--multilingual` later (but this requires `--force --delete-db` to re-index all your documents, since it changes the embedding model).

If you pass `--multilingual` or `--no-multilingual` on the command line, this prompt is skipped entirely.

### Step 2 — Profile selection

The wizard prints a comparison table. The `balanced` profile is annotated as the recommended choice for most users:

```
  Profile      Download    Quality       Speed (CPU / Apple Silicon)
  ─────────    ────────    ───────       ───────────────────────────
  1) Minimal   ~147 MB     ★★☆☆☆         ~40 ms/query  / ~15 ms
  2) Balanced  ~330 MB     ★★★☆☆         ~150 ms/query / ~50 ms   ← Recommended
  3) Max       ~2.3 GB     ★★★★☆         ~400 ms/query / ~130 ms

  Models (all sizes from fastembed registry, verified):
  1) BAAI/bge-small-en-v1.5 + Xenova/ms-marco-MiniLM-L-6-v2
  2) BAAI/bge-base-en-v1.5 + Xenova/ms-marco-MiniLM-L-12-v2
  3) BAAI/bge-large-en-v1.5 + BAAI/bge-reranker-base

  Best for:
  1) Personal use, <10k docs, fast responses, low RAM
  2) Team use, 10k–200k docs, good recall, ~1 GB RAM
  3) Large corpora, 200k+ docs, highest precision, ~2.5 GB RAM

  Add --multilingual to use multilingual models instead.

Choice [1-3, default 1]:
```

**Default**: `1` (Minimal).

Press Enter to accept the default, or type `1`, `2`, or `3`. After three invalid attempts the wizard aborts.

**Profile summary:**

| Profile | Best for | RAM | Download |
|---|---|---|---|
| Minimal | Personal use, <10k docs, low latency | ~0.5 GB | ~147–220 MB |
| Balanced | Team use, 10k–200k docs | ~1.0–1.5 GB | ~330 MB – 2.1 GB |
| Max | Large corpora, 200k+ docs, highest recall | ~2.5–3.0 GB | ~2.3–3.4 GB |

The multilingual Balanced and Max profiles use the Jina reranker (`jinaai/jina-reranker-v2-base-multilingual`), which is **licensed CC-BY-NC-4.0 (non-commercial)**. The Minimal multilingual profile has no reranker.

If you pass `--profile minimal`, `--profile balanced`, or `--profile max`, this prompt is skipped.

### Step 3 — GPU detection and confirmation

Immediately after profile selection, the wizard detects your GPU hardware and confirms whether to use it:

- **Apple Silicon (M-series Mac):**
  ```
  Apple Silicon detected — enable Metal acceleration? [Y/n]:
  ```
  Default: Yes. Press Enter to enable Metal (CoreML) acceleration. Type `n` to use CPU only.

- **NVIDIA GPU (Linux/Windows with CUDA):**
  ```
  NVIDIA GPU detected — enable CUDA acceleration? [Y/n]:
  ```
  Default: Yes. Press Enter to enable CUDA. Type `n` to use CPU only.

- **No GPU detected:** No prompt is shown; CPU is used automatically.

If Metal is selected but CoreML validation fails (e.g., the installed ONNX Runtime build does not support it), the wizard falls back to CPU with a warning:
```
Warning: CoreML validation failed — falling back to CPU.
```

To skip GPU detection entirely and force CPU, pass `--disable-gpu`.

### Step 4 — License gates

#### Jina CC-BY-NC-4.0 (multilingual Balanced and Max only)

If your chosen profile uses the Jina reranker (`jinaai/jina-reranker-v2-base-multilingual`), the wizard shows:

```
WARNING: jinaai/jina-reranker-v2-base-multilingual is licensed CC-BY-NC-4.0
(non-commercial use only). Commercial use of multilingual profiles 2 and 3
requires an alternative reranker. You will be required to confirm license
acceptance before this model is downloaded.
Type 'accept' to confirm license acceptance and continue, or anything else to abort:
```

Type `accept` to proceed. Anything else aborts the install. For non-interactive installs, pass `--accept-jina-license`.

#### fasttext CC-BY-SA 3.0 (multilingual only)

If you chose a multilingual profile and did not pass `--skip-preload`, the wizard shows:

```
WARNING: lid.176.ftz (fasttext language identification model) is licensed CC-BY-SA 3.0.
This model was created by Facebook Research and redistributed under CC-BY-SA 3.0.
You must comply with its terms for any use.
Type 'accept' to confirm license acceptance and continue, or anything else to abort:
```

Type `accept` to proceed. For non-interactive installs, pass `--accept-fasttext-license`.

### Step 5 — Optional features

After the license gates, the wizard asks seven questions about optional features. Each question is preceded by a short plain-text explanation so you can make an informed choice. Each has a default that is applied if you press Enter without typing anything.

#### 5a. Code enrichment

```
Code enrichment (tree-sitter):
  Parses and indexes code files structurally — functions, classes, docstrings.
  Installs tree-sitter and language parsers (~50 MB). Recommended if your corpus
  includes source code. Default: disabled.
Index code files (installs tree-sitter enrichment)? [y/N]:
```

**Default**: No.

If you answer `y`, the wizard installs the `archon-search[code]` extra packages (tree-sitter grammars for Python, TypeScript, JavaScript, Go, Rust, Java, and Bash). Once installed, ingesting code files automatically extracts symbol-level metadata (`_symbol_type`, `_containing_function`, `_containing_class`, etc.) from each chunk. This makes code search significantly more precise.

You can install this separately at any time with `pip install archon-search[code]`.

#### 5b. Reranker toggle

```
Reranker:
  A second-stage cross-encoder model that re-scores results for better precision.
  Disabling it reduces latency and RAM but lowers recall quality.
  Default: enabled (for profiles that include a reranker).
Disable reranker for lower latency? [y/N]:
```

**Default**: No (reranker stays enabled).

**This question is only shown when your selected profile includes a reranker.** The Minimal multilingual profile has no reranker, so this question is skipped for that combination.

The reranker is a second-stage cross-encoder that re-scores retrieved results for higher precision. Disabling it reduces search latency (no second model pass) at the cost of result quality. Useful for interactive UIs or real-time search where latency matters more than perfect recall.

#### 5c. Filesystem watcher

```
Filesystem watcher:
  Monitors watched directories and automatically re-indexes files when they change.
  Uses watchdog. Increases background CPU usage slightly.
  Default: disabled.
Auto-watch directories and re-index on file changes? [y/N]:
```

**Default**: No.

If you answer `y`, archon-search monitors your collection source directories with a watchdog and automatically re-indexes files when they are created, modified, or deleted. Useful for corpora that change frequently (e.g., a notes folder, a code repository).

#### 5d. Telemetry

```
Local telemetry:
  Logs per-query metadata (collection, result count, latency) to
  ~/.archon-search/search-logs/. No query text is stored. Opt-in.
  Default: disabled.
Enable local query telemetry? [y/N]:
```

**Default**: No.

If you answer `y`, archon-search appends one JSON line per search request to daily files under `~/.archon-search/search-logs/`. Only structural metadata is recorded — collection names, latency, result counts — **raw query text is never written**. Files older than 30 days are pruned automatically. Telemetry is entirely local; no data is sent anywhere.

#### 5e. Eager load

```
Eager embedder loading:
  Pre-loads the embedding model at server startup instead of on the first query.
  Eliminates first-query latency (~5-15s on first search without this).
  Default: disabled.
Pre-load embedding models at startup (eliminates first-query latency)? [y/N]:
```

**Default**: No.

By default, embedding models are loaded lazily on the first search request, which causes a ~5–15 second delay for that request. If you answer `y`, models are loaded when the server starts instead. This increases startup time but makes every query fast from the first one. Recommended for automated workflows or production use where predictable latency matters.

#### 5f. Routing strategy

```
Routing strategy:
  centroid: routes queries to collections using centroid similarity (fast, default).
  hybrid: combines centroid with keyword scoring (slightly slower, more accurate
  for mixed corpora with distinct topic clusters).
  Default: centroid.
Routing strategy (centroid/hybrid) [centroid]:
```

**Default**: `centroid`.

This setting only matters if you have multiple collections. The router decides which collections to search for a given query.

- `centroid` — routes using pure vector centroid similarity between the query and each collection's document centroids. Fast and works well for most setups.
- `hybrid` — blends centroid similarity with description-embedding cosine similarity. More precise for corpora where collections have distinct domain boundaries and clear descriptions.

Type `centroid` or `hybrid`, or press Enter to keep the default.

#### 5g. Log format

```
Log format:
  text: human-readable log lines (default).
  json: structured JSON logs, suitable for log aggregation pipelines.
  Default: text.
Log format (text/json) [text]:
```

**Default**: `text`.

- `text` — human-readable log lines. Best for local development and direct log reading.
- `json` — structured JSON log lines. Best for container deployments (Docker, Kubernetes) or log aggregation pipelines (Datadog, Loki, Splunk).

Type `text` or `json`, or press Enter to keep the default.

### Step 6 — Summary screen

Before downloading anything, the wizard prints a summary of what it is about to install:

```
  Installing: Balanced · English
  Embedder:   BAAI/bge-base-en-v1.5
  Reranker:   Xenova/ms-marco-MiniLM-L-12-v2
  Chunk size: 512 tokens
  Providers:  (CPU default)
  Database:   /Users/you/.archon-search/search
  Server:     http://127.0.0.1:8765
  API key:    abcdefgh…nopqrstu  (full key: /Users/you/.archon-search/.search.env)
  Download:   ~330 MB

  Note: Model files are downloaded now. ONNX session initialization happens in the
  server process on first query — expect ~5–15s latency on first search.

  Optional features:
    • Code enrichment (tree-sitter)
    • Watch directories (auto-reindex)
```

Only non-default optional features are listed. The `API key` line shows a masked preview and the path to the env file that holds the full key. If the key file does not exist yet (first install, server not yet started), `(not yet generated)` is shown instead. Then:

```
Proceed? [Y/n]:
```

Press Enter or type `y` to continue. Type `n` to abort without making any changes.

### Step 7 — Code enrichment package install (if requested)

If you enabled code enrichment (Step 5a), the wizard installs the packages now:

```
Installing code enrichment packages...
Code enrichment packages installed.
```

If the install fails, a warning is shown but the overall wizard continues — code enrichment is optional.

### Step 8 — Model download

```
[4/5] Downloading models...
```

The wizard downloads the embedding model and (if applicable) the reranker model weights to the fastembed/HuggingFace cache. For multilingual installs, `lid.176.ftz` is also downloaded to `~/.archon-search/models/`.

Depending on your chosen profile and connection speed this can take from a few seconds (Minimal) to several minutes (Max). Progress is printed to the terminal by fastembed.

To skip this step and download on the first search request instead, pass `--skip-preload`.

### Step 9 — Service registration, startup, and next steps

```
[5/5] Starting search service...
Waiting for search service......... ready.

archon-search is running on http://127.0.0.1:8765

Next steps:
  archon-search ingest <path>           # add documents to search
  archon-search status                  # check service health
  archon-search sync                    # sync watched directories
  archon-search stop                    # stop the service

API key: (full key: /Users/you/.archon-search/.search.env)
Config:  /Users/you/.archon-search/archon-search.toml

archon-search installed and running. Profile: Balanced · English.
```

The wizard registers the service with your OS (launchd on macOS, systemd user unit on Linux) and starts it. It then polls `GET /health` for up to 60 seconds. If the service does not become ready within that window, the wizard exits with an error.

After startup, the wizard prints a "Next steps" block with the four most common follow-up commands and the paths to your API key file and config. The "Next steps" block is suppressed in `--dry-run` mode.

---

## CLI Flags Reference

All flags for the `wizard` command:

| Flag | Default | Description |
|---|---|---|
| `--profile {minimal,balanced,max}` | Interactive | Select the install profile, skipping the interactive prompt. |
| `--multilingual` / `--no-multilingual` | Not set (interactive) | `--multilingual`: use multilingual model stack. `--no-multilingual`: force English models explicitly. Both skip the "non-English documents?" prompt. |
| `--skip-preload` | False | Skip model weight pre-download. Models download on first query instead. |
| `--force` | False | Force reinstall of an existing install. **Must be combined with `--delete-db`.** |
| `--delete-db` | False | Delete the existing database on reinstall. All indexed data will be lost. Use only with `--force`. |
| `--accept-jina-license` | False | Pre-accept the Jina CC-BY-NC-4.0 license for multilingual Balanced/Max profiles. |
| `--accept-fasttext-license` | False | Pre-accept the fasttext CC-BY-SA 3.0 license for multilingual installs. |
| `--dry-run` | False | Print every action the wizard would take without executing any of them. |
| `--non-interactive` | False | Skip all interactive prompts and use defaults. Combine with feature flags for full automation. |
| `--config PATH` | `~/.archon-search/archon-search.toml` | Use a non-default config file path. |
| `--code / --no-code` | Not set (interactive) | Install (`--code`) or skip (`--no-code`) tree-sitter code enrichment packages. |
| `--watch / --no-watch` | Not set (interactive) | Enable (`--watch`) or disable (`--no-watch`) the filesystem watcher. |
| `--telemetry / --no-telemetry` | Not set (interactive) | Enable (`--telemetry`) or disable (`--no-telemetry`) local query telemetry. |
| `--eager-load / --no-eager-load` | Not set (interactive) | Enable (`--eager-load`) or disable (`--no-eager-load`) model pre-loading at startup. |
| `--no-reranker` | False | Disable the cross-encoder reranker for lower latency. |
| `--routing-strategy {centroid,hybrid}` | Not set (interactive) | Set the routing strategy directly, skipping the interactive prompt. |
| `--log-format {text,json}` | Not set (interactive) | Set the log format directly, skipping the interactive prompt. |
| `--disable-gpu` | False | Force CPU execution; skip GPU detection and confirmation entirely. |

**Note on `--force` and `--delete-db`:** These flags must always be used together. `--force` alone is rejected with an error. This requirement exists because changing profiles requires a different embedding model, which invalidates the existing vector index. Deleting the database is irreversible.

---

## Non-Interactive Mode

Pass `--non-interactive` to run the wizard without any prompts. Combined with feature flags, this is suitable for CI, Docker builds, or automated provisioning.

**Defaults applied when `--non-interactive` is used without other flags:**

| Question | Non-interactive default |
|---|---|
| Multilingual corpus | No (English models). Pass `--multilingual` to force multilingual; `--no-multilingual` to explicitly force English (same outcome as the default, but logs the explicit choice). |
| Profile | `minimal` |
| Code enrichment | Disabled |
| Reranker | Enabled (profile default) |
| Filesystem watcher | Disabled |
| Telemetry | Disabled |
| Eager load | Disabled |
| Routing strategy | `centroid` |
| Log format | `text` |
| GPU acceleration | Auto-enabled if detected |
| Jina license | Declined (install aborts for multilingual balanced/max unless `--accept-jina-license` is passed) |
| fasttext license | Declined (install aborts for multilingual installs unless `--accept-fasttext-license` is passed) |

**Example: fastest minimal CI install (no license required)**

```bash
archon-search wizard \
  --profile minimal \
  --non-interactive \
  --skip-preload
```

**Example: multilingual balanced install with all licenses pre-accepted**

```bash
archon-search wizard \
  --profile balanced \
  --multilingual \
  --accept-jina-license \
  --accept-fasttext-license \
  --non-interactive
```

**Example: minimal install with optional features for a container deployment**

```bash
archon-search wizard \
  --profile minimal \
  --non-interactive \
  --no-reranker \
  --watch \
  --telemetry \
  --log-format json \
  --disable-gpu
```

---

## What Gets Configured

The wizard writes to `~/.archon-search/archon-search.toml`. The following table maps each wizard choice to its TOML key:

### `[database]` section

| Wizard choice | TOML key | Example value |
|---|---|---|
| Profile | `profile` | `"balanced"` |
| Multilingual | `multilingual` | `true` |
| Embedding model (from profile) | `embedding_model` | `"BAAI/bge-base-en-v1.5"` |
| Reranker model (from profile) | `reranker_model` | `"Xenova/ms-marco-MiniLM-L-12-v2"` |
| Chunk size (from profile) | `chunk_size` | `512` |
| GPU (Metal) | `providers` | `["CoreMLExecutionProvider"]` |
| GPU (CUDA) | `providers` | `["CUDAExecutionProvider"]` |
| GPU declined | `providers` | `[]` |
| Reranker disabled (`--no-reranker`) | `reranker_model` | `""` |
| Eager load | `eager_load_embedders` | `true` |

### `[collections]` section

| Wizard choice | TOML key | Example value |
|---|---|---|
| Filesystem watcher enabled | `watch` | `true` |

### `[telemetry]` section

| Wizard choice | TOML key | Example value |
|---|---|---|
| Telemetry enabled | `enabled` | `true` |

### `[routing]` section

| Wizard choice | TOML key | Example value |
|---|---|---|
| Routing strategy (hybrid only) | `routing_strategy` | `"hybrid"` |

### `[logging]` section

| Wizard choice | TOML key | Example value |
|---|---|---|
| Log format (json only) | `format` | `"json"` |

**Only non-default values are written.** If you accept the default for a question (e.g., keep `centroid` routing or `text` log format), that key is not written to the file. All other keys in `archon-search.toml` remain at their defaults.

The wizard also backs up your existing config to `~/.archon-search/archon-search.toml.bak` before making any changes.

---

## What the Wizard Does NOT Configure

The following settings exist in `archon-search.toml` but are not touched by the wizard. You must edit the file manually to change them.

### Server bind address and port

```toml
[server]
host = "127.0.0.1"   # Change to "0.0.0.0" to expose on all interfaces
port = 8765
```

To expose archon-search to other machines on your network (or within Docker), change `host` to `"0.0.0.0"`. The wizard always installs with the default loopback address.

### API key

The API key is stored in `~/.archon-search/.search.env` (file mode 600, auto-generated on first server start). It is not set through the wizard. To use a custom key, set the `ARCHON_SEARCH_API_KEY` environment variable or write a custom key to the file. To move the key file, set `ARCHON_SEARCH_KEY_FILE`.

### Database path

```toml
[database]
db_path = "~/.archon-search/search"
```

To store the index on a different volume or path, edit this key manually before running the wizard (or before the first server start).

### Collection definitions

```toml
[collections]
collections = []           # Static collection list (name + source path)
pinned_collections = []    # Collections always searched, bypassing the router
```

Collections are normally managed through the HTTP API or the `archon-search ingest` CLI command. The wizard does not configure them.

### Telemetry retention and log directory

The wizard can enable telemetry but does not configure `retention_days` (default: 30) or `log_dir` (default: `~/.archon-search/search-logs/`). Edit `[telemetry]` in the config file to change these.

### Log level, file path, rotation, and backup count

```toml
[logging]
level = "INFO"
log_file = "~/.archon-search/logs/archon-search.log"
backup_count = 7
```

The wizard sets the log format but not the level, file path, or rotation policy. Set `log_file = ""` to disable file logging (stderr only — useful in containers).

### Routing tuning parameters

The following `[routing]` keys are not exposed in the wizard:

```toml
routing_shortlist_size = 8        # Collections evaluated before parallel search
routing_confidence_threshold = 0.30
max_parallel_collections = 3
routing_description_weight = 0.3  # Only used when routing_strategy = "hybrid"
```

### Search fan-out parameters

```toml
[search]
max_fanout = 8
fanout_leg_trim = 40
fanout_timeout_seconds = 30.0
```

### Advanced features requiring manual setup

The following features are **not configured by the wizard** and require both manual package installation and config file edits:

- **HyDE query expansion** — requires `pip install archon-search[hyde]`, setting `ANTHROPIC_API_KEY`, and enabling `[hyde].enabled = true` in config. Sends query text to Anthropic's API on every request — do not enable in air-gapped deployments.
- **RAG Fusion** — requires `pip install archon-search[rag_fusion]`, setting `ANTHROPIC_API_KEY`, and enabling `[rag_fusion].enabled = true`. Same privacy caution as HyDE.
- **Custom ONNX providers** — the wizard auto-detects Metal and CUDA, but if you want a custom provider chain or need to override what was configured, edit `[database].providers` directly.
- **Language detection tuning** — the `language_detection_confidence_threshold` (default: 0.7) controls when a language code is recorded vs. stored as `"unknown"`. Adjust in `[database]` if you see too many `"unknown"` tags.

---

## Re-Running the Wizard

You can re-run the wizard at any time. What happens depends on whether anything has changed:

### Same profile (idempotent re-run)

If you re-run with the same profile and multilingual setting, the wizard:

1. Checks whether you have hand-edited any wizard-managed keys in `archon-search.toml` since the last run. If hand-edits are detected, an overwrite warning is shown:
   ```
   Existing config has custom values. Overwrite with profile defaults? [y/N]:
   ```
   - Answer `y` to proceed (the wizard continues normally).
   - Answer `n` (or press Enter, since the default is No) to abort. The config is left unchanged and no `.bak` file is created.
   - In `--non-interactive` mode, the wizard auto-accepts with a logged warning and proceeds.
2. Backs up your existing config to `archon-search.toml.bak` (only if proceeding past the overwrite check).
3. Overwrites the profile-related keys in `archon-search.toml` with the new values (including any optional feature changes you make in the prompts).
4. Downloads any missing model weights.
5. Re-registers and restarts the service.

Your indexed data is **preserved**. Changing optional features (telemetry, watcher, etc.) without changing the profile is safe.

The overwrite detection compares wizard-written keys only (`[database]` profile fields and optional-feature keys). Keys in `[server]` and other sections are never compared. If a config was written by an older version of the wizard, the detection may produce a false positive — answer `y` to overwrite safely.

### Different profile (requires `--force --delete-db`)

If you try to re-run with a different profile that uses a different embedding model or chunk size, the wizard aborts:

```
Existing index uses BAAI/bge-small-en-v1.5 (chunk_size=512).
Switching to BAAI/bge-large-en-v1.5 (chunk_size=1024) requires re-indexing all documents.
Run with --force --delete-db to proceed.
```

To proceed:

```bash
archon-search wizard --profile max --force --delete-db
```

In interactive mode, you are asked to type `yes` to confirm deletion of all indexed data. In `--non-interactive` mode the confirmation is skipped.

**This permanently deletes your vector index.** Re-index your documents after the wizard completes using `archon-search ingest`.

### Changing optional features only

To update optional features without changing the profile, re-run the wizard without `--profile`. The interactive prompts will ask the same questions, or you can pass flags to answer them directly:

```bash
# Enable the filesystem watcher without changing anything else
archon-search wizard --profile minimal --watch --non-interactive
```

---

## Troubleshooting

### GPU detection is wrong or causes errors

If the wizard enables Metal or CUDA but the server fails to start or produces ONNX errors, you can disable GPU acceleration by editing `~/.archon-search/archon-search.toml`:

```toml
[database]
providers = []
```

Then restart the service:

```bash
archon-search stop
archon-search install
```

Alternatively, re-run the wizard with `--disable-gpu` to force CPU:

```bash
archon-search wizard --profile minimal --disable-gpu --non-interactive
```

### Service does not become ready (60-second timeout)

If the wizard exits with:
```
Warning: Search service did not become ready within 60 seconds.
```

Check the service log for errors:

```bash
tail -50 ~/.archon-search/logs/archon-search.log
```

Common causes:
- Another process is using port 8765. Change `[server].port` in `archon-search.toml` and re-run `archon-search install`.
- Model weights failed to download or are corrupt. Re-run the wizard without `--skip-preload`.
- Insufficient disk space. Check with `df -h ~/.archon-search`.

### The wizard says another install is running

```
Install is already running (PID 12345). Wait for it to finish or remove
~/.archon-search/.install.lock if stale.
```

If you are certain no install is running (e.g., a previous run crashed), remove the stale lock file:

```bash
rm ~/.archon-search/.install.lock
```

Then re-run the wizard.

### Model download fails or times out

If model download fails partway through, re-run the wizard. The wizard validates the config and model state before downloading; already-downloaded files are not re-downloaded.

For large profiles (Max), downloads can take 10–30 minutes on slow connections. Use `--skip-preload` to defer the download to first query time if the timeout is a problem.

### Service install is not supported (Windows)

Service registration (`launchd` on macOS, `systemd` on Linux) is not supported on Windows. The wizard will complete the config and model download steps, but service registration will fail. On Windows, run the server manually in the foreground with `archon-search start`.

### How to undo the wizard

To fully remove archon-search:

```bash
archon-search uninstall           # stop + unregister the service
archon-search uninstall --delete-db  # also remove all indexed data
```

The config file `~/.archon-search/archon-search.toml` and model weights in the fastembed/HuggingFace cache are not removed by `uninstall`. Delete `~/.archon-search/` manually to remove everything.

---

## Related Documents

- [`01_installation.md`](./01_installation.md) — installation prerequisites, install profiles table, and the `archon-search install` command.
- [`02_configuration.md`](./02_configuration.md) — full reference for every key in `archon-search.toml`.
- [`03_running_the_server.md`](./03_running_the_server.md) — start, stop, and status commands.
- [`07_troubleshooting.md`](./07_troubleshooting.md) — detailed troubleshooting for service and ingestion failures.
