# Review: MigrationGuide/02_upgrade_procedure.md

## Summary

Document is largely accurate. Most commands, file paths, env vars, and the search response schema verify against source. The single notable factual error is the claim that `config.load_config` "ignores unknown keys silently" — the loader does iterate only known keys (so unknown top-level keys are effectively ignored), but the doc's parenthetical citation that this is documented behavior of `load_config` is at best implicit; the only explicit "silent coerce" actually present in `config.py` is for `telemetry.export_enabled = true`, not for unknown keys generally. A few minor caveats around CLI behavior and rollback flow noted below.

## Inaccuracies (numbered)

1. **Line 76 — "The config loader ignores unknown keys silently (see `archon_search/config.py` `load_config`)"**: Partly misleading. `load_config` (config.py:114-…) walks an explicit allowlist of known keys per section; unknown keys are skipped because they are never read, not because of an explicit "ignore-unknown" branch. The only **explicit** silent coercion in the loader is `telemetry.export_enabled = true` → silently set to `false` with a warning (config.py:209-217). The CLAUDE.md project notes describe this same export_enabled coercion — not a generic unknown-key policy. The doc conflates the two.

2. **Line 71 — `pip install archon-search==26.5.123`**: The pinned version string is illustrative but the format is correct per `pyproject.toml` (`hatch-vcs` produces `YY.M.<rev-count>` tags, e.g. `26.5.<count>`). Not an inaccuracy in form, but flag as illustrative (no such version exists yet — `release.sh` line 70-72 confirms the formula).

3. **Line 110-124 — Rollback "Step 2 Restore state"**: The numbered comment "2. Restore state ONLY if…" sits before "3. Pin to the previous version" but does not include a "Stop the server" being re-issued nor a backup-of-current-state step. The forward procedure mandates backup before any change (principle 2); the rollback skips re-backing-up the now-failed-upgrade state before overwriting it with the tar. Not factually wrong about commands, but inconsistent with principle 2 of the same document.

4. **Line 131 — `archon-search uninstall && archon-search install`**: Verified that both `install` and `uninstall` subcommands exist (cli/main.py:8,29-30; cli/install_cmd.py:126 defines `uninstall`). Accurate.

## Verified claims

- **Line 23 `archon-search --version`**: Verified. `cli/main.py:21` registers `@click.version_option(_VERSION, prog_name="archon-search")`, so `--version` works.
- **Line 24 `pip index versions archon-search`**: Generic pip command; package name `archon-search` matches `pyproject.toml:2`.
- **Line 34 backup tar command**: Path `~/.archon-search` confirmed as state dir (CLAUDE.md, key_manager.py:18).
- **Line 46 `archon-search stop`**: Verified. `cli/main.py:27` registers `stop` subcommand.
- **Line 51 default port `127.0.0.1:8765`**: Verified. `config.py:30-31`: `host: str = "127.0.0.1"`, `port: int = 8765`.
- **Line 54 `archon-search status`**: Verified. `cli/main.py:28`; `cli/status.py` is a client-side probe via `_get_service().status()`. The "purely a client-side probe" wording matches the implementation.
- **Line 65-68 pip / uv install commands**: Standard pip/uv syntax; package name correct.
- **Line 82 `archon-search start`**: Verified. `cli/main.py:26`.
- **Line 89 `GET /health` unauthenticated, returns version**: Verified. `routes_health.py:18-20` returns `HealthResponse(status="running", version=_VERSION)`. CLAUDE.md confirms `/health` exempt from auth.
- **Line 91-96 smoke search**: 
  - Env file path `~/.archon-search/.search.env` — verified `key_manager.py:18`.
  - Env var name `ARCHON_SEARCH_API_KEY` — verified `key_manager.py:20`.
  - `Bearer` token auth — confirmed by CLAUDE.md (middleware_auth.py).
  - `POST /search` request body `{"collection": "...", "query": "..."}` — verified `routes_search.py:17-20` (SearchRequest has `collection: str` and `query`).
- **Line 102 response shape `{"results": [...], "acl_filtered": <bool>}`**: Verified. `routes_search.py:58-59`: `results: list[SearchResultSchema]`, `acl_filtered: bool`; line 84 returns `SearchResponse(results=[], acl_filtered=False)` on empty.
- **Reference to `archon_search/platform/`**: Verified the dir exists per CLAUDE.md project layout.

## Unverifiable / ambiguous

- **Line 49 "`stop` is still the correct command — it routes through the platform service manager"**: Plausible given `_get_service()` abstraction in `cli/_helpers.py`, but the actual routing logic for `stop` was not opened in this review. Likely correct.
- **Line 130 "roadmap item D3"**: Referenced doc `Backlog/03_world_class_roadmap.md` not inspected; cannot confirm the D3 ID. The claim "no automated migrations today" is consistent with the absence of any migration module in the source tree.
- **Line 27 reference to `[next release]` entries in `BREAKING.md`**: Not inspected in this review.
- **Line 76 reference to `archon-search.toml.example` "at the repo root"**: File is at the repo root per git status (`M archon-search.toml.example`). Verified the file exists; its current content vs. live config defaults was not diff-checked here.
