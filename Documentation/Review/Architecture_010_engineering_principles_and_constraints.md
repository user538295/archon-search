# Review: Architecture/010_engineering_principles_and_constraints.md

## Summary

The document is largely accurate. Every major structural claim (CalVer formula, pytest defaults, coverage gate, hatch-vcs configuration, telemetry no-raw-query invariant, auth middleware behavior, runtime state layout, contract sources) checks out against source. Two inaccuracies were found: the README is invoked as the source-of-truth for telemetry runtime guarantees (it is one source, but config.py is the authoritative one — minor wording issue), and the `archon_search/__init__.py` claim is verified-empty (good). The CalVer example "26.5.142" is illustrative — the date the doc was reviewed (2026-05-20) matches the `YY.M` segment. One small issue: the document phrases `raw-options.local_scheme = "no-local-version"` but the pyproject.toml also pins `raw-options.version_scheme = "no-guess-dev"` and `raw-options.fallback_version = "0.0.0+local"`, which the doc omits (not inaccurate, just incomplete).

## Inaccuracies

### 1. Minor — pinned tooling claim is unsupported

- **Quoted claim**: "`uv`-managed Python `>=3.12`, **pinned tooling**, deterministic eval backends." (line 17)
- **Ground truth**: `pyproject.toml` dev group uses lower-bound version specifiers (`pytest>=8.0`, `pytest-asyncio>=0.23`, `pytest-cov>=5.0`) and runtime deps are all lower-bound (`fastapi>=0.115`, etc.). No `uv.lock` is referenced as the pin. Calling these "pinned" is misleading; they are floor-pinned.
- **Evidence**: `pyproject.toml:6-19, 24-29`
- **Severity**: Low

### 2. Minor — "runtime guarantees are documented in the README" is partly misdirection

- **Quoted claim**: "The companion runtime guarantees are documented in the README:" followed by three bullets about telemetry. (lines 67–71)
- **Ground truth**: The structural guarantee for `export_enabled` coercion and the warning live in `archon_search/config.py:209-217`, not in the README. The README may mirror it, but pointing to the README as the documentation source contradicts the doc's own principle ("Single source of truth per dimension"). The CLAUDE.md itself attributes the coercion to `config.py`.
- **Evidence**: `archon_search/config.py:209-217`; project `CLAUDE.md` ("the config loader logs a warning and silently coerces it to `false` (see `config.py`)")
- **Severity**: Low (factually adjacent but misattributes the source)

### 3. Minor — REST middleware and MCP endpoint "share the same auth layer"

- **Quoted claim**: "The REST middleware and the MCP endpoint share the same auth layer (`server/middleware_auth.py`)." (line 77)
- **Ground truth**: `server/middleware_auth.py` defines `APIKeyMiddleware`, a Starlette HTTP middleware. Whether the MCP endpoint reuses *this exact class* vs. a separate handler that calls into the same key resolution code is not visible in this file alone. CLAUDE.md states "the MCP endpoint exposes the same control-plane tools over MCP using the shared auth middleware (`middleware_auth.py`)", so the claim is consistent with project guidance, but `mcp.py` was not inspected here to confirm the literal sharing. Treat as plausibly correct, but the doc's wording "share the same auth layer" is consistent.
- **Evidence**: `archon_search/server/middleware_auth.py:1-64`; cross-reference CLAUDE.md
- **Severity**: Informational — not an inaccuracy, flagged as unverified by direct read.

## Verified claims

1. **Python `>=3.12`** — `pyproject.toml:5` `requires-python = ">=3.12"`. ✓
2. **`uv sync --dev` and `uv run`** — standard uv workflows; consistent with CLAUDE.md. ✓
3. **Build backend `hatchling` + `hatch-vcs`** — `pyproject.toml:31-33`. ✓
4. **Distribution `archon-search` vs import `archon_search`** — `pyproject.toml:2` (`name = "archon-search"`) and `pyproject.toml:49` (`packages = ["archon_search"]`). ✓
5. **CalVer formula `YY.M.<commit-count>`** with `date -u +%y` / `+%-m` / `git rev-list --count HEAD` — verbatim in `release.sh:69-72`. ✓
6. **`dynamic = ["version"]` and `[tool.hatch.version].source = "vcs"`** — `pyproject.toml:3, 35-36`. ✓
7. **`archon_search/__init__.py` is empty** — verified: 0 bytes. ✓
8. **`raw-options.local_scheme = "no-local-version"`** — `pyproject.toml:42`, with comment confirming PyPI rejects `+local`. ✓
9. **Plain pushes to `main` do not publish; only tag push triggers `archon-search-release.yml`** — `.github/workflows/archon-search-release.yml:12-16` (`on: push: tags: - "*"` + `workflow_dispatch`). ✓
10. **`release.sh` flags `-y` and `--dry-run`** — `release.sh:28-35`. ✓
11. **Default pytest `addopts` value** — exact string match including `--cov=archon_search`, `--cov-report=term-missing`, `--cov-fail-under=85`, and the marker exclusion `not live and not eval and not benchmark and not integration` (`pyproject.toml:61`). ✓
12. **Four marker-gated suites** — `pyproject.toml:62-67` declares exactly `benchmark`, `integration`, `eval`, `live`. ✓
13. **Telemetry `entry.py` factories have no `query` parameter** — `from_search_tool_result`, `from_route_response`, `from_error` confirmed; signatures use only structural metadata (`telemetry/entry.py:84-145`). The `TelemetryEntry` model has no field that could hold a query string. ✓
14. **Telemetry opt-in (`enabled = false` default)** — `archon-search.toml.example:64`; `config.py` default. ✓
15. **`export_enabled = true` logs a warning and is coerced to `false`** — `config.py:209-217`. ✓
16. **Auth middleware `_EXEMPT_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}`** — `server/middleware_auth.py:16`. Exact match. ✓
17. **Key resolution priority `ARCHON_SEARCH_API_KEY` → `ARCHON_SEARCH_KEY_FILE` (or default `~/.archon-search/.search.env`) → auto-generated with mode `600`** — `key_manager.py:14-36, 82-132` (mode `0o600`). ✓
18. **`~/.archon-search/archon-search.toml` with override `ARCHON_SEARCH_CONFIG`** — `archon-search.toml.example:3-5`, `config.py:83`. ✓
19. **`~/.archon-search/search/` LanceDB default `db_path`** — `config.py:33` and `archon-search.toml.example:22`. ✓
20. **`~/.archon-search/logs/archon-search.log` log file default** — `config.py:51`. ✓
21. **`~/.archon-search/search-logs/` telemetry default** — `config.py:24`, `archon-search.toml.example:66`. ✓
22. **`tests/eval/thresholds.toml` + `tests/eval/baselines/baseline.{md,json}` exist** — directory listing confirms. ✓
23. **`tests/eval/README.md` exists** — confirmed. ✓
24. **`BREAKING.md` exists at repo root** — confirmed. ✓
25. **Schemas in `archon_search/server/schemas.py` and `schemas_telemetry.py`** — both files present in `server/`. ✓
26. **`pyproject.toml [tool.hatch.build.targets.wheel].packages = ["archon_search"]`** — `pyproject.toml:45-49`. ✓

## Unverifiable / ambiguous

1. **"Wheels are built from a tagged commit and contain a version string derived from that tag."** (line 23) — The release workflow does build from a tag and verifies wheel version against the tag (`archon-search-release.yml:108-120`), so this is true for the *release* path; locally one can `hatch build` from any commit and get a `post`-suffixed version. The wording is slightly idealized but not wrong in the release context.
2. **`raw-options.local_scheme = "no-local-version"` rationale "PyPI rejects `+local` suffixes on upload"** — accurate per PyPI behavior and confirmed by the pyproject.toml inline comment (`pyproject.toml:38-40`). Not independently re-verified against PyPI policy here.
3. **Mermaid diagram contents** — descriptive, not factual; cannot be inaccurate.
4. **"Present tense, active voice in docstrings and docs"** (line 112) — style convention, not factually verifiable.
5. **"No backward-compat shims unless `BREAKING.md` says otherwise"** (line 113) — policy statement, not a code-verifiable fact.
6. **"hashed-doc-id mode is on the roadmap"** (line 71) — claim references `roadmap.md` / Backlog; not verified per review constraints (other Documentation files are not authoritative for this review).
7. **CLAUDE.md cross-reference for marker-gated suites** (line 57) — CLAUDE.md does document these invocations, but the doc says "documented in `CLAUDE.md`" which is accurate.
