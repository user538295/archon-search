**Purpose**: Get a new contributor from a fresh clone to a running, queryable `archon-search` server in under ten minutes.
**Audience**: Developers onboarding to the project; medior-level Python familiarity assumed.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2026-08-20

# Quick Start

This guide covers a developer install from source. For the PyPI install path, see `../README.md`.

## Principles

1. **`uv` is the only supported workflow.** Run everything through `uv sync` and `uv run`. Mixing `pip` against the same checkout will drift.
2. **State lives under `~/.archon-search/`.** Config, API key, indexes, and logs all live there. Nothing in the repo gets mutated at runtime.
3. **`GET /health` is the only unauthenticated endpoint.** Everything else requires a bearer token.
4. **The OpenAPI document is the contract.** When in doubt, read `GET /openapi.json` (or browse `GET /docs`).
5. **Don't bake `--no-cov` or hardcoded versions into the project.** Both are forbidden by the engineering constraints (see `Architecture/010_engineering_principles_and_constraints.md`).

## Prerequisites

- Python `>=3.12`
- `uv` installed (`https://docs.astral.sh/uv/`)
- `git`

## 1. Clone and Install

```bash
git clone https://github.com/user538295/archon-search.git
cd archon-search
uv sync --dev
```

`uv sync --dev` installs runtime dependencies plus the `dev` dependency group declared in `pyproject.toml` (currently `pytest`, `pytest-asyncio`, `pytest-cov` — consult `pyproject.toml` for the authoritative list).

## 2. Run the Server

`archon-search` (entry point `archon_search.cli.main:main`) is a Click **command group** with subcommands (`start`, `stop`, `status`, `install`, `uninstall`, `ingest`, `sync`, `collection`, `config`). Invoking `uv run archon-search` with no subcommand prints the Click help and exits — it does **not** start the server.

For a foreground developer run, use the module entry point:

```bash
uv run python -m archon_search.server
```

This calls `run_server(load_config())` in `archon_search/server/__main__.py`, which boots `uvicorn` against the FastAPI app on the configured host/port (default `http://127.0.0.1:8765`).

`uv run archon-search start` is **not** an in-process server boot: it loads/validates config, then delegates to the platform service manager (`launchctl start` on macOS, `systemctl --user start` on Linux) via `_get_service().start()`. It therefore requires a prior `uv run archon-search install` to register the service — otherwise it fails.

On first start the server:

- creates `~/.archon-search/` if it does not exist (the key bootstrap creates it when generating a new key; if `ARCHON_SEARCH_API_KEY` is set, the directory is only created lazily by other components such as the telemetry writer); #Unverified
- generates an API key and writes it to `~/.archon-search/.search.env` with file mode `600` (POSIX);
- opens the LanceDB store at `~/.archon-search/search/` (the default `db_path`).

## 3. Find or Override the API Key

The key bootstrap is handled by `archon_search/key_manager.py`. Resolution order:

1. **`ARCHON_SEARCH_API_KEY` environment variable** (highest priority — useful for Docker / CI / multi-host).
2. **`ARCHON_SEARCH_KEY_FILE` environment variable** redirects which file the server reads.
3. **Default file**: `~/.archon-search/.search.env` (mode `600` on POSIX).

To read the bootstrapped key for local use:

```bash
cat ~/.archon-search/.search.env
```

To inject a key for the current shell session:

```bash
export ARCHON_SEARCH_API_KEY="your-key-here"
```

## 4. Locate the Config File

Server configuration lives in `~/.archon-search/archon-search.toml`. To override the path, set `ARCHON_SEARCH_CONFIG` (absolute or tilde-prefixed; relative paths resolve against the working directory).

A fully annotated reference is checked in at `../archon-search.toml.example`. Notable sections: `[server]`, `[database]`, `[routing]`, `[collections]`, `[logging]`, `[telemetry]`. Telemetry is **opt-in and off by default**; see `../README.md` "Telemetry (opt-in)" for the full surface.

**Install profiles (C0):** `archon-search install` now selects a tiered model profile at install time (`minimal`, `balanced`, or `max`). The chosen profile sets `[database].profile`, `embedding_model`, `reranker_model`, and `multilingual` in the config. For a non-interactive developer install:

```bash
uv run archon-search install --profile minimal --non-interactive --skip-preload
```

## 5. Verify the Server Is Up

```bash
curl http://127.0.0.1:8765/health
```

`GET /health` is unauthenticated. Every other endpoint requires `Authorization: Bearer <key>`.

Interactive API explorer:

```
http://127.0.0.1:8765/docs
```

Machine-readable schema (the authoritative REST contract):

```
http://127.0.0.1:8765/openapi.json
```

## 6. Hit `/search`

First populate a collection. The fastest path from the CLI is:

```bash
uv run archon-search collection add <path>
```

`collection add` takes a single path argument: it appends the path to `[collections] collections` in `~/.archon-search/archon-search.toml`, derives the collection name from the path (`archon_search.sync.path_to_collection_name`), and ingests the directory. Alternatively, drive it over HTTP via the `/collections` and ingest endpoints (see `GET /openapi.json`).

With a collection already ingested:

```bash
curl -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection": "docs", "query": "how does the router work?"}'
```

Note: the `top_k` field on `SearchRequest` is ignored at the route level — the pipeline uses `config.top_k_return` from `[database]` in `~/.archon-search/archon-search.toml`. See `../BREAKING.md` for the rationale (note: BREAKING.md currently refers to this knob as `[search] top_k_return`; the actual config section is `[database]`). #Unverified

## 7. Run the Tests

The default test command:

```bash
uv run pytest
```

This run excludes only the `live_benchmark` and `smoke` markers; `live`, `eval`, `benchmark`, and `integration` markers run in the default suite and skip gracefully when their infrastructure is absent. It enforces `--cov-fail-under=85` and runs tests in parallel via `pytest-xdist` (`-n 8 --dist=loadgroup`; raised from 4 on 2026-07-20 — the default suite stubs fastembed so each worker is only ~0.3 GB; never `-n auto`, see the testing policy in `CLAUDE.md`). For an even faster local run on macOS, `bash scripts/test-fast.sh` runs the suite on a RAM disk. To skip coverage while iterating locally:

```bash
uv run pytest --no-cov
```

To run serially (required for fail-fast and stdout passthrough):

```bash
uv run pytest -n0          # serial mode
uv run pytest -n0 -x       # stop on first failure
uv run pytest -n0 -s       # show stdout (suppressed by xdist)
```

Marker-gated suites (run explicitly when relevant):

```bash
uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py
uv run pytest -m integration
uv run pytest -m live
uv run pytest -m benchmark   # needs a running server; auto-skips if unreachable
```

`live_benchmark` and `smoke` are excluded from the default run and must be run separately:

```bash
uv run pytest -m live_benchmark tests/eval/live_benchmark/ --no-cov
uv run pytest tests/smoke/ --no-cov
```

## Common Pitfalls

- **401 on every call.** You forgot the bearer header, or `ARCHON_SEARCH_API_KEY` points at a stale key. Re-read the file or unset the env var.
- **Config changes ignored.** The server reads config at start; restart after editing `~/.archon-search/archon-search.toml`.
- **`export_enabled = true` in `[telemetry]`.** This is coerced to `false` at config load with a warning (`archon_search/config.py`); external telemetry export is a non-goal in v1 — see `Architecture/000_introduction_and_guiding_principles.md` non-goals.
- **Coverage failure on a partial test run.** `--cov-fail-under=85` applies to the default single-run invocation only. Split / matrix runs must `coverage combine` before applying the threshold.

## Where to Go Next

- **Component map and module seams**: `Architecture/100_system_architecture_overview.md` is the canonical next read.
- **Vision and non-goals**: `Architecture/000_introduction_and_guiding_principles.md`.
- **Engineering constraints** (versioning, coverage, structural invariants): `Architecture/010_engineering_principles_and_constraints.md`.
- **Forward plan**: `roadmap.md`.
- **Compatibility log**: `../BREAKING.md`.
- **Evaluation harness maintenance** (fixtures, thresholds, waivers): `../tests/eval/README.md`.
