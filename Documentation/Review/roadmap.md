# Review: roadmap.md

## Summary

`Documentation/roadmap.md` is mostly accurate where it summarises today's state. Verified against `archon_search/` source, all "Done" bullets in the Status Snapshot correspond to code that exists, with one phrasing inaccuracy about where `namespace` lives (collection-level, not chunk-level). Priority lists are forward-looking aspirations and are not falsifiable from source, but two factual sub-claims inside Priority 1 were checked: "no metadata filters today" and "`CollectionMeta.embedding_model` already exists" — both correct. The cross-reference to `Backlog/03_world_class_roadmap.md` resolves.

## Inaccuracies (numbered)

1. **Line 28 — "Namespace and ACL handling at the document/chunk layer (`acl.py`, `CollectionMeta.namespace`)."**
   Misleading: in `archon_search/store.py` the chunk schema (lines 125–138) carries an `acl` column but **no `namespace` column**. `namespace` is a column on `_archon_collection_meta` only (line 157) and a field on `CollectionMeta` (`collection_meta.py:23`). So ACL is per-chunk, but namespace is per-collection — not "at the document/chunk layer". The parenthetical citation `CollectionMeta.namespace` even contradicts the sentence it supports.

## Verified claims

- Line 23 — Standalone package with own CLI, config, release process: `archon_search/cli/main.py` and `release.sh` exist.
- Line 24 — FastAPI REST + MCP endpoint sharing auth: `archon_search/server/app.py`, `server/mcp.py`, `server/middleware_auth.py` all present.
- Line 25 — Bearer-token auth bootstrap with `.search.env` and env-var overrides: `key_manager.py` exists (confirmed in CLAUDE.md and listed in package directory).
- Line 26 — Hybrid retrieval, cross-encoder reranking, multi-collection centroid routing: `store.py`, `reranker.py`, `router.py`, `pipeline.py` all present.
- Line 27 — Async job model: `archon_search/jobs/` exists with `model.py`, `store.py`.
- Line 29 — Opt-in telemetry, structural no-raw-query guarantee, `export_enabled=true` coerced to `false` with warning: confirmed in `archon_search/config.py:209-217` (`_logger.warning("telemetry: export_enabled is reserved for a future release and will be ignored")` then `telemetry.export_enabled = False`).
- Line 30 — Deterministic eval harness with thresholds and baseline: `tests/eval/thresholds.toml`, `tests/eval/baselines/` exist.
- Line 31 — Per-OS service install: `archon_search/platform/` contains `macos.py`, `linux.py`, `windows.py`, `service.py`; `cli/install_cmd.py` exists.
- Line 50 — "`CollectionMeta.embedding_model` already exists": confirmed at `collection_meta.py:19` (`embedding_model: str = ""`).
- Priority 1 item 1 — implicit claim that metadata filters are NOT yet implemented: confirmed by absence of any `filter`-related field in `server/schemas.py` and `server/routes_search.py` (the only `filter` references in `pipeline.py` are `apply_acl_filter`, which is ACL, not user-facing metadata filters).
- Line 98 — `Backlog/03_world_class_roadmap.md` resolves (file exists under `Documentation/Backlog/`).
- Line 94 — "`[telemetry].export_enabled = true` is coerced to `false` with a warning at config load": confirmed (same evidence as line 29).

## Unverifiable / ambiguous (separate aspirations from factual)

Aspirational — not falsifiable from source:

- Lines 13–17 (Principles): policy statements, not factual claims about code.
- Lines 33–38 (Priority 0 — "Product Boundary, largely landed"): "largely" is fuzzy but matches observed reality (separate package, own release process).
- Lines 40–51 (Priority 1 list): forward-looking ordering, not state.
- Lines 54–61 (Priority 2), 64–69 (Priority 3), 72–76 (Priority 4), 78–87 (Priority 5): all explicit roadmap items, not "current state" claims.

Ambiguous wording that is technically defensible but soft:

- Line 9 — "Where the two diverge, the backlog file wins." Policy claim; not a fact about code. The backlog file does exist, so the pointer is at least valid.
- Line 42 — "None are documented as complete in the current code; treat them as the next inbound work." Verified for item 1 (filters) above; the rest are aspirations.
- Lines 91–94 (Already Captured As Known Risks): the path-derived `doc_id` claim and the README cross-reference were not re-verified here, but the `export_enabled` half is correct (see Verified claims).
