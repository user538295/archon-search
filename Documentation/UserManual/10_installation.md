**Purpose**: Install `archon-search` on a workstation or server.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-08-20 / **Next review**: 2027-07-29

# Installation

## Principles

1. **One process, one user.** `archon-search` runs as your own user account; all state lives under `~/.archon-search/`.
2. **Python 3.12+ only.** The package declares `requires-python = ">=3.12"`; older interpreters will refuse to install.
3. **Two supported install paths.** `pip install archon-search` from PyPI for end users, or a `uv sync --dev` checkout for self-built users.
4. **Optional service install.** The `archon-search install` subcommand registers a launchd plist (macOS) or a systemd user unit (Linux) so the server starts on login. This is optional — you can also run it in the foreground.

## Requirements

- Python `>=3.12`.
- macOS, Linux, or Windows (service install is supported on macOS and Linux only — see `archon_search/platform/`).
- A few hundred MB of disk for the index under `~/.archon-search/search/` (grows with corpus size).
- Network access on the first run to download the embedding and reranker model weights from Hugging Face.

## Install from PyPI (recommended)

```bash
# pip
pip install archon-search

# uv (installs the CLI into an isolated managed environment)
uv tool install archon-search
```

Both methods install the `archon-search` CLI entry point (declared in `pyproject.toml` as `archon_search.cli.main:main`).

Verify:

```bash
archon-search --version
```

### Linux: install the CPU-only PyTorch build

On Linux, the commands above install the CUDA build of PyTorch. PyTorch is a core dependency: docling's PDF and image-OCR parsing runs on it. PyPI's Linux `torch` wheel is the CUDA build, and it pulls in `triton` and 18 `nvidia-*`/`cuda-*` packages on x86_64 and arm64 alike. The CUDA wheel and those packages take about 4.4 GB more disk than the CPU-only build, for GPU code that a host without an NVIDIA GPU never runs. Measured with torch 2.14.1 on 2026-10-09, a `uv tool install` for Linux x86_64 took 6.6 GB on disk, against 2.2 GB with the CPU-only build. PyPI's macOS and Windows `torch` wheels are CPU-only already.

On a Linux host without an NVIDIA GPU, install the CPU-only build instead:

```bash
# pip — add PyTorch's CPU wheel index next to PyPI
pip install --extra-index-url https://download.pytorch.org/whl/cpu archon-search

# uv — `--torch-backend` is a preview option in uv
uv tool install --torch-backend cpu archon-search
```

Both install `torch==<version>+cpu` and `torchvision==<version>+cpu` and no `nvidia-*` or `cuda-*` package. pip takes the highest version from either index, and `<version>+cpu` sorts above PyPI's plain `<version>` (PEP 440 local version), so the CPU-only build wins whenever PyTorch's CPU index carries the same release as PyPI. The pip command adapts PyTorch's selector command, which installs torch alone with `--index-url`, to `--extra-index-url`, so archon-search and its other dependencies still come from PyPI. After a pip install, check the build in the same environment: `python -c "import torch; print(torch.__version__)"` must print a version ending in `+cpu`.

Put any extras in the same command (`"archon-search[graph,code]"`). To upgrade, repeat the command with `--upgrade` and list the same extras as the original install — `uv tool install --upgrade` without them removes the extras installed earlier:

```bash
pip install --upgrade --extra-index-url https://download.pytorch.org/whl/cpu "archon-search[graph,code]"
uv tool install --upgrade --torch-backend cpu "archon-search[graph,code]"
```

Do not use `uv tool upgrade archon-search` for this install: it has no `--torch-backend` option and ignores `UV_TORCH_BACKEND` (uv 0.12.23), so it replaces the CPU-only build with PyPI's CUDA build as soon as a newer `torch` is out. Sources: PyTorch's [install selector](https://pytorch.org/get-started/locally/) (Linux, Pip, CPU), uv's [PyTorch guide](https://docs.astral.sh/uv/guides/integration/pytorch/) and the [`uv tool install` reference](https://docs.astral.sh/uv/reference/cli/#uv-tool-install). The guide still says `--torch-backend` works only in the `uv pip` interface; the CLI reference lists it for `uv tool install`, and uv 0.12.23 accepts it there.

To switch an existing CUDA install to the CPU-only build:

- **uv:** run the same `uv tool install --upgrade --torch-backend cpu "archon-search[graph,code]"` command, with your extras. It converts the install in place and removes the `nvidia-*`/`cuda-*` packages.
- **pip:** create a new virtual environment and run the CPU-only `pip install` command there. In the existing environment the command changes nothing, because the installed CUDA torch already satisfies the requirement. Forcing a torch reinstall or upgrade there replaces torch but leaves the `nvidia-*`/`cuda-*` packages behind, because pip does not remove dependencies that are no longer required.

The CPU-only pin in this repository's `pyproject.toml` (`[tool.uv.sources]`, Linux x86_64 only) does not reach these installs. It is uv project configuration, not part of the published wheel's metadata ([uv docs](https://docs.astral.sh/uv/concepts/projects/dependencies/#dependency-sources): "Sources are only respected by uv"), so only `uv sync` in a checkout applies it. The `:latest` Docker image installs the CPU-only build through its own pip configuration — see [`140_running_with_docker.md`](./140_running_with_docker.md).

## Install from a checkout

For local development or to track `main` directly:

```bash
git clone https://github.com/user538295/archon-search.git
cd archon-search
uv sync --dev
uv run archon-search --version
```

`uv` is the canonical environment manager for this project; see `pyproject.toml` for the dependency lock.

## Optional extras

Some features ship as optional dependency groups (`[project.optional-dependencies]` in `pyproject.toml`). Install them by adding the extra name in brackets, e.g. `pip install "archon-search[graph,code]"` (or `uv sync --extra graph`).

| Extra | Installs | Unlocks |
| --- | --- | --- |
| `graph` | gliner (pulls torch + transformers), networkx, leidenalg, igraph | Knowledge-graph extraction, community detection, PPR / naive graph search — see [`65_graph_search.md`](./65_graph_search.md). |
| `code` | tree-sitter parsers (Python, TS/JS, Go, Rust, Java, Bash, Swift, C#) | AST-aware def/ref extraction and impact traversal for code files — see [`70_code_graph_and_impact.md`](./70_code_graph_and_impact.md). Also selectable via the wizard's `--code` flag. |
| `multilingual` | fasttext-wheel | Non-English language detection (`language=<code>` search filter). Pair with a multilingual install profile (below). |
| `hyde` | anthropic SDK | HyDE query expansion at search time — see [`60_searching.md`](./60_searching.md). |
| `rag_fusion` | anthropic SDK | RAG-Fusion multi-query retrieval — see [`60_searching.md`](./60_searching.md). |
| `ollama` | ollama SDK | Local Ollama provider for HyDE / RAG-Fusion. |
| `openai-provider` | openai SDK | OpenAI provider for HyDE / RAG-Fusion. |
| *(none for llama.cpp)* | `httpx` (core dep) | Local llama-server provider for HyDE / RAG-Fusion / graph enrichment — no extra install. Set `provider = "llama_cpp"` in the relevant config sections. |

`graph.enabled = true` with the `gliner` *library* absent raises a `ConfigError` at startup (`ensure_graph_engine_importable` in `archon_search/graph_extractor.py`); missing `code` parsers only log a warning and skip code files, so prose graphing still works.

The `graph` extra installs the `gliner` library but **not** its model weights. The prose extraction engine is a pinned GLiNER PyTorch checkpoint — `knowledgator/gliner-relex-multi-v1.0` at the revision pinned in `archon_search/paths.py` (`GRAPH_NER_MODEL_NAME` / `GRAPH_NER_MODEL_REVISION`) — and model weights are not distributed on PyPI, so they are fetched from Hugging Face by `huggingface_hub`, exactly the way the embedding and reranker models already are. `archon-search wizard` pre-warms that fetch (an estimated 1218 MB, Apache-2.0) so the first ingest is not the one that pays for it; the runtime otherwise downloads it lazily on first use into `<data-dir>/models/graph/`. A checkpoint that cannot be fetched or loaded is neither a startup failure nor an ingest failure: prose entity and relation extraction is skipped, code-symbol graphing keeps working, and each affected response carries a warning in `IngestResult.warnings`. The distinction matters — the *library* is checked at startup and fails it; the *checkpoint* is not checked at startup at all, and only degrades. `GET /status` → `model_validation.provider_warnings` reports the missing library (`graph_ner_status` in `archon_search/model_validation.py`), not a failed checkpoint load: a failed load latches for the life of the process and is visible only in the ingest warnings and the server log. Remedies: [`../OperatorGuide/60_graph_operations.md`](../OperatorGuide/60_graph_operations.md).

## ONNX Runtime providers (GPU acceleration)

Embedding and reranking run through ONNX Runtime. By default the CPU provider is used. To enable hardware acceleration, set `[database].providers` in `~/.archon-search/archon-search.toml` (see `archon_search/config.py` and `archon-search.toml.example`):

```toml
[database]
# Apple Silicon (M-series Macs)
providers = ["CoreMLExecutionProvider"]

# NVIDIA GPU (Linux/Windows with CUDA toolkit installed)
providers = ["CUDAExecutionProvider"]

# CPU only (default)
# providers = []
```

`providers` is the list of ONNX Runtime execution providers to load the models with, in order. A provider the locally installed ONNX Runtime build does not offer — for example `CUDAExecutionProvider` without `onnxruntime-gpu`, which a native `pip`/`uv` install does not include — is dropped: the models load on the remaining providers, or on CPU when none remain, the log carries one `configured ONNX providers not available: …` warning, and `GET /ready` reports `checks.models: "warn"` (`archon_search/onnx_providers.py`). Install the matching onnxruntime build to use the accelerator.

The CLI's GPU autodetection (`archon_search/platform/runtime.py:detect_gpu_type`) currently only reports the available accelerator type — it does not auto-populate `providers`. You must set this manually.

**PDF / image-OCR acceleration (PyTorch).** The `providers` setting above only covers embedding and reranking (ONNX Runtime). PDF and image-OCR parsing (docling) runs on PyTorch. Which PyTorch build you have depends on how you installed (see [Linux: install the CPU-only PyTorch build](#linux-install-the-cpu-only-pytorch-build)). To run docling on an NVIDIA GPU, answer **Yes** to the wizard's "enable CUDA acceleration?" prompt on a Linux x86_64 host. The wizard then reinstalls torch and torchvision as the CUDA 12.6 (`+cu126`) build of the installed version. This step is best-effort: it keeps the installed build if the download fails. There is no manual TOML setting for this; it is driven by the wizard's GPU confirmation.

## Install profiles (C0)

The installer ships three tiered profiles. Choose based on your hardware and quality requirements:

| Profile | English stack | Multilingual stack | Download | Quality | Memory |
| --- | --- | --- | --- | --- | --- |
| `minimal` | `bge-small-en-v1.5` + MiniLM-L6 reranker | `paraphrase-multilingual-MiniLM-L12-v2` (no reranker) | ~150–220 MB | ★★☆☆☆ / ★☆☆☆☆ | ~0.5 GB |
| `balanced` | `bge-base-en-v1.5` + MiniLM-L12 reranker | `paraphrase-multilingual-mpnet-base-v2` + Jina reranker | ~330 MB / ~2.1 GB | ★★★☆☆ | ~1.0–1.5 GB |
| `max` | `bge-large-en-v1.5` + BGE reranker | `multilingual-e5-large` + Jina reranker | ~2.3–3.4 GB | ★★★★☆ | ~2.5–3.0 GB |

**Multilingual `balanced` and `max` profiles use the Jina reranker (`jinaai/jina-reranker-v2-base-multilingual`)**, which is licensed CC-BY-NC-4.0 (non-commercial). The installer requires explicit license acceptance before downloading it.

**C2 — Language detection**: when `--multilingual` is set, the installer also downloads `lid.176.ftz` (Facebook Research fasttext language identification model, licensed CC-BY-SA 3.0) to `~/.archon-search/models/`. You must accept this license interactively, or pass `--accept-fasttext-license` for non-interactive installs. The model enables the `language=<code>` filter on searches. This ~1 MB model is downloaded even with `--skip-preload` — it is required for the server to start, so it is never deferred (if the download fails, the installer falls back to English-only mode).

The chosen profile is recorded in `[database].profile` and `[database].multilingual` in `~/.archon-search/archon-search.toml`. Reinstalling with a different profile rebuilds the index: an interactive run prompts you to confirm, while a `--non-interactive` run requires `--force --delete-db` (the installer prints the exact command).

## Install as a background service (optional)

The setup flow uses two separate commands:

- **`archon-search wizard`** — the full interactive setup: choose a profile, configure optional features, download models, register and start the service. Run this first.
- **`archon-search install`** — register and start the service only (`--dry-run` and `--config` are its only flags; no prompts, no model download). Requires `wizard` to have been run first.

```bash
archon-search wizard
```

The wizard prompts you to choose a profile, answer questions about optional features, and then downloads models, test-loads them, and starts the service. An embedding model that downloads but cannot be loaded stops the install with exit code `1` and a message naming the model and the cause; a reranker that cannot load only warns (see [`20_wizard.md` Step 8](./20_wizard.md#step-8--model-download)). To skip all prompts:

```bash
# Non-interactive English minimal install (fastest, no license required)
archon-search wizard --profile minimal --non-interactive --skip-preload

# Multilingual balanced install with Jina license accepted
archon-search wizard --profile balanced --multilingual --accept-jina-license --non-interactive

# Enable optional features non-interactively
archon-search wizard --profile minimal --non-interactive --watch --telemetry --log-format json
```

Key `wizard` flags (verified against `archon_search/cli/install_cmd.py`):

| Flag | Effect |
| --- | --- |
| `--profile {minimal,balanced,max}` | Select the install profile (skips interactive prompt). |
| `--multilingual / --no-multilingual` | Use (or force off) the multilingual model stack for the chosen profile. |
| `--skip-preload` | Skip pre-warming the heavy embedder/reranker weights after install (they download on first use), including the load check, so a model that cannot load is not caught at install time. For multilingual profiles the small `lid.176.ftz` language-detection model is still downloaded — it is required for the server to start. |
| `--force` | Overwrite an existing install. Required when changing profiles. |
| `--delete-db` | Also delete the database when reinstalling (`--force` required). Use with caution — this removes all indexed data. |
| `--accept-jina-license` | Accept the Jina CC-BY-NC-4.0 license non-interactively (required for multilingual `balanced`/`max`). |
| `--accept-fasttext-license` | **C2** — Accept the fasttext `lid.176.ftz` CC-BY-SA 3.0 license non-interactively (required when `--multilingual`). |
| `--dry-run` | Print actions without executing. |
| `--non-interactive` | Skip all confirmation prompts. |
| `--config PATH` | Use a non-default config file when computing data/log paths. |
| `--code / --no-code` | Install tree-sitter code enrichment packages (`archon-search[code]`). Enables symbol extraction for code files. |
| `--watch / --no-watch` | Enable filesystem watcher: auto-reindex collection source directories on file changes. Either flag writes `[collections].watch` (`true`/`false`). |
| `--telemetry / --no-telemetry` | Enable local query telemetry (structural metadata only, no raw query strings). Either flag writes `[telemetry].enabled` (`true`/`false`). |
| `--eager-load / --no-eager-load` | Widen startup warm-up — which always covers the default embedder and the reranker, with `/ready` held at `503` until it finishes — to every per-collection embedding model (`--eager-load`), or keep it at the default embedder and reranker only (`--no-eager-load`). Either flag writes `[database].eager_load_embedders` (`true`/`false`). |
| `--no-reranker` | Disable the cross-encoder reranker for lower latency (less precise results). |
| `--routing-strategy {centroid,hybrid}` | Set routing strategy. `hybrid` blends centroid + description-embedding scores. |
| `--log-format {text,json}` | Log format. Use `json` for container deployments and log aggregators. |
| `--disable-gpu` | Force CPU execution; skip Metal/CUDA acceleration even when auto-detected. |

Additional install-time overrides accepted by `wizard` include `--host`, `--port`, `--db-path`, `--log-level`, `--top-k`, `--telemetry-retention-days`, `--enable-hyde`, `--enable-rag-fusion`, and `--server-key` — these write into the installed config. `--telemetry-retention-days` is always written as `[telemetry].retention_days` but never enables telemetry; while telemetry is not enabled it has no effect, and the wizard prints a warning on stderr. For the full walkthrough of the interactive flow, see [`20_wizard.md`](./20_wizard.md).

## Uninstall

**Step 1 — stop and unregister the service** (while the CLI is still on PATH):

```bash
archon-search uninstall
```

Pass `--delete-db` to also remove the search database directory (default: `~/.archon-search/search/`). This is irreversible — all indexed data is lost:

```bash
archon-search uninstall --delete-db
```

**Step 2 — remove the package:**

```bash
# pip
pip uninstall archon-search

# uv tool
uv tool uninstall archon-search

# checkout / dev install — delete the cloned directory
```

**User data is not removed by either step.** `archon-search uninstall` only stops the OS service (launchd plist on macOS, systemd user unit on Linux) and unregisters it. The following paths are left on disk:

| Path | Contents |
|------|----------|
| `~/.archon-search/archon-search.toml` | Server config |
| `~/.archon-search/.search.env` | API key (mode `600`) |
| `~/.archon-search/search/` | LanceDB vector store and FTS index (removed by `--delete-db`) |
| `~/.archon-search/logs/` | Server logs |
| `~/.archon-search/models/` | Downloaded model weights |
| `~/.archon-search/search-logs/` | Telemetry JSONL (only if telemetry was enabled) |

To remove all user data after uninstalling:

```bash
rm -rf ~/.archon-search/
```

## What gets created on first run

| Path | Purpose |
| --- | --- |
| `~/.archon-search/archon-search.toml` | Server config (auto-created by `install` or `config set`). |
| `~/.archon-search/.search.env` | API key, mode `600`. Auto-generated on first server start (see `archon_search/key_manager.py`). |
| `~/.archon-search/search/` | LanceDB vector store and FTS index. |
| `~/.archon-search/logs/archon-search.log` | Server log. |
| `~/.archon-search/search-logs/` | Telemetry JSONL (only when telemetry is enabled). |
| `~/.archon-search/models/lid.176.ftz` | **C2** — fasttext language identification model (only when installed with `--multilingual`). |
| `~/.archon-search/models/graph/knowledgator--gliner-relex-multi-v1.0-<revision>/` | Hugging Face cache for the pinned GLiNER prose extraction checkpoint (only when the graph extra is in use). Written by the wizard's pre-warm, or lazily by the server on first graph ingest — `get_graph_models_dir()` in `archon_search/paths.py` builds this path from `GRAPH_NER_MODEL_NAME` and `GRAPH_NER_MODEL_REVISION`. |

> The entire runtime tree relocates with a single env var: set `ARCHON_SEARCH_DATA_DIR` to move DB, models, keys, and logs off `~/.archon-search/`.

Once installed, richer file-type ingestion (Office and legacy document formats, PDFs, code) is covered in [`50_ingestion_and_collections.md`](./50_ingestion_and_collections.md).

## Related documents

- [`00_index.md`](./00_index.md) — UserManual table of contents.
- [`20_wizard.md`](./20_wizard.md) — interactive setup walkthrough.
- [`30_configuration.md`](./30_configuration.md) — full configuration reference.
- [`40_running_the_server.md`](./40_running_the_server.md) — start/stop/status.
- [`50_ingestion_and_collections.md`](./50_ingestion_and_collections.md) — ingesting files and managing collections.
- [`160_troubleshooting.md`](./160_troubleshooting.md) — install failure modes.
