## Bug: Wizard reports readiness failure for an install that succeeded; first service launch crashes on a missing dependency

**ID**: S02-wizard_reports_timeout_for_a_successful_install
**Scenario**: S02
**Severity**: medium
**Version**: archon-search, version 26.8.1751

### What happened
`archon-search wizard --profile minimal --non-interactive --skip-preload` on a clean machine printed:

    Waiting for search service............................................................ timed out.
    Warning: Search service did not become ready within 60 seconds.

The install had in fact SUCCEEDED. No repair command was run afterwards — no `archon-search install`, no `archon-search start` — and the service was healthy ~20 s later and has stayed healthy since: `/health` 200, `archon-search status` "running (PID 29427)", `~/Library/LaunchAgents/com.archon.search.plist` present. Only the readiness gate reported failure.

The server log explains why, and the reason is not that startup is slow. The very first lines of a log file created fresh by this install are a crash:

    Traceback (most recent call last):
      File "<frozen runpy>", line 203, in _run_module_as_main
      ...
      File ".../site-packages/fastapi/__init__.py", line 5, in <module>
        from starlette import status as status
    ModuleNotFoundError: No module named 'starlette'

The service was then restarted by launchd, and that attempt succeeded: PID 29427 started 13:20:23 and logged "Application startup complete" ~15 s later, at 13:20:38. So a healthy server needs roughly 15 s, well inside the 60 s budget — the budget was consumed by the crashed first launch plus the supervisor's restart, not by slow startup.

INFERENCE, flagged as such: that the crash is what exhausted the poll is the most likely reading of the timeline, but I could not confirm it by re-running. Re-running the wizard triggers the Step 0 global-plist deletion filed as bugs/S199-profile_switch_rejected_and_no_mutation.md, and other agents were using the machine.

The missing dependency appears to be a one-shot install race, not a packaging gap: there is exactly one `ModuleNotFoundError` in the log, and `starlette` (1.3.1, alongside `fastapi` 0.141.1) is present in the tool environment now. The service was evidently launched before the freshly-installed environment was fully materialised.

### What should happen
Two defects, either of which alone would prevent the bad operator experience.

1. The wizard should not launch the service before its Python environment is complete. A first launch that dies on `ModuleNotFoundError: No module named 'starlette'` — a transitive dependency of `fastapi`, which the server imports at module load — indicates the service was started against a half-written environment.

2. The readiness gate should not report failure for an install that is converging. `UserManual/20_wizard.md:429` — "It then polls \`GET /health\` for up to 60 seconds. If the service does not become ready within that window, the wizard exits with an error." The 60 s budget does not survive one crash-and-restart cycle. The wizard should either tolerate a supervisor restart within the window, or surface the crash it can see in the log rather than a bare timeout.

OPERATIONAL INTERACTION — the reason this is medium and not low. Following the documentation to the letter, from this warning, breaks a working install. The chain, step by step:

1. The wizard prints `Warning: Search service did not become ready within 60 seconds.` for an install that in fact succeeded.
2. `20_wizard.md:836-846` handles exactly this warning. It first says to read the service log, then lists three common causes. Two are excluded by the evidence here — the log contains zero "address already in use"/port-conflict lines and the service did bind 8765 successfully on its second launch, and `df -h ~/.archon-search` reports 344 GiB available at 62% capacity (measured after the fact, but far from any plausible exhaustion threshold). The third, "Model weights failed to download or are corrupt", carries the remedy **"Re-run the wizard without \`--skip-preload\`"** — and it is the only one of the three that fits a `--skip-preload` install, so it is where the manual lands the operator.
3. The log the manual just told them to read does not name any of the three causes. It shows `ModuleNotFoundError: No module named 'starlette'` — an environment defect the troubleshooting section does not cover at all, leaving the operator with no documented match and only the wizard re-run as a plausible next step.
4. Re-running the wizard triggers the Step 0 defect filed as **bugs/S199-profile_switch_rejected_and_no_mutation.md**, which unconditionally deletes the global launchd plist.
5. Net effect: a service that had already recovered on its own and was serving `/health` 200 is torn down by the documented remedy for a failure that never happened.

Each defect is survivable alone. Together — a false failure report, a troubleshooting entry with no matching cause, and a prescribed remedy that is itself destructive — they convert a self-correcting install into a broken one. Fixing either end of the chain (the spurious warning, or S199's unconditional plist deletion) breaks it.

### Steps to reproduce
1. Ensure a clean machine: `archon-search uninstall --delete-db`, `uv tool uninstall archon-search`, `trash ~/.archon-search/`
2. `uv tool install archon-search`
3. `archon-search wizard --profile minimal --non-interactive --skip-preload`
4. Observe the timeout warning, then poll: `for i in $(seq 1 6); do sleep 20; curl -s -o /dev/null -w "%{http_code}
" http://127.0.0.1:8765/health; done`
5. `head -20 ~/.archon-search/logs/archon-search.log`
6. `archon-search status` and `ls ~/Library/LaunchAgents/ | grep archon.search` — both show a healthy, registered service with no repair step run

### Evidence
```
Wizard tail (step 3):
    [5/5] Starting search service...
    Waiting for search service............................................................ timed out.
    Warning: Search service did not become ready within 60 seconds.

Health after the wizard gave up (step 4) — 200 on the first poll, ~20 s later:
    health=200

First 16 lines of a log file created fresh by this install (step 5):
    Traceback (most recent call last):
      File "<frozen runpy>", line 203, in _run_module_as_main
      File "<frozen runpy>", line 88, in _run_code
      File ".../archon_search/server/__main__.py", line 3, in <module>
      File ".../archon_search/server/app.py", line 17, in <module>
      File ".../fastapi/__init__.py", line 5, in <module>
        from starlette import status as status
    ModuleNotFoundError: No module named 'starlette'
    2026-08-01T11:20:25Z INFO archon_search.server.app API key authentication enabled (source: auto-generated)
    INFO:     Started server process [29427]
    INFO:     Waiting for application startup.
    2026-08-01T11:20:37Z INFO archon_search.server.app MCP HTTP endpoint mounted at /mcp
    INFO:     Application startup complete.
    INFO:     Uvicorn running on http://127.0.0.1:8765 (Press CTRL+C to quit)

Timing of the healthy process — 15 s from exec to ready, well inside the 60 s budget:
    $ ps -o pid,lstart -p 29427
    29427 Szo aug.  1 13:20:23 2026
    "Application startup complete" logged at 13:20:38

Crash is one-shot, and the dependency is present now — an install race, not a packaging gap:
    $ grep -c ModuleNotFoundError ~/.archon-search/logs/archon-search.log
    1
    $ .../archon-search/bin/python -c "import starlette, fastapi; ..."
    starlette 1.3.1
    fastapi 0.141.1

Install is healthy with no repair command run:
    $ archon-search status
    running (PID 29427)
    $ ls ~/Library/LaunchAgents/ | grep archon.search
    com.archon.search.plist
```
