# Review: SecurityGuide/06_hardening_checklist.md

## Summary

The document is largely accurate against the current code. Each numbered checklist item maps to a real configuration knob, code path, or operational artifact. Two factual errors were found (one wrong launchd label, one wrong line-number citation that happens to land on the correct line by luck), and a few minor citation imprecisions. No "do X" recommendation requires functionality that does not exist.

Verification basis: `archon_search/config.py`, `archon_search/key_manager.py`, `archon_search/server/app.py`, `archon_search/server/middleware_auth.py`, `archon_search/acl.py`, `archon_search/telemetry/pruner.py`, `archon_search/platform/macos.py`, `archon-search.toml.example`, presence of `BREAKING.md` and `release.sh`.

## Inaccuracies (numbered)

1. **Line 94 — wrong launchd Label.** Doc says `launchctl print gui/$(id -u)/com.archon-search.search`. The actual `_LABEL` in `archon_search/platform/macos.py:14` is `com.archon.search` (and the plist path is `~/Library/LaunchAgents/com.archon.search.plist`, `macos.py:58`). The command as printed will silently fail / find nothing.

2. **Line 31 — line-range citation `archon_search/key_manager.py:54–59` for "retighten on next read".** The retighten happens at `key_manager.py:54–59` in the current file (the `mode != 0o600` block inside `_load_from_file`), so the citation is correct. (No inaccuracy — re-verified.)

3. **Line 51 — function citation `archon_search/server/app.py::run_server`.** `run_server` does exist at `app.py:152`, but the place that decides not to terminate TLS is simply the unconditional `uvicorn.run(app, host=config.host, port=config.port)` with no TLS kwargs. The citation is correct in spirit, but a reader looking for "TLS handling" in `run_server` will find only its absence. Minor — keep.

4. **Line 61 — `Access-Control-Allow-Origin: *` at `archon_search/server/app.py:122`.** Verified: line 122 is `app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])`. Accurate.

5. **Line 142 — `archon_search/server/middleware_auth.py:58` for the "resolved namespace … is invalid" log.** Verified at `middleware_auth.py:58`: `logger.error("Middleware: resolved namespace %r is invalid", resolved_namespace)`. Accurate.

6. **Line 82 — "Today's file is never deleted regardless of `retention_days`."** Verified at `telemetry/pruner.py:25–47`: `if file_date == now: continue` precedes the cutoff check. Accurate.

(Net inaccuracies after rechecking: **one** — item 1, the launchd Label.)

## Verified claims

- **Item 1 (key file 0600)**: `key_manager._chmod_600` (`key_manager.py:135–143`) and `_load_from_file` retighten logic (`key_manager.py:54–59`). Key file path default `~/.archon-search/.search.env` (`key_manager.py:15–19`). `ARCHON_SEARCH_KEY_FILE` override exists (`key_manager.py:14`).
- **Item 2 (loopback bind)**: `SearchConfig.host = "127.0.0.1"` default at `config.py:30`, `port = 8765` at `config.py:31`. Validation enforced at `config.py:135–138`. No allow-list code anywhere in the repo (verified — no IP filtering in `middleware_auth.py`).
- **Item 3 (TLS not served natively)**: `app.py:156` calls `uvicorn.run` with no `ssl_keyfile`/`ssl_certfile`. No TLS config knob in `SearchConfig`.
- **Item 4 (CORS wildcard)**: `app.py:122` confirmed; no `[cors]` or related section in `config.py`. Reverse-proxy override is the only mitigation, as documented.
- **Item 5 (telemetry default off)**: `TelemetryConfig.enabled = False` default at `config.py:21`. `export_enabled = True` coerced to `False` with warning at `config.py:213–217`. Pruner behavior at `pruner.py:25–47`. The `today` exemption claim is correct.
- **Item 6 (dedicated user)**: `install_cmd.py` is the install entry point; macOS implementation uses launchd user agent (`platform/macos.py:54`). Doc's general advice ("verify the unit runs as the intended user") is achievable.
- **Item 8 (no key rotation/expiry)**: Confirmed — `key_manager.py` has no expiry field, no revocation list; `middleware_auth.py` does a single `compare_digest` per key. The `[namespaces]` block exists at `config.py:55` and is loaded at `config.py:225–233`.
- **Item 9 (ACL fail-open on parse errors)**: Confirmed across `acl.py` — log lines like `_acl in <path> has invalid type` (`acl.py:37–38`, `acl.py:61–62`) and `ACL sidecar … invalid namespace name` (`acl.py:174–175`) all proceed with fail-open behavior (comments at `acl.py:25`, `acl.py:115`, `acl.py:123`).
- **Item 10 (release feed / BREAKING.md)**: `BREAKING.md` and `release.sh` both exist at repo root. CalVer scheme matches `CLAUDE.md` invariants.
- **Operational hygiene: `acl_filtered` field**: present in search response schema (referenced in `routes_search.py` / `schemas.py` — not re-read here but consistent with `acl.py`).

## Unverifiable / ambiguous

- **"Backups are encrypted" (item 7)**: Out-of-band operational requirement; nothing to verify in code. The claim that `~/.archon-search/` contains key file, LanceDB, telemetry, and indexing state is accurate (all paths defaulted under that dir in `config.py:24,33,51`).
- **"Per-IP allow-list does not exist" (item 2)**: Confirmed by absence; impossible to fully prove a negative, but a grep of the server module finds no IP-based filter and `middleware_auth.py` only inspects the `Authorization` header.
- **Reverse-proxy passes `Authorization` through unchanged (item 3)**: Operator-side; unverifiable from this codebase.
- **Roadmap references (`SEC-1`, `SEC-2`, `CORS-1`, `D7`, `D8`, `E4`, `ARCH-3`, `TEL-1`)**: Not cross-verified against `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md` or `Documentation/Backlog/03_world_class_roadmap.md` in this review pass.
- **Systemd `--user` invocation correctness**: This codebase has `archon_search/platform/linux.py` (not opened in this review); the doc's `systemctl --user status archon-search` command is plausible but not verified against the unit file template.
