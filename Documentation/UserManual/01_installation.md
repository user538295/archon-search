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

## Install as a background service (optional)

To register and start `archon-search` as a user service:

```bash
archon-search install
```

Flags (verified against `archon_search/cli/install_cmd.py`):

| Flag | Effect |
| --- | --- |
| `--dry-run` | Print actions without executing. |
| `--non-interactive` | Skip the `Proceed with installation? [y/N]` prompt. |
| `--config PATH` | Use a non-default config file when computing data/log paths. |

The installer:

1. Creates `~/.archon-search/archon-search.toml` from defaults if missing.
2. Ensures `db_path` and `log_file` parent directories exist.
3. Detects and removes any legacy service file (`~/Library/LaunchAgents/com.archon.search.plist` on macOS, `~/.config/systemd/user/archon-search.service` on Linux).
4. Registers and starts the service via the platform adapter (`archon_search/platform/macos.py`, `linux.py`).
5. Polls `GET http://<host>:<port>/health` for up to 60 seconds; exits non-zero if the service does not become ready.

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
