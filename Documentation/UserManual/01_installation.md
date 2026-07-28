**Purpose**: Install `archon-search` on a workstation or server.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-05-20 / **Next review**: 2027-05-20

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

## Install from a checkout

For local development or to track `main` directly:

```bash
git clone https://github.com/user538295/archon-search.git
cd archon-search
uv sync --dev
uv run archon-search --version
```

`uv` is the canonical environment manager for this project; see `pyproject.toml` for the dependency lock.

## ONNX Runtime providers (GPU acceleration)

Embedding and reranking run through ONNX Runtime. By default the CPU provider is used. To enable hardware acceleration, set `[database].providers` in `~/.archon-search/archon-search.toml` (see `archon_search/config.py:156` and `archon-search.toml.example`):

```toml
[database]
# Apple Silicon (M-series Macs)
providers = ["CoreMLExecutionProvider"]

# NVIDIA GPU (Linux/Windows with CUDA toolkit installed)
providers = ["CUDAExecutionProvider"]

# CPU only (default)
# providers = []
```

`providers` is a plain list passed to ONNX Runtime. Behavior on a provider name that the locally installed ONNX Runtime build does not support depends on that build (typically a warning, possible fallback, or an error). #Unverified

The CLI's GPU autodetection (`archon_search/platform/runtime.py:detect_gpu_type`) currently only reports the available accelerator type — it does not auto-populate `providers`. You must set this manually.

## Install profiles (C0)

The installer ships three tiered profiles. Choose based on your hardware and quality requirements:

| Profile | English stack | Multilingual stack | Download | Quality | Memory |
| --- | --- | --- | --- | --- | --- |
| `minimal` | `bge-small-en-v1.5` + MiniLM-L6 reranker | `paraphrase-multilingual-MiniLM-L12-v2` (no reranker) | ~150–220 MB | ★★☆☆☆ / ★☆☆☆☆ | ~0.5 GB |
| `balanced` | `bge-base-en-v1.5` + MiniLM-L12 reranker | `paraphrase-multilingual-mpnet-base-v2` + Jina reranker | ~330 MB / ~2.1 GB | ★★★☆☆ | ~1.0–1.5 GB |
| `max` | `bge-large-en-v1.5` + BGE reranker | `multilingual-e5-large` + Jina reranker | ~2.3–3.4 GB | ★★★★☆ | ~2.5–3.0 GB |

**Multilingual `balanced` and `max` profiles use the Jina reranker (`jinaai/jina-reranker-v2-base-multilingual`)**, which is licensed CC-BY-NC-4.0 (non-commercial). The installer requires explicit license acceptance before downloading it.

**C2 — Language detection**: when `--multilingual` is set, the installer also downloads `lid.176.ftz` (Facebook Research fasttext language identification model, licensed CC-BY-SA 3.0) to `~/.archon-search/models/`. You must accept this license interactively, or pass `--accept-fasttext-license` for non-interactive installs. The model enables the `language=<code>` filter on searches. This ~1 MB model is downloaded even with `--skip-preload` — it is required for the server to start, so it is never deferred (if the download fails, the installer falls back to English-only mode).

The chosen profile is recorded in `[database].profile` and `[database].multilingual` in `~/.archon-search/archon-search.toml`. Reinstalling with a different profile requires `--force --delete-db` (the installer will tell you if this is needed).

## Install as a background service (optional)

The setup flow uses two separate commands:

- **`archon-search wizard`** — the full interactive setup: choose a profile, configure optional features, download models, register and start the service. Run this first.
- **`archon-search install`** — register and start the service only (no prompts, no model download). Requires `wizard` to have been run first.

```bash
archon-search wizard
```

The wizard prompts you to choose a profile, answer questions about optional features, and then downloads models and starts the service. To skip all prompts:

```bash
# Non-interactive English minimal install (fastest, no license required)
archon-search wizard --profile minimal --non-interactive --skip-preload

# Multilingual balanced install with Jina license accepted
archon-search wizard --profile balanced --multilingual --accept-jina-license --non-interactive

# Enable optional features non-interactively
archon-search wizard --profile minimal --non-interactive --watch --telemetry --log-format json
```

All `wizard` flags (verified against `archon_search/cli/install_cmd.py`):

| Flag | Effect |
| --- | --- |
| `--profile {minimal,balanced,max}` | Select the install profile (skips interactive prompt). |
| `--multilingual` | Use multilingual model stack for the chosen profile. |
| `--skip-preload` | Skip pre-warming the heavy embedder/reranker weights after install (they download on first use). For multilingual profiles the small `lid.176.ftz` language-detection model is still downloaded — it is required for the server to start. |
| `--force` | Overwrite an existing install. Required when changing profiles. |
| `--delete-db` | Also delete the database when reinstalling (`--force` required). Use with caution — this removes all indexed data. |
| `--accept-jina-license` | Accept the Jina CC-BY-NC-4.0 license non-interactively (required for multilingual `balanced`/`max`). |
| `--accept-fasttext-license` | **C2** — Accept the fasttext `lid.176.ftz` CC-BY-SA 3.0 license non-interactively (required when `--multilingual`). |
| `--dry-run` | Print actions without executing. |
| `--non-interactive` | Skip all confirmation prompts. |
| `--config PATH` | Use a non-default config file when computing data/log paths. |
| `--code / --no-code` | Install tree-sitter code enrichment packages (`archon-search[code]`). Enables symbol extraction for code files. |
| `--watch / --no-watch` | Enable filesystem watcher: auto-reindex collection source directories on file changes. |
| `--telemetry / --no-telemetry` | Enable local query telemetry (structural metadata only, no raw query strings). |
| `--eager-load / --no-eager-load` | Pre-load embedding models at startup (eliminates ~5–15s first-query latency). |
| `--no-reranker` | Disable the cross-encoder reranker for lower latency (less precise results). |
| `--routing-strategy {centroid,hybrid}` | Set routing strategy. `hybrid` blends centroid + description-embedding scores. |
| `--log-format {text,json}` | Log format. Use `json` for container deployments and log aggregators. |
| `--disable-gpu` | Force CPU execution; skip Metal/CUDA acceleration even when auto-detected. |

The wizard:

1. Detects and removes any legacy service file (`~/Library/LaunchAgents/com.archon.search.plist` on macOS, `~/.config/systemd/user/archon-search.service` on Linux).
2. Asks "Will your corpus include non-English documents?" (sets multilingual models if yes; skipped when `--multilingual` flag is passed).
3. Prompts for (or validates) the install profile.
4. Asks about optional features: code enrichment, reranker toggle, filesystem watcher, telemetry, eager loading, routing strategy, and log format.
5. Prompts for Jina license acceptance if the profile requires it (or checks `--accept-jina-license`).
6. **C2**: When `--multilingual`, prompts for fasttext `lid.176.ftz` CC-BY-SA 3.0 license acceptance (or checks `--accept-fasttext-license`) and downloads the model to `~/.archon-search/models/`.
7. Detects GPU hardware (Metal on Apple Silicon, CUDA on NVIDIA) and prompts for confirmation (or auto-enables with `--non-interactive`; skip with `--disable-gpu`).
8. Creates `~/.archon-search/archon-search.toml` from the profile defaults (and optional feature choices) if missing.
9. Checks available disk space for the selected profile.
10. Installs code enrichment packages if requested (`--code`).
11. Pre-warms model weights (unless `--skip-preload`).
12. Registers and starts the service via the platform adapter (`archon_search/platform/macos.py`, `linux.py`).
13. Polls `GET http://<host>:<port>/health` for up to 60 seconds; exits non-zero if the service does not become ready.

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

## Related documents

- [`02_configuration.md`](./02_configuration.md) — full configuration reference.
- [`03_running_the_server.md`](./03_running_the_server.md) — start/stop/status.
- [`07_troubleshooting.md`](./07_troubleshooting.md) — install failure modes.
