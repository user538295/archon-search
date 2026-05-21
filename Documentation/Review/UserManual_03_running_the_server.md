# Review: UserManual/03_running_the_server.md

Source under review: `/Users/manczg/Documents/development/archon-search/Documentation/UserManual/03_running_the_server.md`

## Summary

The document is mostly accurate at the conceptual level (one bind address, single process serves REST+MCP, fixed service identity, default `127.0.0.1:8765`, `~/.archon-search/.search.env` workflow) but contains several concrete inaccuracies. The two most important are: (1) the "Foreground" section is wrong — `archon-search start` always delegates to launchctl/systemctl; there is no foreground mode in the CLI; (2) `/docs`, `/openapi.json`, and `/redoc` are unauthenticated (in `_EXEMPT_PATHS`), not bearer-protected as the endpoints table claims. The doc also references files that do not exist (`archon_search/cli/start.py` line cite is fine, but `archon_search/cli/status.py` references for the exact format gloss over a real edge case where running with no PID prints just `running`). Several smaller items below.

## Inaccuracies (numbered)

1. **"Foreground" section header (lines 15–27)** — Misleading. `archon-search start` does not run the server in the foreground. `archon_search/cli/start.py` only (a) validates config via `load_config`, (b) calls `_get_service().start()`, which on macOS runs `launchctl load` + `launchctl start` (`platform/macos.py:107-112`) and on Linux runs `systemctl --user start` (`platform/linux.py:47-53`). The exact same code path is used by the "Background service" section below. There is no `--foreground` / `--no-daemon` flag and no in-process uvicorn launcher reachable from the CLI. `run_server()` exists in `server/app.py:152` but is never wired to a CLI command.

2. **"The default bind is `http://127.0.0.1:8765`." (line 27)** — The `start` command does not print or otherwise surface the bind address. The success message is literally `"archon-search started"` (`cli/start.py:27`). The default values are correct (`config.py:30-31`) but the claim implies `start` reports them, which it does not.

3. **Endpoint table — `GET /docs` "bearer" (line 52)** — Wrong. `/docs` is in `_EXEMPT_PATHS` (`server/middleware_auth.py:16`) and therefore reachable without an Authorization header.

4. **Endpoint table — `GET /openapi.json` "bearer" (line 53)** — Wrong. Also in `_EXEMPT_PATHS` (`server/middleware_auth.py:16`); served without auth.

5. **Endpoint table is missing `/redoc`** — `/redoc` is also exempt (`_EXEMPT_PATHS`). Minor, but worth listing alongside `/docs` for completeness.

6. **Endpoint table — `POST /route` "Centroid routing pre-context" (line 57)** — Description label is unclear/unsupported by source phrasing. The route exists in `routes_route.py`, but "pre-context" is not a term used in the codebase; the router's job is centroid pre-ranking to pick collections (`router.py` / `MultiCollectionRouter`). Recommend rewording to "Centroid-based collection routing".

7. **Endpoint table — `GET/POST/DELETE /collections/...` (line 58)** — Incomplete: the router also exposes `POST /collections/{name}/reindex` (`routes_collections.py:299`). Worth noting since reindex is operationally relevant.

8. **Endpoint table — `POST /ingest`, `GET/DELETE /jobs/{id}` (line 59)** — Path parameter shown as `{id}` but the source uses `{job_id}` (`routes_jobs.py:108,120`). Cosmetic but inconsistent with the OpenAPI schema.

9. **Endpoint table — `POST /mcp` (line 61)** — The MCP endpoint is exposed via FastMCP's `streamable_http_app()` mount and accepts more than `POST` (streamable HTTP transport includes `GET` for SSE-like streaming). Listing only `POST` is incomplete. Source: `server/mcp.py:244-249`.

10. **Status output formatting (lines 39–43)** — Partially inaccurate. The doc lists three cases:
    - `running (PID <n>, uptime <s>s)`
    - `running (PID <n>)` if uptime is unknown
    - `stopped` otherwise
    
    The source (`cli/status.py:18-23`) actually produces:
    - `running (PID <n>, uptime <s>s)` when both PID and uptime are known
    - `running (PID <n>)` when PID is known but uptime is None
    - `running` (no parens, no PID) when PID is None — **this case is missing from the doc**
    - `stopped` otherwise
    
    Also, the uptime is formatted with `:.0f` (integer seconds), which the doc's `<s>s` notation does imply but should be made explicit if precision matters.

11. **"`archon-search start    # equivalent to launchctl/systemctl start`" (line 34)** — Approximately true on Linux (`systemctl --user start`), but on macOS the start does `launchctl load` + `launchctl start` (load + start, not just start) — see `platform/macos.py:107-112`. So the equivalence comment is slightly misleading for first-time-after-reboot scenarios.

12. **"The command validates the config (raising on `ConfigError`)" (line 27)** — Accurate, but the wording "raising on ConfigError" is wrong from a user perspective: `cli/start.py:21-23` *catches* `ConfigError`, prints `Error: <msg>` to stderr, and exits with code 1. It does not raise to the caller.

13. **"`archon-search install`" reference (line 31)** — The install command is registered as `install` in `main.py:29`, so this is correct; however, the doc presents it as a prerequisite for `start`/`stop`/`status` working. In fact `start` will attempt `launchctl load` of the plist whether or not `install` was run; `register()` and `start()` are distinct lifecycle steps (`platform/service.py:15-33`). Worth clarifying that `install` is what writes the plist/unit file in the first place, so without `install` the load step will fail.

14. **"`source ~/.archon-search/.search.env`" example (line 70)** — Correct in effect (the file contains `ARCHON_SEARCH_API_KEY=<hex>`), but `source` on a file containing just `KEY=value` does *not* `export` the variable in bash/zsh; it only assigns it. To make `$ARCHON_SEARCH_API_KEY` available to a sub-process spawned by `curl` you need either `set -a; source ...; set +a` or `export $(grep -v '^#' ~/.archon-search/.search.env | xargs)`. The example as written will work in the same shell for variable expansion in subsequent `curl -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY"` calls (since the shell expands the variable before exec), so it is *practically* correct, but the comment "now $ARCHON_SEARCH_API_KEY is set" understates that it is a shell variable, not an environment variable. Borderline — flag as minor.

15. **`auto_reindex_on_chunk_size_change` mention (line 95)** — Correct that the flag exists and defaults to `True` (`config.py:37`). However, the doc does not say where to set it; readers should know it's under `[database]` (which `config.py:147-156` confirms). Minor doc completeness issue.

16. **"The server reads config only at startup." (line 89)** — Verified true for `host`, `port`, `chunk_size`, and most settings (loaded into `SearchConfig` in `app.py:124`). However, the file watcher (`watcher.py`) and sync are runtime components — this statement is broadly correct but would benefit from a caveat that on-disk corpus changes are detected without restart while configuration changes require one.

## Verified claims

- Default bind `127.0.0.1:8765` — `config.py:30-31`.
- `[server]` is the config section for `host`/`port` — `config.py:132-138`.
- `GET /health` is unauthenticated — `_EXEMPT_PATHS` in `server/middleware_auth.py:16`, exempted before the bearer check.
- Auth is enforced by `server/middleware_auth.py` — confirmed (`APIKeyMiddleware`).
- Same process serves REST and MCP, MCP at `/mcp` with shared auth — `server/mcp.py:244-249`.
- Service identity is fixed (plist label / systemd unit name hard-coded) — `platform/macos.py` uses `_LABEL`, `platform/linux.py` uses `_SERVICE_NAME`; `stop` takes no `--config` (`cli/stop.py:9-21`).
- `archon-search start` accepts `--config PATH` — `cli/start.py:13`.
- `archon-search stop`/`status` take no flags — `cli/stop.py`, `cli/status.py`.
- `GET /health` returns `{"status":"running","version":"<pkg-version>"}` — `routes_health.py:18-20` returns `HealthResponse(status="running", version=_VERSION)`.
- `~/.archon-search/.search.env` is the default key file path with `ARCHON_SEARCH_API_KEY=<hex>` format — `key_manager.py:14-22, 65-75`.
- `ARCHON_SEARCH_API_KEY` env var overrides the file — `key_manager.py:24-35`.
- `/status`, `/indexing-state`, `/search`, `/route`, `/collections`, `/ingest`, `/jobs`, `/telemetry/*` all require bearer — they are not in `_EXEMPT_PATHS` and the middleware enforces 401 on missing/invalid token.
- Source file references for each route module (`routes_*.py`) are correct.

## Unverifiable / ambiguous

- "never expose `0.0.0.0` without firewalling the port" (line 10) — Advisory/normative statement, not a code-verifiable fact. Reasonable guidance; consistent with `SecurityGuide_05_network_exposure_and_tls.md` posture.
- The cross-references in "Related documents" (lines 99–102) — Files `02_configuration.md`, `04_ingestion_and_collections.md`, `05_searching.md`, `07_troubleshooting.md` were not all enumerated in the workspace listing (`05_searching.md` and `02_configuration.md` exist; `04_ingestion_and_collections.md` and `07_troubleshooting.md` were not visible in `Documentation/` flat listing — could not verify they exist).
- "raising on `ConfigError`" (line 27) — See item 12; ambiguous between "the validation function raises ConfigError internally" (true) and "the CLI command propagates the exception" (false). Reader interpretation may differ.
- Whether `archon-search install` is documented as a prerequisite elsewhere — not verified across the whole doc set.
