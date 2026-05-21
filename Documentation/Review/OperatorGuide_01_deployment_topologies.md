# Review: OperatorGuide/01_deployment_topologies.md

## Summary

The doc is broadly correct on service-file paths, plist/unit contents, and the general "bind locally, terminate TLS upstream" posture, but contains several **concrete factual errors**:

1. There is no `archon-search start --foreground` subcommand. `archon-search start` delegates to `launchctl`/`systemctl` and returns; the doc presents it as a foreground runner.
2. `/health` is NOT the only unauthenticated path. `middleware_auth.py` exempts `{/health, /docs, /openapi.json, /redoc}`.
3. The "install warns but proceeds when /health is already answering on that port" sentence does not match `install_cmd.py` — there is no port-busy detection at all; install always calls `service.register()` + `service.start()` then polls health.
4. The "Wrapped in `/usr/sbin/taskpolicy -b` … background QoS — ingest will not contend with foreground UI work" gloss is technically accurate (`-b` = background QoS) but is rendered as a strong functional guarantee that isn't supported by anything in code.
5. "`SIGTERM` triggers FastAPI lifespan shutdown — drain pending jobs, flush telemetry. Allow ~30s grace." There is no explicit drain/flush logic for jobs or telemetry in the FastAPI `lifespan` in `archon_search/server/app.py` (lifespan body around lines 88–119 sets up state and yields; no explicit job-drain or telemetry-flush on shutdown). The 30s grace number is unsourced.

Smaller issues with cross-references and code locations are listed below.

## Inaccuracies (numbered)

1. **Line 22, foreground row**: `archon-search start --foreground` does not exist. `archon_search/cli/start.py` defines `start` with only a `--config` option; it calls `_get_service().start()` which dispatches to `launchctl start` (macOS) or `systemctl --user start` (Linux). The same line says `uv run archon-search` is equivalent — running `uv run archon-search` with no subcommand prints Click help (see `cli/main.py`, `@click.group()`). The genuine foreground entry is `python -m archon_search.server` (see `archon_search/server/__main__.py` + `app.run_server` calling `uvicorn.run(app, host, port)` at `app.py:156`).

2. **Lines 30–35, the Foreground code block**: same as (1). The shown commands do not invoke `uvicorn` in the user's shell; they ask the OS service manager to start the daemon and return. Correct command is `uv run python -m archon_search.server`.

3. **Line 37**: "`Ctrl-C` is the only stop signal" — only true for the actual foreground command (`python -m archon_search.server`), not for what the doc shows.

4. **Line 41**: "via `archon_search/platform/macos.py::LaunchdSearchService`" — correct class name and file. Verified.

5. **Line 44**: "`KeepAlive=true`, `ThrottleInterval=60`: crashes restart after 60s." Both keys are present in `_PLIST_TEMPLATE` (lines 42–47 of `macos.py`). `ThrottleInterval=60` is the minimum spawn interval, not literally "restart after 60s"; the wording is approximately right but loose.

6. **Line 45**: `/usr/sbin/taskpolicy -b` — present in plist (`macos.py` lines 25–26). The claim "ingest will not contend with foreground UI work" is editorial colour with no test or measurement behind it in the repo.

7. **Line 46**: log path `~/.archon-search/logs/archon-search.log` — matches `macos.py:73`. Verified.

8. **Line 47**: "There is no equivalent of `loginctl enable-linger`; if the host reboots and no one logs in, the service does not run." Correct for launchd user agents — verified by absence of any LaunchDaemon path in `macos.py` (writes only to `~/Library/LaunchAgents`).

9. **Lines 50–54, launchctl manual ops**: `launchctl list com.archon.search`, `unload`, `load` against `~/Library/LaunchAgents/com.archon.search.plist`. Plist path matches `macos.py:58`. Verified.

10. **Line 58**: "writes `~/.config/systemd/user/archon-search.service` via `archon_search/platform/linux.py::SystemdSearchService`. The install path also runs `loginctl enable-linger $USER`." Both true — see `linux.py:42` and `linux.py:119–122`.

11. **Line 60**: `Restart=always`, `RestartSec=5` — present (`linux.py:27–28`). Verified.

12. **Line 61**: `Nice=10`, `CPUQuota=50%` — present (`linux.py:29–30`). Verified.

13. **Line 62**: "Logs go to `journalctl --user -u archon-search`" — implied by absence of `StandardOutput=`/`StandardError=` redirection in the unit template; systemd default is journal. Verified by inspection of `_UNIT_TEMPLATE` (`linux.py:18–34`).

14. **Line 74**: default bind `127.0.0.1:8765` — matches `config.py:30–31`. Verified.

15. **Line 80**: "If the configured port is busy, `install` warns but proceeds when `/health` is already answering on that port (`archon_search/cli/install_cmd.py`)." **Not in the code.** `install_cmd.py` does no pre-check for port-in-use; it just calls `_get_service().register()`, then `.start()`, then `_wait_for_health` (lines 107–118). If another process already owns the port, `service.start()` will succeed (launchd/systemd) and the agent's child uvicorn will then fail to bind — the doc's described behaviour does not exist.

16. **Line 121**: "`GET /health` is the only unauthenticated path (`archon_search/server/middleware_auth.py:16`)." **Wrong.** `_EXEMPT_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})` at `middleware_auth.py:16`. Three additional paths are exempt.

17. **Line 122**: "CORS is wide-open in v1 (`CORSMiddleware(allow_origins=["*"])` in `archon_search/server/app.py:122`)." Verified at `app.py:122` — actually `allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]`.

18. **Line 123**: "The server does not parse `X-Forwarded-For` or correlate request IDs" — confirmed; `grep` for `X-Forwarded` / `request_id` in `archon_search/server/` returns nothing.

19. **Line 129**: "`archon-search start` runs `uvicorn` in-process (`archon_search/server/app.py::run_server`)." **Wrong about `archon-search start`.** That command runs `launchctl`/`systemctl`, not uvicorn. `run_server` (`app.py:152`) is what `python -m archon_search.server` invokes via `server/__main__.py`. The plist and unit both `ExecStart={python} -m archon_search.server`, so uvicorn runs in the launchd/systemd-spawned child, not in the shell that ran `archon-search start`.

20. **Line 131**: "Required environment: none; `ARCHON_SEARCH_CONFIG`, `ARCHON_SEARCH_API_KEY`, `ARCHON_SEARCH_KEY_FILE` are the only recognised overrides." These three env vars are real (`config.py:83`, `key_manager.py:14,20`). "Required: none" is technically accurate for the foreground path but misleading: the registered plist/unit *bake in* `ARCHON_SEARCH_CONFIG=~/.archon-search/archon-search.toml` (`macos.py:79`, `linux.py:93`), so if you wrap the process yourself (the topic of this section), you DO need to point it at a config.

21. **Line 132**: "Mounts: persistent volume on `~/.archon-search/` (or wherever `db_path`, `log_file`, `[telemetry].log_dir` point)." Field names plausible — `db_path` and `log_file` exist on `SearchConfig`; `[telemetry].log_dir` not verified in this review (CLAUDE.md mentions `~/.archon-search/search-logs/` as the telemetry path under `telemetry/writer.py`, so the config key name should be cross-checked).

22. **Line 133**: "`SIGTERM` triggers FastAPI lifespan shutdown — drain pending jobs, flush telemetry. Allow ~30s grace." The lifespan in `app.py` (~lines 88–119) wires up state on enter and yields; there is no observed explicit job-drain or telemetry-flush on the shutdown side. The 30s figure is not anchored to any constant in `app.py`, `install_cmd.py`, the plist, or the systemd unit.

23. **Line 135**: "CI exercises only the Linux/macOS branches (`PLT-2`)." Tech-debt tag not verified in this review.

24. **Line 26 / Windows row**: `windows.py` does raise `NotImplementedError` (`windows.py:11,14,17,20,23`); `status()` returns a non-running `ServiceStatus` instead of raising (line 25–26). The summary "Not supported" is correct but operators should know `status()` won't throw.

## Verified claims

- Single-process / single-writer LanceDB framing (line 13) is consistent with `store.py` being the only writer in the codebase, and with the ADR cited.
- Plist label `com.archon.search` and path `~/Library/LaunchAgents/com.archon.search.plist` (lines 41, 52–54) — verified `macos.py:14, 58`.
- launchd plist contents (`KeepAlive`, `RunAtLoad`, `ThrottleInterval=60`, `taskpolicy -b`, `StandardOutPath==StandardErrorPath`, both pointing at the same log file) — all match the template.
- systemd unit path `~/.config/systemd/user/archon-search.service`, `Restart=always`, `RestartSec=5`, `Nice=10`, `CPUQuota=50%` (lines 58, 60–61) — verified `linux.py:18–34, 42`.
- `loginctl enable-linger $USER` run during install (line 58) — verified `linux.py:119–122`.
- Default `127.0.0.1:8765` (line 74) — verified `config.py:30–31`.
- CORS wide-open at `app.py:122` (line 122) — verified.
- No `X-Forwarded-For` / request-id handling (line 123) — verified by absence.
- `ARCHON_SEARCH_CONFIG`, `ARCHON_SEARCH_API_KEY`, `ARCHON_SEARCH_KEY_FILE` env vars exist (line 131) — verified in `config.py` and `key_manager.py`.
- Windows stub raises `NotImplementedError` (line 26) — verified `windows.py`.

## Unverifiable / ambiguous

- "Tracked as `ARCH-2`" / "Tracked as `PLT-1`" / "Tracked as `PLT-2`" / "see `ARCH-3`" (lines 16, 26, 123, 135): not cross-checked against `Architecture/530_technical_debt_refactoring_roadmap.md` in this review.
- "ingest will not contend with foreground UI work" (line 45): editorial; no benchmark or test enforces this.
- "reranker + ingest can be slow on cold start" justifying `proxy_read_timeout 120s` (line 114): plausible operator guidance but not a code-anchored fact.
- "~30s grace" on SIGTERM (line 133): not anchored to any constant; cannot be confirmed or denied without running the server and timing it.
- `[telemetry].log_dir` config key name (line 132): not re-verified against `config.py` schema in this pass.
- Caddy / nginx snippets (lines 88–116): syntactically reasonable; correctness depends on operator environment and is not testable from the repo.
