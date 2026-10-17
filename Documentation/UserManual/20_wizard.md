**Purpose**: Comprehensive guide to the `archon-search wizard` command — what it does, every prompt it asks, all CLI flags, what it configures, and what it does not.
**Audience**: End users and operators setting up archon-search for the first time or reconfiguring an existing install.
**Status**: Stable
**Last reviewed**: 2026-08-20 / **Next review**: 2027-07-29

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

If you are unsure, answer `n`. You can always reinstall with `--multilingual` later; because that changes the embedding model, the wizard rebuilds the index — an interactive run prompts you to confirm, or pass `--force --delete-db` non-interactively.

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

  The wizard detects an NVIDIA GPU by running `nvidia-smi`. When you confirm, it first checks that ONNX Runtime can load the embedding and reranking models under `CUDAExecutionProvider` — the same check the Apple Silicon path runs for CoreML (`archon_search/install/installer.py`, `_probe_and_configure_cuda`). The check fails without loading anything when the installed `onnxruntime` has no CUDA provider.

  - **Check passes** (the environment has `onnxruntime-gpu` instead of `onnxruntime`): the wizard writes `providers = ["CUDAExecutionProvider"]`, removes `reranker_providers` if it is `[]` (a value you set by hand is kept), and the summary's `Providers:` line shows `CUDA`.
  - **Check fails** (the normal case: a native `pip`/`uv` install ships the CPU `onnxruntime` that fastembed and gliner depend on, which has no CUDA provider; fastembed 0.9.0 has no `[gpu]` extra): the wizard writes `providers = []`, the summary shows `(CPU default)`, and stdout shows:

    ```
    Warning: CUDA validation failed — falling back to CPU.
      Text search and result ranking (ONNX Runtime) will run on CPU: the installed onnxruntime has no usable CUDAExecutionProvider.
      To run them on the GPU, replace onnxruntime with onnxruntime-gpu (its PyPI build needs CUDA 13 and an NVIDIA driver >= 580), then re-run the wizard.
      Commands for each install method: https://github.com/user538295/archon-search/blob/main/Documentation/UserManual/10_installation.md#nvidia-gpu-run-embedding-and-reranking-on-cuda
    ```

    Those commands are in [NVIDIA GPU: run embedding and reranking on CUDA](./10_installation.md#nvidia-gpu-run-embedding-and-reranking-on-cuda). Answer `n` here or pass `--disable-gpu` to skip the check; both also write `providers = []`.

  On a **Linux x86_64** host, a failed check is followed by a PyTorch reinstall: torch and torchvision become the CUDA 12.6 (`+cu126`) build of the installed version, so PDF and image-OCR parsing (docling) and the graph engine run on the GPU — they use PyTorch, not ONNX Runtime. Which build it replaces depends on how you installed (see [`10_installation.md`](./10_installation.md#linux-install-the-cpu-only-pytorch-build)). This step is best-effort: if the CUDA download fails, the wizard keeps the installed build and the install still succeeds. It does not run on other platforms (Windows and ARM are out of scope). In a `uv tool` install, `uv tool upgrade archon-search` can revert this build to PyPI's default (CUDA 13) because the uv tool receipt does not record `--torch-backend`. Upgrade with `uv tool install --upgrade --torch-backend cu126 "archon-search[<your extras>]"` instead.

  When the check passes, the wizard skips that reinstall and prints `Keeping the installed PyTorch build: onnxruntime-gpu shares its CUDA libraries, which the CUDA 12.6 PyTorch build would replace.` The cuDNN wheel the `+cu126` torch pulls (`nvidia-cudnn-cu12`) and the one `onnxruntime-gpu[cudnn]` pulls (`nvidia-cudnn-cu13`) install the same file, `nvidia/cudnn/lib/libcudnn.so.9`, so the swap would overwrite the cuDNN that `onnxruntime-gpu` loads. Keep the CUDA 13 torch that the GPU install commands select.

  With `--dry-run`, the wizard prints `[DRY RUN] Would configure [database] execution providers.` and, on Linux x86_64, `[DRY RUN] Would install CUDA torch from https://download.pytorch.org/whl/cu126`; it runs no check, because the check loads the models.

- **No GPU detected:** No prompt is shown; CPU is used automatically.

If Metal is selected but CoreML validation fails (e.g., the installed ONNX Runtime build does not support it), the wizard tries a split-provider probe: it checks whether the **embedder** works under CoreML even if the combined probe failed. If the embedder passes but the reranker does not:

- `providers = ["CoreMLExecutionProvider"]` is written for the embedder.
- `reranker_providers = []` is also written, forcing the reranker to CPU.
- The install summary shows `CoreML — text search; CPU — result ranking`.

If both probes fail, the wizard falls back to CPU entirely and writes `providers = []`, replacing a CoreML entry an earlier run wrote:
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
  Parses and indexes code files structurally — functions, classes, docstrings,
  and enables graph.enabled in the generated config. Both features below are set
  up together automatically so code graphing works out of the box. Recommended if
  your corpus includes source code. Default: disabled.
  Estimated download sizes, shown before anything is fetched:
    - Code enrichment: tree-sitter language parsers, about 18 MB, installed now.
    - Graph extraction: a prose entity/relation model, an estimated 1218 MB
      (approximate, not a verified size), fetched on first use, licensed under
      Apache-2.0.
Index code files (installs tree-sitter + graph enrichment, enables graph)? [y/N]:
```

**Default**: No.

**This is one question covering two features, and it discloses their two download costs separately** — the tree-sitter parsers, installed during the wizard run, and the prose extraction model, fetched from Hugging Face. The two are quoted apart rather than as a single combined figure because they differ by two orders of magnitude and are paid at different moments. Both figures come from `archon_search/install/extras.py` (`CODE_EXTRA_SIZE_ESTIMATE`, `GRAPH_MODEL_SIZE_ESTIMATE`); the model figure is an estimate from the graph-NER spike, not a verified download size, and it also feeds the pre-install disk guard and the displayed download total (`_planned_total_bytes` in `archon_search/install/prewarm.py`).

If you answer `y`, the wizard writes `[graph].enabled = true` to the generated config first, then installs the `archon-search[code]` extra packages (tree-sitter grammars for Python, TypeScript, JavaScript, Go, Rust, Java, and Bash) *and* the `archon-search[graph]` extra. Write-first is load-bearing: if installing the `archon-search[graph]` package itself fails, the wizard rolls back the already-written `graph.enabled = true` flag rather than leaving a half-configured install (a failed *model* fetch is handled differently — see below). Code enrichment and code graphing are always set up as a bundle so code graphing works out of the box. Once installed, ingesting code files automatically extracts symbol-level metadata (`_symbol_type`, `_containing_function`, `_containing_class`, etc.) from each chunk. This makes code search significantly more precise.

**The prose extraction model is pre-warmed, not provisioned.** There is no bespoke provisioning seam for it: the wizard's pre-warm step calls `GLiNER.from_pretrained()` for the pinned checkpoint and revision (`GRAPH_NER_MODEL_NAME` / `GRAPH_NER_MODEL_REVISION` in `archon_search/paths.py`), letting `huggingface_hub` download and cache it under `<data-dir>/models/graph/` — the same cache directory the server's own lazy load uses, so pre-warm and first use share one copy on disk. This is the same pattern the embedding and reranker models already follow. Model weights are not published on PyPI, which is why the extra installs the `gliner` library but never its checkpoint.

**A failed pre-warm is non-fatal.** It is logged as a WARNING and the install completes with `[graph].enabled = true` left in place; the model is downloaded on first use instead:

```
Failed to pre-warm graph NER model 'knowledgator/gliner-relex-multi-v1.0': <reason>
 — the model will be downloaded on first use.
```

Only a failure of the `archon-search[graph]` *package* install reverts the flag, and that failure says so explicitly:

```
Warning: graph enrichment install failed: <reason>
Warning: graph.enabled has been reverted to false because the install did not
complete; re-run the wizard or install archon-search[graph] manually to enable
code/prose graphing.
```

Reverting the flag disables the code-symbol/def-ref graph too, not just prose extraction — `[graph].enabled` gates the whole graph subsystem.

The pre-install summary lists the graph bullet with the two confidence knobs you can tune afterwards:

```
  Optional features:
    • Graph enrichment (code graphing)
      Tune prose extraction for your corpus with [graph].ner_confidence
      and [graph].relation_confidence in <data-dir>/archon-search.toml
```

Corpus language is not a constraint: the pinned checkpoint is multilingual, so non-English documents produce prose entities and relations like any other. Only the interface — logs, CLI copy, error bodies — is English by design; see [`../Architecture/220_accessibility_and_internationalization.md`](../Architecture/220_accessibility_and_internationalization.md#internationalization).

You can install this separately at any time; the command depends on how you installed archon-search (see [Optional extras](./10_installation.md#optional-extras)), e.g. `pip install "archon-search[code]"`. Code enrichment also feeds the code graph — see [`70_code_graph_and_impact.md`](./70_code_graph_and_impact.md) for def/ref extraction and impact analysis.

#### 5b. Reranker toggle

```
Reranker:
  A second-stage cross-encoder model that re-scores results for better precision.
  Disabling it reduces latency and RAM but lowers recall quality.
  Default: enabled (for profiles that include a reranker).
Keep reranker enabled? [Y/n]:
```

**Default**: Yes (for profiles that include a reranker). Answering `n` disables the reranker, equivalent to passing `--no-reranker` on the command line.

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

If you answer `y`, archon-search monitors your collection source directories with a watchdog and automatically re-indexes files when they are created, modified, or deleted. Useful for corpora that change frequently (e.g., a notes folder, a code repository). Either way the answer is recorded — `[collections].watch` is written to the config as `true` or `false`, including when you press Enter to keep the default. Only a `--non-interactive` run without `--watch`/`--no-watch` leaves the key out.

#### 5d. Telemetry

```
Local telemetry:
  Logs per-query metadata (collection, result count, latency) to
  ~/.archon-search/search-logs/. No query text is stored. Opt-in.
  Default: disabled.
Enable local query telemetry? [y/N]:
```

**Default**: No.

If you answer `y`, archon-search appends one JSON line per search request to daily files under `~/.archon-search/search-logs/`. Only structural metadata is recorded — collection names, latency, result counts — **raw query text is never written**. Files older than 30 days are pruned automatically (change this with `--telemetry-retention-days`). Telemetry is entirely local; no data is sent anywhere. See [`120_telemetry.md`](./120_telemetry.md) for the full telemetry surface. Either way the answer is recorded — `[telemetry].enabled` is written to the config as `true` or `false`, including when you press Enter to keep the default. Only a `--non-interactive` run without `--telemetry`/`--no-telemetry` leaves the key out.

#### 5e. Eager load

```
Eager embedder loading:
  The server always pre-loads the default embedding model and the reranker at startup,
  and /ready returns 503 until model warm-up and the background model validation finish
  (a failed warm-up ends the wait) — after a successful warm-up no query pays the cold-load cost.
  This option widens that warm-up to every per-collection embedding model as well,
  at the cost of a longer startup. Default: disabled.
Also pre-load every per-collection embedding model at startup? [y/N]:
```

**Default**: No.

Model warm-up is **not** optional and this answer does not turn it off. On every start the server loads `[database].embedding_model` and the cross-encoder reranker in a background task, and `GET /ready` returns `503` until model warm-up and the background model validation finish (a failed warm-up ends the wait). After a successful warm-up, a client that waits for readiness never pays the ~5–15 s (much longer for the `max` profile's models) cold-load cost on its first search; after a failed one, searches run degraded (FTS-only without the embedder, unreranked without the cross-encoder) while each retries the cold model. Answering `y` only widens the *scope* of that same warm-up to every distinct per-collection `active_embedding_model`, so a collection pinned to a non-default model is warm too; the trade-off is a longer window before the server reports ready, and the models must fit within `[database].embedder_cache_size`. Recommended when collections pin their own models. Either way the answer is recorded — `[database].eager_load_embedders` is written to the config as `true` or `false`, including when you press Enter to keep the default. Only a `--non-interactive` run without `--eager-load`/`--no-eager-load` leaves the key out.

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

Type `centroid` or `hybrid`, or press Enter to keep the default. Either way the answer is recorded — `[routing].routing_strategy` is written to the config even when you keep `centroid`.

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

Type `text` or `json`, or press Enter to keep the default. Either way the answer is recorded — `[logging].format` is written to the config even when you keep `text`.

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

**Default**: No. Answering `n` (or pressing Enter, or closing stdin) writes `[hyde].enabled = false` and `[rag_fusion].enabled = false`.

If you answer `y`, the wizard prompts for a provider for each feature independently:

```
Which provider for HyDE? (anthropic/openai/ollama/claude_cli/llama_cpp) [anthropic]:
Which provider for RAG Fusion? (anthropic/openai/ollama/claude_cli/llama_cpp) [anthropic]:
```

For **Anthropic** (default): no further prompts. Add your API key to `~/.archon-search/.secrets.env` (`ANTHROPIC_API_KEY=<key>`) so the managed service can reach Anthropic's API. The managed service (launchd on macOS, systemd on Linux) sources this file at start time; existing files are left untouched.

For **OpenAI**: the wizard prompts for a model name (required). Add `OPENAI_API_KEY=<key>` to `~/.archon-search/.secrets.env`. Query text is sent to OpenAI's API on every request — do not enable in air-gapped deployments or where data residency requirements apply.

For **Ollama**: the wizard asks for the server base URL first (default `http://localhost:11434`; on a re-run it pre-fills the address already in your config, so pressing Enter keeps it), then contacts that address and lists the models installed there as a numbered menu — you pick by number instead of typing a name. An address without a scheme, such as `localhost:11434` or `myollama.internal`, is accepted: the wizard fills in `http://` and, when no port is given, Ollama's default `11434` — the same address the Ollama client resolves at runtime. The completed address is what gets written to `ollama_base_url`, so typing `localhost:11434` is recorded as the default and leaves the key out of your config entirely:

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

For **llama.cpp**: the wizard asks for the llama-server base URL first — that address is what the server talks to at runtime. On a first run the prompt offers the built-in default `http://localhost:8080`; on a re-run it is pre-filled with the address already saved in config instead, so pressing Enter keeps a previously configured custom URL rather than reverting to the default. The model list is independent of it and is built in two tiers: first `llama cli -cl`, which lists your **local llama.cpp cache**; when that command is missing or its cache is empty, the wizard scans the known GGUF download directories for `.gguf` files. Either way you pick by number instead of typing a name:

```
llama-server base URL for HyDE [http://localhost:8080]:

Locally cached llama.cpp models:
  1. ggml-org/Qwen3-1.7B-GGUF:Q8_0
Select a model by number [1-1]:
```

The list deliberately comes from your local caches rather than from a running llama-server: a live server reports only the single model it currently has loaded, often as an ad-hoc absolute path you cannot re-use in the config.

Tier 2 exists for models you fetched by other means — `huggingface-cli`, a manual download — that `llama cli -cl` does not know about. It scans, in order: `$LLAMA_CACHE`; your huggingface hub cache (`~/.cache/huggingface/hub` by default, with the standard huggingface and XDG cache environment variables honoured exactly as `huggingface_hub` itself resolves them); the XDG llama.cpp cache when that variable is set; and the platform default `~/Library/Caches/llama.cpp` (macOS) or `~/.cache/llama.cpp` (Linux). Entries found this way are named by their path relative to the cache root they were found under (`models/squ11z1/gpt-oss-nano/Q4_K_M.gguf`), not as a cache ID (`squ11z1/gpt-oss-nano:Q4_K_M`), so they may not be a valid model name for the server you run.

If both tiers come up empty, the wizard says so, points you at `llama download -hf <user>/<model>[:quant]`, and falls back to manual model-name entry so you can still finish setup. The name you pick is only honoured by a router-mode llama-server (started with `--models-dir`); a single-model llama-server ignores it and answers with whatever model it has loaded, so serve the model you pick. HyDE and RAG Fusion each get their own base-URL prompt and picker. No API key is needed — query text never leaves your host, so it is safe for air-gapped deployments. Use a small, direct-response instruct model, not a reasoning model — a reasoning model spends the whole `max_tokens` budget on hidden chain-of-thought and HyDE/RAG Fusion silently stay disabled (`hyde_applied`/`rag_fusion_applied: false`) even though the server is reachable.

After answering, the wizard:

- Enables both `[hyde].enabled = true` and `[rag_fusion].enabled = true` in your config.
- Writes `[hyde].provider` / `[rag_fusion].provider` (and `model`, `ollama_base_url`/`llama_cpp_base_url` where applicable) for non-Anthropic providers. For `claude_cli`, `model` is only written when you choose one — a blank leaves it unset so the config default applies.
- Installs the provider's package so the feature actually works: `anthropic` → `archon-search[hyde]`, `openai` → `archon-search[openai-provider]`, `ollama` → `archon-search[ollama]`. When both features share a provider (the default is `anthropic` for both), the package is installed once. `claude_cli` and `llama_cpp` need no package (the former uses the `claude` command on your PATH, the latter uses `httpx`, a core dependency). If the install fails, the wizard prints a warning and reverts `[hyde].enabled` / `[rag_fusion].enabled` to `false` so the next server start does not hard-fail on the missing package — re-run the wizard or install the package manually to enable it.
- Creates `~/.archon-search/.secrets.env` (mode 600, empty) if it does not already exist and the selected provider requires an API key. (`ollama`, `claude_cli`, and `llama_cpp` need no key, so no secrets file is created for them.)

To configure providers manually after the wizard, edit `archon-search.toml` directly: set `[hyde].provider` and `[rag_fusion].provider` to `"anthropic"`, `"openai"`, `"ollama"`, `"claude_cli"`, or `"llama_cpp"`, and set the corresponding `model` and `ollama_base_url`/`llama_cpp_base_url` fields as needed. You can also re-run the wizard with `--enable-hyde --enable-rag-fusion` to reconfigure.

#### 5i. Graph enrichment provider

Shown right after the HyDE/RAG Fusion questions above, on the same "truly interactive" gate (skipped when `--enable-hyde`/`--enable-rag-fusion` were passed as flags, or the wizard is running non-interactively). This step is independent of both HyDE/RAG Fusion and of `[graph].enabled`: graph enrichment is **community summarisation only** (BE-17 / [ADR 12](../ADRs/12_local_prose_relations_enrichment_narrowed.md) removed LLM relationship labelling — typed edges such as `uses`/`implements`/`depends_on` come from the local prose extraction model, with no provider and no network), it is optional, and `[graph].provider` is itself the enable gate — there is no separate `[graph].enrichment_enabled`. The graph subsystem (entity extraction, PPR, communities) works fine without it.

```
LLM-backed graph enrichment:
  Uses an LLM to write community summaries during graph community
  builds. Optional — the graph subsystem (entity extraction, PPR,
  communities) works without it.
  Default: disabled.
Enable LLM-backed graph enrichment? [y/N]:
```

Enabling this buys community summaries and nothing else.

**Default**: No — `[graph].provider` stays `null`.

If you answer `y`, the wizard prompts for one of the four v1 enrichment providers (`claude_cli` is not offered here — it has no v1 HTTP enrichment client, deferred post-v1):

```
Which provider for graph enrichment? (anthropic/openai/ollama/llama_cpp) [anthropic]:
```

`extraction_model` is prompted as free text for `anthropic` and `openai`, because unlike `HyDEConfig`/`RAGFusionConfig`, `GraphConfig.extraction_model` has no built-in default. Choosing `ollama` or `llama_cpp` instead gets the same base-URL-then-model-picker flow shown above for HyDE/RAG Fusion — for `ollama` the list comes from the server you name, for `llama_cpp` from the same two-tier local lookup (`llama cli -cl`, then a GGUF cache-directory scan). After answering, the wizard writes `[graph].provider`, `[graph].extraction_model`, and the base URL for whichever local provider you picked (`[graph].ollama_base_url` or `[graph].llama_cpp_base_url`) to your config. As everywhere else, a base URL equal to the built-in default is left out of the config rather than written, and on a re-run the prompt is pre-filled with the address already saved in `[graph]`.

### Step 6 — Summary screen

Before the model download step (Step 8), the wizard prints a summary of what it is about to install. On Apple Silicon with GPU acceleration on, the CoreML check from Step 3 has already run by then, and it downloads and loads the embedder and reranker to test them:

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

  Note: Models are downloaded and test-loaded before the service starts (unless
  --skip-preload); an embedder that cannot load stops the install. The server
  loads them again at startup.

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

Press Enter or type `y` or `yes` to continue. Any other input (including EOF) aborts the installation. On abort, graph-enabled, multilingual, and query-expansion flags are reverted in the config file, but the config file itself was already written to disk before this prompt appeared.

### Step 7 — Optional extras install (if requested)

This step runs **after** the summary and the "Proceed?" confirmation, so aborting there skips it entirely.

If you enabled code enrichment (Step 5a), the wizard installs the packages now:

```
Installing code enrichment...
Code enrichment installed.
```

If the install fails, a warning is shown but the overall wizard continues — code enrichment is optional.

If you enabled the **graph** (Step 5a), the `archon-search[graph]` extra is installed at this step too. Its prose extraction model is *not* fetched here — that happens in Step 8, together with the embedder and reranker weights. If the package install fails, the wizard prints `Warning: graph enrichment install failed: ...`, reverts `graph.enabled` to `false`, and continues; see Step 5a for the exact copy.

If you chose a **multilingual** profile, the wizard also installs the `archon-search[multilingual]` extra (`fasttext-wheel`, used for language detection) at this step — the server needs it to start when `multilingual = true`. If that install fails, the wizard reverts `multilingual = false` in the config so the server still starts (in English-only mode) instead of crashing on the next start. You can install it separately at any time (see [Optional extras](./10_installation.md#optional-extras)), e.g. `pip install "archon-search[multilingual]"`.

**How the wizard installs extras.** In a pip virtual environment, a pipx environment or a checkout, the wizard installs each extra with `uv pip install --python <the running interpreter>`, and falls back to `python -m pip install` when that fails. In a `uv tool` install it re-runs `uv tool install` with the extras, `--with` packages and index options already recorded in the tool's receipt, plus the new extra and a `--torch-backend` matching the installed torch build. uv records the extra, so `uv tool upgrade archon-search` keeps it. If the receipt holds settings the wizard cannot reproduce, it installs the extra with `uv pip install` and warns that the next `uv tool upgrade` will remove it (see [Optional extras](./10_installation.md#optional-extras)).

### Step 8 — Model download

```
[4/5] Downloading and loading models...
```

The wizard downloads the configured embedding model and (if one is configured) the reranker model weights to the fastembed cache — `$FASTEMBED_CACHE_PATH` when it is set in the wizard's shell, otherwise `<data-dir>/models/` — then **loads each one and runs a single test inference**, exactly as the server will: same model, same `[database].providers` (the reranker uses `[database].reranker_providers` when it is set), same cache directory. The launchd/systemd service the wizard registers does not inherit the wizard's shell, so it does not see that `FASTEMBED_CACHE_PATH`: it loads from `<data-dir>/models/` and downloads the weights again there. To relocate a service's cache, set `FASTEMBED_CACHE_PATH` in the environment the service loads — `<data-dir>/.secrets.env` on macOS (sourced by the launchd wrapper script), `~/.archon-search/.secrets.env` on Linux (the systemd unit's `EnvironmentFile`). Each model is released before the next one loads, so this step holds one model at a time. On a re-run, a service still running from the earlier install keeps its own models loaded meanwhile — it is replaced only in Step 9 (a `--force --delete-db` reinstall stops it earlier) — so this step's peak memory is that service plus one model. The Step 3 CoreML check on Apple Silicon is different: it loads the embedder and reranker together. A reranker you disabled (`--no-reranker`, or `n` at "Keep reranker enabled?") is neither downloaded nor loaded. For multilingual installs, `lid.176.ftz` is also downloaded to `~/.archon-search/models/`.

Loading here is what catches a model whose files download but cannot run — a corrupt file, or weights the installed onnxruntime rejects — at install time instead of at your first ingest. The message names the model, the phase that failed (`Could not resolve or download …` or `… was downloaded but cannot be loaded`), and the underlying error; see [Model pre-warm fails](#model-pre-warm-fails-was-downloaded-but-cannot-be-loaded) for the fixes:

- **Embedder failure — the install stops.** The server cannot embed without it. The wizard prints the cause, does not start the service, exits `1`, and rolls the config back: a fresh install leaves no config, `.bak` or wizard record, a re-run restores the previous config from its `.bak` and keeps the previous wizard record, and a `--force --delete-db` reinstall keeps the new config and its `.bak` and records the new config:

  ```
  Model pre-warm failed: The embedder model 'BAAI/bge-small-en-v1.5' was downloaded but cannot be loaded: [ONNXRuntimeError] : 7 : INVALID_PROTOBUF : Load model from <data-dir>/models/.../model_optimized.onnx failed:Protobuf parsing failed.
  ```

- **Reranker failure — a warning, and the install continues.** While the reranker cannot load, `/search` runs without reranking, but `/explain` with reranking on (its default) fails with HTTP 500.

  ```
  The reranker model 'Xenova/ms-marco-MiniLM-L-6-v2' was downloaded but cannot be loaded: <reason> — the install continues, but /search skips reranking and /explain with rerank fails until the model loads. See 'Model pre-warm fails' in the wizard troubleshooting.
  ```

A provider in `[database].providers` (or `reranker_providers`) that the installed onnxruntime does not have is not a load failure: like the server, the pre-warm drops it and loads on the remaining providers, or on CPU when none remain, and prints a `configured ONNX providers not available: <names> — loading models on <providers> instead …` warning to stderr, once per distinct provider list (see Step 3 for the NVIDIA case).

If the pre-warm runs past its time limit (5 to 30 minutes, scaled to the profile's download size), the models not yet started are skipped once the embedder is ready, and one warning names them — for example `Model pre-warm timed out after <N>s with the embedder ready — skipping the reranker and graph NER models; the service downloads them when it starts or first uses them.` (or `the reranker model` / `the graph NER model` … `it` when only one is left) — so they are not load-checked at install time. The limit is checked between models: a download or load already running is never interrupted, and when no model remains, nothing is logged.

If you enabled the graph (Step 5a), the prose extraction checkpoint is pre-warmed at the end of this step, into `<data-dir>/models/graph/`. This is the ~1218 MB download quoted in Step 5a and it dominates the step's wall time. A failure is non-fatal and logged, not fatal to the install — the model downloads on first graph ingest instead.

Immediately after the pre-warm, and only when the graph extra was installed and `--skip-preload` was not passed, the wizard settles `[graph].providers`. The accelerator is offered only when an accelerator was *requested* (defined below) and both probe stages agreed — stage 1 found the device available on this host, *and* the pre-warm actually resolved the model onto it:

```
Graph extraction validated on cuda — use it? (accelerated but unmeasured;
guardrails are tuned for CPU) [Y/n]:
```

The accelerator is the default here: a bare Enter accepts it, `n` steps down to CPU (`[graph].providers = []`), and a `--non-interactive` install accepts it without asking.

An accelerator is *requested* when the first entry of the graph's candidate provider list is `CUDAExecutionProvider` or `CoreMLExecutionProvider`. The candidates are the `--graph-providers` value when that flag is passed; otherwise, in an interactive install, the provider of the GPU you accepted in Step 3 (`CUDAExecutionProvider` for NVIDIA, `CoreMLExecutionProvider` for Apple Silicon); otherwise none, so a `--non-interactive` install without `--graph-providers` requests nothing. `--disable-gpu`, or declining the Step 3 prompt, empties the candidates even against an explicit `--graph-providers`.

Every other outcome settles without a prompt. Here "the pre-warm produced no device" means the graph NER pre-warm failed (download or load), the pre-warm time limit skipped it, or the run is a `--dry-run` (which pre-warms nothing):

- **Accelerator requested, but the pre-warm landed the model on CPU or produced no device** — the wizard logs `Graph accelerator not validated (conflicting_onnx_runtimes) — writing [graph].providers = [] (CPU).` at WARNING and writes `[graph].providers = []` (CPU). Despite its name, `conflicting_onnx_runtimes` here only means the requested device was not usable.
- **Nothing requested, and the pre-warm placed the model on CPU** — `[graph].providers = []` (CPU) is written, with no warning, replacing any list you set by hand.
- **Nothing requested, and the pre-warm produced no device** — nothing is written. The wizard logs `Graph accelerator not validated (pre-warm did not run) — leaving CPU.` at WARNING; unset already means CPU, and there is no evidence to justify overwriting a list you may have set by hand — so a hand-set accelerator list survives despite the message.

With `--skip-preload` the settle step does not run: the stage-1 availability check still runs (and can log a CPU step-down WARNING), but there is no stage-2 validation and `[graph].providers` is left as it was. A non-empty `--graph-providers` is ignored outright, with this note, when the graph extra is not being installed:

```
  Note: --graph-providers was ignored — graph extraction is not being installed,
  so [graph].providers is not written.
```

Depending on your chosen profile and connection speed this can take from a few seconds (Minimal) to several minutes (Max). Progress is printed to the terminal by fastembed. Loading adds each model's session build time on top of the download.

To skip this step and download the heavy weights on the first search request instead, pass `--skip-preload`. This also skips the load check above, so a model that cannot load surfaces only when the server tries to use it. Note: for multilingual profiles the small `lid.176.ftz` language-detection model is **always** downloaded, even with `--skip-preload` — it is a required ~1 MB runtime asset without which the server cannot start, so it is never deferred. If that download fails, the wizard reverts to English-only mode so the server still boots.

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
| `--skip-preload` | False | Skip the heavy embedder/reranker weight pre-download and the load check that follows it (Step 8); those download on first query instead, and a model that cannot load is not caught at install time. The small `lid.176.ftz` language-detection model for multilingual profiles is still downloaded (it is required for the server to start). |
| `--force` | False | Force reinstall of an existing install. **Must be combined with `--delete-db`.** |
| `--delete-db` | False | Delete the existing database on reinstall. All indexed data will be lost, and the `[collections]` registrations (`collections` and `pinned_collections`) are cleared so `collection list` no longer shows phantom entries for the wiped store (`watch` is left untouched). Use only with `--force`. |
| `--accept-jina-license` | False | Pre-accept the Jina CC-BY-NC-4.0 license for multilingual Balanced/Max profiles. |
| `--accept-fasttext-license` | False | Pre-accept the fasttext CC-BY-SA 3.0 license for multilingual installs. |
| `--dry-run` | False | Print every action the wizard would take without executing any of them. |
| `--non-interactive` | False | Skip all interactive prompts and use defaults. Combine with feature flags for full automation. |
| `--config PATH` | `~/.archon-search/archon-search.toml` | Use a non-default config file path. |
| `--code / --no-code` | Not set (interactive) | Install (`--code`) or skip (`--no-code`) tree-sitter code enrichment packages. |
| `--watch / --no-watch` | Not set (interactive) | Enable (`--watch`) or disable (`--no-watch`) the filesystem watcher. Either flag writes `[collections].watch` (`true`/`false`), skipping the interactive prompt. |
| `--telemetry / --no-telemetry` | Not set (interactive) | Enable (`--telemetry`) or disable (`--no-telemetry`) local query telemetry. Either flag writes `[telemetry].enabled` (`true`/`false`), skipping the interactive prompt. |
| `--eager-load / --no-eager-load` | Not set (interactive) | Widen startup warm-up to every per-collection embedding model (`--eager-load`), or keep it at the default embedder and reranker only (`--no-eager-load`). Warm-up always covers those two, and `/ready` returns `503` until model warm-up and the background model validation finish (a failed warm-up ends the wait). Either flag writes `[database].eager_load_embedders` (`true`/`false`), skipping the interactive prompt. |
| `--no-reranker` | False | Disable the cross-encoder reranker for lower latency. |
| `--routing-strategy {centroid,hybrid}` | Not set (interactive) | Set the routing strategy directly, skipping the interactive prompt. |
| `--log-format {text,json}` | Not set (interactive) | Set the log format directly, skipping the interactive prompt. |
| `--disable-gpu` | False | Force CPU execution: skip the GPU confirmation prompt and write `[database].providers = []`. Also empties the `[graph].providers` candidates, overriding `--graph-providers`, so no graph accelerator is requested. |
| `--graph-providers TEXT` | Not set | Comma-separated ONNX execution providers for the graph extraction engine (`[graph].providers`), e.g. `CUDAExecutionProvider`. Unknown names are rejected. An empty string requests no accelerator. This supplies *candidates* only: an accelerator value is written just when the install-time two-stage device probe validates it against the pre-warmed model — otherwise `[]` (CPU) is written, except for the no-write cases in [Step 8](#step-8--model-download). Without this flag a `--non-interactive` install never opts into an accelerator, since nobody is there to accept one the guardrails were not tuned for. Ignored (with a note) when graph extraction is not being installed. |
| **Tier 1 deployment flags** | | |
| `--host TEXT` | Not set (uses `127.0.0.1`) | Bind address for the HTTP API. Use `0.0.0.0` for remote or Docker access. Non-loopback values print a security note reminding you to add a firewall or reverse proxy. Cannot be an empty string. |
| `--port INTEGER` | Not set (uses `8765`) | HTTP port for the installed config (valid range 1–65535). This is an **install-time config flag** — it writes `[server].port`; there is no runtime `--port` flag on `serve`/`start`. Port conflicts are not detected at wizard time; the OS reports an error at service start. |
| `--db-path PATH` | Not set (uses `~/.archon-search/search`) | Database directory. The tilde is written as-is to the config file; `config.py` expands it at use sites. The wizard creates the directory (including parent dirs) and checks writability. If the existing config uses a different path, a migration note is printed. |
| `--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}` | Not set (uses `INFO`) | Server log level. Case-sensitive. |
| `--top-k INTEGER` | Not set (uses `5`) | Number of results returned per query (`top_k_return`). Valid range: 1–100. Values > 100 are rejected with a message to edit TOML directly. The wizard also sets `top_k_retrieve = max(15, 3 × top_k)` automatically. This flag is flags-only; no interactive prompt. A hint appears in the "Next steps" block. |
| `--telemetry-retention-days INTEGER` | Not set (uses `30`) | Days before telemetry log files are pruned. Must be ≥ 1 (same rule as `[telemetry].retention_days` in the config). Always written to TOML as `[telemetry].retention_days`, but never enables telemetry: when this run's telemetry answer is not on (`--no-telemetry`, `n`/Enter at the prompt, or a `--non-interactive` run without `--telemetry`) the value is still written and a stderr warning says it has no effect until telemetry is enabled. The warning is printed after the config is written, so a run that aborts before writing prints none; with `--dry-run` it says the value would be written. This is a warning rather than an error because the value stays valid and is kept for when you turn telemetry on. |
| **Tier 2 AI flags** | | |
| `--enable-hyde` | False (flag) | Enable HyDE (Hypothetical Document Embeddings) query expansion. Provider defaults to `"anthropic"` but can be changed by setting `[hyde].provider` in `archon-search.toml` (supported values: `"anthropic"`, `"openai"`, `"ollama"`, `"claude_cli"`, `"llama_cpp"`). For Anthropic or OpenAI, the corresponding API key must be set; for Ollama or llama.cpp, no API key is needed and query text stays on-host; for Claude CLI, `claude` must be on PATH and logged in (no API key). See [`60_searching.md`](./60_searching.md). |
| `--enable-rag-fusion` | False (flag) | Enable RAG Fusion multi-query expansion. Same provider options and privacy considerations as `--enable-hyde`; provider controlled by `[rag_fusion].provider` in config. See [`60_searching.md`](./60_searching.md). |
| **Tier 2 security** | | |
| `--server-key HEX_KEY` | Not set | Set a custom Bearer token for the server. Must be a lowercase hex string of at least 32 characters (e.g., generated with `python -c "import secrets; print(secrets.token_hex(32))"`). Writes `ARCHON_SEARCH_API_KEY=<key>` to `~/.archon-search/.search.env` (mode 600), after the Step 8 model pre-warm: an install that fails before then leaves the key file unchanged. A shell-history warning and restart note are printed when the key is written. If `ARCHON_SEARCH_API_KEY` env var is set, it takes priority over the file; an additional warning is printed in that case. |

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
| Filesystem watcher | Disabled (key not written — pass `--watch`/`--no-watch` to write it explicitly) |
| Telemetry | Disabled (key not written — pass `--telemetry`/`--no-telemetry` to write it explicitly) |
| Eager load | Disabled (key not written — pass `--eager-load`/`--no-eager-load` to write it explicitly) |
| Routing strategy | `centroid` (key not written — pass `--routing-strategy` to write it explicitly) |
| Log format | `text` (key not written — pass `--log-format` to write it explicitly) |
| AI query expansion (HyDE/RAG Fusion) | Disabled (keys not written — use `--enable-hyde` and/or `--enable-rag-fusion` to enable; configure provider in TOML or re-run the interactive wizard) |
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
| Fresh install, `--host` not passed | `host` | `"127.0.0.1"` (always written on a fresh install — see below) |
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
| GPU (Metal, full CoreML) | `providers` (+ `reranker_providers` removed if it is `[]`) | `["CoreMLExecutionProvider"]` |
| GPU (Metal, split: embedder CoreML / reranker CPU) | `providers` + `reranker_providers` | `["CoreMLExecutionProvider"]` + `[]` |
| GPU (CUDA, CUDA check passes: `onnxruntime-gpu` installed) | `providers` (+ `reranker_providers` removed if it is `[]`) | `["CUDAExecutionProvider"]` |
| GPU (CUDA, CUDA check fails or not in the installed ONNX Runtime — the stock `onnxruntime`) | `providers` | `[]` |
| GPU (Metal, CoreML check fails or not in the installed ONNX Runtime) | `providers` | `[]` |
| GPU declined, or `--disable-gpu` | `providers` | `[]` |
| No GPU detected | nothing written (CPU) | — |
| Reranker disabled (`--no-reranker`) | `reranker_model` | `""` |
| Eager load (interactive answer, or `--eager-load`/`--no-eager-load`) | `eager_load_embedders` | `true` / `false` |
| `--db-path PATH` | `db_path` | `"~/data/archon-search"` |
| `--top-k INTEGER` | `top_k_return` | `20` |
| `--top-k INTEGER` (derived) | `top_k_retrieve` | `60` (= max(15, 3 × top_k)) |

GPU rows: `reranker_providers` is touched only on the paths where a check passes: a full CoreML or CUDA pass removes it when it is `[]` (a value you set by hand is kept), and the Metal split writes `[]`. A `providers` list that already contains the detected provider is left unchanged, so a chain you extended by hand survives a re-run.

### `[collections]` section

| Wizard choice / flag | TOML key | Example value |
|---|---|---|
| Filesystem watcher (interactive answer, or `--watch`/`--no-watch`) | `watch` | `true` / `false` |

### `[telemetry]` section

| Wizard choice / flag | TOML key | Example value |
|---|---|---|
| Telemetry (interactive answer, or `--telemetry`/`--no-telemetry`) | `enabled` | `true` / `false` |
| `--telemetry-retention-days` (always written; inert, with a stderr warning, while telemetry is not enabled) | `retention_days` | `90` |

### `[routing]` section

| Wizard choice / flag | TOML key | Example value |
|---|---|---|
| Routing strategy (interactive answer, or `--routing-strategy`) | `routing_strategy` | `"hybrid"` |

### `[logging]` section

| Wizard choice / flag | TOML key | Example value |
|---|---|---|
| Log format (interactive answer, or `--log-format`) | `format` | `"json"` |
| `--log-level TEXT` | `level` | `"DEBUG"` |

### `[hyde]` section

| Wizard choice / flag | TOML key | Example value |
|---|---|---|
| AI query expansion (interactive answer, or `--enable-hyde`) | `enabled` | `true` / `false` |

### `[rag_fusion]` section

| Wizard choice / flag | TOML key | Example value |
|---|---|---|
| AI query expansion (interactive answer, or `--enable-rag-fusion`) | `enabled` | `true` / `false` |

**Only the choices you actually made are written.** A default you never answered — a `--non-interactive` run without the matching flag — is not written to the file, so a fresh install with no optional answers holds only `[server].host`, the profile's `[database]` keys (`embedding_model`, `reranker_model`, `chunk_size`, `profile`, `multilingual`) and `[database].providers` when the GPU step writes it (a detected GPU, or `--disable-gpu`/a declined GPU prompt, which write `[]`) — keys such as `[server].port`, `[database].db_path`, the `[routing]` tuning keys, `[collections].collections`/`pinned_collections` and the `[logging]` keys are left out and resolve to their built-in defaults (`archon-search config show` prints them filled in). The one exception is `[server].host = "127.0.0.1"`, which a fresh install (and `--force --delete-db` without an existing config) always writes unless `--host` is passed: `archon-search serve` defaults the host to `0.0.0.0`, so without the key a wizard-written config would bind all interfaces. The host is written only when the wizard creates the file: a re-run, or `--force --delete-db` over an existing config, keeps whatever `[server].host` the file has (or lacks) unless `--host` is passed, and `archon-search serve` then binds `0.0.0.0` if the key is absent. For `[collections].watch`, `[telemetry].enabled`, `[database].eager_load_embedders`, `[routing].routing_strategy`, `[logging].format`, `[hyde].enabled`, and `[rag_fusion].enabled`, a re-run that leaves the question unanswered also removes the key a prior run wrote. An answer you actually gave *is* written, even when it matches the default: passing an explicit flag (e.g., `--port 8765`, `--routing-strategy centroid`, `--no-watch`) always writes the key, and so does answering the interactive `routing_strategy`, `log_format`, filesystem-watcher, telemetry, eager-load, or AI-query-expansion prompt (the last writes both `[hyde].enabled` and `[rag_fusion].enabled`) — including pressing Enter to accept the shown default, which counts as an answer (a declined y/n prompt is written as `false`). In an interactive run this also applies when stdin closes early (EOF, piped input): the wizard falls back to the shown default and records it as your answer, so `[routing].routing_strategy`, `[logging].format`, `[collections].watch`, `[telemetry].enabled`, `[database].eager_load_embedders`, `[hyde].enabled`, and `[rag_fusion].enabled` are still written. All other keys in `archon-search.toml` remain at their defaults.

The wizard also backs up your existing config to `~/.archon-search/archon-search.toml.bak` before making any changes, and records the wizard-managed values it left in the config in `~/.archon-search/archon-search.toml.wizard.json` (used by the next re-run to tell its own values from hand edits — see [Same profile (idempotent re-run)](#same-profile-idempotent-re-run)). If the record cannot be updated, the wizard prints a warning naming the record file and finishes the run as it would otherwise; a later re-run may then misreport hand edits. `--dry-run` writes neither file.

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

Collections are normally managed through the HTTP API or the `archon-search ingest` CLI command. The wizard does not configure them — the one exception is `--force --delete-db`, which empties `collections` and `pinned_collections` where the existing config has them, because it wipes the database they point to. That clear step leaves `watch` alone, but `watch` still follows the normal re-run rule: it is removed when the run leaves the filesystem-watcher question unanswered. It does not add these keys to a config that lacks them, and with no existing config it writes the same minimal file as a fresh install.

### Graph search

The wizard writes `[graph].enabled = true` when `--code` is passed (because code graphing requires the graph subsystem). It also writes `[graph].provider` and `[graph].extraction_model` when the user selects an LLM enrichment provider during the interactive flow. All other `[graph]` keys (synonym enrichment, PageRank, PPR tuning) are configured by editing the config directly. See [`65_graph_search.md`](./65_graph_search.md) for prose graph search and [`70_code_graph_and_impact.md`](./70_code_graph_and_impact.md) for the code graph and impact analysis.

### Telemetry log directory

The wizard can enable telemetry (the telemetry prompt, or `--telemetry`) and set `retention_days` (via `--telemetry-retention-days`, which never enables telemetry on its own), but does not configure `log_dir` (default: `~/.archon-search/search-logs/`). Edit `[telemetry]` in the config file to change the log directory.

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
2. Backs up your existing config to `archon-search.toml.bak` (only if proceeding past the overwrite check), and after writing the config records its wizard-managed values in `archon-search.toml.wizard.json`.
3. Overwrites the profile-related keys in `archon-search.toml` with the new values (including any optional feature changes you make in the prompts).
4. Downloads any missing model weights.
5. Re-registers and restarts the service.

Your indexed data is **preserved**. Changing optional features (telemetry, watcher, etc.) without changing the profile is safe for your index, but every re-run rewrites the optional-feature keys from that run's answers alone — see [Changing optional features only](#changing-optional-features-only) before re-running with `--non-interactive`.

The overwrite detection compares the wizard-managed keys only — the keys a re-run rewrites or removes even when no flag names them, apart from the GPU provider list: the `[database]` profile fields (`embedding_model`, `reranker_model`, `chunk_size`, `profile`, `multilingual`), `[collections].watch`, `[telemetry].enabled`, `[telemetry].retention_days`, `[database].eager_load_embedders`, `[routing].routing_strategy`, `[logging].format`, `[hyde].enabled` and `[rag_fusion].enabled`. `[server]` and the other deployment keys, `[graph]` and the HyDE/RAG Fusion provider/model keys are never compared, because a re-run writes them only when their flag or answer is given. `[database].providers` is not compared either, although a re-run can rewrite it from the GPU answer without a flag (`--non-interactive` accepts GPU acceleration): it keeps a provider list that already contains the detected accelerator and leaves the list untouched when no GPU is detected, so comparing it would warn on re-runs that do not change it. A hand-edited provider list is therefore replaced without a warning when the re-run enables a detected GPU the list does not contain, and with `[]` when you decline GPU acceleration, pass `--disable-gpu`, or the Metal check fails. The wizard compares these keys against `archon-search.toml.wizard.json`, the record of the values it left in the config on its last run (written at every exit after the config write, including a declined `Proceed?` and a failed install). A key changed, added or removed since then is a hand edit — including an edit back to a default — while a value the wizard wrote itself (e.g. `--watch`, `hybrid` routing, `--no-reranker`) never is. A config without a readable record (written by an older version of the wizard, or with the record deleted or corrupt) is checked once against a fallback baseline: the profile fields against the profile's values and the other keys against the built-in defaults (absent or set to the default does not count). That fallback cannot tell a wizard-written non-default value from a hand edit, so such a config may show the warning once; answer `y` to overwrite safely. The record exists from that run on.

### Different profile (rebuilds the index)

Re-running with a different profile that uses a different embedding model or chunk size requires rebuilding the index, because the existing vectors are incompatible with the new model.

In **interactive** mode the wizard does not dead-end — it explains the change and asks you to confirm:

```
Switching to BAAI/bge-large-en-v1.5 (chunk_size=1024) changes the embedding model and requires rebuilding the index. The existing index (BAAI/bge-small-en-v1.5, chunk_size=512) and its data will be permanently deleted.
Rebuild the index now? Type 'yes' to proceed:
```

Type `yes` to rebuild (the wizard deletes the old index, writes the new profile, and restarts the service). Anything else aborts cleanly, leaving your configuration and index unchanged.

In **`--non-interactive`** mode the wizard exits with a non-zero status and prints the exact command to re-run (automation needs an explicit failure signal, not an interactive prompt):

```
Switching to BAAI/bge-large-en-v1.5 (chunk_size=1024) requires rebuilding the index; the existing index (BAAI/bge-small-en-v1.5, chunk_size=512) and its data will be deleted. To proceed, run:
  archon-search wizard --profile max --no-multilingual --force --delete-db
```

**This permanently deletes your vector index.** Re-index your documents after the wizard completes using `archon-search ingest`.

### Changing optional features only

To update optional features without changing the profile, re-run the wizard with the same `--profile`. The interactive prompts ask every question again, or you can pass flags to answer them directly.

A re-run does not merge with the previous optional-feature answers: each optional-feature key is rewritten from this run's answers alone. In a `--non-interactive` re-run, every optional-feature question you do not answer with a flag is reset — `[collections].watch`, `[telemetry].enabled` (and `[telemetry].retention_days`), `[database].eager_load_embedders`, `[routing].routing_strategy`, `[logging].format`, `[hyde].enabled`, and `[rag_fusion].enabled` are removed from the config (HyDE and RAG Fusion stay disabled by default; their provider/model keys from an earlier opt-in are left in place), and a reranker removed with `--no-reranker` is restored to the profile default. The deployment settings are not reset: `--host`, `--port`, `--db-path`, `--log-level`, and `--top-k` write their keys (`[server].host`, `[server].port`, `[database].db_path`, `[logging].level`, `[database].top_k_return`/`top_k_retrieve`) only when you pass the flag, so a re-run without them keeps the previous values. The exceptions keep their previous value: `[graph].enabled = true` from an earlier code-enrichment opt-in is never cleared (declining code enrichment or omitting `--code` does not set it back to `false`); `[graph].provider`, `[graph].extraction_model`, and the graph base URL are written only when you choose a graph-enrichment provider, so a run that does not choose one leaves the previous values in place; and `[telemetry].retention_days` survives any re-run that turns telemetry on (`--telemetry`, or `y` at the interactive prompt) without `--telemetry-retention-days`, and is removed by a re-run that leaves telemetry off or unanswered without the flag. A re-run with `--telemetry-retention-days` writes the new value whatever the telemetry answer. Edit the graph keys by hand to change them. In an interactive re-run, the watcher, telemetry, eager-load, routing, and log-format prompts offer their built-in default (shown in brackets), not your previous answer, so pressing Enter writes that default. To change one feature and keep the rest, either re-run interactively and answer every prompt again, or pass every flag you want to keep alongside the new one:

```bash
# Enable the filesystem watcher, keeping telemetry and JSON logs from the previous run
archon-search wizard --profile minimal --watch --telemetry --log-format json --non-interactive
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

A later wizard re-run without `--disable-gpu` re-enables GPU acceleration on a detected GPU and replaces that list (the GPU question defaults to yes, `--non-interactive` accepts it, and the hand-edited list is not flagged as a hand edit), so keep passing `--disable-gpu` on this machine.

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
- Another process is using port 8765. Set a different port with `archon-search config set server.port 9000` (or add `port` under `[server]` in `archon-search.toml` — a fresh wizard install does not write it) and re-run `archon-search install`.
- Model weights failed to download or are corrupt. Re-run the wizard without `--skip-preload`; if pre-warm reports the model cannot be loaded, see [Model pre-warm fails](#model-pre-warm-fails-was-downloaded-but-cannot-be-loaded).
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

### Model pre-warm fails: "was downloaded but cannot be loaded"

```
Model pre-warm failed: The embedder model '<model>' was downloaded but cannot be loaded: <onnxruntime error>
```

The files arrived but onnxruntime cannot build a session from them, so the wizard stopped before starting a service whose every ingest would fail (Step 8). The same text without the `Model pre-warm failed:` prefix, ending in `— the install continues, but /search skips reranking and /explain with rerank fails until the model loads. …`, is the non-fatal reranker variant: the service is already running, and the fixes below apply to it too.

- **A damaged file** (for example `Protobuf parsing failed`): the onnxruntime error names the file it tried to load, under `<data-dir>/models/models--…/` (`$FASTEMBED_CACHE_PATH/models--…/` when that is set). That path is a link: it can resolve into the cache's shared store `blobs/` beside it (`<data-dir>/models/blobs/`, or `$FASTEMBED_CACHE_PATH/blobs/`), which a re-download re-links rather than replaces — so moving only the `models--…` directory to the trash does not repair it. Resolve the path first with `readlink -f '<path>'` or, where your `readlink` has no `-f`, `python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' '<path>'`; move both the file it prints and the `models--…` directory to the trash, then re-run the wizard; the model is downloaded again. The cache check only compares file sizes, so a same-size corruption is not repaired by re-running alone.
- **`External data path escapes model directory`**: the model keeps its weights in a separate `model.onnx_data` file that the cache stored in a different directory than `model.onnx` (among the shipped profiles, `intfloat/multilingual-e5-large`). fastembed 0.9.0 or later repairs this on load by hard-linking both files side by side, so check that the wizard runs with fastembed 0.9.0 or later, and that the fastembed cache (`<data-dir>/models`, or `$FASTEMBED_CACHE_PATH` when set) is writable on a filesystem that supports hard links (move it with `ARCHON_SEARCH_DATA_DIR` or `FASTEMBED_CACHE_PATH` if not). Otherwise move the model's `models--<repo>` directory to the trash and re-run the wizard with `HF_HUB_DISABLE_SHARED_BLOBS=1` set, so the pre-warm downloads it again in the flat per-repo layout. Details: [the incident runbook](../OperatorGuide/90_incident_runbook.md#ingest-fails-with-external-data-path-escapes-model-directory).
- **An execution-provider error** (for example CoreML rejecting the model): the model is fine, but the GPU provider written to `[database].providers` cannot run it. A provider the installed onnxruntime does not have at all is not this case: it is dropped with a `configured ONNX providers not available: …` warning and the model loads on CPU instead of failing. Re-run the wizard with `--disable-gpu` to install on CPU. `--disable-gpu` writes only `[database].providers`: if you set `[database].reranker_providers` by hand and it names the failing provider, remove that line as well.
- **The model still cannot be loaded on CPU after a fresh download**: the installed onnxruntime cannot run it at all. Pick another profile until it is fixed, and include the full message when you report it.
- **`Could not resolve or download …` instead of `… cannot be loaded`**: the download phase failed, before any load was tried — see [Model download fails or times out](#model-download-fails-or-times-out).

### Service install is not supported (Windows)

Service registration (`launchd` on macOS, `systemd` on Linux) is not supported on Windows. The wizard will complete the config and model download steps, but service registration will fail. On Windows, run the server manually in the foreground with `archon-search serve` (see [`40_running_the_server.md`](./40_running_the_server.md)).

### How to undo the wizard

To fully remove archon-search:

```bash
archon-search uninstall           # stop + unregister the service
archon-search uninstall --delete-db  # also remove all indexed data
```

The config file `~/.archon-search/archon-search.toml` and model weights in the fastembed/HuggingFace cache are not removed by `uninstall`. Delete `~/.archon-search/` manually to remove everything — plus the `FASTEMBED_CACHE_PATH` directory, if you exported one, since the fastembed weights then live there instead of `~/.archon-search/models/`.

---

## Related documents

- [`00_index.md`](./00_index.md) — UserManual table of contents and reading order.
- [`10_installation.md`](./10_installation.md) — installation prerequisites, install profiles, and the `archon-search install` command.
- [`30_configuration.md`](./30_configuration.md) — full reference for every key in `archon-search.toml`.
- [`40_running_the_server.md`](./40_running_the_server.md) — start, serve, stop, and status commands.
- [`160_troubleshooting.md`](./160_troubleshooting.md) — detailed troubleshooting for service and ingestion failures.
