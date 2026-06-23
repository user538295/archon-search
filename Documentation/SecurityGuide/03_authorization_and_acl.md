**Purpose**: Document how `archon-search` authorizes access to indexed content, what the per-chunk ACL does, and what it does not.
**Audience**: Security engineers, IT admins, integrators planning multi-namespace deployments.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2027-05-20

# Authorization and ACL

Authorization in `archon-search` has two layers: (1) the namespace that the bearer token resolved to, and (2) a per-chunk allow-list stored in the LanceDB row itself. Both are evaluated at search time.

For how the namespace is established, see [`02_authentication_and_keys.md`](./02_authentication_and_keys.md). For the broader architecture, see [`../Architecture/150_security_and_privacy_architecture.md`](../Architecture/150_security_and_privacy_architecture.md).

## Principles

1. **Namespace is a label, not a vault.** A namespace scopes ACL decisions; it does not partition the LanceDB tables.
2. **ACL is per-chunk, default-open.** Chunks without an `_acl` declaration are visible to every namespace.
3. **Misconfiguration fails open, with a warning.** Parsing errors degrade to "no restriction" rather than crash or deny.
4. **Comparison is exact and case-sensitive.** No prefix matching, no wildcards.

## Namespace resolution

After `APIKeyMiddleware` accepts a token, `request.state.namespace` is set to either:

- `DEFAULT_NAMESPACE` if the token matched the single default key, or
- the namespace string from the `[namespaces]` map if a per-namespace key matched.

The namespace is validated by `_validate_namespace` (`archon_search/constants.py`) before it is stamped on the request. A namespace that fails validation returns `500` (defensive — the config loader should have rejected it earlier).

The auth middleware exempts `/health`, `/docs`, `/openapi.json`, and `/redoc` from bearer-token enforcement (`middleware_auth.py:16`); these paths never have a namespace stamped on them. All other endpoints require a valid token.

## What the ACL is

The ACL is a nullable `list<utf8>` column on every chunk row (added by `SearchStore.migrate_acl` in `archon_search/store.py`). It is set during ingest from one of two sources, with this precedence (`archon_search/acl.py::resolve_acl`):

1. **Front-matter `_acl` key** in the source document (YAML front-matter, extracted by `SearchPipeline._extract_front_matter` in `archon_search/pipeline.py` and consumed in `SearchPipeline._build_records`; `parser.py` does not parse `_acl`).
2. **Sidecar file** `<doc>.acl` next to the document, plain text, one namespace per line.

If both are present, the front-matter wins and a warning is logged (`acl.py:236–241`).

## Decision table

The decision is in `archon_search/acl.py::is_acl_allowed`:

| Stored `acl` value | Meaning | Result for any namespace |
| --- | --- | --- |
| `None` (NULL in LanceDB) | No restriction — chunk is open | Allowed |
| `[]` (empty list) | `deny-all` sentinel | Denied |
| `["a", "b"]` | Allow-list | Allowed only if `namespace in {"a", "b"}`; case-sensitive |

Two important details:

- **Empty namespace fails closed.** If `namespace == ""` and the chunk has any non-`None` ACL, the chunk is denied (`acl.py:196–197`). The middleware never sets an empty namespace on a successful auth, but this is a defense-in-depth check.
- **`deny-all` is a reserved name.** `is_acl_namespace_valid` rejects `"deny-all"` as a namespace identifier (`acl.py:18`), so it can be unambiguously used as the sentinel.

The filter is applied after retrieval, in the search pipeline — see `acl.py::apply_acl_filter`, called by `SearchPipeline.search` (`pipeline.py:302`) and `SearchPipeline.search_with_context` (`pipeline.py:323`). `SearchResponse` carries an `acl_filtered: bool` flag so clients can detect that at least one candidate was dropped.

Note: in `search_with_context`, the per-neighbor ACL filter discards its "dropped" boolean (`pipeline.py:323`), so neighbor chunks hidden by ACL do **not** flip `acl_filtered` in the response. The flag reflects only the primary candidate set.

## Source resolution details

### Front-matter

YAML front-matter in the source document, keyed `_acl`. Accepts:

- A string (comma- or newline-separated namespaces).
- A list of strings.

`bool` values are explicitly rejected and logged (`acl.py:35–42`); any other non-`str` / non-`list` type (e.g. `int`, `dict`, `float`) falls through to the generic invalid-type branch (`acl.py:60–66`) and is logged with its type name. In both cases the chunk degrades to open. Non-string elements inside a list are dropped with a warning; the remaining valid entries form the ACL (`acl.py:51–58`).

An **empty list** in front-matter (`_acl: []`) is the deny-all sentinel — `parse_acl_value` returns `[]` for an empty list input (`acl.py:48–50`), which `is_acl_allowed` treats as deny-all.

`deny-all` is also recognized as a **reserved string token** in front-matter:

- `_acl: deny-all` (or `_acl: [deny-all]`) is interpreted as deny-all (`acl.py:102–109`).
- `deny-all` mixed with valid namespace names → `deny-all` is dropped and the valid names are used; a warning is logged (`acl.py:83–91`).
- `deny-all` mixed only with invalid names → ambiguous; the chunk fails open and a warning is logged (`acl.py:94–101`).

Because `is_acl_namespace_valid` rejects `"deny-all"` as a namespace identifier (`acl.py:18`), it cannot collide with a real namespace.

### Sidecar

A file named `<doc>.acl` in the same directory as the document. Plain UTF-8 text, one namespace per line. Behavior:

- Size cap: `_ACL_SIDECAR_MAX_BYTES = 65536`. Larger files are ignored with a warning (`acl.py:137–143`).
- Symlinks are ignored with a warning (`acl.py:132–134`) — prevents an ACL escape via symlink to an attacker-controlled file.
- Non-UTF-8 content → ignore with a warning (`acl.py:147–149`).
- A UTF-8 BOM at the start of the file is stripped before parsing (`acl.py:152`).
- A file whose first non-empty line is `DENY-ALL` (case-insensitive) is the deny-all sentinel; trailing lines are warned-about and ignored (`acl.py:160–167`).
- A sidecar in which **every** non-empty line is an invalid namespace name yields `None` (fail-open) after each invalid line is logged (`acl.py:169–178`). The same fail-open principle as front-matter applies: parse trouble never produces a deny outcome by accident.

### Combined behavior

Both `parse_acl_value` and `read_acl_sidecar` are deliberately permissive. They are designed to never raise — the worst case is that the chunk degrades to open and a warning is written to the application log. Operators relying on ACL **must** also monitor the log for these warnings; there is no metric counter for ACL parse failures in v1.

## What ACL does not do

The ACL surface is narrower than its name suggests. It does **not**:

- **Trim chunk content within a document.** ACL is per-chunk, not per-region within a chunk. A chunk is fully visible or fully hidden.
- **Restrict writes via the per-chunk `acl` list.** The chunk-level allow-list is consulted only on read (post-retrieval filtering). Write paths are not gated by the per-chunk ACL list. They are, however, **namespace-scoped** (see next bullet) — so the practical effect is that a namespaced client cannot ingest into or delete collections owned by another namespace, even though it is the namespace check, not the per-chunk `acl`, doing the work.
- **Replace namespace scoping on the control plane.** ACL is **post-retrieval** filtering on search results; it is distinct from the **namespace gating** that the control-plane routes apply. The control-plane routes filter their own responses by `request.state.namespace`:
  - `GET /collections` returns only collections whose `CollectionMeta.namespace` matches the caller (`routes_collections.py:80–94`).
  - `DELETE /collections/{name}` returns 404 for cross-namespace deletes (`routes_collections.py:182–184`).
  - `GET /collections/{name}/meta` returns 404 for cross-namespace access (`routes_collections.py:245–249`).
  - `GET /state` is filtered to the caller's namespace (`routes_state.py:17–25`).
  - `GET /status` is filtered to the caller's namespace (`routes_status.py:26–58`).
  - `POST /route` filters routing candidates to the caller's namespace (`routes_route.py:86–89`).
  - `/jobs/{job_id}` returns 404 when the job's namespace differs from the caller's (`routes_jobs.py:114, 134`).

  The **only** namespace-blind surface is `/telemetry/*` — `routes_telemetry.py` does not consult `request.state.namespace`, so a namespaced client sees the same telemetry as the default-namespace client.
- **Provide field-level redaction.** Source paths, descriptions, and metadata that the chunk row carries are returned verbatim if the chunk passes ACL.
- **Encrypt at rest.** ACL is metadata on plaintext chunk rows. Anyone with filesystem read access bypasses it.
- **Trigger differential ranking.** Ranking ignores ACL; the filter is a post-retrieval drop.

These limits are the explicit scope of v1. Tighter per-collection policies and per-tool authorization are tracked as **E4** in [`../Backlog/03_world_class_roadmap.md`](../Backlog/03_world_class_roadmap.md). The roadmap placed E4 after D7 (key rotation) since granular authorization without rotation is of limited value — D7 has now shipped, so E4 is the next logical step.

## Operational guidance

- **Treat namespace identifiers as labels in URLs**: choose names that you would be comfortable seeing in the application log. Names appear in warnings on ACL parse failures.
- **Pin sensitive collections to a namespace at ingest time** by adding `_acl: [namespace]` to the front-matter, not by relying on the sidecar — front-matter rides with the document through copies and backups, sidecars do not.
- **Sidecar removal only takes effect on reindex.** Ingest-time ACL is sticky in the LanceDB row: the row keeps its `acl` value even after the sidecar file is deleted. The chunk becomes open (or its ACL tightens) only after the document is reindexed. Operators should reindex after deleting a sidecar if they intend the chunks to become open, or after editing front-matter if they intend to tighten access.
- **`acl_filtered` in the response is your only signal that filtering occurred** for the primary candidate set. Watch for it in clients that summarize search hits. Note that in `search_with_context`, ACL-dropped neighbor chunks are not reflected in this flag (see "What the ACL is" above).

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
