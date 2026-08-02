## Bug: `wizard --config <non-default>` starts a service that reads the DEFAULT config, then reports success

**ID**: S206-wizard_config_flag_ignored_by_the_service_it_starts
**Scenario**: S206
**Severity**: high
**Version**: archon-search, version 26.8.1751

### What happened
The wizard writes the requested config to the `--config` path correctly, but step [5/5] registers/starts the launchd service from a plist that hardcodes `ARCHON_SEARCH_CONFIG=/Users/manczg/.archon-search/archon-search.toml`. The running server therefore serves the DEFAULT config while the wizard prints `Config: /tmp/archon-cfgbug-HtuE/archon-search.toml` and exits 0.

Proof the running service is NOT using the requested config: the wizard generated a fresh key into the isolated key file, yet that key is rejected by the server it claims to have started, while the pre-existing default-config key is accepted:
  shared-key   GET /jobs -> 200
  isolated-key GET /jobs -> 401
The plist PID changed (20381 -> 20798), so the service really was restarted by this run — it just came back on the default config.

The same defect surfaces as a hard failure instead of a false success when the requested config uses a non-default port: the wizard then polls its own port, nothing ever answers there, and step [5/5] times out at 60s with exit 1 even though the config on disk is perfect.

The config file is written correctly in both cases — only the service-start step ignores `--config`. The flag is half-implemented, not broken.

### What should happen
The docs document `--config PATH` for `wizard` with no caveat (`docs/UserManual/10_installation.md:142`, `docs/UserManual/20_wizard.md:455`). A wizard run that exits 0 and prints `archon-search is running on <url>` must have started a service that reads the config it just wrote — or it must fail loudly rather than report success for a server running someone elses configuration.

### Note on the scenario ID (audit, 2026-08-01)

**S206 is not a `--config` scenario.** `scenarios/s206_top_k_sets_return_and_retrieve.md:1`
is "`--top-k` sets `top_k_return` and derives `top_k_retrieve`", and its test
(`tests/test_s206_top_k_sets_return_and_retrieve.py`) passes. This defect was found while
exercising that scenario's wizard fixture, and the harness stamps the scenario it was running.
No scenario currently covers `wizard --config`, which is why the finding has no home of its own.
Do not close this by pointing at a green S206 — the two are unrelated. A maintainer acting on
this should read the reproduction below, not the S206 spec.

Second accuracy note: `10_installation.md:142` describes `--config PATH` as "Use a non-default
config file when computing data/log paths" — narrower than `20_wizard.md:455`'s unqualified
"Use a non-default config file path". Neither line carries a caveat that the *service* started
by the wizard ignores the flag, which is the defect; but the "no caveat" wording above is a
better fit for :455 than for :142.

### Steps to reproduce
1. Have an existing default install (plist registered, server healthy on 8765).
2. `T=$(mktemp -d); mkdir -p $T/data`
3. `ARCHON_SEARCH_CONFIG=$T/archon-search.toml ARCHON_SEARCH_DATA_DIR=$T/data ARCHON_SEARCH_KEY_FILE=$T/data/.search.env archon-search wizard --config $T/archon-search.toml --db-path $T/data/search --profile minimal --non-interactive --skip-preload`
4. `grep -A1 ARCHON_SEARCH_CONFIG ~/Library/LaunchAgents/com.archon.search.plist`
5. `curl -s -o /dev/null -w "%{http_code}\
" -H "Authorization: Bearer $(grep -o "[0-9a-f]\\{64\\}" $T/data/.search.env)" http://127.0.0.1:8765/jobs`

### Evidence
```
Wizard stdout (exit 0):
  [5/5] Starting search service...
  Waiting for search service............. ready.
  archon-search is running on http://127.0.0.1:8765
  Config:  /tmp/archon-cfgbug-HtuE/archon-search.toml
    API key: d622f39...72d472bc  (generated fresh ...; also stored at: /tmp/archon-cfgbug-HtuE/data/.search.env)
  archon-search installed and running. Profile: Minimal - English.

Plist after the run (~/Library/LaunchAgents/com.archon.search.plist):
  <key>ARCHON_SEARCH_CONFIG</key>
  <string>/Users/manczg/.archon-search/archon-search.toml</string>

Auth probe against the server the wizard says it started:
  shared key ...955be52b   isolated key ...72d472bc
  shared-key   /jobs -> 200
  isolated-key /jobs -> 401

launchctl before: 20381   after: 20798   (service was restarted, on the default config)

Doc references:
  docs/OperatorGuide/10_deployment_topologies.md:140 - the plist bakes the default ARCHON_SEARCH_CONFIG
  docs/UserManual/10_installation.md:142 - documents `--config PATH`, no caveat
  docs/UserManual/20_wizard.md:455 - documents `--config PATH`, no caveat
```
