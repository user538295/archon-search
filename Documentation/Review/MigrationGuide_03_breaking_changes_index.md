# Review: MigrationGuide/03_breaking_changes_index.md

## Summary

The document is largely accurate as a navigation/index over `BREAKING.md`. All major factual claims about the two queued `[next release]` entries (NR-1 MCP search shape, NR-2 REST `top_k` ignored) match both `BREAKING.md` and the current source. One notable mismatch exists between the doc and `BREAKING.md` on the config-section name for `top_k_return` — but in that case the doc agrees with the **code**, and `BREAKING.md` is the one that is wrong. Per the doc's own rule ("If the discrepancy is between `BREAKING.md` and the code, that is a contract bug"), this should be logged as a contract bug; the doc text itself is not factually inaccurate against the code.

Verifications performed against:
- `/Users/manczg/Documents/development/archon-search/BREAKING.md`
- `/Users/manczg/Documents/development/archon-search/archon_search/server/mcp.py` (line 59)
- `/Users/manczg/Documents/development/archon-search/archon_search/server/routes_search.py` (lines 17–84)
- `/Users/manczg/Documents/development/archon-search/archon_search/config.py` (lines 32–167)
- `/Users/manczg/Documents/development/archon-search/archon-search.toml.example`
- `/Users/manczg/Documents/development/archon-search/Documentation/MigrationGuide/` and `Documentation/Architecture/` directories (cross-reference existence)

## Inaccuracies (numbered)

1. **Line 31 / NR-2 one-line migration**: "set `[database] top_k_return` in `archon-search.toml`". This matches the code (`config.py` parses `top_k_return` under the `[database]` section, and `archon-search.toml.example` line 20 confirms `[database]`). However, `BREAKING.md` line 22 says `[search] top_k_return`. The index doc is correct vs. code but **inconsistent with the very source it claims is authoritative**. The doc states "BREAKING.md wins" yet silently diverges from it. This is a soft inaccuracy: either the doc should match `BREAKING.md` verbatim (and then `BREAKING.md` should be fixed), or the doc should call out the divergence explicitly. As written, it presents the code-correct value as if it were sourced from `BREAKING.md`, which it is not.

2. **Line 54 / NR-2 "Affected config key" note**: same issue as #1 — claims `[database].top_k_return` without flagging that `BREAKING.md` currently names the section `[search]`. The note implicitly contradicts `BREAKING.md` without acknowledging it.

## Verified claims

- Line 9: `BREAKING.md` is described as authoritative — consistent with the file's own self-description (`BREAKING.md` line 5: "This file IS the compatibility contract").
- Line 30 / NR-1 surface, change, and migration: matches `BREAKING.md` lines 11–16 verbatim in substance. `mcp.py` line 59 confirms current code returns `{"results": [asdict(r) for r in result_obj.results], "acl_filtered": result_obj.acl_filtered}`.
- Line 31 / NR-2 surface, change, and "no longer honored" claim: matches `BREAKING.md` lines 18–23. `routes_search.py` line 77 confirms the call is `pipeline.search(body.query, body.collection, namespace=ns)` with no `top_k` argument.
- Line 33: "The Pydantic schema for `SearchRequest` still declares `top_k`." Confirmed: `routes_search.py` line 20 — `top_k: int = Field(default=5, ge=1, le=100)`.
- Line 39: "No tagged release in `BREAKING.md` carries a breaking-change entry." Confirmed: `BREAKING.md` contains only two `[next release]` entries; no tagged-release sections exist.
- Line 47: "the new shape is already what `mcp.py` returns". Confirmed at `mcp.py` line 59.
- Line 49: REST `/search` response shape is `{"results": [...], "acl_filtered": ...}`. Confirmed: `routes_search.py` lines 57–59 define `SearchResponse(results=..., acl_filtered: bool)`; line 80 constructs `acl_filtered=result.acl_filtered`.
- Line 53: route ignores `body.top_k` and passes only `(body.query, body.collection, namespace=ns)`. Confirmed at `routes_search.py` line 77.
- Line 54: default `top_k_return` value `5`. Confirmed at `config.py` line 40: `top_k_return: int = 5`.
- Line 55: "MCP `search` does not accept a `top_k` parameter today." Confirmed by inspecting the MCP `search` tool signature in `mcp.py` (lines 39–59); no `top_k` parameter is exposed.
- Cross-reference targets exist:
  - `Documentation/MigrationGuide/01_versioning_and_release_model.md` ✓
  - `Documentation/MigrationGuide/06_client_migration_examples.md` ✓
  - `Documentation/Architecture/520_api_design_and_contracts.md` ✓
  - `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md` ✓

## Unverifiable / ambiguous

- Lines 1–5 frontmatter (`Last reviewed: 2026-05-20`, `Next review: 2027-05-20`, `Status: Draft`) — metadata, not technically verifiable beyond date plausibility (matches the current date).
- Line 33: "tracked as paydown items in `530_technical_debt_refactoring_roadmap.md` as **API-1** (MCP shape) and **API-2** (`top_k` ignored)." Not verified — would require reading the roadmap doc, which is out of scope per the review rule "NEVER trust Documentation/ files". The IDs API-1/API-2 are claims about another doc, not about code or `BREAKING.md`, so they are neither confirmed nor refuted here.
- Line 47 phrase "pre-change form on prior commits of `main`" — historical claim about git history, not verified.
- Line 48: "the previous shape `was never documented as stable`." This is a quotation from `BREAKING.md` line 16 ("the old shape was never documented as stable"). Quotation accurate; the underlying claim itself is not independently verifiable.
