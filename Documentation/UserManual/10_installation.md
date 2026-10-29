**Purpose**: Install `archon-search` on a workstation or server.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-08-20 / **Next review**: 2027-07-29

# Installation

## Principles

1. **One process, one user.** `archon-search` runs as your own user account; all state lives under `~/.archon-search/`.
2. **Python 3.13+ only.** The package declares `requires-python = ">=3.13"`; older interpreters will refuse to install.
3. **Two supported install paths.** `pip install archon-search` from PyPI for end users, or a `uv sync --dev` checkout for self-built users.
4. **Optional service install.** The `archon-search install` subcommand registers a launchd plist (macOS) or a systemd user unit (Linux) so the server starts on login. This is optional — you can also run it in the foreground.

## Requirements

- Python `>=3.13`.
- macOS, Linux, or Windows (service install is supported on macOS and Linux only — see `archon_search/platform/`). Measured platforms: macOS arm64 (CPU and Apple GPU `mps`) and Linux arm64 (CPU, in Docker). Linux x86_64, Windows, NVIDIA CUDA and Intel Macs have not been measured; the Windows-specific code paths (private file modes, directory fsync, `O_NOFOLLOW`) are guarded and unit-tested but have not been run on Windows.
- A few hundred MB of disk for the index under `~/.archon-search/search/` (grows with corpus size).
- Network access on the first run to download the embedding and reranker model weights from Hugging Face. They are stored under `~/.archon-search/models/`. With `HF_HUB_OFFLINE=1` in the server's environment a model that is not already there is an error instead of a download (`archon_search/model_store.py`).

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

On Linux, the commands above install the CUDA build of PyTorch. PyTorch is a core dependency: embedding and reranking run on it, and so does docling's PDF and image-OCR parsing. PyPI's Linux `torch` wheel is the CUDA build, and it pulls in `triton` and 18 `nvidia-*`/`cuda-*` packages on x86_64 and arm64 alike. The CUDA wheel and those packages take about 4.4 GB more disk than the CPU-only build, for GPU code that a host without an NVIDIA GPU never runs. Measured with torch 2.14.1 on 2026-10-09, a `uv tool install` for Linux x86_64 took 6.6 GB on disk, against 2.2 GB with the CPU-only build. PyPI's macOS and Windows `torch` wheels are CPU-only already (macOS uses the Apple GPU through PyTorch's `mps` device, see [Device selection](#device-selection-gpu-acceleration)).

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

The same applies to the CUDA 13.2 build the wizard installs on a Linux x86_64 GPU host (`torch==<version>+cu132`, see [`20_wizard.md`](./20_wizard.md)). The uv tool receipt does not record `--torch-backend`, so `uv tool upgrade archon-search` can replace that build with PyPI's default Linux build, which is CUDA 13. Upgrade with `uv tool install --upgrade --torch-backend cu132 "archon-search[graph,code]"` instead, listing your extras. When the wizard adds an extra to a uv tool install, it passes `--torch-backend` matching the installed torch build (`+cpu` → `cpu`, `+cu126` → `cu126`, `+cu130` → `cu130`), so adding an extra keeps the build.

To switch an existing CUDA install to the CPU-only build:

- **uv:** run the same `uv tool install --upgrade --torch-backend cpu "archon-search[graph,code]"` command, with your extras. It converts the install in place and removes the `nvidia-*`/`cuda-*` packages.
- **pip:** create a new virtual environment and run the CPU-only `pip install` command there. In the existing environment the command changes nothing, because the installed CUDA torch already satisfies the requirement. Forcing a torch reinstall or upgrade there replaces torch but leaves the `nvidia-*`/`cuda-*` packages behind, because pip does not remove dependencies that are no longer required.

The CPU-only pin in this repository's `pyproject.toml` (`[tool.uv.sources]`, every Linux architecture: x86_64 and arm64) does not reach these installs. It is uv project configuration, not part of the published wheel's metadata ([uv docs](https://docs.astral.sh/uv/concepts/projects/dependencies/#dependency-sources): "Sources are only respected by uv"), so only `uv sync` in a checkout applies it. The `:latest` Docker image installs the CPU-only build through its own pip configuration — see [`140_running_with_docker.md`](./140_running_with_docker.md).

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

Some features ship as optional dependency groups (`[project.optional-dependencies]` in `pyproject.toml`). Install them by adding the extra name in brackets. The command depends on how you installed archon-search:

| Install method | Add extras | Upgrade, keeping the extras |
| --- | --- | --- |
| `uv tool` | `uv tool install "archon-search[graph,code]"` | `uv tool upgrade archon-search` — Linux `+cpu`/`+cu132` torch: see [Linux: install the CPU-only PyTorch build](#linux-install-the-cpu-only-pytorch-build) |
| pip in a virtual environment | `pip install "archon-search[graph,code]"` | `pip install --upgrade archon-search` |
| pipx | `pipx install --force "archon-search[graph,code]"` | `pipx upgrade archon-search` |
| checkout | `uv sync --extra graph --extra code` | `git pull && uv sync --extra graph --extra code` |

**`uv tool` installs: list every extra in one command.** A uv tool environment is declarative. `uv tool upgrade` re-syncs it to the requirements recorded in its receipt (`uv-receipt.toml` in the tool environment) and removes every package installed some other way, for example with `uv pip install`. `uv tool install` records exactly what it is given and replaces the previous receipt. So the command must list every extra you want, the ones already installed included. It must also repeat any `--with`, index or `--torch-backend` options from the original install. Otherwise the next `uv tool upgrade` removes what the command left out. uv documents this: tool environments are ["not intended to be mutated directly"](https://docs.astral.sh/uv/concepts/tools/#tool-environments), and upgrades ["retain the settings provided when installing the tool"](https://docs.astral.sh/uv/concepts/tools/#upgrading-tools). The setup wizard follows this rule. In a uv tool install it adds each extra by re-running `uv tool install` with the receipt's requirements and options plus the new extra (`archon_search/install/extras.py`, `archon_search/uv_tool.py`). The startup errors for a missing extra print the same full command. If the wizard cannot reproduce the receipt (it pins a Python version, or records constraints, overrides or executables from another package) or the installed torch build has a local tag uv has no `--torch-backend` for, it falls back to `uv pip install` with the receipt's index options and prints a warning: the extra works until the next `uv tool upgrade`. To keep it, re-run your original `uv tool install` command with the extra added.

**Upgrading from an older version.** Earlier wizard versions installed extras into a uv tool environment with `uv pip install`, and the receipt did not record them. `uv tool upgrade archon-search` then removed them: `gliner`, `fasttext-predict` and the `tree-sitter` parsers. The next server start failed with `[graph]` or multilingual enabled. Record your extras once, with the same options as your original install, before you upgrade: `uv tool install "archon-search[graph,code,multilingual]"`; on Linux with a `+cpu` or `+cu132` torch build, add `--torch-backend cpu` or `--torch-backend cu132`. If an upgrade already removed them, run the same command afterwards. pip and pipx installs are not affected: `pip install --upgrade` and `pipx upgrade` keep extras installed into the environment. `pipx reinstall` keeps only the extras named in the original `pipx install`.

| Extra | Installs | Unlocks |
| --- | --- | --- |
| `graph` | gliner (pulls torch + transformers), networkx, leidenalg, igraph | Knowledge-graph extraction, community detection, PPR / naive graph search — see [`65_graph_search.md`](./65_graph_search.md). |
| `code` | tree-sitter parsers (Python, TS/JS, Go, Rust, Java, Bash, Swift, C#) | AST-aware def/ref extraction and impact traversal for code files — see [`70_code_graph_and_impact.md`](./70_code_graph_and_impact.md). Also selectable via the wizard's `--code` flag. |
| `multilingual` | fasttext-predict | Non-English language detection (`language=<code>` search filter). Pair with a multilingual install profile (below). |
| `hyde` | anthropic SDK | HyDE query expansion at search time — see [`60_searching.md`](./60_searching.md). |
| `rag_fusion` | anthropic SDK | RAG-Fusion multi-query retrieval — see [`60_searching.md`](./60_searching.md). |
| `ollama` | ollama SDK | Local Ollama provider for HyDE / RAG-Fusion. |
| `openai-provider` | openai SDK | OpenAI provider for HyDE / RAG-Fusion. |
| *(none for llama.cpp)* | `httpx` (core dep) | Local llama-server provider for HyDE / RAG-Fusion / graph enrichment — no extra install. Set `provider = "llama_cpp"` in the relevant config sections. |

`graph.enabled = true` with the `gliner` *library* absent raises a `ConfigError` at startup (`ensure_graph_engine_importable` in `archon_search/graph_extractor.py`); missing `code` parsers only log a warning and skip code files, so prose graphing still works.

The `graph` extra installs the `gliner` library but **not** its model weights. The prose extraction engine is a pinned GLiNER PyTorch checkpoint — `knowledgator/gliner-relex-multi-v1.0` at the revision pinned in `archon_search/paths.py` (`GRAPH_NER_MODEL_NAME` / `GRAPH_NER_MODEL_REVISION`) — and model weights are not distributed on PyPI, so they are fetched from Hugging Face by `huggingface_hub`, exactly the way the embedding and reranker models already are. `archon-search wizard` pre-warms that fetch (an estimated 1218 MB, Apache-2.0) so the first ingest is not the one that pays for it; the runtime otherwise downloads it lazily on first use into `<data-dir>/models/graph/`. A checkpoint that cannot be fetched or loaded is neither a startup failure nor an ingest failure: prose entity and relation extraction is skipped, code-symbol graphing keeps working, and each affected response carries a warning in `IngestResult.warnings`. The distinction matters — the *library* is checked at startup and fails it; the *checkpoint* is not checked at startup at all, and only degrades. `GET /status` → `model_validation.provider_warnings` reports the missing library (`graph_ner_status` in `archon_search/model_validation.py`), not a failed checkpoint load: a failed load latches for the life of the process and is visible only in the ingest warnings and the server log. Remedies: [`../OperatorGuide/60_graph_operations.md`](../OperatorGuide/60_graph_operations.md).

## Device selection (GPU acceleration)

Embedding and reranking run on PyTorch. `[database].device` in `~/.archon-search/archon-search.toml` chooses the device (see `archon_search/config.py`, `archon_search/torch_runtime.py` and `archon-search.toml.example`):

```toml
[database]
# auto (default): CUDA if PyTorch sees an NVIDIA GPU, else the Apple GPU (mps), else CPU
device = "auto"

# force one device
# device = "cpu"
# device = "mps"    # Apple Silicon
# device = "cuda"   # NVIDIA GPU
```

`auto` never warns. An explicit `mps` or `cuda` that the host lacks, or that the installed PyTorch build does not support, steps down to CPU instead of failing: the log carries one `device 'cuda' is not available on this host; using cpu` WARNING per process, `GET /status` → `model_validation.provider_warnings` lists `device unavailable: cuda; models loaded on cpu`, and `GET /ready` reports `checks.models: "warn"`.

`[graph].device` is the separate setting for the graph extraction engine. It defaults to `cpu` and is never inherited from `[database].device`.

The settings `[database].providers`, `[database].reranker_providers` and `[graph].providers` (ONNX Runtime execution providers) no longer exist. `serve` and `start` refuse a config file that still holds one (`Error: [database].providers was replaced by [database].device (auto|cpu|mps|cuda); see BREAKING.md`). `archon-search wizard` tolerates them: it prints `Dropping removed config keys … (replaced by device; see BREAKING.md)`, removes them from the file and writes `device`. `config show`, `config get` and `config set` also still work on such a file (they edit the raw TOML), so you can also delete the keys by hand and run `archon-search config set database.device auto`.

The wizard detects the accelerator with `archon_search/platform/runtime.py:detect_gpu_type` (NVIDIA when `nvidia-smi` succeeds, otherwise Apple Silicon) and asks whether to use it (`--non-interactive` accepts). It then writes `[database].device` (`archon_search/install/installer.py`): `"auto"` only after both the embedder and the reranker pass a load-and-run check on the accelerator, otherwise `"cpu"` — also when no GPU is detected, when you decline, and with `--disable-gpu`. When the check fails the wizard prints `Warning: Apple GPU validation failed — falling back to CPU.` (`CUDA` instead of `Apple GPU` on an NVIDIA host; the CUDA message also points to the section below), and the summary's `Device:` line shows `CPU`. The full per-case matrix is in [`20_wizard.md` — `[database]` section](./20_wizard.md#database-section); `[graph].device` is settled separately, see [`20_wizard.md`](./20_wizard.md#step-8--model-download).

**Apple Silicon.** PyTorch's macOS build includes the Apple GPU (`mps`), so no extra package is needed: once the wizard's check passes on it, it writes `auto`, which picks `mps`.

**PDF / image-OCR parsing.** PDF and image-OCR parsing (docling) also run on PyTorch, but `[database].device` does not apply to them: docling chooses its own accelerator in the parse worker process. On macOS the worker sets `DOCLING_DEVICE=cpu` (an operator-set `DOCLING_DEVICE` wins), because docling 2.132 and later fail on Apple's `mps` device ("Cannot convert a MPS Tensor to float64 dtype"); a large scanned PDF is therefore CPU-bound there (measured about 3.8 s per MiB of scanned PDF on an Apple M-series CPU, which is why `[ingest].parse_timeout_seconds_per_mb` exists). Which PyTorch build you have depends on how you installed (see [Linux: install the CPU-only PyTorch build](#linux-install-the-cpu-only-pytorch-build)).

### NVIDIA GPU: run embedding and reranking on CUDA

Embedding and reranking run on an NVIDIA GPU when PyTorch is a CUDA build and `torch.cuda.is_available()` is true. Requirements:

- A CUDA build of PyTorch. On Linux, PyPI's `torch` is one already: torch 2.15.0 is the CUDA 13.2 build (its metadata requires `cuda-toolkit==13.2.2` and `nvidia-cudnn-cu13`). The CPU-only build from [Linux: install the CPU-only PyTorch build](#linux-install-the-cpu-only-pytorch-build) has no CUDA support. PyPI's Windows and macOS builds are CPU-only too.
- A driver the build supports. CUDA 13.x needs NVIDIA driver 580 or later ([CUDA 13.0 release notes](https://docs.nvidia.com/cuda/archive/13.0.0/cuda-toolkit-release-notes/index.html)); check with `nvidia-smi`. The `+cu132` build the wizard installs targets CUDA 13.2 and needs driver 580 or later too: on Linux x86_64 with Python 3.13, PyTorch publishes no CUDA 12 build of torch 2.15 (the newest `cu126` torch is 2.14.1), so a host with an older driver runs the CPU-only build.
- No system CUDA toolkit: PyTorch's CUDA wheels bring the CUDA and cuDNN libraries as `nvidia-*` packages.

Check what your environment has, then re-run the wizard (`archon-search wizard`) and accept CUDA:

```bash
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

Without a wizard run, `[database].device = "auto"` (the default) already uses CUDA whenever that prints `True`.

**Linux x86_64: the wizard installs the CUDA build for you.** When you accept CUDA and the wizard's own PyTorch does not see a GPU (for example the CPU-only build), it reinstalls `torch` and `torchvision` as the CUDA 13.2 (`+cu132`) build of the installed version from `https://download.pytorch.org/whl/cu132`. It then checks `torch.cuda.is_available()` in a fresh Python process, and writes `device = "auto"` only if that is true. The step is best-effort: if the download fails, the wizard keeps the installed build and writes `device = "cpu"`. On other platforms the wizard does not change the PyTorch build: install a CUDA build yourself (PyTorch's [install selector](https://pytorch.org/get-started/locally/) lists the commands) and re-run the wizard.

**uv tool.** Reinstall with the CUDA build, listing your extras (`cu132` for CUDA 13.2):

```bash
uv tool install --upgrade --torch-backend cu132 "archon-search[graph,code]"
```

The receipt does not record `--torch-backend`: upgrade by re-running this command, not `uv tool upgrade` (see above).

**pip.** Linux: the plain `pip install archon-search` in a new virtual environment already installs PyPI's CUDA 13 build. An existing environment that holds the CPU-only build needs the wizard's swap above, or a new virtual environment.

**Checkout (`uv sync`).** On Linux (x86_64 and arm64) the lock pins the CPU-only torch, so a later `uv sync` re-syncs to that build over a CUDA build the wizard swapped in. Re-run the wizard after each `uv sync`.

None of this was run on a real NVIDIA GPU. The commands and the wizard's swap follow `archon_search/install/extras.py` and `archon_search/install/installer.py`; the swap and the CUDA check are covered by unit tests that stub the PyTorch install and the CUDA probe (`tests/test_install_cuda_torch.py`).

If the wizard's CUDA check fails, it prints `Warning: CUDA validation failed — falling back to CPU.`, writes `device = "cpu"`, and links to this section. The server then runs on CPU. To check the device a running server uses, look for the `device 'cuda' is not available on this host; using cpu` WARNING in the log (only when `device = "cuda"` is set explicitly) or the `device unavailable` entry in `GET /status`.

The `:gpu` Docker image has a CUDA build of PyTorch in place and needs no wizard run: see [`140_running_with_docker.md`](./140_running_with_docker.md#running-the-gpu-image).

## Install profiles (C0)

The installer ships three tiered profiles. Choose based on your hardware and quality requirements:

| Profile | English stack | Multilingual stack | Download | Quality | Memory |
| --- | --- | --- | --- | --- | --- |
| `minimal` | `bge-small-en-v1.5` + MiniLM-L6 reranker | `paraphrase-multilingual-MiniLM-L12-v2` (no reranker) | ~225 MB / ~471 MB | ★★☆☆☆ / ★☆☆☆☆ | ~0.5 GB |
| `balanced` | `bge-base-en-v1.5` + MiniLM-L12 reranker | `paraphrase-multilingual-mpnet-base-v2` + Jina reranker | ~572 MB / ~1.7 GB | ★★★☆☆ | ~1.0–1.5 GB |
| `max` | `bge-large-en-v1.5` + BGE reranker | `multilingual-e5-large` + Jina reranker | ~2.5 GB / ~2.8 GB | ★★★★☆ | ~2.5–3.0 GB |

Download sizes are the PyTorch weight files of the embedder plus the reranker (`weight_mb` in `archon_search/model_specs.py`), rounded up, as the wizard shows them. English / multilingual.

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
| `--eager-load / --no-eager-load` | Widen startup warm-up to every per-collection embedding model (`--eager-load`), or keep it at the default embedder and reranker only (`--no-eager-load`). Warm-up always covers those two, and `/ready` returns `503` until model warm-up and the background model validation finish (a failed warm-up ends the wait). Either flag writes `[database].eager_load_embedders` (`true`/`false`). |
| `--no-reranker` | Disable the cross-encoder reranker for lower latency (less precise results). |
| `--routing-strategy {centroid,hybrid}` | Set routing strategy. `hybrid` blends centroid + description-embedding scores. |
| `--log-format {text,json}` | Log format. Use `json` for container deployments and log aggregators. |
| `--disable-gpu` | Force CPU execution: skip Apple GPU / CUDA acceleration even when auto-detected. Writes `[database].device = "cpu"`. |

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

**User data is not removed by either step.** `archon-search uninstall` only stops the OS service (launchd plist on macOS, systemd user unit on Linux) and unregisters it. The following paths are left on disk (shown for the default data dir; with `ARCHON_SEARCH_DATA_DIR` set they live under that dir instead, and `ARCHON_SEARCH_CONFIG`, when set, names the config):

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

> The entire runtime tree relocates with a single env var: set `ARCHON_SEARCH_DATA_DIR` to move DB, models, keys, logs, and the default `archon-search.toml` off `~/.archon-search/`.

Once installed, richer file-type ingestion (Office and legacy document formats, PDFs, code) is covered in [`50_ingestion_and_collections.md`](./50_ingestion_and_collections.md).

## Related documents

- [`00_index.md`](./00_index.md) — UserManual table of contents.
- [`20_wizard.md`](./20_wizard.md) — interactive setup walkthrough.
- [`30_configuration.md`](./30_configuration.md) — full configuration reference.
- [`40_running_the_server.md`](./40_running_the_server.md) — start/stop/status.
- [`50_ingestion_and_collections.md`](./50_ingestion_and_collections.md) — ingesting files and managing collections.
- [`160_troubleshooting.md`](./160_troubleshooting.md) — install failure modes.
