# Review: OperatorGuide/06_upgrading.md

## Summary

The doc is largely accurate against `release.sh`, `pyproject.toml`, `archon_search/cli/`, `archon_search/server/`, `archon_search/config.py`, and `BREAKING.md`. A few claims about the example config and one verification claim are wrong, and several statements describe documentation files whose contents I did not exhaustively verify here. Net: usable, with the inaccuracies below.

## Inaccuracies (numbered)

1. **Line 82 — "the example file says it 'raises ConfigError'".** False. `archon-search.toml.example` lines 67–71 already say `export_enabled` is "reserved for a future remote-export feature; in v1 the config loader logs a warning and silently coerces this to false." It does NOT claim ConfigError. The actual mismatch documented in `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md` (TEL-1) is between `CLAUDE.md` / ADR-05 and the code, not between `archon-search.toml.example` and the code. The example file already matches the code.

2. **Line 118 — "Set `[database].top_k_return` in `archon-search.toml`".** The path is correct (`config.py` line 162–167 reads `database["top_k_return"]`, and `archon-search.toml.example` line 20 has `[database]`), BUT `BREAKING.md` (the source this section cites) actually says `[search] top_k_return`, which is itself wrong. The operator doc accidentally corrects BREAKING.md without noting the discrepancy — flag for follow-up: BREAKING.md needs fixing or the doc needs to footnote it.

3. **Line 69 — "`/health` reports the new version string".** True in substance (`routes_health.py` returns `HealthResponse(status="running", version=_VERSION)`), but the doc's earlier `curl` example on line 61 calls `/health` without piping to `jq`, while the "Version probing" section on line 128 shows the correct pattern. Minor — the verification step works, just inconsistent presentation.

4. **Line 64 — `curl ... /status | jq '.version, .collections[].status'`.** The `.version` field exists (`routes_status.py` line 84) and `.collections[].status` exists (line 71 sets `status=...`). Verified accurate. NOT an inaccuracy — leaving here to confirm I checked.

5. **Line 16 — references `Documentation/MigrationGuide/02_upgrade_procedure.md` and `03_breaking_changes_index.md`.** Both files exist in `Documentation/MigrationGuide/`. Verified — not an inaccuracy.

6. **Line 146 — references `Backlog/03_world_class_roadmap.md` `D3`.** The file exists; I did not verify that it contains a `D3` item for schema-migration tooling. Marked unverified below.

7. **Lines 112–114 — "[next release]" BREAKING.md MCP entry.** Verified: BREAKING.md does contain this entry with the described shape change `{"results": [...], "acl_filtered": bool}`. Not an inaccuracy.

8. **Line 122 — "`.indexing_state.json`".** Not verified against source in this review (no grep performed). Marked unverified.

## Verified claims

- CalVer format `YY.M.<rev-count>` — confirmed in `release.sh` lines 68–72 (`yy=date +%y`, `m=date +%-m`, `count=git rev-list --count HEAD`) and `pyproject.toml` lines 35–43 (`hatch-vcs`, `no-guess-dev`, `no-local-version`).
- "Plain pushes to `main` do not publish" — confirmed by `release.sh` header comments (lines 16–17) and the `CLAUDE.md` project section.
- `archon-search stop` / `start` / `install` / `--version` exist — confirmed in `archon_search/cli/main.py` (lines 21–30) and `archon_search/cli/{start,stop,install_cmd}.py`.
- `archon-search --version` — confirmed via `@click.version_option(_VERSION, ...)` in `cli/main.py` line 21.
- `/health` and `/status` both return `version` — confirmed in `routes_health.py:20` and `routes_status.py:84`, both reading `version("archon-search")` from `importlib.metadata`.
- `GET /health` is unauthenticated, `/status` requires Bearer — consistent with `CLAUDE.md`'s "All endpoints except `GET /health` require a `Bearer` token."
- `[next release]` BREAKING.md entry shapes — confirmed in `BREAKING.md`. Both entries (MCP `search` shape; REST `/search` per-request `top_k` ignored) exist verbatim.
- `routes_search.py` swallows pipeline failures into empty results (CON-5) — confirmed lines 82–84 (`except Exception ... return SearchResponse(results=[], acl_filtered=False)`).
- `SearchRequest.top_k` exists but is unused — confirmed: `routes_search.py:20` defines `top_k: int = Field(default=5, ...)`, but `pipeline.search(body.query, body.collection, namespace=ns)` on line 77 does not pass it; pipeline uses `self._top_k_return` from config (`pipeline.py:303`, `pipeline.py:439`).
- Config loader silently ignores unknown keys — confirmed: `config.py` has no `extra="forbid"` or unknown-key validation; each section reads keys via `if "x" in section` guards.
- `[database].top_k_return` is the live key — confirmed `config.py:162–167`, `archon-search.toml.example:20`.
- TEL-1 ID / CON-5 ID — both referenced in `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md`.
- Package vs distribution name (`archon-search` PyPI, `archon_search` import) — confirmed `pyproject.toml` lines 2, 45–49.

## Unverifiable / ambiguous

- Whether `Backlog/03_world_class_roadmap.md` contains a `D3` item for schema-migration tooling (line 16, 122, 146). Not opened in this pass.
- Whether `archon-search.toml.example` is "regenerated each release" (line 80). No CI step verified.
- Whether `pip download archon-search==<current>` works against PyPI's yank semantics as described (line 37). Not verified.
- Whether the supervisor/service definitions actually get rewritten by `archon-search install` for service-definition changes (line 56–57). `install_cmd.py` was not opened.
- "PyPI does not guarantee permanent availability of yanked releases" (line 37) — generally true of PyPI policy but not a source-of-truth claim about this repo.
- The recommendation that older versions "may refuse to open" LanceDB tables touched by newer versions (line 86) — plausible but not verified against `store.py` migration behavior.
- Whether `MigrationGuide/02_upgrade_procedure.md` and `03_breaking_changes_index.md` actually contain the long-form material this doc defers to (line 16). Files exist; contents not inspected.
