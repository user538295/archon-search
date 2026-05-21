# Review: ADRs/01_lancedb_as_local_vector_store.md

## Summary

The ADR is short and factually accurate against the current code. All concrete code claims (RRF constant, file paths, config keys, LanceDB role as vector+FTS store) verify cleanly. One minor lint: the negative consequence about "table-level migrations the user must accept" is overstated — the migrations actually shipped (`migrate_namespace`, `migrate_acl` in `store.py:302-352`) are idempotent and run automatically on connect-time; there is no user accept step. No high- or medium-severity inaccuracies found.

## Inaccuracies (numbered: quoted claim, ground truth, file:line, severity)

1. Quoted: "LanceDB upgrades may require table-level migrations the user must accept."
   Ground truth: The two migrations present in the code (`migrate_namespace`, `migrate_acl`) are fully idempotent and silent — they add columns on startup with no user consent flow, and concurrent-write races are caught and logged. The ADR's wording implies a user-facing migration prompt that does not exist.
   Location: `archon_search/store.py:302-352` (migrate_namespace, migrate_acl)
   Severity: low (forward-looking caveat, not a current-behavior misstatement, but worded in a way that misrepresents the current migration UX)

## Verified claims

- "persists all runtime state under `~/.archon-search/`" — confirmed via `archon-search.toml.example:22` (`db_path = "~/.archon-search/search"`) and CLAUDE.md project context.
- "`[database] db_path` (see `archon-search.toml.example`)" — confirmed at `archon-search.toml.example:20-22`.
- "Reciprocal Rank Fusion (`_RRF_K = 60` in `archon_search/store.py`)" — confirmed at `archon_search/store.py:29` (`_RRF_K = 60  # RRF constant`).
- "Each collection is a LanceDB table" — confirmed: `ensure_collection` calls `db.create_table(collection, …)` at `archon_search/store.py:165-172`.
- "`SearchStore` (`archon_search/store.py`) owns table creation, upsert, hybrid search, and RRF fusion." — confirmed: `SearchStore` class at `store.py:75`, `ensure_collection` (165), `ingest_chunks` (408), `hybrid_search` (457), RRF scoring loop at `store.py:496-506`.
- "LanceDB operates directly on the configured `db_path`" — confirmed at `store.py:86-90` (`lancedb.connect_async(str(self._db_path))`).
- "Hybrid retrieval (vector + FTS) lives behind one storage API" — confirmed: vector search (`vector_search`, line 474) and FTS (`search(..., query_type="fts")`, line 481) are fused in `hybrid_search` (lines 457-522).
- LanceDB is a dependency — confirmed at `pyproject.toml:9` (`"lancedb>=0.30.0"`).

## Unverifiable / ambiguous

- "Performance characteristics on very large corpora (>10M chunks) are not validated by the eval harness, which uses a synthetic fixture corpus." — plausible; not directly checked against `tests/eval/` fixtures in this review. No reason to doubt.
- "Not horizontally scalable; concurrent multi-writer access is not a target." — design intent, not a code-verifiable claim; consistent with the single-process architecture and not contradicted by `store.py`.
- The dated metadata block ("Date: 2026-05-20", "Status: Accepted") — date matches today's date in env context; cannot independently verify acceptance decision.
- Alternatives Considered (pgvector, Qdrant/Weaviate, FAISS) — rationale claims (require running service, no FTS) are widely accepted as accurate; not checked against context7 since they describe absence/presence of features rather than version-specific API surface.
