**Purpose**: Comprehensive guide to the `archon-search wizard` command — what it does, every prompt it asks, all CLI flags, what it configures, and what it does not.
**Audience**: End users and operators setting up archon-search for the first time or reconfiguring an existing install.
**Status**: Stable
**Last reviewed**: 2026-07-29 / **Next review**: 2027-07-29

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

If a previous installation left a legacy service file behind, the wizard removes it as part of installing the new service:
- macOS: `~/Library/LaunchAgents/com.archon.search.plist`
- Linux: `~/.config/systemd/user/archon-search.service`

The removal happens only once the run is committed to registering the new service — that is, after every prompt and safety check has passed (profile selection, the reinstall guard, disk-space check, and the final confirmation). A run that aborts at any of those steps leaves the existing service exactly as it found it, so a wizard that refuses its own change never dismantles a running service. When the legacy file is removed you will see a message like:

```
Removed legacy service file: ~/Library/LaunchAgents/com.archon.search.plist
```

Under `--dry-run`, the wizard leaves the legacy service untouched (it does not unload or delete it) and prints what it would do instead:

```
[DRY RUN] Would remove legacy service file: ~/Library/LaunchAgents/com.archon.search.plist
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

  On a **Linux x86_64** host, confirming CUDA also replaces the default CPU-only
  PyTorch build with the matching CUDA build, so PDF and image-OCR parsing
  (docling) runs on the GPU as well. This step is best-effort: if the CUDA
  download fails, the wizard keeps the working CPU build and the install still
  succeeds. It does not run on other platforms (Windows and ARM are out of
  scope; the default install pins CPU-only torch on Linux x86_64 only).

- **No GPU detected:** No prompt is shown; CPU is used automatically.

If Metal is selected but CoreML validation fails (e.g., the installed ONNX Runtime build does not support it), the wizard tries a split-provider probe: it checks whether the **embedder** works under CoreML even if the combined probe failed. If the embedder passes but the reranker does not:

- `providers = ["CoreMLExecutionProvider"]` is written for the embedder.
- `reranker_providers = []` is also written, forcing the reranker to CPU.
- The install summary shows `CoreML — text search; CPU — result ranking`.

If both probes fail, the wizard falls back to CPU entirely:
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

If you chose a multilingual profile, the wizard shows (this runs even with `--skip-preload`, because the language-detection model is a required ~1 MB asset, not one of the deferred heavy weights):

```
WARNING: lid.176.ftz (fasttext language identification model) is licensed CC-BY-SA 3.0.
This model was created by Facebook Research and redistributed under CC-BY-SA 3.0.
You must comply with its terms for any use.
Type 'accept' to confirm license acceptance and continue, or anything else to abort:
```

Type `accept` to proceed. For non-interactive installs, pass `--accept-fasttext-license`.

### Step 5 — Optional features

After the license gates, the wizard asks about optional features. Each question is preceded by a short plain-text explanation so you can make an informed choice. Each has a default that is applied if you press Enter without typing anything.

#### 5a. Code enrichment

```
Code enrichment (tree-sitter) + code graphing:
  Parses and indexes code files structurally — functions, classes, docstrings.
  Installs tree-sitter language parsers (~50 MB) and graph enrichment (spaCy),
  and enables graph.enabled in the generated config. Both are set up together
  automatically so code graphing works out of the box. Recommended if your
  corpus includes source code. Default: disabled.
Index code files (installs tree-sitter + graph enrichment, enables graph)? [y/N]:
```

**Default**: No.

If you answer `y`, the wizard installs the `archon-search[code]` extra packages (tree-sitter grammars for Python, TypeScript, JavaScript, Go, Rust, Java, and Bash) *and* the `archon-search[graph]` extra plus the `en-core-web-sm` spaCy model, then writes `[graph].enabled = true`. Code enrichment and code graphing are always set up as a bundle so code graphing works out of the box. Once installed, ingesting code files automatically extracts symbol-level metadata (`_symbol_type`, `_containing_function`, `_containing_class`, etc.) from each chunk. This makes code search significantly more precise.

You can install this separately at any time with `pip install archon-search[code]`. Code enrichment also feeds the code graph — see [`70_code_graph_and_impact.md`](./70_code_graph_and_impact.md) for def/ref extraction and impact analysis.

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

If you answer `y`, archon-search appends one JSON line per search request to daily files under `~/.archon-search/search-logs/`. Only structural metadata is recorded — collection names, latency, result counts — **raw query text is never written**. Files older than 30 days are pruned automatically. Telemetry is entirely local; no data is sent anywhere. See [`120_telemetry.md`](./120_telemetry.md) for the full telemetry surface.

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

#### 5h. AI query expansion (HyDE + RAG Fusion)

This prompt is **always shown** (no API key precondition). For how HyDE and RAG Fusion change search results at query time, see [`60_searching.md`](./60_searching.md).

```
AI query expansion (HyDE + RAG Fusion):
  HyDE generates hypothetical answers to improve embedding recall.
  RAG Fusion runs multiple query reformulations and merges results.
  Providers:
    anthropic  - Anthropic API (needs ANTHROPIC_API_KEY)
    openai     - OpenAI API (needs OPENAI_API_KEY)
    ollama     - runs locally, no API key
    claude_cli - uses Claude Code's login, no API key
    llama_cpp  - runs against a local llama-server, no API key
  Default: disabled.
Enable AI query expansion (HyDE + RAG Fusion)? [y/N]:
```

**Default**: No.

If you answer `y`, the wizard prompts for a provider for each feature independently:

```
Which provider for HyDE? (anthropic/openai/ollama/claude_cli/llama_cpp) [anthropic]:
Which provider for RAG Fusion? (anthropic/openai/ollama/claude_cli/llama_cpp) [anthropic]:
```

For **Anthropic** (default): no further prompts. Add your API key to `~/.archon-search/.secrets.env` (`ANTHROPIC_API_KEY=<key>`) so the managed service can reach Anthropic's API. The managed service (launchd on macOS, systemd on Linux) sources this file at start time; existing files are left untouched.

For **OpenAI**: the wizard prompts for a model name (required). Add `OPENAI_API_KEY=<key>` to `~/.archon-search/.secrets.env`. Query text is sent to OpenAI's API on every request — do not enable in air-gapped deployments or where data residency requirements apply.

For **Ollama**: the wizard asks for the server base URL first (default `http://localhost:11434`; on a re-run it pre-fills the address already in your config, so pressing Enter keeps it), then contacts that address and lists the models installed there as a numbered menu — you pick by number instead of typing a name:

```
Ollama base URL for HyDE [http://localhost:11434]:

Installed Ollama models:
  1. llama3.2:latest
  2. mistral:latest
Select a model by number [1-2]:
```

If the server is unreachable or has no models installed, the wizard says so, suggests `ollama pull <model-name>` to install one, and falls back to manual model-name entry so you can still finish setup. HyDE and RAG Fusion each get their own base-URL prompt and picker. No API key is needed for Ollama — query text never leaves your host, so it is safe for air-gapped deployments.

For **Claude CLI**: uses your existing Claude Code login — no API key. The wizard checks that the `claude` command is on your PATH (warning, not a hard stop, if it isn't) and then offers a curated list of model aliases with a free-text fallback for full model IDs:

```
Claude model aliases:
  1. haiku
  2. sonnet
  3. opus
  4. fable
  Or type a full model ID (e.g. claude-haiku-4-5-20251001).
  Leave blank to use Claude Code's configured default.
Model for HyDE (number, name, or blank):
```

Leaving it blank omits `--model` so Claude Code uses its own configured default. If `claude` is not found, the wizard prints an install pointer (https://claude.ai/code) and still writes the config — install Claude Code before starting the server, and query expansion falls back silently until it is available. Unlike Ollama, this alias list is hardcoded in the wizard (the Claude CLI has no runtime model-listing command) and is updated with each release.

For **llama.cpp**: the wizard asks for the llama-server base URL first (default `http://localhost:8080`; a re-run pre-fills the address already in your config), then contacts that address's OpenAI-compatible `/v1/models` endpoint and lists the loaded models as a numbered menu — you pick by number instead of typing a name:

```
llama-server base URL for HyDE [http://localhost:8080]:

Available llama-server models:
  1. qwen2.5-0.5b-instruct
Select a model by number [1-1]:
```

If the server is unreachable or has no models loaded, the wizard says so and falls back to manual model-name entry so you can still finish setup. HyDE and RAG Fusion each get their own base-URL prompt and picker. No API key is needed — query text never leaves your host, so it is safe for air-gapped deployments. Use a small, direct-response instruct model, not a reasoning model — a reasoning model spends the whole `max_tokens` budget on hidden chain-of-thought and HyDE/RAG Fusion silently stay disabled (`hyde_applied`/`rag_fusion_applied: false`) even though the server is reachable.

After answering, the wizard:

- Enables both `[hyde].enabled = true` and `[rag_fusion].enabled = true` in your config.
- Writes `[hyde].provider` / `[rag_fusion].provider` (and `model`, `ollama_base_url`/`llama_cpp_base_url` where applicable) for non-Anthropic providers. For `claude_cli`, `model` is only written when you choose one — a blank leaves it unset so the config default applies.
- Installs the provider's package so the feature actually works: `anthropic` → `archon-search[hyde]`, `openai` → `archon-search[openai-provider]`, `ollama` → `archon-search[ollama]`. When both features share a provider (the default is `anthropic` for both), the package is installed once. `claude_cli` and `llama_cpp` need no package (the former uses the `claude` command on your PATH, the latter uses `httpx`, a core dependency). If the install fails, the wizard prints a warning and reverts `[hyde].enabled` / `[rag_fusion].enabled` to `false` so the next server start does not hard-fail on the missing package — re-run the wizard or install the package manually to enable it.
- Creates `~/.archon-search/.secrets.env` (mode 600, empty) if it does not already exist and the selected provider requires an API key. (`ollama`, `claude_cli`, and `llama_cpp` need no key, so no secrets file is created for them.)

To configure providers manually after the wizard, edit `archon-search.toml` directly: set `[hyde].provider` and `[rag_fusion].provider` to `"anthropic"`, `"openai"`, `"ollama"`, `"claude_cli"`, or `"llama_cpp"`, and set the corresponding `model` and `ollama_base_url`/`llama_cpp_base_url` fields as needed. You can also re-run the wizard with `--enable-hyde --enable-rag-fusion` to reconfigure.

#### 5i. Graph enrichment provider

Shown right after the HyDE/RAG Fusion questions above, on the same "truly interactive" gate (skipped when `--enable-hyde`/`--enable-rag-fusion` were passed as flags, or the wizard is running non-interactively). This step is independent of both HyDE/RAG Fusion and of `[graph].enabled`: graph enrichment (LLM-written community summaries, typed relationship labels) is optional, and `[graph].provider` is itself the enable gate — there is no separate `[graph].enrichment_enabled`. The graph subsystem (entity extraction, PPR, communities) works fine without it.

```
LLM-backed graph enrichment:
  Uses an LLM to write community summaries and label relationship types
  during graph community builds. Optional — the graph subsystem (entity
  extraction, PPR, communities) works without it.
  Default: disabled.
Enable LLM-backed graph enrichment? [y/N]:
```

**Default**: No — `[graph].provider` stays `null`.

If you answer `y`, the wizard prompts for one of the four v1 enrichment providers (`claude_cli` is not offered here — it has no v1 HTTP enrichment client, deferred post-v1):

```
Which provider for graph enrichment? (anthropic/openai/ollama/llama_cpp) [anthropic]:
```

`extraction_model` is always prompted as free text (even for `anthropic`), because unlike `HyDEConfig`/`RAGFusionConfig`, `GraphConfig.extraction_model` has no built-in default. Choosing `llama_cpp` uses the same base-URL-then-`/v1/models`-picker flow shown above for HyDE/RAG Fusion. After answering, the wizard writes `[graph].provider`, `[graph].extraction_model`, and (for `llama_cpp`) `[graph].llama_cpp_base_url` to your config.

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
    • HyDE: enabled (provider: anthropic)
    • RAG Fusion: enabled (provider: anthropic)
```

Only non-default optional features are listed. When you enable AI query expansion, the summary confirms it with `HyDE: enabled (provider: …)` and `RAG Fusion: enabled (provider: …)` lines so you have visible proof the setting took effect before you exit the wizard. The `API key` line shows a masked preview and the path to the env file that holds the full key. If the key file does not exist yet (first install, server not yet started), `(not yet generated)` is shown instead. The summary also reflects any deployment flags you passed (custom host, port, db path, top-k, etc.). Then:

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

If you chose a **multilingual** profile, the wizard also installs the `archon-search[multilingual]` extra (`fasttext-wheel`, used for language detection) at this step — the server needs it to start when `multilingual = true`. If that install fails, the wizard reverts `multilingual = false` in the config so the server still starts (in English-only mode) instead of crashing on the next start. You can install it separately at any time with `pip install archon-search[multilingual]`.

### Step 8 — Model download

```
[4/5] Downloading models...
```

The wizard downloads the embedding model and (if applicable) the reranker model weights to the fastembed/HuggingFace cache. For multilingual installs, `lid.176.ftz` is also downloaded to `~/.archon-search/models/`.

Depending on your chosen profile and connection speed this can take from a few seconds (Minimal) to several minutes (Max). Progress is printed to the terminal by fastembed.

To skip this step and download the heavy weights on the first search request instead, pass `--skip-preload`. Note: for multilingual profiles the small `lid.176.ftz` language-detection model is **always** downloaded, even with `--skip-preload` — it is a required ~1 MB runtime asset without which the server cannot start, so it is never deferred. If that download fails, the wizard reverts to English-only mode so the server still boots.

> **Note — readiness timeout:** The length of the readiness window in Step 9 is controlled by the *eager embedder loading* setting (Step 5e), not by `--skip-preload`. When eager loading is **disabled** (the default), the window is a flat 60 seconds regardless of model size. When eager loading is **enabled**, the window scales with model size (approximately 100 ms per MB, never below 60 seconds, capped at 10 minutes). On a slow or heavily loaded machine the wizard may print "Search service did not become ready within N seconds" and exit non-zero; this is load-sensitive, not a permanent failure. Run `archon-search status` immediately after to check whether the service recovered on its own.

### Step 9 — Service registration, startup, and next steps

```
[5/5] Starting search service...
Waiting for search service......... ready.

archon-search is running on http://127.0.0.1:8765

Next steps:
  archon-search ingest --path <path>    # add documents to search
  archon-search status                  # check service health
  archon-search sync                    # sync watched directories
  archon-search stop                    # stop the service
  archon-search wizard --top-k 20       # increase results per query (default: 5)

  API key: <full-key-here>  (generated fresh — keep this key private; also stored at: /Users/you/.archon-search/.search.env)
Config:  /Users/you/.archon-search/archon-search.toml

archon-search installed and running. Profile: Balanced · English.
```

The wizard registers the service with your OS (launchd on macOS, systemd user unit on Linux) and starts it. It then polls `GET /health` for up to 60 seconds by default (longer when eager loading is enabled — see the note above). If the first launch crashes on an import-time error (`ModuleNotFoundError` / `ImportError`) — a fresh-install race where the service starts before its environment is fully materialised — the wizard detects that crash in the service log and extends the window once so it survives the supervisor's automatic restart. If the service still does not become ready, the wizard exits with an error and prints the last import-time error from the service log.

After startup, the wizard prints a "Next steps" block with common follow-up commands, the full API key with its source label and a "keep this key private" note, and the paths to your API key file and config. The key is always shown in full — it already lives in a plaintext file you own, so terminal masking adds no security. The "Next steps" block is suppressed in `--dry-run` mode.

The API key line format depends on how the key was sourced:
- Auto-generated on first install: `API key: <key>  (generated fresh — keep this key private; also stored at: <KEY_FILE>)`
- Read from the key file: `API key: <key>  (keep this key private; also stored at: <KEY_FILE>)`
- Set via `ARCHON_SEARCH_API_KEY` env var: `API key: <key>  (source: $ARCHON_SEARCH_API_KEY env var — keep this key private)`

---

## CLI Flags Reference

All flags for the `wizard` command (verified against `archon_search/cli/install_cmd.py`):

| Flag | Default | Description |
|---|---|---|
| `--profile {minimal,balanced,max}` | Interactive | Select the install profile, skipping the interactive prompt. |
| `--multilingual` / `--no-multilingual` | Not set (interactive) | `--multilingual`: use multilingual model stack. `--no-multilingual`: force English models explicitly. Both skip the "non-English documents?" prompt. |
| `--skip-preload` | False | Skip the heavy embedder/reranker weight pre-download; those download on first query instead. The small `lid.176.ftz` language-detection model for multilingual profiles is still downloaded (it is required for the server to start). |
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
| **Tier 1 deployment flags** | | |
| `--host TEXT` | Not set (uses `127.0.0.1`) | Bind address for the HTTP API. Use `0.0.0.0` for remote or Docker access. Non-loopback values print a security note reminding you to add a firewall or reverse proxy. Cannot be an empty string. |
| `--port INTEGER` | Not set (uses `8765`) | HTTP port for the installed config (valid range 1–65535). This is an **install-time config flag** — it writes `[server].port`; there is no runtime `--port` flag on `serve`/`start`. Port conflicts are not detected at wizard time; the OS reports an error at service start. |
| `--db-path PATH` | Not set (uses `~/.archon-search/search`) | Database directory. The tilde is written as-is to the config file; `config.py` expands it at use sites. The wizard creates the directory (including parent dirs) and checks writability. If the existing config uses a different path, a migration note is printed. |
| `--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}` | Not set (uses `INFO`) | Server log level. Case-sensitive. |
| `--top-k INTEGER` | Not set (uses `5`) | Number of results returned per query (`top_k_return`). Valid range: 1–100. Values > 100 are rejected with a message to edit TOML directly. The wizard also sets `top_k_retrieve = max(15, 3 × top_k)` automatically. This flag is flags-only; no interactive prompt. A hint appears in the "Next steps" block. |
| `--telemetry-retention-days INTEGER` | Not set (uses `30`) | Days before telemetry log files are pruned. Must be ≥ 1. Only written to TOML when `--telemetry` is also passed; passing it without `--telemetry` prints a warning on stderr and writes nothing. |
| **Tier 2 AI flags** | | |
| `--enable-hyde` | False (flag) | Enable HyDE (Hypothetical Document Embeddings) query expansion. Provider defaults to `"anthropic"` but can be changed by setting `[hyde].provider` in `archon-search.toml` (supported values: `"anthropic"`, `"openai"`, `"ollama"`, `"claude_cli"`, `"llama_cpp"`). For Anthropic or OpenAI, the corresponding API key must be set; for Ollama or llama.cpp, no API key is needed and query text stays on-host; for Claude CLI, `claude` must be on PATH and logged in (no API key). See [`60_searching.md`](./60_searching.md). |
| `--enable-rag-fusion` | False (flag) | Enable RAG Fusion multi-query expansion. Same provider options and privacy considerations as `--enable-hyde`; provider controlled by `[rag_fusion].provider` in config. See [`60_searching.md`](./60_searching.md). |
| **Tier 2 security** | | |
| `--server-key HEX_KEY` | Not set | Set a custom Bearer token for the server. Must be a lowercase hex string of at least 32 characters (e.g., generated with `python -c "import secrets; print(secrets.token_hex(32))"`). Writes `ARCHON_SEARCH_API_KEY=<key>` to `~/.archon-search/.search.env` (mode 600). A shell-history warning and restart note are always printed. If `ARCHON_SEARCH_API_KEY` env var is set, it takes priority over the file; an additional warning is printed in that case. |

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
| AI query expansion (HyDE/RAG Fusion) | Skipped (use `--enable-hyde --enable-rag-fusion` to enable; configure provider in TOML or re-run the interactive wizard) |
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

**Example: container deployment with full logging and remote access**

```bash
archon-search wizard \
  --profile balanced \
  --non-interactive \
  --skip-preload \
  --host 0.0.0.0 \
  --port 9000 \
  --db-path /data/archon-search \
  --log-format json \
  --log-level INFO \
  --disable-gpu
```

Note: `--host 0.0.0.0` prints a security note reminding you to add a firewall or reverse proxy.

**Example: homelab server with custom port, database location, and more search results**

```bash
archon-search wizard \
  --profile balanced \
  --non-interactive \
  --host 0.0.0.0 \
  --port 9000 \
  --db-path ~/data/archon-search \
  --top-k 20 \
  --telemetry \
  --telemetry-retention-days 90
```

**Example: install with AI query expansion (requires Anthropic API key)**

```bash
ANTHROPIC_API_KEY=sk-... archon-search wizard \
  --profile balanced \
  --non-interactive \
  --enable-hyde \
  --enable-rag-fusion
```

**Example: set a custom server API key**

```bash
archon-search wizard \
  --profile minimal \
  --non-interactive \
  --server-key "$(python -c 'import secrets; print(secrets.token_hex(32))')"
```

---

## What Gets Configured

The wizard writes to `~/.archon-search/archon-search.toml`. The following table maps each wizard choice to its TOML key. For the full config reference, see [`30_configuration.md`](./30_configuration.md).

### `[server]` section

| Wizard choice / flag | TOML key | Example value |
|---|---|---|
| `--host TEXT` | `host` | `"0.0.0.0"` |
| `--port INTEGER` | `port` | `9000` |

### `[database]` section

| Wizard choice / flag | TOML key | Example value |
|---|---|---|
| Profile | `profile` | `"balanced"` |
| Multilingual | `multilingual` | `true` |
| Embedding model (from profile) | `embedding_model` | `"BAAI/bge-base-en-v1.5"` |
| Reranker model (from profile) | `reranker_model` | `"Xenova/ms-marco-MiniLM-L-12-v2"` |
| Chunk size (from profile) | `chunk_size` | `512` |
| GPU (Metal, full CoreML) | `providers` | `["CoreMLExecutionProvider"]` |
| GPU (Metal, split: embedder CoreML / reranker CPU) | `providers` + `reranker_providers` | `["CoreMLExecutionProvider"]` + `[]` |
| GPU (CUDA) | `providers` | `["CUDAExecutionProvider"]` |
| GPU declined | `providers` | `[]` |
| Reranker disabled (`--no-reranker`) | `reranker_model` | `""` |
| Eager load | `eager_load_embedders` | `true` |
| `--db-path PATH` | `db_path` | `"~/data/archon-search"` |
| `--top-k INTEGER` | `top_k_return` | `20` |
| `--top-k INTEGER` (derived) | `top_k_retrieve` | `60` (= max(15, 3 × top_k)) |

### `[collections]` section

| Wizard choice / flag | TOML key | Example value |
|---|---|---|
| Filesystem watcher enabled | `watch` | `true` |

### `[telemetry]` section

| Wizard choice / flag | TOML key | Example value |
|---|---|---|
| Telemetry enabled | `enabled` | `true` |
| `--telemetry-retention-days` (only when `--telemetry` also passed) | `retention_days` | `90` |

### `[routing]` section

| Wizard choice / flag | TOML key | Example value |
|---|---|---|
| Routing strategy (hybrid only) | `routing_strategy` | `"hybrid"` |

### `[logging]` section

| Wizard choice / flag | TOML key | Example value |
|---|---|---|
| Log format (json only) | `format` | `"json"` |
| `--log-level TEXT` | `level` | `"DEBUG"` |

### `[hyde]` section

| Wizard choice / flag | TOML key | Example value |
|---|---|---|
| `--enable-hyde` | `enabled` | `true` |

### `[rag_fusion]` section

| Wizard choice / flag | TOML key | Example value |
|---|---|---|
| `--enable-rag-fusion` | `enabled` | `true` |

**Only non-default values are written.** If you accept the default for a question (e.g., keep `centroid` routing or `text` log format), that key is not written to the file. Passing an explicit flag value (even if it matches the default, e.g., `--port 8765`) always writes the key. All other keys in `archon-search.toml` remain at their defaults.

The wizard also backs up your existing config to `~/.archon-search/archon-search.toml.bak` before making any changes.

---

## What the Wizard Does NOT Configure

The following settings exist in `archon-search.toml` but are not exposed in the wizard. You must edit the file manually (or use `archon-search config set`) to change them. See [`30_configuration.md`](./30_configuration.md) for the complete key reference.

### API key (auto-generated; override via env var or `--server-key`)

The API key is stored in `~/.archon-search/.search.env` (file mode 600, auto-generated on first server start). The full key is printed in the success output every time the wizard runs.

To set a custom key, use `--server-key <32-char-hex>` (writes the key file) or set the `ARCHON_SEARCH_API_KEY` environment variable (takes precedence over the file). To redirect the key file path, set `ARCHON_SEARCH_KEY_FILE`.

### Collection definitions

```toml
[collections]
collections = []           # Static collection list (name + source path)
pinned_collections = []    # Collections always searched, bypassing the router
```

Collections are normally managed through the HTTP API or the `archon-search ingest` CLI command. The wizard does not configure them.

### Graph search

The wizard writes `[graph].enabled = true` when `--code` is passed (because code graphing requires the graph subsystem). It also writes `[graph].provider` and `[graph].extraction_model` when the user selects an LLM enrichment provider during the interactive flow. All other `[graph]` keys (synonym enrichment, PageRank, PPR tuning) are configured by editing the config directly. See [`65_graph_search.md`](./65_graph_search.md) for prose graph search and [`70_code_graph_and_impact.md`](./70_code_graph_and_impact.md) for the code graph and impact analysis.

### Telemetry log directory

The wizard can enable telemetry and set `retention_days` (via `--telemetry-retention-days`), but does not configure `log_dir` (default: `~/.archon-search/search-logs/`). Edit `[telemetry]` in the config file to change the log directory.

### Log file path, rotation, and backup count

```toml
[logging]
log_file = "~/.archon-search/logs/archon-search.log"
backup_count = 7
```

The wizard can set `level` (via `--log-level`) and `format` (via `--log-format`), but not the explicit file path or rotation policy. To use a custom log file location, edit `[logging].log_file` directly.

### Routing tuning parameters

The following `[routing]` keys are not exposed in the wizard:

```toml
routing_shortlist_size = 8        # Collections evaluated before parallel search
routing_confidence_threshold = 0.30
routing_description_weight = 0.3  # Only used when routing_strategy = "hybrid"
```

### Search fan-out parameters

```toml
[search]
max_fanout = 8
fanout_leg_trim = 40
fanout_timeout_seconds = 30.0
```

### `top_k_retrieve` (set automatically by `--top-k`)

When you pass `--top-k N`, the wizard sets both `top_k_return = N` and `top_k_retrieve = max(15, 3 × N)`. You can adjust `top_k_retrieve` independently (e.g., for tuning recall vs. latency) by editing `[database]` directly. Values > 100 for `top_k_return` and the coupling ratio are TOML-only.

### HyDE and RAG Fusion sub-knobs

The wizard enables/disables HyDE and RAG Fusion (via `--enable-hyde`/`--enable-rag-fusion`), but does not configure the per-feature sub-knobs:

```toml
[hyde]
model = "claude-haiku-4-5-20251001"
timeout_seconds = 10.0
max_requests_per_minute = 60

[rag_fusion]
model = "claude-haiku-4-5-20251001"
timeout_seconds = 10.0
max_requests_per_minute = 60
num_queries = 2
```

Edit these keys in `archon-search.toml` to tune model choice, timeouts, and rate limits. For how these features behave at query time, see [`60_searching.md`](./60_searching.md).

### Custom ONNX providers and language detection

The wizard auto-detects Metal and CUDA, but if you want a custom provider chain or need to override what was configured, edit `[database].providers` directly. The `language_detection_confidence_threshold` (default: 0.7) controls when a language code is recorded vs. stored as `"unknown"` — adjust in `[database]` if you see too many `"unknown"` tags.

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

For service and ingestion failures beyond the wizard-specific cases below, see [`160_troubleshooting.md`](./160_troubleshooting.md).

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

### Service does not become ready (60-second timeout by default; see note in Step 8 for eager-load timing)

If the wizard exits with:
```
Warning: Search service did not become ready within N seconds.
```

Check the service log for errors:

```bash
tail -50 ~/.archon-search/logs/archon-search.log
```

On timeout the wizard also prints the last import-time error it found in the service log (`Last service-log error: ...`), if any. Common causes:
- An import-time crash such as `ModuleNotFoundError` / `ImportError` on the *first* launch is usually a fresh-install race, not a packaging gap — the environment was still being written when the service started. The wizard already tolerates one supervisor restart, so check whether the service recovered on its own before doing anything: `archon-search status` and `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/health`. If it is healthy, no repair is needed.
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
trash ~/.archon-search/.install.lock   # or: rm ~/.archon-search/.install.lock
```

Then re-run the wizard.

### Model download fails or times out

If model download fails partway through, re-run the wizard. The wizard validates the config and model state before downloading; already-downloaded files are not re-downloaded.

For large profiles (Max), downloads can take 10–30 minutes on slow connections. Use `--skip-preload` to defer the download to first query time if the timeout is a problem.

### Service install is not supported (Windows)

Service registration (`launchd` on macOS, `systemd` on Linux) is not supported on Windows. The wizard will complete the config and model download steps, but service registration will fail. On Windows, run the server manually in the foreground with `archon-search serve` (see [`40_running_the_server.md`](./40_running_the_server.md)).

### How to undo the wizard

To fully remove archon-search:

```bash
archon-search uninstall           # stop + unregister the service
archon-search uninstall --delete-db  # also remove all indexed data
```

The config file `~/.archon-search/archon-search.toml` and model weights in the fastembed/HuggingFace cache are not removed by `uninstall`. Delete `~/.archon-search/` manually to remove everything.

---

## Related documents

- [`00_index.md`](./00_index.md) — UserManual table of contents and reading order.
- [`10_installation.md`](./10_installation.md) — installation prerequisites, install profiles, and the `archon-search install` command.
- [`30_configuration.md`](./30_configuration.md) — full reference for every key in `archon-search.toml`.
- [`40_running_the_server.md`](./40_running_the_server.md) — start, serve, stop, and status commands.
- [`160_troubleshooting.md`](./160_troubleshooting.md) — detailed troubleshooting for service and ingestion failures.
