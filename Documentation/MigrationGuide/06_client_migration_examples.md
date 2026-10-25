**Purpose**: Concrete before/after diffs for client code affected by the queued `[next release]` entries in `BREAKING.md` — MCP `search` response shape (NR-1), REST `/search` `top_k` (NR-2) and the 2026-10-25 security and operations batch (NR-43 … NR-47): keys and admin scope, graph view tokens, ingest paths and limits, export and import.
**Audience**: Developers maintaining REST or MCP client integrations against `archon-search`.
**Status**: Draft
**Last reviewed**: 2026-10-25
**Next review**: 2027-05-20

# Client Migration Examples

These examples cover the breaking changes queued in [`/BREAKING.md`](../../BREAKING.md) under `[next release]` that change what a client sends or reads. When future entries are added there, extend this file in the same PR.

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

The Pydantic schema in `SearchRequest` still declares `top_k: int = Field(default=5, ge=1)`, and the route rejects a value above `[search].top_k_max` with `422`. Sending it is not an error — it is silently ignored. This residual mismatch is logged as **API-2** in [`Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md).

### Operator step (config)

Set the desired result count once, server-side:

```toml
# ~/.archon-search/archon-search.toml
[database]
top_k_return = 10  # was previously implicit per-request from clients
```

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

## NR-43 — keys, admin scope and namespace ownership

**Source**: [BREAKING.md → "[next release] — authorization: admin scope for key, sync, backup, maintenance, telemetry and collection-create routes; a collection name belongs to one namespace; API-key format rules; MCP tools fail closed (2026-10-25)"](../../BREAKING.md). **Code**: `archon_search/server/authz.py`, `archon_search/key_manager.py`.

Keys must be at least 32 characters: `ARCHON_SEARCH_API_KEY` lowercase hex (`python3 -c "import secrets; print(secrets.token_hex(32))"`), a `[namespaces]` token printable ASCII. Only the default key, or a managed key created with `"admin": true`, may call `/keys/rotate`, `/sync`, `/backup/trigger`, `/maintenance/trigger`, `/telemetry/*` and `POST /collections/`; any other key gets `403` `{"detail": "this operation requires an admin API key"}`.

```diff
 import httpx

-# a [namespaces] token used to trigger a sync
-httpx.post(f"{base}/sync", headers={"Authorization": f"Bearer {team_token}"})
+# only an admin key (the default key, or a managed key with admin=true) may; a team token gets 403
+httpx.post(f"{base}/sync", headers={"Authorization": f"Bearer {admin_key}"})

 # a team key issues keys for its own namespace only (never admin, never another namespace)
 httpx.post(f"{base}/keys", headers=team_headers, json={"namespace": "team-a"})
+# an admin key may mint an admin key
+httpx.post(f"{base}/keys", headers=admin_headers, json={"namespace": "default", "admin": True})
```

A write to a collection name another namespace owns is refused. Over REST `POST /ingest` still answers `202`; read the job: `GET /jobs/{id}` ends `FAILED` with `error` `collection name is not available to this namespace`. The MCP `ingest_file` and `ingest_directory` tools return `{"error": "collection name is not available to this namespace", "code": "collection_unavailable"}`. MCP tool calls outside an authenticated HTTP request answer `unauthenticated: …` instead of using the `default` namespace. Python code that imported `validate_token_and_get_namespace` now imports `authenticate_token` and reads `.namespace` and `.admin` of the returned `AuthIdentity`.

## NR-45 — graph viewer: `?token=` replaced by a view token, `/health` and `/docs`

**Source**: [BREAKING.md → "[next release] — network exposure: native `serve` binds `127.0.0.1`; CORS off by default; `/docs` and `/redoc` off and `/openapi.json` authenticated; minimal `/health`; graph viewer `?token=` replaced by view tokens; compose ports and the claude proxy are loopback and token protected (2026-10-25)"](../../BREAKING.md). **Code**: `archon_search/server/routes_graph.py`.

```diff
-url = f"{base}/graph/{collection}/view?token={api_key}"
-webbrowser.open(url)
+resp = httpx.post(f"{base}/graph/{collection}/view-token", headers={"Authorization": f"Bearer {api_key}"})
+resp.raise_for_status()
+# {"view_token": "...", "view_url": "/graph/<collection>/view?view_token=...", "expires_in": 60}
+webbrowser.open(base + resp.json()["view_url"])
```

The view token is valid for 60 seconds, scoped to one namespace and one collection, and works only on the viewer route and `GET /graph/{collection}`; it cannot mint another token. `GET /health` returns only `{"status": "running"}`: read the version from `GET /status` with a key. `/docs` and `/redoc` answer `404` (and `/openapi.json` needs a key) unless `[server].docs_enabled = true`. A browser app on another origin needs that origin in `[server].cors_origins`.

## NR-44 — ingest: paths, limits and the new status codes

**Source**: [BREAKING.md → "[next release] — ingest: path policy with a credential deny list and `[ingest].allowed_roots`; a non-admin key needs `allowed_roots` for path ingest; request bodies over `[server].max_request_body_mb` are `413`; `503` `ingest_busy`; `max_file_mb` defaults to 100; zip, parse-time and discovery limits; hidden, `node_modules`, `venv` and `site-packages` directories are not walked (2026-10-25)"](../../BREAKING.md). **Code**: `archon_search/_path_safety.py`, `archon_search/server/middleware_body_limit.py`, `archon_search/server/_ingest_admission.py`.

```python
resp = httpx.post(f"{base}/ingest", headers=headers, json={"collection": "docs", "path": "/srv/docs/handbook"})
if resp.status_code == 400:
    # "path is unsafe: denied_location" / "outside_allowed_roots" /
    # "path is unsafe: path-based ingest by this API key needs the operator to set [ingest].allowed_roots; ..."
    # -> ask the operator to set [ingest].allowed_roots, or send the text as inline `documents`
    ...
elif resp.status_code == 413:
    # {"detail": "request body too large", "code": "request_body_too_large"}: split the payload (limit [server].max_request_body_mb, 32)
    ...
elif resp.status_code == 503 and resp.json().get("code") == "ingest_busy":
    # [ingest].max_concurrent_tasks jobs are running: retry after the Retry-After header
    time.sleep(int(resp.headers["Retry-After"]))
```

Files over `[ingest].max_file_mb` (now 100) are refused per file. Directory ingest stops at `[ingest].max_discovered_files` (100000) and no longer walks hidden directories, `node_modules`, `__pycache__`, `venv` or `site-packages`.

## NR-46 — export and import without paths

**Source**: [BREAKING.md → "[next release] — data layout: exports and backups live in per-namespace directories, `output_path` and `--output-dir` are removed, archives are bound to their namespace; private file modes; the logged query fingerprint is keyed (2026-10-25)"](../../BREAKING.md). **Code**: `archon_search/server/routes_export.py`, `archon_search/jobs/export_archive.py`.

```diff
-job = client.post(f"{base}/collections/docs/export", json={"output_path": "/backups/team"}).json()
+# the body takes no fields (output_path is 422); the archive lands in <data-dir>/exports/<namespace>/
+job = client.post(f"{base}/collections/docs/export", json={}).json()
+archive = client.get(f"{base}/jobs/{job['job_id']}").json()["output_path"]   # the finished job names the file

-client.post(f"{base}/collections/docs/import", json={"path": "/backups/team/docs.tar.gz"})
+# the path must be in the caller's own exports/<namespace>/ or backups/<namespace>/ directory, else 400 path_unsafe
+client.post(f"{base}/collections/docs/import", json={"path": archive})
```

On the CLI, `archon-search export <collection>` no longer has `--output-dir`. Restoring an archive made before this change is described in [`05_data_migration.md`](./05_data_migration.md).

## Cross-references

- [`03_breaking_changes_index.md`](./03_breaking_changes_index.md) — index entry NR-1 / NR-2 with one-line migrations.
- [`04_config_migration.md`](./04_config_migration.md) — `[database].top_k_return` and the wider config surface.
- [`Architecture/520_api_design_and_contracts.md`](../Architecture/520_api_design_and_contracts.md) — design rules behind the REST/MCP surface.
- [`Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md) — API-1 (MCP shape), API-2 (`top_k` ignored).
- [`Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) and `GET /openapi.json` — the authoritative live API contract.
