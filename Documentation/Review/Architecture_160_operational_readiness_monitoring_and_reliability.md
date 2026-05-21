# Review: Architecture/160_operational_readiness_monitoring_and_reliability.md

## Summary

The doc is largely accurate when measured against the source. Most endpoint shapes, service-template directives, telemetry invariants, and runbook commands check out. There are, however, a small number of concrete inaccuracies — most importantly:

1. **`archon-search status` does NOT call `GET /status`.** The CLI delegates to the local OS service supervisor (`launchctl list` / `systemctl is-active`), not the HTTP API. The doc's runbook table and step (2) imply otherwise.
2. **The "Service install and lifecycle" mermaid diagram refers to `install.py / cli/install_cmd.py` as if both are live.** Only `cli/install_cmd.py::install` is actually wired up by the CLI (`main.py`). `install.py::SearchInstaller` is dead code in the current install path — its `write_service_file`/`load_service` delegate to `get_search_service()` in `platform/runtime.py`, which raises `NotImplementedError`.
3. **GPU detection is not Linux-gated.** `SearchRuntime.detect_gpu_type` runs `nvidia-smi` on every platform; whichever OS returns rc=0 wins.
4. **`StandardOutPath` / `StandardErrorPath` are not the *only* thing fixing the log path.** The plist template uses whatever path `register()` formats in (currently hard-coded to `~/.archon-search/logs/archon-search.log`), and is independent of `cfg.log_file` — `install_cmd.py` does ensure `cfg.log_file`'s parent exists, but the plist itself does not read `cfg.log_file`.
5. A few small status-shape and "where the warning comes from" details are slightly off.

Severity legend: **high** = misleads operator on what to run / where to look; **medium** = factually wrong but not action-blocking; **low** = phrasing imprecision.

## Inaccuracies (numbered)

1. **Claim**: "`archon-search status` — delegates to GET /status with the configured Bearer token" (runbook code block, line ~107, and runbook step 2).
   **Ground truth**: `cli/status.py::status` calls `_get_service().status()` (line 13), which is `LaunchdSearchService.status()` / `SystemdSearchService.status()` / `WindowsSearchService.status()` — pure OS-level supervisor queries. It never makes an HTTP request and does not read the API key. The output is "running (PID N)" / "stopped" only — no per-collection block.
   **File**: `archon_search/cli/status.py:9-23`; `archon_search/platform/macos.py:126-147`; `archon_search/platform/linux.py:63-81`.
   **Severity**: high.

2. **Claim**: Mermaid diagram and surrounding prose under "Service install and lifecycle": "`CLI[archon-search install] --> INST[install.py / cli/install_cmd.py]`".
   **Ground truth**: `cli/main.py` registers only `cli.install_cmd.install` as the `install` subcommand. `archon_search/install.py::SearchInstaller` is not invoked anywhere in the CLI. Its `write_service_file`/`load_service`/`unload_service` call `get_search_service()` in `platform/runtime.py`, which raises `NotImplementedError("archon-search service lifecycle is not yet implemented...")` — i.e., `SearchInstaller.run` would crash if anyone ran it. The active path is `cli/install_cmd.py::install` → `_get_service()` → concrete `*SearchService`.
   **File**: `archon_search/cli/main.py:8,29`; `archon_search/install.py:186-196`; `archon_search/platform/runtime.py:61-66`.
   **Severity**: high (the doc points readers at a legacy/dead file).

3. **Claim**: "GPU detection (`platform/runtime.py::SearchRuntime.detect_gpu_type`) ... CUDA on Linux when `nvidia-smi` returns 0, CoreML on ARM macOS, otherwise CPU."
   **Ground truth**: `detect_gpu_type` does not check the OS before running `nvidia-smi`. It first attempts `nvidia-smi`; if rc=0 it returns `CUDA` on *any* platform. Only if that fails does it check `Darwin + arm64` for METAL. Also: the enum value is `GpuType.METAL`, and `configure_providers` maps `METAL → "CoreMLExecutionProvider"` — the user-facing provider name is CoreML, but the runtime enum is METAL. The doc collapses these two layers.
   **File**: `archon_search/platform/runtime.py:38-50`; `archon_search/install.py:135-167`.
   **Severity**: medium.

4. **Claim**: "This path is fixed by: `platform/macos.py::_PLIST_TEMPLATE` — `StandardOutPath` and `StandardErrorPath` both point at `~/.archon-search/logs/archon-search.log`. `cli/install_cmd.py::install` — ensures `Path(cfg.log_file).expanduser().parent` exists before service start."
   **Ground truth**: `_PLIST_TEMPLATE` uses a `{log_path}` placeholder; `LaunchdSearchService.register` hard-codes the value `Path.home() / ".archon-search" / "logs" / "archon-search.log"` (`macos.py:73`) — it does *not* read `cfg.log_file`. So the macOS log path and `cfg.log_file` can diverge silently if `cfg.log_file` is customized. The doc's two bullets imply they are kept consistent; they aren't.
   **File**: `archon_search/platform/macos.py:70-86`; `archon_search/cli/install_cmd.py:98-99`.
   **Severity**: medium.

5. **Claim**: "GET `/status` ... Returns: `running`, `pid`, `version`, and a per-collection list with `status`, `processed_files`, `total_files`, `eta_seconds`, `error`, `error_count`, filtered to the caller's namespace."
   **Ground truth**: Mostly correct, but the per-collection entry also returns `name`, `path`, `doc_count`, `chunk_count`, and `watching`. (`path`, `doc_count`, `chunk_count` are currently always `""` / `0` placeholders, but they are part of the response schema.) The top-level `running` field is a `bool` literal — not a string status — and is always `True` whenever this handler runs (anything not running 401s or fails to connect).
   **File**: `archon_search/server/schemas.py:15-34`; `archon_search/server/routes_status.py:65-86`.
   **Severity**: low.

6. **Claim**: "If `GET /telemetry/stats` shows `skipped_lines > 0`, one or more lines in the day's JSONL failed schema validation. The line itself is logged at WARNING by `TelemetryReader.read_entries`."
   **Ground truth**: The warning message is `"telemetry: skipping malformed line in %s"` (filename only) — the line *content* is not logged. So an operator following this runbook will see which file is affected, not the offending payload.
   **File**: `archon_search/telemetry/reader.py:109-112`.
   **Severity**: low.

7. **Claim**: "`telemetry/writer.py` runs a single background drain task per process, fed by a bounded `asyncio.Queue` (default 1024). When the queue is full it drops the **oldest** entry, never the new one, and emits one rate-limited warning per minute."
   **Ground truth**: All accurate, but the doc presents this as if the drop policy is unconditional; in practice if the second `put_nowait` *after* dropping also raises (`QueueFull`), the new entry is silently dropped (defensive branch, marked `pragma: no cover`). Not worth correcting in the doc, but worth noting the "never drops the new one" wording is technically not absolute.
   **File**: `archon_search/telemetry/writer.py:74-88`.
   **Severity**: low.

8. **Claim**: "`launchctl load ~/Library/LaunchAgents/com.archon.search.plist` and inspect `~/.archon-search/logs/archon-search.log`. `LaunchdSearchService.start` raises `RuntimeError` containing `launchctl`'s stderr — read it."
   **Ground truth**: Correct as far as `start()` raising on `launchctl load`/`launchctl start` non-zero rc (`macos.py:107-114`). The runbook command itself is fine. No inaccuracy beyond #4 (which file path actually appears in the plist).
   **Severity**: n/a — listed for completeness.

9. **Claim**: "`install.py::SearchInstaller.configure_providers`" is what writes ONNX provider into `[database].providers`.
   **Ground truth**: The function exists and does what the doc says, but per inaccuracy #2 it is not actually invoked by `archon-search install` — `cli/install_cmd.py::install` never calls it. So in the current active install path, no GPU/provider configuration is written. (The Click `install` command only creates dirs, handles legacy service, then `register()`/`start()`.)
   **File**: `archon_search/install.py:135-167`; `archon_search/cli/install_cmd.py:64-120`.
   **Severity**: high (the doc describes behavior that does not occur in the live install command).

10. **Claim**: Observability mermaid label `S --> SS[SearchStore + IndexingStateStore]`.
    **Ground truth**: `/status` reads `request.app.state.search_store` (for namespace filtering) and `request.app.state.state_store` (`IndexingStateStore`). The label is accurate; flagging only because the doc's "where state comes from" arrow is consistent with the code.
    **Severity**: n/a (verified).

## Verified claims

- `GET /health` is unauth and returns `{"status": "running", "version": <vcs version>}`. (`routes_health.py:18-20`, `schemas.py:10-12`)
- All non-health endpoints require Bearer auth — middleware enforced (per `CLAUDE.md` summary; route files include `responses={401: {"model": ErrorDetail}}`).
- `GET /indexing-state` returns empty object when no state file exists, and filters by namespace. (`routes_state.py:14-36`)
- `GET /telemetry/stats` returns `DisabledResponse(enabled=False)` when telemetry is off, before touching the log dir. (`routes_telemetry.py:28-30`)
- `GET /telemetry/entries` enforces `limit ≤ 200` and `offset ≥ 0`. (`routes_telemetry.py:50-51`)
- Telemetry stats response includes `total_queries`, `success_rate`, `latency_ms.p50/p95`, `by_endpoint`, `by_collection`, `error_breakdown`, `skipped_lines`. (`schemas_telemetry.py:39-52`)
- `MAX_ENTRY_BYTES = 8192` (= 8 KiB); oversize entries binary-search-truncate `result_doc_ids` and set `truncated: true`. (`telemetry/writer.py:33, 159-198`)
- Queue default size 1024; drop-oldest policy; rate-limited warnings via `_WARN_WINDOW_S = 60.0`. (`telemetry/writer.py:46, 74-88, 200-205`)
- Pruner: 24-hour loop, filename-based, never deletes today's file. (`telemetry/pruner.py:21-70`)
- `[telemetry].export_enabled = true` is coerced to `false` with a warning at config load. (`config.py:209-217`)
- Telemetry log dir default: `~/.archon-search/search-logs/`. (`config.py:24`)
- macOS plist: `Label=com.archon.search`, wraps in `/usr/sbin/taskpolicy -b`, `KeepAlive=true`, `RunAtLoad=true`, `ThrottleInterval=60`, `StandardOutPath`/`StandardErrorPath` both point at the log path. (`macos.py:16-50`)
- macOS `start()` is a no-op if `_is_loaded()` (`launchctl list <label>` rc=0). (`macos.py:101-114`)
- Linux unit: `Restart=always`, `RestartSec=5`, `Nice=10`, `CPUQuota=50%`; `register()` runs `daemon-reload`, `systemctl --user enable`, `loginctl enable-linger $USER`. (`linux.py:18-34, 91-124`)
- Linux logs go to journald (`journalctl --user -u archon-search`) — implied by absence of `StandardOutput=` redirection in the unit template.
- Windows lifecycle methods raise `NotImplementedError` with the exact message quoted; `status()` returns `ServiceStatus(running=False, pid=None, uptime_seconds=None)`. (`windows.py:6-26`)
- `SearchServiceLifecycle` ABC declares `start`, `stop`, `restart` (with default impl `stop+start`), `status`, `register`, `unregister`. (`platform/service.py:15-33`)
- `install` polls `GET /health` for up to 60s (`_HEALTH_TIMEOUT = 60`, `_wait_for_health`). (`cli/install_cmd.py:14, 49-61, 115-118`)
- `install --dry-run` prints the plan and returns without touching FS/service. (`cli/install_cmd.py:72-77`)
- Legacy service detection + removal on install. (`cli/install_cmd.py:17-41, 101-105`)
- Default config absent → `install` writes a default `archon-search.toml`. (`cli/install_cmd.py:86-90`)
- `data` (`db_path`) and `log_file` parent directories are `mkdir(parents=True, exist_ok=True)`. (`cli/install_cmd.py:96-99`)
- Auto-generated API key at `~/.archon-search/.search.env` with `0o600`; `ARCHON_SEARCH_API_KEY` env override; `ARCHON_SEARCH_KEY_FILE` relocation. (`key_manager.py:14-20, 57, 131-140`)
- Telemetry factory methods do not accept `query` parameter (cross-confirmed by `CLAUDE.md` invariant).
- `routes_status.py` filters per-collection block by `request.state.namespace`. (`routes_status.py:34, 59`)

## Unverifiable / ambiguous

- "No rotation policy in v1 — see `Architecture/140_error_handling_strategy.md` for the accepted-risk register." — The instruction forbids cross-trusting other Documentation/ files. The *fact* that the macOS log path has no rotation is verifiable (no `logrotate` integration in the codebase), so the substantive claim is fine; the referent doc was not opened.
- "GPU detection runs once during `archon-search install`." — In the *active* install path (`cli/install_cmd.py`), GPU detection does **not** run at all (cross-ref inaccuracy #9). In the legacy `install.py::SearchInstaller.run`, it does run once. Whether the doc's claim is right depends on which install path is canonical; per the wiring in `cli/main.py`, the active answer is "no, not at install time." Marking ambiguous because the doc may be describing intended (vs. actual) behavior.
- "Today's file is never deleted regardless of retention." — Verified, but the doc also says "To force a prune, restart the service." Restarting kicks off `_run` which calls `prune_once` immediately, then sleeps 86400s — so the claim is operationally correct; calling it out as fully verified but slightly under-specified.
- The line `proc[archon-search process] --> LOG[~/.archon-search/logs/archon-search.log]` in the observability mermaid implies the process writes directly to that file. On macOS that's via `StandardOutPath`; on Linux it's journald (not that file). The doc later clarifies the Linux case separately, but the diagram is platform-agnostic and could mislead a Linux reader. Marking ambiguous rather than wrong.
