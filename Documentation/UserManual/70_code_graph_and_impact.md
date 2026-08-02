**Purpose**: Index a code corpus into a typed def/ref graph and ask "what breaks if I change X?"
**Audience**: End users / operators indexing source code
**Status**: Stable
**Last reviewed**: 2026-07-29 / **Next review**: 2027-07-29

# Code graph and impact analysis

When you ingest source files into a graph-enabled collection, archon-search does more than embed the text. A syntax-aware pass over each file (tree-sitter) extracts the real relationships between symbols — which function calls which, what imports what, which class inherits from which — and writes them as typed edges into the collection's knowledge graph. The payoff is **impact analysis**: a caller/callee blast-radius answer to "what is affected if I change this symbol?", reachable over both REST and MCP.

This is the code counterpart to prose GraphRAG. For query-time graph search modes (`naive`/`local`/`global`/`ppr`) see [Graph search](65_graph_search.md); for the code-symbol chunk metadata this same pass produces (`_symbol_type`, etc.) see [Chunk metadata and enrichment](55_chunk_metadata_and_enrichment.md).

## What gets extracted

During ingest, `DefRefExtractor` (`archon_search/defref_extractor.py`) walks each code file's AST and emits four typed edge kinds between `code_symbol` nodes:

| Edge type | Meaning |
|---|---|
| `calls` | A caller symbol invokes a callee. |
| `imports` | A file's module imports a name. |
| `defines` | An enclosing scope (module or class) defines a symbol. |
| `inherits` | A class extends a base class. |

Every edge carries an honesty label in `extraction_method`:

- **`extracted`** — proven from this file's own text (same-file calls, explicit imports, in-file inheritance). Trust these.
- **`inferred`** — a best-guess cross-file name match: a callee or base class not defined in this file is looked up by exact (case-sensitive) name against the collection's existing `code_symbol` nodes. When a name matches definitions in several files, an edge is added to **all** of them — there is no "best" candidate to pick. Common names (`run`, `get`, `init`) can therefore produce false links, so filter impact answers to `extracted` when you need certainty (see `extraction_method_filter` below).

`code_symbol` node IDs are **file-qualified** (`name::path`), so two same-named symbols in different files stay distinct nodes; the node's display `entity_name` remains the bare symbol name.

### Supported languages

Real def/ref extraction runs for these nine extensions (`_LANG_LABEL` in `defref_extractor.py`; grammars pinned in `pyproject.toml` under the `code` extra):

`.py` Python · `.ts` TypeScript · `.js` JavaScript · `.go` Go · `.rs` Rust · `.java` Java · `.sh` Bash · `.swift` Swift · `.cs` C#

Notes: Go/Rust/Bash have no class inheritance, so `inherits` stays empty for them (expected, not a gap). SwiftUI is covered by Swift — `.swift` files include it.

## Prerequisites

1. **The `code` extra must be installed** — it provides tree-sitter and the nine grammars:

   ```bash
   uv sync --extra code --extra graph      # dev
   pip install 'archon-search[code,graph]' # end users
   ```

   The wizard installs both bundles automatically. If you enable `[graph]` **without** the code parsers, the server still starts and prose graphing still works, but code graphing is skipped with a one-time startup WARNING (and a per-file warning in the ingest result). It never blocks boot.

2. **`[graph] enabled = true`** in `~/.archon-search/archon-search.toml`. With `spacy` absent this raises a `ConfigError` at startup; the code grammars degrade gracefully as above. See [Configuration](30_configuration.md) and [Graph operations](../OperatorGuide/60_graph_operations.md).

3. **Re-ingest to gain edges.** Existing collections do **not** retroactively gain def/ref edges — there is no backfill pass. Edges appear only for newly ingested or re-ingested files. After upgrading or enabling the graph, re-ingest your code corpus:

   ```bash
   archon-search ingest --path ./src --collection code --wait
   ```

   Cross-file `inferred` edges also depend on ingest order: a reference resolves only against symbols already written by a prior ingest, so a full re-ingest is the reliable way to connect everything.

## Importance (PageRank)

A PageRank score is computed in the background over the collection's code-symbol edges and persisted on the nodes (built by `pagerank_builder.py`, debounce-triggered by the maintenance loop). Symbols the rest of the code points at score high.

You can browse by importance with the graph inspection endpoints, which accept `salience=importance` (persisted PageRank, nulls-last) alongside the existing `frequency` and `tfidf` modes:

```bash
curl -H "Authorization: Bearer $KEY" \
  "http://127.0.0.1:8765/graph/code?salience=importance"
```

Impact answers are also ordered by this score. **PageRank does not influence search ranking** — it is browsing and impact ordering only.

## Impact analysis — "what breaks if I change X?"

### REST

```
GET /graph/{collection}/impact/{symbol}
```

Query parameters (`routes_graph.py:get_graph_impact`):

| Param | Default | Meaning |
|---|---|---|
| `file_path` | — | Disambiguates same-named symbols to the one defined in this file. A bare filename (e.g. `helpers_a.py`, no path separator) matches by exact string or basename. A path containing a separator (e.g. `sub/helpers_a.py`) matches by exact string or path-suffix only — it will *not* match a same-named file in a different directory. Matching is case-insensitive. If it matches no definition, the response is empty (`depth_used=0`) rather than another file's blast radius — **unless** the collection predates the S68 `source_path` column and hasn't been re-ingested yet, in which case every candidate's `source_path` is still `NULL` and resolution falls back to highest-pagerank (re-ingest to get real disambiguation). |
| `depth` | `2` (`DEFAULT_IMPACT_DEPTH`) | Ripple distance; hard-capped server-side at `5` (`MAX_IMPACT_DEPTH`). |
| `direction` | `both` | `callers`, `callees`, or `both`. Any other value → `422`. |
| `extraction_method_filter` | — | Traverse only edges with this method, e.g. `extracted` for proven-only. |

Worked example — the blast radius of `parse_config`, proven edges only:

```bash
curl -H "Authorization: Bearer $KEY" \
  "http://127.0.0.1:8765/graph/code/impact/parse_config?depth=2&direction=both&extraction_method_filter=extracted"
```

The response mirrors `ImpactResult` 1:1:

```json
{
  "symbol": "parse_config",
  "callers": {
    "direct":   [{"entity_name": "load_config", "relationship_type": "calls", "extraction_method": "extracted", "depth": 1}],
    "indirect": [{"entity_name": "main", "relationship_type": "calls", "extraction_method": "extracted", "depth": 2}],
    "truncated": false,
    "omitted_count": 0
  },
  "callees": { "direct": [], "indirect": [], "truncated": false, "omitted_count": 0 },
  "depth_used": 2
}
```

Reading it:

- **`callers` vs `callees`** are separated — callers are what would break if you change the symbol; callees are what it depends on.
- **`direct` (hop 1) vs `indirect` (hop 2+)** are grouped and ordered by PageRank descending.
- **Truncation is explicit.** Each group caps its combined `direct`+`indirect` population at `50` (`MAX_IMPACT_GROUP_SIZE`, a fixed product choice — not configurable). When a hub symbol overflows, `truncated: true` and `omitted_count` make it visible — the answer is never silently partial.
- **`depth_used`** is the deepest hop actually reached; `0` means the symbol could not be resolved or had no reachable neighbours.

For the exhaustive response schema, `GET /openapi.json` is authoritative (`GraphImpactResponse`).

### MCP

The `graph_impact` tool (`server/mcp.py`) is the agent-facing twin, with the same semantics:

```json
{
  "name": "graph_impact",
  "arguments": {
    "collection": "code",
    "symbol": "parse_config",
    "file_path": "src/config.py",
    "depth": 2,
    "direction": "both",
    "extraction_method_filter": "extracted"
  }
}
```

It returns the same `symbol` / `callers` / `callees` / `depth_used` shape, or an `McpErrorResponse` (e.g. `graph_disabled`, `not_found`, `validation_error`). For MCP client setup see [MCP integration](../DeveloperGuide/05_mcp_integration.md).

## Related documents

- [UserManual index](00_index.md)
- [Graph search](65_graph_search.md) — prose GraphRAG query modes (`naive`/`local`/`global`/`ppr`)
- [Chunk metadata and enrichment](55_chunk_metadata_and_enrichment.md) — code-symbol chunk metadata (`_symbol_type`, etc.)
- [Ingestion and collections](50_ingestion_and_collections.md) — how to ingest and re-ingest a corpus
- [Configuration](30_configuration.md) — the `[graph]` section and knobs
- [Graph operations](../OperatorGuide/60_graph_operations.md) — operator-side graph administration, GC, and community rebuilds
- [MCP integration](../DeveloperGuide/05_mcp_integration.md) — connecting an agent to the `graph_impact` tool
- [API reference](../Architecture/600_api_reference_or_public_interface.md) — full REST + MCP + CLI surface
