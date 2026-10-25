**Purpose**: Document how `archon-search` authorizes access to indexed content, what the per-chunk ACL does, and what it does not.
**Audience**: Security engineers, IT admins, integrators planning multi-namespace deployments.
**Status**: Draft
**Last reviewed**: 2026-10-25
**Next review**: 2027-05-20

# Authorization and ACL

Authorization in `archon-search` has three layers: (1) the **admin** flag of the key, which gates server-wide operations (see [`02_authentication_and_keys.md`](./02_authentication_and_keys.md#admin-scope)); (2) the **namespace** the bearer token resolved to, which owns collections and scopes every listing, job and write; and (3) a per-chunk allow-list stored in the LanceDB row itself, evaluated at read time.

For how the namespace is established, see [`02_authentication_and_keys.md`](./02_authentication_and_keys.md). For the broader architecture, see [`../Architecture/150_security_and_privacy_architecture.md`](../Architecture/150_security_and_privacy_architecture.md).

## Principles

1. **A namespace owns what it creates, and the store enforces it.** Each collection belongs to the namespace that first wrote to it, recorded in its metadata row; every write path (ingest, document delete, collection drop, import, export, sync) refuses another namespace's collection with a fixed error, the admin key included. It is an access rule between cooperating clients, not a vault: all namespaces share one process and one set of LanceDB files, so anyone with filesystem access bypasses it.
2. **ACL is per-chunk, default-open.** Chunks without an `_acl` declaration are visible to every namespace.
3. **A declared restriction that cannot be read fails closed, with a warning.** An `_acl` value or `.acl` sidecar that exists but is unusable denies the chunk rather than opening it; only an absent or blank declaration is open.
4. **Comparison is exact and case-sensitive.** No prefix matching, no wildcards.

## Namespace resolution

After `APIKeyMiddleware` accepts a token, `request.state.namespace` is set to either:

- `DEFAULT_NAMESPACE` if the token matched the single default key, or
- the namespace string from the `[namespaces]` map if a per-namespace key matched.

The namespace is validated by `_validate_namespace` (`archon_search/constants.py`) before it is stamped on the request. A namespace that fails validation returns `500` (defensive — the config loader should have rejected it earlier).

The auth middleware exempts only `/health` and `/ready` from bearer-token enforcement (`middleware_auth.py::_EXEMPT_PATHS`; `/docs`, `/redoc` and `/openapi.json` are exempt only when `[server].docs_enabled = true`); these paths never have a namespace stamped on them. All other endpoints require a valid token; the one other credential is the 60-second graph **view token**, accepted only on `GET /graph/{collection}/view` and `GET /graph/{collection}` for the one collection and namespace it was minted for (`POST /graph/{collection}/view-token`; the old `?token=` parameter is gone). The MCP tools fail closed: a tool call that carries no authenticated request is refused (`unauthenticated`), never run as the default namespace.

## Collection ownership

Ownership is the `namespace` of a collection's metadata row. `SearchStore` checks it under the collection's write lock in `ensure_collection`, `ingest_chunks`, `delete_document`, `drop_collection`, `count_chunks` and `list_chunks_raw`, and raises `CollectionOwnershipError`, whose wire text is always `collection name is not available to this namespace` (it names no owner). The claim row is written before the first rows land and is rolled back if the write fails.

- **Another namespace's collection** is refused for every key, admin included. Through REST, `POST /ingest` still answers `202` and the job ends `FAILED` with that text; through MCP, `ingest_file` and `ingest_directory` return `{"error": …, "code": "collection_unavailable"}` and `delete_document` answers exactly as for a missing collection. A foreign ingest is refused before it parses, embeds or takes the collection lock. Sync and the file watcher act as the collection's recorded owner.
- **Orphans.** A table that has rows but no metadata row is treated as `default`-namespace data (which is how `list_collections` already reports it); other namespaces are refused. An empty or absent table can be claimed by anyone. The server logs one WARNING at startup that lists collections without a metadata row.
- **Names are global.** One LanceDB table exists per collection name, so "this name is taken" is observable across namespaces (no owner or content leaks). Namespacing the table names is not done.
- **Archives are bound to their exporter.** Exports are written to `<data dir>/exports/<namespace>/` (`output_path` no longer exists on REST, MCP or the CLI), scheduled backups to `<[backup].output_dir>/<namespace>/`, and the manifest records the namespace. An import reads only from the caller's own two directories; any other path, existing or not, answers the same `400 outside_allowed_dirs`, and a manifest naming another namespace answers `422 archive_namespace_mismatch`. The admin key has no cross-namespace archive access. An archive from before the manifest carried a namespace is accepted only from the caller's own directory; a flat `<data dir>/exports/*.tar.gz` file is importable only by the `default` namespace.

## What the ACL is

The ACL is a nullable `list<utf8>` column on every chunk row (added by `SearchStore.migrate_acl` in `archon_search/store.py`). It is set during ingest from one of two sources, with this precedence (`archon_search/acl.py::resolve_acl`):

1. **Front-matter `_acl` key** in the source document (YAML front-matter, extracted by `SearchPipeline._extract_front_matter` in `archon_search/pipeline.py` and consumed in `SearchPipeline._build_records`; `parser.py` does not parse `_acl`).
2. **Sidecar file** `<doc>.acl` next to the document, plain text, one namespace per line.

If both are present, the front-matter wins and a warning is logged and returned in the ingest result.

## Decision table

The decision is in `archon_search/acl.py::is_acl_allowed`:

| Stored `acl` value | Meaning | Result for any namespace |
| --- | --- | --- |
| `None` (NULL in LanceDB) | No restriction — chunk is open | Allowed |
| `[]` (empty list) | `deny-all` sentinel | Denied |
| `["a", "b"]` | Allow-list | Allowed only if `namespace in {"a", "b"}`; case-sensitive |

Two important details:

- **Empty namespace fails closed.** If `namespace == ""` and the chunk has any non-`None` ACL, the chunk is denied. The middleware never sets an empty namespace on a successful auth, but this is a defense-in-depth check.
- **`deny-all` is a reserved name.** `is_acl_namespace_valid` rejects `"deny-all"` as a namespace identifier, so it can be unambiguously used as the sentinel.

The filter is applied after retrieval, in the search pipeline — see `acl.py::apply_acl_filter`, called by every `SearchPipeline` search path (standard, RAG Fusion, graph and community modes, multi-collection fan-out) and by `SearchPipeline.search_with_context`. The graph inspection routes and MCP graph tools, `GET /collections/{name}/documents` and MCP `list_documents` also drop ACL-hidden chunks, with the same rule. `SearchResponse` carries an `acl_filtered: bool` flag so clients can detect that at least one candidate was dropped.

Note: in `search_with_context`, the per-neighbor ACL filter discards its "dropped" boolean, so neighbor chunks hidden by ACL do **not** flip `acl_filtered` in the response. The flag reflects only the primary candidate set.

## Source resolution details

### Front-matter

YAML front-matter in the source document, keyed `_acl`. Accepts:

- A string (comma- or newline-separated namespaces).
- A list of strings.

Values that are not a string or a list (`bool`, `int`, `float`, `dict`, ...) deny the chunk and log a warning with the type name. A list with no string element denies the chunk. Non-string elements inside an otherwise valid list are dropped with a warning; the remaining valid entries form the ACL.

An **empty list** in front-matter (`_acl: []`) is the deny-all sentinel — `parse_acl_value` returns `[]` for an empty list input, which `is_acl_allowed` treats as deny-all. A blank scalar (`_acl:` with only whitespace) declares nothing and is open.

`deny-all` is also recognized as a **reserved string token** in front-matter:

- `_acl: deny-all` (or `_acl: [deny-all]`) is interpreted as deny-all.
- `deny-all` mixed with valid namespace names → `deny-all` is dropped and the valid names are used; a warning is logged.
- `deny-all` mixed only with invalid names → ambiguous; the chunk is **denied** and a warning is logged. A list in which no entry is a valid namespace name is denied the same way, and a partly valid list narrows to its valid names.

Because `is_acl_namespace_valid` rejects `"deny-all"` as a namespace identifier, it cannot collide with a real namespace.

### Sidecar

A file named `<doc>.acl` in the same directory as the document. Plain UTF-8 text, one namespace per line. A sidecar that exists declares a restriction, so one that cannot be used **denies** the chunk (and a warning is returned in the ingest result) instead of opening it:

- Size cap: `_ACL_SIDECAR_MAX_BYTES = 65536`. A larger file denies.
- A symlink (even a dangling one) is never read and denies — prevents an ACL escape via symlink to an attacker-controlled file.
- Non-UTF-8 content denies. An unreadable file (I/O or permission error) denies.
- A UTF-8 BOM at the start of the file is stripped before parsing.
- A file whose first non-empty line is `DENY-ALL` (case-insensitive) is the deny-all sentinel; trailing lines are warned-about and ignored.
- A sidecar in which **every** non-empty line is an invalid namespace name denies; invalid lines among valid ones are dropped with a warning.
- An empty sidecar (no non-empty line) declares nothing and is open.

### Combined behavior

Both `parse_acl_value` and `read_acl_sidecar` are designed never to raise. The principle is that an author who declared an ACL meant a restriction: the worst case of a typo, a bad type or a damaged sidecar is that the chunk is hidden until the declaration is fixed and the document reindexed, not that it leaks. Operators should still watch the application log and the ingest result for these warnings; there is no metric counter for ACL parse failures.

## What ACL does not do

The ACL surface is narrower than its name suggests. It does **not**:

- **Trim chunk content within a document.** ACL is per-chunk, not per-region within a chunk. A chunk is fully visible or fully hidden.
- **Gate writes by the per-chunk `acl` list.** The chunk-level allow-list is consulted only on read (post-retrieval filtering). Writes are gated by **collection ownership** instead (see above): a namespaced client cannot ingest into, delete from, import over or export another namespace's collection.
- **Replace namespace scoping on the control plane.** ACL is **post-retrieval** filtering on search results; it is distinct from the **namespace gating** that the control-plane routes apply. The control-plane routes filter their own responses by `request.state.namespace`:
  - `GET /collections` returns only collections whose `CollectionMeta.namespace` matches the caller (`routes_collections.py`).
  - `DELETE /collections/{name}` returns 404 for cross-namespace deletes.
  - `GET /collections/{name}` returns 404 for cross-namespace access.
  - `GET /indexing-state` is filtered to the caller's namespace (`routes_state.py`).
  - `GET /status` is filtered to the caller's namespace (`routes_status.py`).
  - `POST /route` filters routing candidates to the caller's namespace (`routes_route.py`).
  - `/jobs/{job_id}` returns 404 when the job's namespace differs from the caller's (`routes_jobs.py`).

  `/telemetry/*` is not namespace-filtered, so it is **admin-only**: a namespaced (non-admin) key gets `403`. The other admin-only routes are listed in [`02_authentication_and_keys.md`](./02_authentication_and_keys.md#admin-scope).
- **Provide field-level redaction.** Source paths, descriptions, and metadata that the chunk row carries are returned verbatim if the chunk passes ACL.
- **Encrypt at rest.** ACL is metadata on plaintext chunk rows. Anyone with filesystem read access bypasses it.
- **Trigger differential ranking.** Ranking ignores ACL; the filter is a post-retrieval drop.

These limits are the explicit scope of v1. Tighter per-collection policies and per-tool authorization are tracked as **E4** in [`../Backlog/03_world_class_roadmap.md`](../Backlog/03_world_class_roadmap.md). The roadmap placed E4 after D7 (key rotation) since granular authorization without rotation is of limited value — D7 has now shipped, so E4 is the next logical step.

## Operational guidance

- **Treat namespace identifiers as labels in URLs**: choose names that you would be comfortable seeing in the application log. Names appear in warnings on ACL parse failures.
- **Pin sensitive collections to a namespace at ingest time** by adding `_acl: [namespace]` to the front-matter, not by relying on the sidecar — front-matter rides with the document through copies and backups, sidecars do not.
- **Sidecar removal only takes effect on reindex.** Ingest-time ACL is sticky in the LanceDB row: the row keeps its `acl` value even after the sidecar file is deleted. The chunk becomes open (or its ACL tightens) only after the document is reindexed. Operators should reindex after deleting a sidecar if they intend the chunks to become open, or after editing front-matter if they intend to tighten access.
- **`acl_filtered` in the response is your only signal that filtering occurred** for the primary candidate set. Watch for it in clients that summarize search hits. Note that in `search_with_context`, ACL-dropped neighbor chunks are not reflected in this flag (see "Decision table" above). A chunk hidden because its declaration was unusable is dropped the same way and also sets the flag.

## Verifying ACL is enforced

Add a document with front-matter `_acl: [team-a]`, ingest it under any namespace, then query:

```bash
# Query as team-a — should return the chunk
curl -H "Authorization: Bearer $TEAM_A_KEY" \
     -d '{"query":"<expected hit>"}' http://127.0.0.1:8765/search

# Query as the default key — should return acl_filtered=true and miss the chunk
curl -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
     -d '{"query":"<expected hit>"}' http://127.0.0.1:8765/search
```

If both calls return the chunk, the most likely cause is a parse warning on the front-matter — check the server log for `_acl in <path> has invalid …`.

## Related documents

- [`02_authentication_and_keys.md`](./02_authentication_and_keys.md) — how the namespace is established.
- [`01_threat_model.md`](./01_threat_model.md) — boundary the ACL operates inside.
- [`../Architecture/150_security_and_privacy_architecture.md`](../Architecture/150_security_and_privacy_architecture.md) — broader privacy architecture.
- [`../Backlog/03_world_class_roadmap.md`](../Backlog/03_world_class_roadmap.md) — item **E4** (per-collection access-control policies).
- [`../Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md) — `SEC-1` (rotation prerequisite for stronger authz) — **resolved by D7**.
