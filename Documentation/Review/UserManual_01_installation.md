# Review: UserManual/01_installation.md

Sources of truth consulted: `pyproject.toml`, `README.md`, `archon-search.toml.example`, `archon_search/cli/main.py`, `archon_search/cli/install_cmd.py`, `archon_search/config.py`, `archon_search/platform/runtime.py`.

## Summary

The doc is largely accurate. The install / uninstall command surface, the install flags, the post-install steps, the auto-key-file behavior, and the default created paths all match the source. The ONNX providers description is broadly correct but contains one over-confident claim about ORT silently ignoring unknown providers (not verified — likely false; ORT typically raises). One minor inaccuracy: the doc lists `archon-search uninstall --delete-db` as removing `~/.archon-search/search/`, which is only true when `db_path` is at its default. A line-number reference to `config.py:156` is correct.

## Inaccuracies (numbered)

1. **Line 65 — "values not understood by the local runtime are ignored by ORT itself, so a misconfigured provider falls back to CPU rather than failing the server."** Not verified in source. ONNX Runtime's documented behavior is to warn or raise on unknown/unavailable providers, not silently ignore them; fastembed / lancedb behavior on a bad `providers` list is not asserted anywhere in this repo. The claim should be removed or downgraded to "behavior depends on the installed ONNX Runtime build."

2. **Line 97 — `archon-search uninstall --delete-db  # also remove ~/.archon-search/search/`.** The flag exists (`install_cmd.py:124`), but it removes whatever path `cfg.db_path` resolves to (`_get_db_path` → `load_config(...).db_path`, `install_cmd.py:43-46`), not a hardcoded `~/.archon-search/search/`. The default config value happens to be `~/.archon-search/search` (`config.py:51`-area / `archon-search.toml.example:22`), so the comment is correct only by coincidence on a default install.

3. **Line 12 — "Two supported install paths. `pip install archon-search` from PyPI for end users, or a `uv sync --dev` checkout for self-built users."** Minor: `uv sync --dev` is described in README.md as the checkout-based path, but the phrasing "self-built users" is awkward and not a project term. Cosmetic, not factually wrong.

## Verified claims

- L11: `requires-python = ">=3.12"` — matches `pyproject.toml:5`.
- L28: entry point `archon_search.cli.main:main` — matches `pyproject.toml:22`.
- L41-44: clone URL `https://github.com/user538295/archon-search.git`, `uv sync --dev`, `uv run archon-search --version` — clone URL matches README.md:30; the `uv` workflow matches README and CLAUDE.md.
- L51: providers config is read at `archon_search/config.py:156` — verified exact line: `if "providers" in database:` is on line 156.
- L54-62: TOML keys `[database].providers` with `CoreMLExecutionProvider`, `CUDAExecutionProvider`, empty list for CPU — matches `archon-search.toml.example:31-34`.
- L67: `detect_gpu_type` exists in `archon_search/platform/runtime.py` (line 38) and only reports a `GpuType` (CUDA / METAL / NONE) — it does not write to `config.providers`. Verified.
- L74: `archon-search install` command — registered in `cli/main.py:29` via `main.add_command(install)`.
- L80-83: install flags `--dry-run`, `--non-interactive`, `--config PATH` — all three present in `install_cmd.py:64-67`. Prompt text "Proceed with installation? [y/N]" matches `install_cmd.py:80`.
- L87: "Creates `~/.archon-search/archon-search.toml` from defaults if missing" — matches `install_cmd.py:86-90`.
- L88: "Ensures `db_path` and `log_file` parent directories exist" — matches `install_cmd.py:96-99` (note: it creates `db_path` itself, and the *parent* of `log_file`).
- L89: legacy paths `~/Library/LaunchAgents/com.archon.search.plist` (macOS) and `~/.config/systemd/user/archon-search.service` (Linux) — match `install_cmd.py:17-21`.
- L90: registers/starts via platform adapter — matches `install_cmd.py:107-112`; `macos.py` and `linux.py` exist under `archon_search/platform/`.
- L91: "Polls `GET http://<host>:<port>/health` for up to 60 seconds; exits non-zero if not ready" — matches `_HEALTH_TIMEOUT = 60` (`install_cmd.py:14`), `_wait_for_health` (`install_cmd.py:49-61`), and the `raise SystemExit(1)` on timeout (`install_cmd.py:118`).
- L96: `archon-search uninstall` — registered in `cli/main.py:30`.
- L104: `~/.archon-search/archon-search.toml` default config path — matches `archon-search.toml.example:3`.
- L105: `~/.archon-search/.search.env`, mode `600`, auto-generated — matches CLAUDE.md description of `key_manager.py` and `archon-search.toml.example:10-13`.
- L106: `~/.archon-search/search/` LanceDB store — matches `archon-search.toml.example:22` (`db_path = "~/.archon-search/search"`).
- L107: `~/.archon-search/logs/archon-search.log` — matches `archon-search.toml.example:55` and `config.py:51`.
- L108: `~/.archon-search/search-logs/` telemetry JSONL — matches `archon-search.toml.example:66` and `config.py:24`.
- L18: "service install is supported on macOS and Linux only" — consistent with `install_cmd.py` (no Windows branch in `_legacy_service_path`); `windows.py` exists but is not wired into the install command.

## Unverifiable / ambiguous

- L19: "A few hundred MB of disk for the index" — no quantitative claim in code or docs; an operator-experience estimate, not falsifiable from source. Leave as-is or qualify.
- L20: "Network access on the first run to download the embedding and reranker model weights from Hugging Face." — plausible (fastembed/cross-encoder default behavior), but no explicit code path in this repo was inspected to confirm Hugging Face is the registry. Not contradicted.
- L26: `pip install archon-search` works — depends on PyPI availability; package is declared (`pyproject.toml:2`) and README.md:5 links to the PyPI page, so this is taken as verified by external state.
- L33 / L44: `archon-search --version` — the entry point exists, but no `--version` flag is asserted to exist in the inspected portion of `cli/main.py`. Not contradicted; quick sanity check by running the command would be needed to fully verify. Marking as ambiguous rather than an inaccuracy.
