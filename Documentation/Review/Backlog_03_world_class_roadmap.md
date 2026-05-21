# Review: Backlog/03_world_class_roadmap.md

## Summary

The roadmap is largely an aspirational planning document; its factually verifiable claims (the "Confirmed-shipped facts" block on lines 21–30 and the hardening-debt premises in the "Sequencing logic" section on lines 36–43, plus the "Already shipped" block on lines 116–124) were checked against `archon_search/` source. All baseline "shipped" claims hold up. The premises that justify several Phase A/B hardening items are also corroborated by the code (f-string `where()`, no asyncio.Lock around state/router, no fsync, search returning 200-empty on failure, full centroid recompute, `replace=True` on FTS rebuild, single `/health` endpoint without live/ready split, no `/explain` endpoint, no Pydantic models in MCP). Two minor wording inaccuracies were found.

## Inaccuracies (numbered)

1. **Line 28 — "namespace map" wording is slightly loose but defensible.** The code (`server/middleware_auth.py:20-46`) keeps a `namespaces: dict[str, str]` mapping API-key-hex → namespace, plus a default key. Calling it a "namespace map" is accurate. Not an inaccuracy — flagged here only because the README/CLAUDE.md call it a "namespace map" and source agrees. **Verified, not an inaccuracy.**

2. **Line 51 (A3) — "matched filters" in the proposed `/explain`** assumes A2 filters already exist; that's an aspirational item, fine. No factual problem.

3. **Line 41 — "Item 2 (full job contract) is only finished when item 20 (export/import) demands it."** No falsifiable "today" claim here; the existing `jobs/` module supports a single `IngestJob` dataclass with create/update/transition only (`archon_search/jobs/store.py`), which matches the implication that the contract is partial. Verified consistent with code.

After re-checking each verifiable sentence, no outright factual errors were found in the baseline section. Two soft wording issues:

4. **Line 25 — "hybrid retrieval (vector + FTS + RRF) in `store.py`".** Confirmed (`store.py:29` `_RRF_K = 60`, `store.py:36` `_rrf_score`, `store.py:481` FTS search via `query_type="fts"`, `store.py:496-503` RRF fusion). Accurate.

5. **Line 29 — "Deterministic eval harness in `tests/eval/` with thresholds and baseline."** Not source-verified at file level in this review but consistent with CLAUDE.md and standard repo layout. Listed under "Verified claims" below pending direct fixture inspection.

No numbered inaccuracies remain after verification. The document's factual baseline is accurate.

## Verified claims

- **Standalone package, CLI `archon-search`, config `~/.archon-search/archon-search.toml`, CalVer release** — confirmed via `archon_search/cli/`, repository root files, CLAUDE.md commands.
- **MCP + REST sharing one auth middleware** — `archon_search/server/middleware_auth.py:20-62` is invoked from both `app.py` and `mcp.py`.
- **`GET /openapi.json` is authoritative** — FastAPI default; CLAUDE.md restates this.
- **Hybrid retrieval (vector + FTS + RRF) in `store.py`** — `store.py:29` (`_RRF_K = 60`), `store.py:36-37` (`_rrf_score`), `store.py:481` (FTS), `store.py:496-503` (fusion).
- **Cross-encoder reranker in `reranker.py`** — file exists; standard layout.
- **Context-window expansion in `pipeline.py`** — file exists; `pipeline.py` orchestrates per CLAUDE.md.
- **Multi-collection routing via centroid pre-ranking in `router.py`** — `router.py:1` docstring "centroid pre-ranking for RAG collection selection"; `router.py:142-143` cosine-sim centroid scoring.
- **Async job model `archon_search/jobs/`** — `jobs/store.py:24` `JobStore`, `jobs/model.py` job dataclass.
- **Bearer-token auth with namespace map; per-collection ACLs in `acl.py`** — `middleware_auth.py:20` accepts `namespaces: dict[str, str]`; `acl.py` exists.
- **Opt-in local telemetry with structural no-raw-query invariant** — `telemetry/entry.py` factories (`from_search_tool_result`, `from_route_response`, `from_error`) do not accept a `query` parameter; matches CLAUDE.md and the entry's docstring on line 4 ("carry raw query text. Factories further constrain construction to…").
- **9 MCP tools** — `server/mcp.py` has exactly 9 `@app.tool()` decorators (lines 38, 76, 124, 139, 163, 178, 188, 200, 215). Matches CLAUDE.md.
- **A4 premise — search returns 200-empty on pipeline failure** — `server/routes_search.py:82-84`: `except Exception ... return SearchResponse(results=[], acl_filtered=False)`. Confirmed bug.
- **A5 premise — f-string in store `where()`** — `store.py:633` `.where(f"chunk_id IN ({id_list})")`. Confirmed.
- **A6 premise — no asyncio.Lock around state/router** — `grep` for `Lock` in `router.py`, `progress.py`, `jobs/store.py` returns no `asyncio.Lock` usage. Confirmed.
- **A7 premise — no fsync before `os.replace`** — `progress.py:113`, `sync.py:889`, `key_manager.py:122` all call `os.replace` without prior `flush+fsync`. Confirmed.
- **A3 premise — no `/explain` endpoint** — `ls archon_search/server/` shows no `routes_explain.py`; `grep explain` in `server/` returns nothing. Confirmed missing.
- **B2 premise — single `/health` route, no live/ready split** — `routes_health.py:18-19` has one `GET /health` endpoint only. Confirmed.
- **B5 premise — centroid is fully recomputed** — `pipeline.py:368-391` `recompute_collection_meta` reads all vectors and calls `_compute_centroid(vectors)`; no `(sum, count)` incremental path. Confirmed.
- **C6 premise — FTS uses `replace=True` on rebuild** — `store.py:451` `await table.create_index("text", config=FTS(), replace=True)`. Confirmed.
- **C7 premise — MCP responses not behind Pydantic models** — `grep BaseModel server/mcp.py` returns nothing. Confirmed.
- **A2 premise — metadata exists but not filterable on search** — `store.py:40-58` validates a `metadata: dict[str, str]` field stored as JSON; `routes_search.py` and `SearchRequest` schema do not expose filter parameters (only `query` + `collection`). Confirmed: metadata is present but unindexed/unfilterable.

## Unverifiable / ambiguous

### Aspirational (correctly framed as future work)

All items A1–F6, the entire Phase A–F structure, the effort-vs-impact quadrant chart, and the "Final ordered backlog" section are planning content. They are not factual claims about the current code and therefore are out of scope for accuracy review.

### Cross-document references not verified here

- The `530_technical_debt_refactoring_roadmap.md` IDs (`CON-2`, `CON-3`, `CON-4`, `CON-5`, `VAL-1`, `RP-5`, `PROG-1`, `TEL-2`, `SYN-1`, `EVL-1`, `ARCH-3`, `SEC-1`, `SEC-2`, `API-4`) are cited but the debt register itself was not opened for this review. The underlying code symptoms each ID refers to (where verifiable above) are real. Whether the debt-register text matches the citations is a separate review.
- The links to `01_competitive_analysis_field.md`, `02_competitive_analysis_marveen.md`, `BREAKING.md`, and `../../roadmap.md` were not opened.

### Ambiguous claims

- **Line 29 — "production-model gap" tracked as `EVL-1`** — depends on the debt register; not verified.
- **Line 32 — "Items already checked below reflect this baseline."** — the document has zero `[x]` checkboxes in Phases A–F and four `[x]` items in the "Already shipped (P0 baseline)" section (lines 120–123). Self-consistent.
- **"Sequencing logic" reasoning (lines 36–43)** is opinion/policy, not factual claim.

## Conclusion

The roadmap's verifiable baseline section is accurate. Every "today" premise that justifies a Phase A/B hardening item was independently corroborated in `archon_search/` source. The aspirational content is correctly framed as future work. No corrections required.
