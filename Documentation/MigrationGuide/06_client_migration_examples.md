**Purpose**: Concrete before/after diffs for client code affected by the two queued `[next release]` entries in `BREAKING.md` — MCP `search` response shape (NR-1) and REST `/search` `top_k` (NR-2).
**Audience**: Developers maintaining REST or MCP client integrations against `archon-search`.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2027-05-20

# Client Migration Examples

These examples cover only the two breaking changes queued in [`/BREAKING.md`](../../BREAKING.md) under `[next release]`. When future entries are added there, extend this file in the same PR.

## Principles

1. **`BREAKING.md` defines the contract; this doc shows the diff.** Names and shapes below mirror the authoritative source — do not paraphrase.
2. **Switch in one step.** There is no shim period; when the tag lands, both shapes do not coexist.
3. **Symmetry across REST and MCP applies only where stated.** REST `/search` and MCP `search` are aligned on response shape (NR-1) but diverge on `top_k` handling (NR-2 is REST-only).

## NR-1 — MCP `search` response shape

**Source**: [BREAKING.md → "[next release] — MCP `search` tool response shape"](../../BREAKING.md). **Code**: `archon_search/server/mcp.py` `search` tool.

Old shape (pre-change): a bare list of result dicts.

```json
[
  {"doc_id": "...", "chunk_id": "...", "text": "...", "score": 0.81, "source_path": "..."},
  {"doc_id": "...", "chunk_id": "...", "text": "...", "score": 0.77, "source_path": "..."}
]
```

New shape (current `main`, will be tagged in the next release):

```json
{
  "results": [
    {"doc_id": "...", "chunk_id": "...", "text": "...", "score": 0.81, "source_path": "..."},
    {"doc_id": "...", "chunk_id": "...", "text": "...", "score": 0.77, "source_path": "..."}
  ],
  "acl_filtered": false
}
```

### Python MCP client

```diff
 from mcp import ClientSession

 async def top_hits(session: ClientSession, collection: str, query: str):
     resp = await session.call_tool("search", {"collection": collection, "query": query})
-    # Old: response is a bare list of dicts.
-    return [r["text"] for r in resp]
+    # New: response is {"results": [...], "acl_filtered": bool}.
+    if resp.get("acl_filtered"):
+        log.info("search results were ACL-filtered for caller %s", session.identity)
+    return [r["text"] for r in resp["results"]]
```

### TypeScript MCP client

```diff
 import type { CallToolResult } from "@modelcontextprotocol/sdk";

-type SearchHit = {
-  doc_id: string; chunk_id: string; text: string; score: number; source_path: string;
-};
-type SearchResponse = SearchHit[];
+type SearchHit = {
+  doc_id: string; chunk_id: string; text: string; score: number; source_path: string;
+};
+type SearchResponse = { results: SearchHit[]; acl_filtered: boolean };

 export async function topHits(session: Session, collection: string, query: string): Promise<string[]> {
   const resp = (await session.callTool("search", { collection, query })) as unknown as SearchResponse;
-  return resp.map((r) => r.text);
+  if (resp.acl_filtered) {
+    console.info("search results were ACL-filtered");
+  }
+  return resp.results.map((r) => r.text);
 }
```

### Notes

- REST `/search` is **unaffected** by NR-1. Its response has always been `{"results": [...], "acl_filtered": ...}` — see `SearchResponse` in `archon_search/server/routes_search.py`. NR-1 only aligns MCP to that pre-existing REST shape.
- The new `acl_filtered` flag was previously unavailable on the MCP surface; if your client cares about ACL filtering decisions, surface it now.

## NR-2 — REST `/search` per-request `top_k` no longer honored

**Source**: [BREAKING.md → "[next release] — REST `/search` per-request `top_k` no longer honored"](../../BREAKING.md). **Code**: `archon_search/server/routes_search.py`.

Before: clients could specify `top_k` per request, overriding the configured default.
After: the route ignores `top_k`; the pipeline uses `config.top_k_return` from `archon-search.toml` (`[database].top_k_return`, default `5`).

The Pydantic schema in `SearchRequest` still declares `top_k: int = Field(default=5, ge=1, le=100)`. Sending it is not an error — it is silently ignored. This residual mismatch is logged as **API-2** in [`Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md).

### Operator step (config)

Set the desired result count once, server-side:

```toml
# ~/.archon-search/archon-search.toml
[database]
top_k_return = 10  # was previously implicit per-request from clients
```

> **Note**: `BREAKING.md` currently states the key lives under `[search]`, but the config loader (`archon_search/config.py:158–167`) reads `top_k_return` from the `[database]` section. The `[database]` form shown above is what the code actually accepts; `BREAKING.md` is out of date on this point and should be corrected in a follow-up. #Unverified (whether `[search]` was ever a valid section in any historical release)

Then restart the server. See [`04_config_migration.md`](./04_config_migration.md) and [`02_upgrade_procedure.md`](./02_upgrade_procedure.md).

### Python REST client

```diff
 import httpx

 async def search(client: httpx.AsyncClient, key: str, collection: str, query: str):
     resp = await client.post(
         "http://127.0.0.1:8765/search",
         headers={"Authorization": f"Bearer {key}"},
-        json={"collection": collection, "query": query, "top_k": 10},
+        # top_k is ignored by the route; configure [database].top_k_return server-side.
+        json={"collection": collection, "query": query},
     )
     resp.raise_for_status()
     return resp.json()["results"]
```

### TypeScript REST client

```diff
 export async function search(baseUrl: string, key: string, collection: string, query: string) {
   const resp = await fetch(`${baseUrl}/search`, {
     method: "POST",
     headers: {
       "Authorization": `Bearer ${key}`,
       "Content-Type": "application/json",
     },
-    body: JSON.stringify({ collection, query, top_k: 10 }),
+    // top_k is ignored by the route; set [database].top_k_return in archon-search.toml.
+    body: JSON.stringify({ collection, query }),
   });
   if (!resp.ok) throw new Error(`search failed: ${resp.status}`);
   const data = (await resp.json()) as { results: unknown[]; acl_filtered: boolean };
   return data.results;
 }
```

### MCP client

MCP `search` does **not** accept a `top_k` parameter, so NR-2 has no MCP-side code change. The same server-side config (`[database].top_k_return`) governs the result count for both surfaces.

## Cross-references

- [`03_breaking_changes_index.md`](./03_breaking_changes_index.md) — index entry NR-1 / NR-2 with one-line migrations.
- [`04_config_migration.md`](./04_config_migration.md) — `[database].top_k_return` and the wider config surface.
- [`Architecture/520_api_design_and_contracts.md`](../Architecture/520_api_design_and_contracts.md) — design rules behind the REST/MCP surface.
- [`Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md) — API-1 (MCP shape), API-2 (`top_k` ignored).
- [`Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) and `GET /openapi.json` — the authoritative live API contract.
