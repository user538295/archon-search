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
pip install archon-search
```

This installs the `archon-search` CLI entry point (declared in `pyproject.toml` as `archon_search.cli.main:main`).

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

The chosen profile is recorded in `[database].profile` and `[database].multilingual` in `~/.archon-search/archon-search.toml`. Reinstalling with a different profile requires `--force --delete-db` (the installer will tell you if this is needed).

## Install as a background service (optional)

To register and start `archon-search` as a user service:

```bash
archon-search install
```

The interactive installer will prompt you to choose a profile. To skip the prompt:

```bash
# Non-interactive English minimal install (fastest, no license required)
archon-search install --profile minimal --non-interactive --skip-preload

# Multilingual balanced install with Jina license accepted
archon-search install --profile balanced --multilingual --accept-jina-license --non-interactive
```

All flags (verified against `archon_search/cli/install_cmd.py`):

| Flag | Effect |
| --- | --- |
| `--profile {minimal,balanced,max}` | Select the install profile (skips interactive prompt). |
| `--multilingual` | Use multilingual model stack for the chosen profile. |
| `--skip-preload` | Skip model weight pre-warming after install (weights download on first use). |
| `--force` | Overwrite an existing install. Required when changing profiles. |
| `--delete-db` | Also delete the database when reinstalling (`--force` required). Use with caution — this removes all indexed data. |
| `--accept-jina-license` | Accept the Jina CC-BY-NC-4.0 license non-interactively (required for multilingual `balanced`/`max`). |
| `--dry-run` | Print actions without executing. |
| `--non-interactive` | Skip all confirmation prompts. |
| `--config PATH` | Use a non-default config file when computing data/log paths. |

The installer:

1. Detects and removes any legacy service file (`~/Library/LaunchAgents/com.archon.search.plist` on macOS, `~/.config/systemd/user/archon-search.service` on Linux).
2. Prompts for (or validates) the install profile and multilingual flag.
3. Prompts for Jina license acceptance if the profile requires it (or checks `--accept-jina-license`).
4. Creates `~/.archon-search/archon-search.toml` from the profile defaults if missing.
5. Checks available disk space for the selected profile.
6. Pre-warms model weights (unless `--skip-preload`).
7. Registers and starts the service via the platform adapter (`archon_search/platform/macos.py`, `linux.py`).
8. Polls `GET http://<host>:<port>/health` for up to 60 seconds; exits non-zero if the service does not become ready.

To remove:

```bash
archon-search uninstall            # stop + unregister
archon-search uninstall --delete-db  # also remove the configured db_path (default: ~/.archon-search/search/)
```

## What gets created on first run

| Path | Purpose |
| --- | --- |
| `~/.archon-search/archon-search.toml` | Server config (auto-created by `install` or `config set`). |
| `~/.archon-search/.search.env` | API key, mode `600`. Auto-generated on first server start (see `archon_search/key_manager.py`). |
| `~/.archon-search/search/` | LanceDB vector store and FTS index. |
| `~/.archon-search/logs/archon-search.log` | Server log. |
| `~/.archon-search/search-logs/` | Telemetry JSONL (only when telemetry is enabled). |

## Related documents

- [`02_configuration.md`](./02_configuration.md) — full configuration reference.
- [`03_running_the_server.md`](./03_running_the_server.md) — start/stop/status.
- [`07_troubleshooting.md`](./07_troubleshooting.md) — install failure modes.
