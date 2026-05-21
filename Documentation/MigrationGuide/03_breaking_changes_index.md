**Purpose**: Curated chronological index of breaking changes drawn directly from `BREAKING.md`, with a one-line migration step per entry and links back to the authoritative source.
**Audience**: Operators planning an upgrade across multiple versions; maintainers scanning for surface-level impact before tagging.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2027-05-20

# Breaking Changes Index

[`/BREAKING.md`](../../BREAKING.md) is authoritative. This file is a navigation aid — it summarises and groups entries, but every detail (Surface / Change / Migration / Announced in) lives in `BREAKING.md` itself.

## Principles

1. **`BREAKING.md` wins.** If this index and `BREAKING.md` disagree, fix this file.
2. **One row per published entry.** Releases with no contract change have no row.
3. **`[next release]` entries are included.** They describe behavior already on `main`; consumers building against the development head are already affected.

## How to use this index

1. Identify the version range you are upgrading across (see [`01_versioning_and_release_model.md`](./01_versioning_and_release_model.md)).
2. Read every row whose release falls inside that range. If your target is the next un-tagged build of `main`, also read the `[next release]` rows.
3. Click through to the `BREAKING.md` entry for the full Migration text. Apply it to the affected client (REST, MCP, or config).
4. Cross-reference [`06_client_migration_examples.md`](./06_client_migration_examples.md) for the queued REST/MCP changes — that doc carries concrete before/after diffs.

## Index

### `[next release]` (queued on `main`, not yet tagged)

| ID | Surface | What changed | Who is affected | One-line migration | Source |
| --- | --- | --- | --- | --- | --- |
| NR-1 | MCP (`mcp.py` `search` tool) | Response shape changed from a bare list of result dicts to `{"results": [...], "acl_filtered": bool}`. | MCP clients calling the `search` tool. | Read `response["results"]` instead of iterating the response directly; the new `response["acl_filtered"]` flag is informational. | [BREAKING.md → "[next release] — MCP `search` tool response shape"](../../BREAKING.md) |
| NR-2 | REST (`POST /search`) | The `top_k` field in `SearchRequest` is no longer honored at the route level; the pipeline uses `config.top_k_return` from `archon-search.toml`. | REST clients that previously set per-request `top_k`. | Remove `top_k` from request bodies; set `top_k_return` in `archon-search.toml` to the desired result count (per `BREAKING.md`, this lives under `[search]`; the current code parses it under `[database]` — see note below). #Unverified | [BREAKING.md → "[next release] — REST `/search` per-request `top_k` no longer honored"](../../BREAKING.md) |

Both `[next release]` entries are tracked as paydown items in [`Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md) as **API-1** (MCP shape) and **API-2** (`top_k` ignored). The Pydantic schema for `SearchRequest` still declares `top_k`; this is intentional and documented as debt — it will be removed when the entry is promoted out of `[next release]`.

### Tagged releases

| Release | ID | Surface | Summary | Source |
| --- | --- | --- | --- | --- |
| _none yet_ | — | — | No tagged release in `BREAKING.md` carries a breaking-change entry at the time this index was last reviewed. | [BREAKING.md](../../BREAKING.md) |

When a release lands that ships an entry, add one row per entry under a new sub-heading for that tag. Keep the most recent release at the top of the tagged-releases table.

## Notes on the queued entries

### NR-1 — MCP `search` response shape

- **Implementation status**: the new shape is already what `mcp.py` returns (`archon_search/server/mcp.py`, the `search` tool returns `{"results": [...], "acl_filtered": ...}`). The "old shape" referenced in `BREAKING.md` is the pre-change form on prior commits of `main`; it is **not** the current behavior of the code.
- **No prior deprecation period**: per `BREAKING.md`, the previous shape "was never documented as stable." There is no compatibility shim — clients must switch in one step when the tag lands.
- **REST `/search` is unaffected by NR-1.** The REST response has always been `{"results": [...], "acl_filtered": ...}` (see `SearchResponse` in `archon_search/server/routes_search.py`); NR-1 only aligns MCP to that shape.

### NR-2 — REST `/search` `top_k` no longer honored

- **Implementation status**: the route already ignores the field — see `archon_search/server/routes_search.py`, where `pipeline.search(body.query, body.collection, namespace=ns)` is called without passing `body.top_k`. The Pydantic schema still accepts `top_k` for backward-compatible request payloads; sending it no longer changes behavior.
- **Affected config key**: `top_k_return` (default `5`). Set this in `~/.archon-search/archon-search.toml` to control the returned result count. **Contract bug**: `BREAKING.md` documents this under `[search] top_k_return`, but the current code (`archon_search/config.py`) parses it under `[database]` (see the `# [database]` block above `top_k_return: int = 5`), and `archon-search.toml.example` also places it under `[database]`. Per the "When this index is wrong" rule below, this discrepancy between `BREAKING.md` and the code is a contract bug that must be resolved before the entry is promoted out of `[next release]`. #Unverified
- **MCP is not affected by NR-2.** MCP `search` does not accept a `top_k` parameter today.

## When this index is wrong

This is the second-source-of-truth document, by design. If you find a discrepancy:

1. Open `BREAKING.md`. Trust it.
2. Update the corresponding row here in the same PR that touches `BREAKING.md`.
3. If the discrepancy is between `BREAKING.md` and the **code**, that is a contract bug — log it in [`Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md) and resolve it before the next tag.

## Related documents

- [`/BREAKING.md`](../../BREAKING.md) — the compatibility contract (authoritative).
- [`06_client_migration_examples.md`](./06_client_migration_examples.md) — code-level diffs for NR-1 and NR-2.
- [`Architecture/520_api_design_and_contracts.md`](../Architecture/520_api_design_and_contracts.md) — design rules behind the contract.
- [`Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md) — API-1, API-2 debt entries.
