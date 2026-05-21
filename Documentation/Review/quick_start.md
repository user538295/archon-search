# Review: quick_start.md

## Summary

The guide is mostly accurate at the level of paths, env vars, key bootstrap, and config layout, but it contains one **load-bearing factual error**: `uv run archon-search` does **not** start the server. `archon-search` is a Click `@click.group()` (`archon_search/cli/main.py`) with no `invoke_without_command=True` and no default callback that runs the server — invoking it with no subcommand prints Click usage and exits. The genuine foreground server entry point is `python -m archon_search.server` (`archon_search/server/__main__.py`), and `archon-search start` is a service-manager wrapper that calls `_get_service().start()` (launchd / systemd), not an in-process uvicorn boot.

A second factual error: the `[telemetry]` paragraph says the loader logs a warning when `export_enabled = true` and coerces to `false`. Source agrees, but the section name in the README ("Configuration" claims `[search]` while config.py uses `[database]`) is inconsistent across the project — quick_start does **not** name a wrong section, it just lists `[server], [database], [routing], [collections], [logging], [telemetry]`, which matches `archon-search.toml.example` and `config.py::load_config`. Verified.

A third minor issue: the BREAKING.md migration note quoted by the guide refers consumers to `[search] top_k_return`, but the actual config section is `[database]`. quick_start re-points to `archon-search.toml` without naming a section, so the doc itself is not wrong, only the linked source is. Worth flagging but not a defect of quick_start.

## Inaccuracies (numbered)

1. **L37-41: `uv run archon-search` starts the server.** False. `archon_search/cli/main.py` defines `main` as `@click.group()` with subcommands `start, stop, status, install, uninstall, ingest, sync, collection, config`. There is no default action; running `uv run archon-search` prints the Click help text and exits 0. The same wrong claim appears in the project `README.md` (L37-43) and in `CLAUDE.md` (Common commands), so quick_start has inherited an existing repo-wide inaccuracy rather than introduced one. The actual foreground entry is `uv run python -m archon_search.server` (see `archon_search/server/__main__.py`, which calls `run_server(load_config())` → `uvicorn.run(app, host=config.host, port=config.port)` at `archon_search/server/app.py:156`). `archon-search start` (see `archon_search/cli/start.py:14-30`) only loads/validates config then calls `_get_service().start()`, which dispatches to `launchctl start` on macOS or `systemctl --user start` on Linux — it requires a prior `archon-search install` to register the service, otherwise it fails.

2. **L41: "default `http://127.0.0.1:8765`".** The host/port defaults are correct (`SearchConfig.host = "127.0.0.1"`, `SearchConfig.port = 8765` in `archon_search/config.py:30-31`), but the sentence is attached to the wrong invocation — see (1).

3. **L33: `uv sync --dev` installs `pytest`, `pytest-asyncio`, `pytest-cov`.** Accurate as a list of the `dev` dependency group (`pyproject.toml` L24-29), but phrased as if those are the only additions; that is true here only because the `dev` group has exactly three entries. Not technically wrong, but the wording invites drift.

4. **L106: "the `top_k` field on `SearchRequest` is ignored at the route level".** Code-level accurate: `routes_search.py:77` calls `await pipeline.search(body.query, body.collection, namespace=ns)` with no `top_k` argument, and `SearchRequest.top_k` (L20) is parsed but unused. However, the pointer "the pipeline uses `config.top_k_return`" is correct (see `app.py:138` passing `top_k_return=config.top_k_return` into `SearchPipeline`). The BREAKING.md entry the guide cites says the migration is to `[search] top_k_return`, but the real config section is `[database]` (see `config.py:163-167` and `archon-search.toml.example:20-34`). quick_start itself does not name a wrong section, so this is only an inherited weakness in the cited reference, not in quick_start.

5. **L97: "use the `archon-search collection` CLI or the `/collections` endpoints first".** Both surfaces exist (`archon_search/cli/collection.py` exposes `list, add, remove, info, reindex`; `archon_search/server/routes_collections.py` is registered in `app.py:140`), so the claim is true, but the guide does not mention that `archon-search collection add` writes back into `archon-search.toml` and *then* ingests — a user following the guide blind will not know how to populate a collection before searching. Not an inaccuracy per se; an omission that defeats the "queryable in ten minutes" promise.

## Verified claims

- **L21: Python `>=3.12`.** `pyproject.toml:5` `requires-python = ">=3.12"`. Confirmed.
- **L28: clone URL `https://github.com/user538295/archon-search.git`.** Matches `README.md:30`. Confirmed.
- **L30: `uv sync --dev`.** Standard `uv` invocation; the `dev` dependency group exists at `pyproject.toml:24`. Confirmed.
- **L41: `archon_search.cli.main:main` entry point.** `pyproject.toml:22` `archon-search = "archon_search.cli.main:main"`. Confirmed.
- **L46-47: API key generated at `~/.archon-search/.search.env` mode `600` (POSIX).** `archon_search/key_manager.py:17-19, 82-132` (creates with `os.open(..., O_EXCL, 0o600)`, then `_chmod_600` which skips on Windows). Confirmed.
- **L47: LanceDB store at `~/.archon-search/search/`.** `config.py:33` `db_path = "~/.archon-search/search"`. Confirmed.
- **L51-55: key resolution order — env var, `ARCHON_SEARCH_KEY_FILE`, default file.** `key_manager.py:25-36` `load_or_generate_key` checks env first via `_load_from_env`, then file via `_load_from_file`, then generates. `_key_file_env` at L14-19 wires `ARCHON_SEARCH_KEY_FILE` to override `KEY_FILE`. Confirmed.
- **L71: `ARCHON_SEARCH_CONFIG` override, absolute/tilde, relative resolved against cwd.** `config.py:82-91` `get_default_config_path`: `os.path.expanduser(env_val)`, then `(Path.cwd() / path).resolve()` for non-absolute. Confirmed.
- **L73: Notable sections `[server]`, `[database]`, `[routing]`, `[collections]`, `[logging]`, `[telemetry]`.** Matches `archon-search.toml.example` and `config.py::load_config` (L131-223). Confirmed. (`[namespaces]` also exists at L225-233 but is not "notable" for a quick start.)
- **L78-81: `GET /health` unauthenticated; all others require Bearer.** `routes_health.py` exists; `server/app.py:24` imports `_EXEMPT_PATHS` from `middleware_auth`; `app.py:67-69` skips per-path security on `_EXEMPT_PATHS`. Confirmed.
- **L86, L92: `/docs` and `/openapi.json`.** FastAPI defaults; `app.py:46-77` customises `app.openapi`. Confirmed.
- **L100-104: `POST /search` JSON body with `collection`, `query`.** Matches `routes_search.py:17-36` `SearchRequest`. Confirmed.
- **L106: `top_k` ignored.** See inaccuracy (4) — the *first half* of the sentence is verified at `routes_search.py:77`.
- **L116: default test run excludes `live`, `eval`, `benchmark`, `integration` and enforces `--cov-fail-under=85`.** `pyproject.toml:61` `addopts = "--strict-markers --strict-config --cov=archon_search --cov-report=term-missing --cov-fail-under=85 -m 'not live and not eval and not benchmark and not integration'"`. Confirmed.
- **L119: `uv run pytest --no-cov` skip coverage locally.** Standard pytest-cov flag; `pyproject.toml:55-60` comment explicitly endorses it as a CLI-only override. Confirmed.
- **L125-128: marker-gated suites.** Markers defined at `pyproject.toml:62-67`. Confirmed.
- **L135: `export_enabled = true` coerced to `false` with warning.** `config.py:209-217` raises a warning and forces `telemetry.export_enabled = False`. Confirmed.

## Unverifiable / ambiguous

- **L33: "`uv sync --dev` installs runtime dependencies plus the `dev` group".** True today (no other dep groups), but if more `[dependency-groups]` are added later the sentence will silently become wrong.
- **L46: "creates `~/.archon-search/` if it does not exist".** `key_manager._generate_and_write` calls `os.makedirs(KEY_FILE.parent, exist_ok=True)` (L83) only when key generation is needed. If the user supplies `ARCHON_SEARCH_API_KEY`, the directory is *not* auto-created by the key bootstrap; it would be created by `TelemetryWriter` only if telemetry is enabled (`app.py:96-97`). The unconditional "on first start the server creates `~/.archon-search/`" is loose but not strictly false because key generation is the default path.
- **L96 "With a collection already ingested".** The guide does not say *how* to ingest. There is an `archon-search ingest` subcommand (`cli/ingest.py`) and an `archon-search collection add <path>` (`cli/collection.py:49-89`), plus REST `/collections` and `/ingest_*` MCP tools. The omission is intentional ("see the OpenAPI doc") but ambiguous for a ten-minute onboarding goal.
- **L106 reference to `BREAKING.md`.** BREAKING.md L21 names `[search] top_k_return`, but the config schema uses `[database] top_k_return`. quick_start does not repeat the section name, so it is not itself wrong — but the cross-reference is misleading.
- **L141-145: forward references to `Architecture/000_*`, `010_*`, `100_*`, `roadmap.md`, `tests/eval/README.md`.** Not verified file-by-file in this review; per the project instruction "treat the source code as the single source of truth; the docs are explanatory and may lag", forward references to other docs are not load-bearing for correctness of quick_start.
