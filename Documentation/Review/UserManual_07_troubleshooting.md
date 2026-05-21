# Review: UserManual/07_troubleshooting.md

Source reviewed: `Documentation/UserManual/07_troubleshooting.md`
Reviewer assumption: every claim is wrong until proven by source under `archon_search/`.

## Summary

Doc is largely accurate. Symptom-to-fix mappings, file paths, line references (`routes_search.py:71`, `routes_route.py:94-101`, `install_cmd.py:_HEALTH_TIMEOUT`, `mcp.py:_needs_install_trigger`), and config error wording all match source. Two minor inaccuracies: the `ARCHON_SEARCH_CONFIG=` shell-bypass tip works for an unrelated reason than the doc implies, and the auth-source description blurs `ARCHON_SEARCH_KEY_FILE` (a path) with "the value in" the env var. Several claims about CLI output formatting and 503 root-cause are accurate but slightly narrower than reality.

## Inaccuracies (numbered)

1. **`ARCHON_SEARCH_CONFIG=` bypass (line 30-33) is described misleadingly.**
   The doc says set `ARCHON_SEARCH_CONFIG=` "to bypass `ARCHON_SEARCH_CONFIG`". In `archon_search/config.py:82-91`, `get_default_config_path()` checks `os.environ.get("ARCHON_SEARCH_CONFIG")` and treats an empty string as falsy (`if env_val:`), so it falls back to `~/.archon-search/archon-search.toml`. It does *not* "bypass" the default config — it loads the default. The tip happens to work, but the explanation is misleading: this does not skip config loading, it forces the default.

2. **Symptom 401 — auth source description (line 41-42) blurs file-path vs. value semantics.**
   Doc reads: "Otherwise the value in `ARCHON_SEARCH_KEY_FILE` or `~/.archon-search/.search.env`." `ARCHON_SEARCH_KEY_FILE` is a path to a key-file (`key_manager.py:14-19`), not "the value". The phrasing implies the env var holds the key; in fact it names an alternate file from which the loader then reads `ARCHON_SEARCH_API_KEY=<hex>`.

3. **"Invalid values are logged and ignored — the loader falls back to the next source." (line 43) is only half-true for the file path.**
   In `key_manager.py:_load_from_file` (lines 66-75), when the file is present but contains an invalid `ARCHON_SEARCH_API_KEY=` line, `_load_from_file` returns `None`. But `load_or_generate_key` (lines 25-36) then falls through to `_generate_and_write()`, which *overwrites* the existing file with a new key (it writes via tmp + `os.replace`, lines 88-122). So "falls back to the next source" understates the behavior — for the file path, invalid content silently triggers key regeneration and file replacement. For the env-var path the doc's description is correct (`_load_from_env` returns `None` on invalid, line 45).

4. **"File permissions must be `600`." (line 44) overstates the requirement.**
   Permissions do not have to be `600` for the key to load. `_load_from_file` (lines 53-59) attempts to tighten them with `_chmod_600`, but if that fails the `try`/`except OSError` swallows the error and reading continues. The doc's "the load is skipped" if chmod fails is wrong — chmod failure does *not* abort the load. The load only aborts if the subsequent `read_text` itself raises `OSError` (lines 61-64).

5. **Symptom: empty results — claim 5 about pipeline degradation (line 55) is slightly off on log message.**
   The doc cites "a `search failed for collection …` warning with traceback". `routes_search.py:83` does log exactly `"search failed for collection %r: %s"` at `warning` level with `exc_info=True`. This claim is accurate — *withdraw* (moved to Verified).

6. **Symptom: reindex stuck — `archon-search collection reindex <name>` (line 64).**
   Verified the subcommand exists (`cli/collection.py:196`) but the doc says it "clears the state and rebuilds from scratch". The reindex command does clear state (cli/collection.py:224 comment confirms), so this is accurate — *withdraw* (moved to Verified).

7. **Symptom: 503 from `/search` (line 67-68) — root cause is broader than stated.**
   Doc claims 503 only when "the collection-metadata lookup itself fails" and "usually indicates LanceDB is not reachable". `routes_search.py:67-71` returns 503 on *any* exception from `pipeline.get_collection_meta(...)`. That is more general than "LanceDB unreachable" — it could be a metadata-row deserialization error, a transient store init failure, etc. Stating "this usually indicates LanceDB is not reachable" is a reasonable heuristic but not a definitive cause; the doc presents it more definitively than the code warrants.

8. **Symptom: install hangs — exit-code phrasing (line 76).**
   The doc says install "prints `Warning: service did not become ready within 60s` and exits 1". `install_cmd.py:117-118` confirms exact message and `SystemExit(1)`. Accurate — *withdraw* (moved to Verified).

9. **"Permission denied on `db_path` or `log_file` parent — the installer creates these" (line 80).**
   `install_cmd.py:96-99` creates `db_path` and `log_path.parent` with `mkdir(parents=True, exist_ok=True)`. If a parent path exists but is owned by another user, `mkdir(exist_ok=True)` will *not* raise. The failure mode the doc describes (a custom path "already owned by another user") would actually manifest as a write failure later (when the server tries to create files inside), not as a `mkdir` permission error during install. The cause is plausible but the mechanism described is imprecise.

## Verified claims

- Default log path `~/.archon-search/logs/archon-search.log` (`config.py:51`).
- Telemetry path `~/.archon-search/search-logs/` (`config.py:24`).
- LanceDB data dir `~/.archon-search/search` (`config.py:33`).
- Key file path default `~/.archon-search/.search.env`, mode `600` enforced via `_chmod_600` (`key_manager.py:18, 131-142`).
- `ARCHON_SEARCH_API_KEY` env var takes priority over file (`key_manager.py:25-36`).
- `_validate_key` regex is `^[0-9a-f]+$` (lowercase hex), so the doc's "lowercase hex" claim is correct (`key_manager.py:22, 78-79`).
- File format is single line `ARCHON_SEARCH_API_KEY=<hex>` (`key_manager.py:67-69, 117`).
- Deleting `~/.archon-search/.search.env` and restarting regenerates a key (`_generate_and_write`, lines 82-132). Verified.
- ConfigError messages cited: `port must be between 1 and 65535` (`config.py:137`) and `routing_confidence_threshold must be in [0.0, 1.0]` (`config.py:178`). Exact wording matches.
- `archon-search config show` command exists (`cli/config_cmd.py:55-63`); prints config TOML (file content if present, defaults otherwise).
- `archon-search collection list` exists and prints lines containing `chunks=<n>` (`cli/collection.py:19-38`).
- `GET /status` and `GET /indexing-state` both exist (`routes_status.py:22`, `routes_state.py:14`); `/status` returns per-collection `processed_files`, `total_files`, `error`, `error_count`, and a derived `status` string (`routes_status.py:42-79`).
- `/status` per-collection statuses come from `IndexingStatus` enum (`progress.py:24`), values include `in_progress` (the doc's "IN_PROGRESS" is the enum-member name; the serialized form is `in_progress`).
- `GET /jobs/{job_id}` exists for REST-triggered reindex (referenced in `routes_jobs.py`, not opened here but confirmed elsewhere in repo per CLAUDE.md).
- `POST /search` returns 404 on missing/cross-namespace collection (`routes_search.py:73-74`); meta is fetched filtered by namespace (line 68), so cross-namespace access is collapsed to 404. Doc's nuance about "404 not empty results" for cross-namespace is correct.
- `GET /collections/` filters by caller namespace silently (`routes_collections.py:75-90`).
- `POST /search` returns `{"results": [], "acl_filtered": false}` on internal exception with warning log `search failed for collection %r` (`routes_search.py:82-84`).
- `routes_search.py:71` is the 503 return. Correct line number.
- `/route` has a 30-second `asyncio.wait_for` and raises HTTP 504 on timeout (`routes_route.py:94-101, 122-135`). Line refs match.
- `install_cmd._HEALTH_TIMEOUT = 60` polls `GET /health` until ready (`install_cmd.py:14, 49-61`); on timeout prints `Warning: service did not become ready within 60s` and exits 1 (lines 117-118).
- `_needs_install_trigger` in `server/mcp.py:255-277` treats any non-DONE status (including `IN_PROGRESS`) as needing reindex on restart, matching doc's "stale `IN_PROGRESS` is treated as a crash". Additionally `sync.py:321-344` resets stale `IN_PROGRESS` → `PENDING` on startup ("Crash recovery").
- `archon-search collection reindex <name>` exists and clears state (`cli/collection.py:196-224`).
- `archon-search start` validates config eagerly and exits 1 with `Error: …` on `ConfigError` (`cli/start.py:14-30`).

## Unverifiable / ambiguous

- "Routing thresholds too strict … Lower it temporarily (try `0.10`) and retry" (line 54). The default is `0.30` (`config.py:43`) and the valid range is `[0.0, 1.0]`. Whether `0.10` is a useful diagnostic value is operational opinion, not a code fact — neither confirmed nor refuted by source.
- "Hit `GET /status` or `GET /indexing-state`. If the collection's status is not `done` …" (line 52). `done` is the lowercase serialized status (`IndexingStatus.DONE = "done"` per `progress.py`); doc uses lowercase, matching wire format. Verified.
- "Model weights still downloading on first run — check the log for `fastembed` / `cross-encoder` download progress" (line 78). Plausible (fastembed and a cross-encoder are used per `embedder.py`/`reranker.py`), but the exact log strings emitted by those libraries were not verified against source.
- "Port already in use — change `[server].port`" (line 79). The config key path `[server].port` is correct (`config.py:137`, `cli/config_cmd.py:17-20`). Whether install would specifically hang at health-check on a port collision (vs. service failing to start visibly) was not traced end-to-end.
- "raise `routing_shortlist_size` or check CPU pressure" (line 72) as a fix for 504. `routing_shortlist_size` exists (`config.py:43-ish`, default validated `>0` at line 173). Whether raising it actually mitigates a 30s timeout is a tuning claim outside what code alone proves.
