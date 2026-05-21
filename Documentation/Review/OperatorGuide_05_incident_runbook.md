# Review: OperatorGuide/05_incident_runbook.md

## Summary

Most claims are accurate against the current source. Two operationally relevant items are wrong: (a) the pruner DOES run at startup (not "only every 24h from process start"), so the "restart to force prune" advice is misleading; (b) the example TOML was already updated and no longer mentions "raises ConfigError", contradicting the runbook's note that the example is stale. A few minor citation/precision issues are listed below. No fabricated endpoints, routes, paths, env vars, or CLI commands were found.

## Inaccuracies (numbered)

1. **Line 121 — pruner first-run timing is wrong.**
   The runbook says: *"The pruner runs every 24h from process start … If the service has been up for less than 24h, prune has not run yet — restart to force it."*
   Source `archon_search/telemetry/pruner.py:63-70` (`_run`) runs `prune_once()` immediately when the task starts and only then `await asyncio.sleep(86400)`. So a fresh service has *already* pruned once. Restarting does not "force" an extra prune that would otherwise be delayed; it only triggers another one after the current process state is lost. The advice happens to work (restart causes a fresh prune) but the stated mechanism is incorrect.

2. **Line 131 — claim about the example TOML is stale.**
   The runbook says: *"The example TOML still says 'raises ConfigError'; the code does not."*
   `archon-search.toml.example` lines 67-71 already describe the actual behavior: *"the config loader logs a warning and silently coerces this to false. No external transmission occurs. Tracked as TEL-1…"*. The string "raises ConfigError" does not appear in the example. This sentence in the runbook should be deleted (the alignment it asserts is broken is in fact present).

3. **Line 99 — citation `routes_search.py:82-84` is approximately correct but the response is slightly mischaracterized.**
   The runbook says `POST /search` returns `200 OK` with `results: []` and `acl_filtered: false` on pipeline error — this matches `routes_search.py:82-84` (`return SearchResponse(results=[], acl_filtered=False)`). Accurate. However the sibling claim on line 86 (*"503 from `/search` with log lines mentioning LanceDB lock/IO"*) is only partially supported: `routes_search.py` only emits a 503 from the **meta lookup** branch (line 71, `"meta lookup failed"`), not from arbitrary LanceDB IO inside the pipeline path — those are swallowed into the 200/empty branch. So a LanceDB-lock incident may surface as either 503 (meta path) **or** silent empty results (pipeline path), not exclusively 503.

4. **Line 109 — `MultiCollectionRouter._cached_metadata` is process-local cache; the description "router staleness after a recent reindex" is plausible but unverifiable from the runbook wording.**
   `router.py:50, :69-70, :124` confirms a one-shot in-process cache with no invalidation API. The remedy (restart) is correct. Minor: the symbol is private (`_cached_metadata`); the runbook should note that there is no public bust mechanism, which matches the broader "CON-2" framing but is worth being explicit about.

5. **Line 122 — "today's file excluded" when deleting manually is operator-supplied advice, not a code guarantee.**
   The pruner (`pruner.py:44-45`) skips today's file. Manual `rm` of today's file is what the operator would do, so the caveat is correct guidance, but the runbook implies a system-level invariant. Re-phrase to "you must exclude today's file manually" to avoid implying the reader endpoints will protect it.

## Verified claims

- First-five-minutes commands:
  - `GET /health` — `routes_health.py:18-20`, no auth required (confirmed against `middleware_auth.py` allow-list in `server/app.py` setup).
  - `GET /status` — `routes_status.py:22`, requires Bearer token.
  - Log path `~/.archon-search/logs/archon-search.log` — default `config.py:51`, `platform/macos.py:73`.
  - `journalctl --user -u archon-search` — consistent with linux service unit naming used in `platform/linux.py` (verified by `_LABEL`/service naming convention used elsewhere; matches macOS label `com.archon.search` at `platform/macos.py:14`).

- Stuck job triage:
  - `GET /jobs/{job_id}` and `DELETE /jobs/{job_id}` — `routes_jobs.py:108-157`. DELETE transitions ACTIVE → `CANCELLING` and returns 202; terminal jobs return 200 idempotently. Matches runbook's "transitions it to CANCELLING. On the next tick the job marks itself CANCELLED" — `_default_ingest_task` (`routes_jobs.py:64-79`) checks for `CANCELLING` after pipeline returns and writes `CANCELLED`. Cited line range `routes_jobs.py:119-157` matches.
  - `_EVICTION_DAYS = 7` — `jobs/store.py:17`. Confirmed.
  - `_CRASH_STATUSES = {RUNNING, CANCELLING}` recovered on load — `jobs/store.py:16, :96`. Restart-marks-as-failed claim is accurate.
  - Job-id surfacing from `POST /ingest`, `POST /collections`, `POST /collections/{name}/reindex` — confirmed (`routes_jobs.py:91`, `routes_collections.py:114`, `routes_collections.py:299`), all return `JobResponse` with `202`.
  - Jobstore filename `archon-search-jobs.json` — `jobs/model.py:8`. Confirmed.

- Watcher / config:
  - `[collections].watch = false` toggle — `config.py:44-47, :191-192`. Confirmed.
  - No event-rate limiter, no health flag exposed by `watcher.py` — confirmed by absence in `watcher.py` surface (no health endpoint reads from watcher in `routes_*`).
  - `archon-search sync` command — present in CLI (`cli/main.py`).

- Key rotation:
  - `.search.env` mode 0600 — `key_manager.py:89, :131, :135-143`. Confirmed.
  - `secrets.token_hex(32)` produces 64-char lowercase hex matching `_HEX_RE` (`key_manager.py:22`). The example shell snippet is valid.
  - `ARCHON_SEARCH_API_KEY` env precedence — `key_manager.py:25-36, :39-46`. Confirmed.
  - `ARCHON_SEARCH_KEY_FILE` redirect — `key_manager.py:14-19`. Confirmed.
  - Auto-generation on missing file — `key_manager.py:_generate_and_write` at `:82-132`. Confirmed.
  - "No live-reload — every rotation requires a restart" — true: key is loaded once in `server/app.py` startup (`logger.info("API key authentication enabled …")` at `app.py:123`).

- Search-empty-on-error semantics:
  - 200 + empty `results` + `acl_filtered: false` on pipeline exception — `routes_search.py:82-84`. Confirmed.
  - 503 on meta lookup failure — `routes_search.py:69-71`. Confirmed.
  - 404 on missing collection — `routes_search.py:73-74`. Confirmed (not mentioned by runbook but consistent).
  - Log strings `"search failed for collection"` and `"meta lookup failed"` — `routes_search.py:70, :83`. Exact match.
  - Telemetry filtering `GET /telemetry/entries?collection=&endpoint=search&status=error` — `routes_telemetry.py:41-78` accepts these query params via enums `EndpointKind`, `Status`. Confirmed (param values match: `endpoint=search`, `status=error` are valid enum values from `telemetry/entry.py`).

- Telemetry:
  - Default `retention_days = 30` — `config.py:22`. Confirmed.
  - Log directory `~/.archon-search/search-logs` — `config.py:24`. Confirmed.
  - `GET /telemetry/stats` returns `{"enabled": false}` when disabled — `routes_telemetry.py:29-30` returns `DisabledResponse`. Confirmed.
  - `skipped_lines` reflects malformed JSONL lines, logged at WARNING — `telemetry/reader.py:108-112`. Confirmed.
  - Pruner: today's file exempt, files older than `now - retention_days` deleted — `pruner.py:21-54`. Confirmed.

- `export_enabled = true` no-op:
  - Config loader logs warning and coerces to false — `config.py:209-217`. Confirmed exactly; cited line range matches.

- Service-will-not-start:
  - macOS plist `~/Library/LaunchAgents/com.archon.search.plist` — `platform/macos.py:58, :14`. Confirmed.
  - macOS log path `~/.archon-search/logs/archon-search.log` — `platform/macos.py:73`. Confirmed.
  - Default port 8765 — `config.py:31`. Confirmed (`lsof -i :8765` is a valid suggestion).

## Unverifiable / ambiguous

- **`archon-search-release.yml` / release-pipeline references** — not relevant to this runbook (the runbook does not cite release infra).
- **B2 / SEC-1 / TEL-1 / CON-2 / CON-5 / A4 / D7 debt IDs** — these are referenced as labels in `Architecture/530…` and `Architecture/roadmap.md`. The instruction "never trust Documentation/" means I cannot validate them from source alone. The runbook uses them as cross-references; they are not actionable assertions about code.
- **"`watcher.py` does not expose a health flag — gap tracked under B2"** — the "no health flag" half is verifiable (no such symbol in `watcher.py`); the "B2" label is a doc-only artifact.
- **`pgrep -af archon-search`** — generic system command, not codebase-derived; accuracy depends on the OS, not the project.
- **"As a last resort, restore `search/` from backup"** — references `OperatorGuide/03_…`; per the rule I do not validate that doc, but the path `~/.archon-search/search` matches `database.db_path` default in `archon-search.toml.example:22`, so the directory name `search/` is correct.
- **"stale lock file remains after an unclean shutdown, restart resolves it"** — LanceDB lock-file semantics are external to this repo. Not contradicted by source, but not verifiable from `archon_search/` either.
- **"the asyncio task itself is unresponsive" recovery via process restart** — backed by `_CRASH_STATUSES` recovery (`jobs/store.py:16, :96`); the operational claim that the task can be "unresponsive" in a way that DELETE doesn't address is plausible (FastAPI DELETE only flips the DB status; an unresponsive task won't honor it) but is inferential rather than directly proven.
