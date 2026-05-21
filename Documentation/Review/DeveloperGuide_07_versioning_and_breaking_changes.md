# Review: DeveloperGuide/07_versioning_and_breaking_changes.md

## Summary

The document is largely accurate. CalVer formula, `hatch-vcs` derivation, `BREAKING.md` as the contract, `/health` version source, OpenAPI `info.version` source, and the two listed `BREAKING.md` entries all match the source. The "no raw query" telemetry invariant referenced at the bottom matches `CLAUDE.md`.

Two factual issues: the cross-reference to "debt item `API-4` (roadmap C7)" is partially wrong — `API-4` exists in `Architecture/530_technical_debt_refactoring_roadmap.md`, but no identifier `C7` exists anywhere in `Documentation/roadmap.md` or the debt register. The `server/routes_health.py:13` line citation is correct. A handful of minor inaccuracies are listed below.

## Inaccuracies (numbered)

1. **Line 15 — "roadmap C7" identifier.** The doc says "debt item `API-4` (roadmap C7)". Verified: `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md` defines `API-4`, but the string `C7` does not appear in `Documentation/roadmap.md`, in the debt register, or anywhere under `Documentation/`. The parenthetical cross-reference is wrong or stale.

2. **Line 8 — CalVer example "26.5.123".** The example is plausibly synthetic and not actually wrong, but note the script in `release.sh` computes `count="$(git rev-list --count HEAD)"`, i.e. the **total** commit count, not a per-month count. The doc's wording "next month's count" (line 12) implies a per-month rev-count, which is incorrect: the third segment is total commits, monotonic across months. This is misleading even if not strictly false.

3. **Line 14 — "captured `openapi.json`".** Minor: the live endpoint is `GET /openapi.json` (with leading slash). The doc later uses `/openapi.json` correctly (lines 52, 93, 95, 100). The "captured `openapi.json` snapshot" phrasing on line 14 is fine as a filename reference but inconsistent with the rest of the doc.

4. **Line 41 — "verified against `/BREAKING.md`".** Path is wrong: `BREAKING.md` lives at the repo root, not under `/`. The link at line 104 correctly resolves to `../../BREAKING.md`. The inline reference on line 41 should be `BREAKING.md` (no leading slash) or `../../BREAKING.md` for consistency.

5. **Line 43 — `top_k_return` config key.** `BREAKING.md` line 22 confirms `config.top_k_return` and the suggested migration `[search] top_k_return`. The doc's claim is accurate against `BREAKING.md`, but I did not independently verify this is the actual TOML key path in `config.py` — the doc's claim is no worse than `BREAKING.md`'s own claim. Treat as **verified via `BREAKING.md`, not independently verified against `config.py`**.

6. **Line 89 — "the factory has no `query` parameter".** The user `CLAUDE.md` confirms this invariant ("factory methods in `entry.py` do not accept a `query` parameter"). Accurate.

## Verified claims

- **Line 8** — "version is `YY.M.<rev-count>` ... derived from git tags via `hatch-vcs`". Verified against `pyproject.toml` lines 35–43 (`[tool.hatch.version] source = "vcs"`) and `release.sh` lines 68–72 (`yy.${m}.${count}` where `count=git rev-list --count HEAD`).
- **Line 14** — "info.version field in that document is the running server's CalVer string (`server/app.py::_configure_openapi`)". Verified: `archon_search/server/app.py` lines 46–76 define `_configure_openapi` which passes `version=_VERSION` to `get_openapi(...)`; `_VERSION` is set from `importlib.metadata.version("archon-search")` at lines 28–29.
- **Line 25** — "package version is resolved via `importlib.metadata` at import time (`server/routes_health.py:13`)". Verified: `routes_health.py:13` is exactly `_VERSION = version("archon-search")`.
- **Lines 42–43** — Both `BREAKING.md` entries (MCP `search` shape `{"results": [...], "acl_filtered": bool}`, REST `/search` `top_k` ignored) match `BREAKING.md` lines 11–23 verbatim in substance. The MCP shape is also verifiable in `server/mcp.py:59`: `return {"results": [asdict(r) for r in result_obj.results], "acl_filtered": result_obj.acl_filtered}`. The REST `top_k` ignoring is verifiable in `routes_search.py`: `top_k` is declared on `SearchRequest` (line 20, `Field(default=5, ge=1, le=100)`) but never referenced again in the file beyond that declaration.
- **`release.sh` description (implied by lines 8, 51–54)** — Manual-only tag-push triggered release, matches `release.sh` and `.github/workflows/archon-search-release.yml` (`on: push: tags: "*"`). Verified.
- **Wheel-version-must-match-tag invariant** — Not stated in the doc but consistent with `.github/workflows/archon-search-release.yml` lines 108–120 ("Verify wheel version matches the pushed tag"). The doc does not contradict this.
- **CalVer ≠ SemVer; pin to tag/commit rather than CalVer segments** — Consistent with `BREAKING.md` lines 5–7 ("CalVer segments encode time only").

## Unverifiable / ambiguous

- **Line 15** — "Until debt item `API-4` (roadmap C7) lands, MCP responses are `dataclasses.asdict(...)` payloads." The MCP-uses-`asdict` part is verified (`server/mcp.py` line 5 `from dataclasses import asdict`; many call sites). The "roadmap C7" identifier is not verifiable — see Inaccuracy #1.
- **Line 53 — "Snapshot `/openapi.json` in CI."** Advice, not a verifiable factual claim. No CI workflow currently does this; that's fine because the doc only recommends it to clients.
- **Lines 76–87 (the inclusion/exclusion table)** — These are policy statements about what *should* trigger a `BREAKING.md` entry, not assertions about the current code. They cannot be verified or falsified by `pyproject.toml` / `release.sh` / workflows / `BREAKING.md` / `README.md`. They are internally consistent with the "structural" invariant on line 89.
- **Line 54 — "Pin to a tag or commit."** Advice. Not falsifiable from the listed sources.
- **Line 61 — `REQUIRED_MIN = (26, 5, 0)`** — Illustrative example; `(yy, mm, rev)` ordering matches `release.sh`'s tag formula. The `parse_calver` example assumes exactly three dot-separated segments — correct for clean tags, but `pyproject.toml` lines 38–41 note that **between tags** `hatch-vcs` produces `26.5.0.post{N}`. The example would `ValueError` on a `.postN` build (4 segments). Doc does not flag this.
