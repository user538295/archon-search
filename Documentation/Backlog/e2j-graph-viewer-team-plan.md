---
id: E2j
feature: Graph Viewer (Single-File Local UI)
brief: e2j-graph-viewer-brief.md
purpose: Let operators open GET /graph/{collection}/view in a browser and see an interactive force-directed graph — zero install, zero external requests.
audience: Developers and operators running archon-search locally or on a private server who want to explore their knowledge graph without downloading raw files.
status: planned
roles: [frontend, backend, tester]
architecture: clean
---

# E2j · Graph Viewer (Single-File Local UI) — Team Plan

**How to read this file**
- **Architecture approach:** Clean Architecture — the default; no override skill was requested. **Layers:** Presentation · Use Cases · Interface Adapters · Entities · Frameworks & Drivers. Each task's first sub-bullet names the layer it touches.
- In this server-side project, **Frontend (Presentation)** = the `graph_viewer.html` file and its embedded JS/CSS that the browser runs. **Backend** = all Python code in `archon_search/` (route handler, schema models, resource loading).
- The **Frontend, Backend, and Tester** sections are the depth view; the **Task Breakdown** is the execution-order view.
- **Phases are vertical slices** — each delivers a working end-to-end increment. Sliced with the **`vertical-slicer` skill** (installed). This feature is one end-to-end behavior, so there is one feature slice plus Kickoff and Close-out.
- **Tests** are tagged by level. Unit and integration tests belong to the implementing dev (test-first); e2e and manual tests are the tester's tasks.
- **Contracts** are TypeSpec-backed: HTTP/API seams emit `openapi.yaml`; internal logical seams use core-construct `.tsp` only. Links below.
- IDs (`S#`, `C#`, `BE-#`/`FE-#`/`T-#`/`K#`, `Q#`) are the traceability thread.
- **Rule:** edit your own tasks freely; change a contract only by team agreement.

---

## Background

Operators who want to understand their knowledge graph must download raw JSON or GraphML files and open them in third-party tools — there is no built-in visual interface. The graph inspection REST API (`GET /graph/{collection}`) already exists and returns all the data needed to render an interactive graph.

---

## Goal

Opening `GET /graph/{collection}/view` in a browser delivers a self-contained interactive graph page — nodes, edges, search, and click-through inspection — with no installation, no external tools, and no second login prompt. The page is bundled into the Python package and served directly by the archon-search server.

---

## Scope

### In Scope
- Single-file HTML served by the archon-search server at `GET /graph/{collection}/view`
- Force-directed layout using inlined **vis-network** (~500 KB embedded — no CDN, no npm, no build step; batteries-included physics, node drag, zoom, and tooltips)
- Bearer token embedded in the page at render time via `str.replace()` placeholder substitution
- Nodes: sized by `salience`, colored by `entity_type` (person, concept, system, event, code_symbol)
- Edges: thickness proportional to co-occurrence `weight`; `relationship_type` visible on hover
- Click-to-inspect side panel: `entity_name`, `entity_type`, `chunk_count`, and co-occurrence chunk IDs derived from incident edges
- Text search: filters visible nodes by name as the user types
- Truncation banner: displayed when `truncated = true`, showing displayed vs. total counts from `node_count`/`edge_count`
- Empty-state message: "No entities found — ingest some documents first" when `nodes` is empty
- `graph.enabled = false` handling: 422, same guard as existing graph endpoints
- HTMX-compatible markup: no custom Web Components, no Shadow DOM
- Zero external network requests after page load
- Bundled into the Python package as a resource file loaded via `importlib.resources`
- **Schema addition:** `entity_type: str` added to `GraphNodeInspection` and `GraphNodeResponse` (additive, non-breaking); OpenAPI snapshot updated

### Out of Scope
- Cross-collection merged view
- Salience mode selector in the UI (always uses `frequency` default)
- Impact / blast-radius drill-down on `code_symbol` nodes
- Community highlighting / cluster overlay
- PNG/SVG export
- E8 admin UI integration (E8's scope; E2j's markup must only be HTMX-compatible)

---

## Acceptance criteria
- `GET /graph/{collection}/view` with a valid Bearer token returns 200 `text/html` with a self-contained HTML page
- The page embeds the caller's Bearer token as a JS variable so the force-directed graph loads automatically
- The page contains no `<script src="https://...">`, `<link href="https://...">`, `fetch()`/`XHR` to external hosts, or CDN URLs
- Nodes are sized by `salience` and colored by `entity_type`
- Edges have thickness proportional to `weight`; hovering an edge shows `relationship_type`
- Clicking a node opens a side panel with `entity_name`, `entity_type`, `chunk_count`, and co-occurrence chunk IDs from incident edges
- Typing in the search box filters visible nodes by name (case-insensitive)
- When `truncated = true`, the page shows "Showing X of Y nodes and A of B edges"
- When `nodes` is empty, the page shows "No entities found — ingest some documents first"
- `graph.enabled = false` → 422 `{"detail": "graph inspection requires [graph] enabled=true in server config"}`
- Missing or invalid auth → 401
- Unknown collection → 404
- `GET /graph/{collection}` now includes `entity_type` on every node in its JSON response
- OpenAPI snapshot test passes after regen
- All existing tests pass; test coverage ≥ 85%

---

## What does NOT change
- `GET /graph/{collection}` URL, method, or existing response fields — `entity_type` is additive
- `graph.enabled` guard mechanism and its exact 422 detail string
- `APIKeyMiddleware` — **requires modification** for `?token=` support: **Header takes priority:** the exemption branch only fires when `Authorization` header is ABSENT. If a valid `Authorization` header is present, the middleware runs normally for `/view` too. The conditional should be: `_GRAPH_VIEW_RE.match(path) and "token" in query_params and "authorization" not in request.headers.keys()`. the middleware needs a conditional branch using an exact path-pattern match (e.g. `import re; _GRAPH_VIEW_RE = re.compile(r'^/graph/[^/]+/view$')` at module level) and an exact query-parameter key check (e.g. `"token" in request.query_params` where `request.query_params` is a dict-like object, so this is exact-key matching, not substring). The example: `if _GRAPH_VIEW_RE.match(request.url.path) and "token" in request.query_params:`. **`endswith("/view")` is too broad** (any future route ending in `/view` inherits the auth bypass); **`"token=" in str(request.url.query)` is a substring match** (false-exempts `?other_token=abc`). Use the compiled regex + `query_params` dict-key check. to detect `/graph/{collection}/view` requests that supply a `?token=<raw>` query parameter, and skip the header-presence check for those requests only. **`_EXEMPT_PATHS` cannot be used** — it is an exact-string frozenset; no real request path (e.g. `/graph/my-docs/view`) would ever equal a string like `"/view"`. **The handler must NOT inline-duplicate this cascade** — instead, BE-2 must extract the cascade logic into a module-level function `validate_token_and_get_namespace(token: str, request: Request) -> str | None` (where the helper reads `request.app.state.key_store`, `request.app.state.config.namespaces`, `request.app.state.api_key`) in `middleware_auth.py` or a shared auth helper, callable from both the middleware and the handler. This function encapsulates: KeyStore SHA-256 lookup (lines 57–62), TOML raw-token compare_digest (lines 65–68), legacy api_key with revocation guard (lines 80–96), and `_validate_namespace()` (lines 104–108). **Implementation note:** the helper must compute `token_hash = hashlib.sha256(token.encode()).hexdigest()` (plain SHA-256, no key — matching `middleware_auth.py:58`) once at the top of the function body, not inside the `if key_store is not None` branch — the revocation guard at `middleware_auth.py:88–92` (which is nested inside `if key_store is not None`, lines 85–92) reuses `token_hash` on the legacy-token path when `key_store` IS present. Leaving `token_hash` inside the inner KeyStore branch and relying on it in a sibling branch of the same `if` block causes a `NameError`. Hoist it above the `if key_store is not None` block. The handler calls this shared function, sets `request.state.namespace = result` on success, and returns 401 on `None`. If the handler inlines the cascade instead, any future auth change silently fails to protect the `/view` endpoint — this is a security drift hazard. All other routes are unchanged.
- `_truncate_graph` logic and the `max_inspection_nodes` / `max_inspection_edges` config keys
- Any existing MCP tools or CLI commands

---

## Known limitations / accepted trade-offs
- **Token exposure (two surfaces):** (1) The validated Bearer token is embedded verbatim in the HTML source — visible in browser DevTools and any page archive. (2) When delivered via `?token=<raw>` query parameter, the raw token also appears in browser history, server access logs, and HTTP referrer headers. Both are acceptable for local/private deployments only. Not appropriate for public or multi-tenant servers. No key-rotation or short-lived-token mechanism is provided in v1 — operators on shared networks should be aware.
- `source_chunk_ids` shown in the side panel are derived from co-occurrence edges incident to the clicked node (union of all incident edges' `source_chunk_ids`), not from the raw mentions table — a practical proxy using existing data; exact per-mention chunk IDs would require a new API field
- The viewer always uses `frequency` salience mode — no UI toggle (deferred)
- Text search, node-click panel, edge hover, truncation banner, and empty-state message are all rendered by client-side JS — not visible to TestClient-based integration tests; verified by manual tests
- Client-side error handling: if the embedded token is valid for `/view` but the subsequent JS `fetch` to `GET /graph/{collection}` fails (401/500/network error), the page behavior is unspecified in v1 — the client may show a blank canvas or a vis-network error. A dedicated fetch-error banner is deferred.

---

## Approach & architecture

The feature adds one new route handler (Interface Adapters), one new Pydantic field (Interface Adapters) piped through a domain inspection object (Entities), and one new package resource file (Frameworks & Drivers). The HTML file is the Presentation layer — it runs in the browser and calls the existing `GET /graph/{collection}` API using the embedded token.

```mermaid
flowchart TD
  P["Presentation — FE<br/>graph_viewer.html<br/>(force-directed canvas, search, side panel)"]
  UC["Use Cases — BE<br/>inspect_collection (graph_inspector.py)"]
  AD["Interface Adapters — BE<br/>get_graph_view route handler<br/>GraphNodeResponse + entity_type<br/>GraphNodeInspection + entity_type"]
  EN["Entities — BE<br/>GraphNodeInspection · CollectionGraphView<br/>EntityType enum (graph_types.py)"]
  FW["Frameworks & Drivers — BE<br/>graph_viewer.html (importlib.resources)<br/>GraphStore.get_all_nodes (LanceDB)<br/>APIKeyMiddleware (auth)"]
  P -->|"Bearer-authed fetch\nGET /graph/{collection}"| AD
  AD --> UC
  UC --> EN
  FW --> AD
```

**Layer map (and role mapping)**

| Layer | Role | Components |
|-------|------|-----------|
| Presentation | **Frontend** | `archon_search/server/graph_viewer.html` (new) |
| Use Cases | Backend | `inspect_collection()` in `graph_inspector.py` — **modified by BE-1** (four constructor call sites gain `entity_type`) |
| Interface Adapters | Backend | `get_graph_view` route handler (new, `routes_graph.py`); `GraphNodeResponse.entity_type` (new field, `schemas.py`); `GraphNodeInspection.entity_type` (new field, `graph_inspector.py`); `_view_to_response` builder (updated, `routes_graph.py`) |
| Entities | Backend | `GraphNodeInspection`, `CollectionGraphView` (`graph_inspector.py`); `EntityType` enum (`graph_types.py`) — no change, just projected through |
| Frameworks & Drivers | Backend | `importlib.resources` resource loading; `APIKeyMiddleware` (`middleware_auth.py`) — **requires modification** (conditional branch for `?token=` path — see BE-2) |

> **Note on layer ownership:** `GraphNodeInspection` is defined in `graph_inspector.py` (Use Cases layer) but the `entity_type` field addition in BE-1 also requires editing `graph_inspector.py`'s constructor call sites — making BE-1 a Use Cases change, not just Interface Adapters. The `_view_to_response` builder lives in `routes_graph.py` (Interface Adapters). The `entity_type` field on `GraphNodeResponse` lives in `schemas.py` (Interface Adapters).

**What changes**
- `GraphNodeInspection` (graph_inspector.py lines ~41–56) gains `entity_type: str` — projected from `GraphNode.entity_type.value`
- `GraphNodeResponse` (schemas.py lines 665–678) gains `entity_type: str`
- `_view_to_response` builder (routes_graph.py) maps the new field
- `_cross_collection_view_to_response` builder (routes_graph.py line ~301) maps the new field — required to avoid a Pydantic ValidationError on `GET /graph/cross-collection` when `entity_type` is a required field
- `routes_graph.py` gets a new `get_graph_view` handler. Register it anywhere — FastAPI resolves `/graph/{collection}/view` (3 segments) unambiguously from `/graph/{collection}` (2 segments); no routing precedence conflict exists between routes with different path-segment counts.
- `middleware_auth.py` gains a module-level `validate_token_and_get_namespace(token: str, request: Request) -> str | None` helper (reads `request.app.state.{key_store, config.namespaces, api_key}`). **Note:** the existing middleware reads from instance attributes (`self._key_store`, `self._namespaces`, `self._api_key` at lines 57/66/79 of `middleware_auth.py`). When the helper is extracted, the middleware's `dispatch` method must switch to passing `request.app.state` instead of `self._*` attributes. The values are equivalent (app.state is the construction-time source); the only exception is unit-test scenarios that build the middleware without a full app — those tests must be updated to set `app.state` equivalents.
- `archon_search/server/graph_viewer.html` is created as a new package resource
- `tests/server/openapi_snapshot.json` is regenerated

**Key decisions (from the brief)**
- Inline a proven force-simulation library, not hand-rolled physics — faster to ship, looks better
- Token embedded at render time, not entered by user — single-step flow; security trade-off accepted for local tool
- Single collection only — cross-collection viewer is a follow-on

---

## Contracts / seams

Boundaries where roles must agree. **Logical, not code.** Changing one requires team agreement. TypeSpec is available (v1.13.0) — HTTP/API seams use TypeSpec HTTP services with emitted OpenAPI; internal seams use core-construct `.tsp` only.

**C1 — Graph data response** *(Interface Adapters ↔ Presentation)*
The existing `GET /graph/{collection}` JSON endpoint. **E2j adds `entity_type: str` to every node.** Frontend JS reads `nodes[].entity_type` to color-code nodes by the five EntityType values. `edges[].weight` drives thickness; `edges[].relationship_type` drives hover tooltip. `truncated`, `node_count`, `edge_count` drive the truncation banner. When `nodes` is empty, JS renders the empty-state message.
— See [`api-contracts/e2j-graph-data-api.tsp`](api-contracts/e2j-graph-data-api.tsp) + [`api-contracts/e2j-graph-data-api.openapi.yaml`](api-contracts/e2j-graph-data-api.openapi.yaml)
- Realised by: BE-1 (schema + builder) · Verified by: BE-1 (integration test), T-1 (integration)

**C2 — Viewer endpoint** *(HTTP client ↔ Backend)*
`GET /graph/{collection}/view` → 200 `text/html`. Auth via `Authorization: Bearer <token>` header **or** `?token=<raw>` query parameter. Self-contained page with token embedded as a JS variable. 422 when graph disabled. 401 when unauthenticated (neither path supplies a valid token). 404 when collection unknown.
All 401 responses — whether middleware-emitted or handler-emitted — must include `WWW-Authenticate: Bearer` header. The handler's inline 401s (invalid ?token=, revoked token) must explicitly set this header.
— See [`api-contracts/e2j-graph-viewer-api.tsp`](api-contracts/e2j-graph-viewer-api.tsp) + [`api-contracts/e2j-graph-viewer-api.openapi.yaml`](api-contracts/e2j-graph-viewer-api.openapi.yaml)
- Realised by: BE-2 · Verified by: BE-2 (unit + integration tests), T-1 (integration)

**C3 — Token extraction** *(Middleware / Query param ↔ View handler)*
`/view` accepts the Bearer token via two paths: (1) `Authorization: Bearer <token>` header (standard; middleware validates before handler runs, handler re-reads from `request.headers["Authorization"].split(" ", 1)[1]`); (2) `?token=<raw>` query parameter (browser-friendly; `/view` is exempt from the middleware's header-presence check when this param is present (exemption scoped by path-pattern `^/graph/[^/]+/view$` + exact query-param key `token` — not substring or endswith); the route handler validates inline via the full three-source, revocation-aware auth cascade from `middleware_auth.py:57–96` (KeyStore SHA-256 lookup, TOML raw-token `compare_digest`, legacy `api_key` with revocation guard) and exits 401 on failure). **Note: this requires adding `/view` to the middleware's exemption logic — a required change, not a non-change.** The validated raw token is then substituted into the HTML via the C4 placeholders.
**Precedence when both present:** if a request to `/view` supplies both an `Authorization: Bearer` header and a `?token=` query parameter, the **header takes priority** — the middleware runs normally (no exemption when `Authorization` header is present), validates the header token, sets `request.state.namespace`, and the handler recovers the raw token from the header for C4 embedding. The `?token=` path is only active when no `Authorization` header is present.
— See [`e2j-token-extraction.tsp`](e2j-token-extraction.tsp) (validated `--no-emit`)
- Realised by: BE-2 · Verified by: BE-2 (integration — token appears in response.text)

**C4 — Handler → HTML template data** *(View handler ↔ HTML resource file)*
The route handler substitutes four placeholder tokens into the HTML file before returning it: `__ARCHON_COLLECTION__`, `__ARCHON_TOKEN__`, `__ARCHON_MAX_NODES__`, `__ARCHON_MAX_EDGES__`. FE-1 must use these exact placeholder strings. Handler reads caps from `request.app.state.config.graph.max_inspection_nodes` and `.max_inspection_edges`. The `__ARCHON_MAX_NODES__` and `__ARCHON_MAX_EDGES__` values are display-only (for the truncation banner "Showing X of Y" label) — they are **not** sent as parameters to `GET /graph/{collection}`. The server enforces truncation independently based on config; the injected values let the HTML show the cap without an extra API call. **Escaping requirement and placeholder form:** `__ARCHON_COLLECTION__` and `__ARCHON_TOKEN__` placeholders must appear **bare** in the HTML JS (no surrounding quotes): `const token = __ARCHON_TOKEN__;`. The handler then substitutes `json.dumps(value)` (which adds the quotes and escapes special characters), producing `const token = "mytoken";`. **If the placeholder is already inside quotes in the HTML, json.dumps produces double-quoting (`""tok""`), a JS syntax error.** FE-1 must use bare placeholders for string values. `__ARCHON_MAX_NODES__` and `__ARCHON_MAX_EDGES__` are integer-typed — the handler substitutes `str(int(value))`, no quotes needed or emitted; FE-1 may use bare placeholder: `const maxNodes = __ARCHON_MAX_NODES__;`.
— See [`e2j-viewer-template-data.tsp`](e2j-viewer-template-data.tsp) (validated `--no-emit`)
- Realised by: BE-2, FE-1 · Verified by: BE-2 (integration — token substituted), T-1 (integration)

---

## Scenarios #tester-role

| id | Scenario (Given / When / Then) |
|----|--------------------------------|
| **S1** | **Given** graph enabled, collection has nodes and edges, valid token · **When** `GET /graph/{collection}/view` is called · **Then** 200 `text/html`, page contains `<canvas`, bearer token literal appears in page source, no external URLs |
| **S2** | **Given** page loaded with a non-empty graph · **When** user clicks a node · **Then** side panel shows `entity_name`, `entity_type`, `chunk_count`, and co-occurrence chunk IDs. **Definition of correct chunk IDs:** the union of `source_chunk_ids` from all edges where `from` or `to` equals the clicked node's ID. This is a display proxy; it may include chunks where the node does not appear in isolation. The T-2 manual test verifies that clicking a known node shows the expected union (pre-check the expected IDs from `GET /graph/{collection}` before the manual test run). |
| **S3** | **Given** page loaded · **When** user hovers an edge · **Then** tooltip shows `relationship_type` value |
| **S4** | **Given** page loaded with multiple nodes · **When** user types in the search box · **Then** only nodes whose name contains the substring (case-insensitive) remain visible; incident edges to filtered-out nodes are hidden |
| **S5** | **Given** collection has more nodes than `max_inspection_nodes` · **When** page loads · **Then** truncation banner reads "Showing X of Y nodes and A of B edges" with correct counts from API response |
| **S6** | **Given** graph enabled, collection exists, no documents ingested (empty graph) · **When** page loads · **Then** canvas renders with "No entities found — ingest some documents first" message; no JS errors |
| **S7** | **Given** `graph.enabled = false` · **When** `GET /graph/{collection}/view` called with valid token · **Then** 422 `{"detail": "graph inspection requires [graph] enabled=true in server config"}` |
| **S8** | **Given** no `Authorization` header · **When** `GET /graph/{collection}/view` called · **Then** 401 with `WWW-Authenticate: Bearer` header |
| **S9** | **Given** wrong/invalid token (via header or `?token=` query param) · **When** `GET /graph/{collection}/view` called · **Then** 401 with `WWW-Authenticate: Bearer` header |
| **S10** | **Given** collection does not exist · **When** `GET /graph/{collection}/view` called with valid token · **Then** 404 `{"detail": "collection not found"}` |
| **S11** | **Given** page loaded · **Then** HTML source contains no `<script src="https://...">`, `<link href="https://...">`, or XHR/fetch to external hosts — works offline after first data load |
| **S12** | **Given** valid token (64-char hex) · **When** page loads · **Then** token literal appears in HTML `<script>` block as a JS string value in the form `= "tok64chars";` — the json.dumps substitution wraps the token in double quotes. The placeholder `__ARCHON_TOKEN__` appears bare in the raw HTML file (no surrounding quotes). |
| **S13** | **Given** page source · **Then** no custom Web Components, no `shadowRoot`, no `type="module"` blocking HTMX swaps — markup is HTMX-compatible |
| **S14** | **Given** page fully loaded with graph data · **When** server is stopped · **Then** already-rendered graph remains interactive (pan, zoom, click, search) — client-side only |
| **S15** | **Given** `entity_type` now present in `GET /graph/{collection}` response · **When** any caller fetches the endpoint · **Then** every node object includes `entity_type` with a valid EntityType string value |

---

## Frontend — Presentation #frontend-role

**Scope:** the single-file `archon_search/server/graph_viewer.html` resource — force-directed rendering, interactive features, HTMX-compatible markup. The server is responsible for substituting the four C4 placeholder tokens before returning the file; the HTML file must use them correctly.
**Owns layer:** Presentation.

**Tasks** *(checkable in the Task Breakdown)*
- Presentation: FE-1 — create `graph_viewer.html` with force layout, search, side panel, empty/truncation states

**Done when**
- [ ] A browser opening the view URL sees a force-directed canvas with nodes and edges — S1
- [ ] Nodes are sized by salience and colored by entity_type — S1, S15
- [ ] Edge thickness reflects weight; edge hover shows relationship_type — S3
- [ ] Clicking a node shows the inspect panel — S2
- [ ] Typing in the search box filters nodes — S4
- [ ] Truncation banner shows correct counts — S5
- [ ] Empty-state message shown when no nodes — S6
- [ ] Page contains no external network requests — S11
- [ ] HTMX-compatible markup (no Web Components / Shadow DOM) — S13

---

## Backend — Entities · Use Cases · Adapters · Frameworks #backend-role

**Scope:** schema addition (`entity_type` field), new route handler (`get_graph_view`), HTML resource loading via `importlib.resources`, placeholder token substitution. Writes both unit and integration tests for its tasks.
**Owns layers:** Entities, Use Cases, Interface Adapters, Frameworks & Drivers.

**Tasks by layer** *(checkable in the Task Breakdown)*
- Entities: BE-1 — `entity_type` in `GraphNodeInspection` + `GraphNodeResponse` + `_view_to_response` builder
- Interface Adapters: BE-2 — `get_graph_view` route handler (resource load, guard, token inject, response)
- Frameworks & Drivers: BE-2 also (importlib.resources)

**Done when**
- [ ] `GET /graph/{collection}` response includes `entity_type` on every node — S15
- [ ] OpenAPI snapshot regenerated and test passes — S15
- [ ] `GET /graph/{collection}/view` returns 200 `text/html` with token in body — S1, S12
- [ ] Graph-disabled, no-auth, and unknown-collection guards work correctly — S7, S8, S9, S10
- [ ] HTML contains no external URLs — S11
- [ ] `middleware_auth.py` modified: graph-view `?token=` path is exempt via pattern match + exact query-param key check — S8, S9 (query-param path)

---

## Tester #tester-role

**Scope:** the tester owns **integration and manual** tests (T-1, T-2) plus the project **close-out** (T-3). **Unit and integration** tests (dev-owned) belong to the implementing dev.

**Tasks** *(checkable in the Task Breakdown)*
- T-1 — integration: server HTTP contract (5 scenarios)
- T-2 — manual: JS interactive features (6 cases)
- T-3 — close-out & acceptance fact-check

**Allocation** — each scenario at the cheapest level that proves it

| Scenario | Cheapest level |
|----------|----------------|
| S7, S8, S9, S10, S15 | unit + integration (dev-owned, BE-1/BE-2) |
| S1, S11, S12, S13 | integration (TestClient + HTML string assertion) |
| S2, S3, S4, S5, S6, S14 | manual (JS-rendered, no browser in test harness) |

*Note: S5 (truncation banner) and S6 (empty state) are JS-rendered after the page fetches `/graph/{collection}`. The TestClient returns the HTML shell only; the rendered states require a real browser. They are manual.*

---

## Documentation update

- [ ] `Documentation/Backlog/e2j-graph-viewer-brief.md` — no changes needed (source brief)
- [ ] `Documentation/Backlog/e2j-graph-viewer-team-plan.md` — this file
- [ ] `Documentation/Architecture/600_api_reference_or_public_interface.md` — add `GET /graph/{collection}/view` entry; update `GET /graph/{collection}` node-field table to include `entity_type`
- [ ] `Documentation/Architecture/110_component_catalog_and_layer_breakdown.md` — add `graph_viewer.html` as a Presentation component
- [ ] `Documentation/Architecture/100_system_architecture_overview.md` — one-line mention of graph viewer endpoint
- [ ] `CLAUDE.md` — update graph subsystem bullet to mention the `/view` endpoint
- [ ] `archon-search.toml.example` — no changes (no new config keys)
- [ ] `BREAKING.md` — no entry needed (`entity_type` is additive; viewer is a new endpoint)

---

## Open questions

All resolved. Status: `planned`.

| id | Area | Decision |
|----|------|----------|
| **Q1** | auth / UX | **`?token=` query parameter (Option A).** `/view` is exempted from the middleware's header-presence check when `?token=` is supplied. The route handler validates the token inline via the full three-source, revocation-aware auth cascade from `middleware_auth.py:57–96` and exits 401 on failure. Tokens appear in browser history — acceptable for local/private deployments. |
| **Q2** | frontend | **vis-network (~500 KB inlined).** User choice. Batteries-included physics, node drag, zoom, and tooltips; zero glue-code for rendering. Adds ~500 KB to the Python wheel. **Required before FE-1 starts:** pin the exact vis-network version (e.g. v9.1.9), document the CDN source URL and expected SHA-256 hash of the minified bundle. The hash must be committed alongside the HTML file and checked in `test_viewer_html_contains_canvas_and_placeholders` so that an inadvertent library swap fails the build. Inlining a different version than pinned is a build error. **Hash gate implementation:** commit a sidecar file `archon_search/server/graph_viewer.html.sha256` containing the expected SHA-256 hex digest of the bundled vis-network script block (not the full HTML, which changes with placeholders). The `test_viewer_html_contains_canvas_and_placeholders` unit test reads this sidecar, extracts the vis-network script block by selecting `<script id="vendor-vis-network">` (FE-1 must emit the vendored library in this element — see FE-1 task). The test reads the element's text content, hashes with `hashlib.sha256`, and asserts against the sidecar value. |
| **Q3** | frontend | **`importlib.resources`.** `files("archon_search.server").joinpath("graph_viewer.html").read_bytes()` — clean separation, already covered by hatchling wheel config. |
| **Q4** | frontend | **Fixed seed.** `mulberry32` PRNG (5 lines of inline JS) seeded by the collection name replaces `Math.random` before the simulation starts — same data → same layout on every reload. |
| **Q5** | backend | **Yes — call `pipeline.get_collection_meta(collection, namespace=ns)`.** Returns 404 before serving HTML if the collection does not exist; consistent with all other graph routes. |

---

## Task Breakdown

Single-role tasks in execution order, grouped into **vertical slices** (set using `vertical-slicer`).

### Dependency graph

```mermaid
flowchart LR
  K1([K1 · align])
  subgraph P1["Phase 1 · Viewer page loads and renders graph (walking skeleton)"]
    BE1["BE-1\nentity_type\nin schema"]
    FE1["FE-1\ngraph_viewer\n.html"]
    BE2["BE-2\n/view route\nhandler"]
    T1["T-1\ne2e HTTP\ncontract"]
    T2["T-2\nmanual JS\nfeatures"]
  end
  T3([T-3 · close-out])

  K1 --> BE1
  K1 --> FE1
  BE1 --> FE1
  BE1 --> BE2
  FE1 --> BE2
  BE2 --> T1
  FE1 --> T2
  T1 --> T3
  T2 --> T3
  BE1 --> T3
  BE2 --> T3
  FE1 --> T3
```

### Phase 0 · Kickoff *(prerequisite; the one cross-cutting step)*
- [x] **K1** — Contracts and Scenarios agreed; Q1–Q5 resolved (see Open Questions section) #team
    - — · 1.0h
    - completes C1, C2, C3, C4
    - Tests

### Phase 1 · Viewer page loads and renders graph *(the walking skeleton: the whole feature is one end-to-end behavior)*

- [x] **BE-1** — Add `entity_type: str` to `GraphNodeInspection`, `GraphNodeResponse`, and `_view_to_response` builder; regenerate OpenAPI snapshot; also update `_cross_collection_view_to_response` (routes_graph.py line ~301) to populate `entity_type` — this is a second `GraphNodeResponse` construction site that fails with a Pydantic `ValidationError` (→ 500) if `entity_type` is a required field and the cross-collection path is not updated #backend-role
    - Interface Adapters · 3.0h
    - needs K1 · completes C1, S15
    - **Implementation note:** `entity_type` is absent from all four `GraphNodeInspection` constructor call sites in `graph_inspector.py` (lines ~253, 363, 526, 535):
      - **Line ~363 and ~535**: standard and community paths — have a `GraphNode` in scope; pass `entity_type=node.entity_type.value`.
      - **Line ~526**: cross-collection merge path (Out of Scope per Scope section) — has a `GraphNode` in scope; pass `entity_type=node.entity_type.value`. **Decision required:** editing an OOS site is the safest option (avoids blank field on `/graph/cross-collection`); the alternative is adding `entity_type: str = ""` as a default field, which silently emits wrong colors from cross-collection. Recommend: add as required field, edit all four sites.
      - **Line ~253**: `_apply_tfidf` path — operates on already-built `GraphNodeInspection` objects (no `GraphNode` in scope); **`node.entity_type.value` would crash here**. This site must instead preserve `entity_type` from the input `GraphNodeInspection` (e.g. `entity_type=existing_inspection.entity_type`).
      - **Layer note:** `graph_inspector.py` is the Use Cases layer — BE-1 is a Use Cases change, not just Interface Adapters. Adjust time estimate from 3.0h accordingly.
    - Tests
        - #unit_test — `test_node_inspection_includes_entity_type` — `GraphNodeInspection` carries `entity_type` from the underlying `GraphNode.entity_type.value`
        - #unit_test — `test_view_to_response_maps_entity_type` — `_view_to_response` in `routes_graph.py` propagates `entity_type` onto `GraphNodeResponse`
        - #integration_test — `test_graph_response_includes_entity_type` — `GET /graph/{col}` JSON response includes `entity_type` on every node with a valid EntityType value
        - #integration_test — `test_cross_collection_graph_response_includes_entity_type` — `GET /graph/cross-collection` (if enabled in test config) returns 200 with `entity_type` on every merged node, not a 500 Pydantic error

- [x] **FE-1** — Create `archon_search/server/graph_viewer.html`: inlined **vis-network** library (~500 KB, no CDN), force-directed canvas (nodes sized by salience, colored by entity_type; edges sized by weight), edge hover tooltip, click-to-inspect side panel, text search, truncation banner, empty-state message, HTMX-compatible markup, fixed-seed layout (`mulberry32` PRNG seeded by `__ARCHON_COLLECTION__` value replaces `Math.random` before simulation starts), four C4 placeholder tokens (bare in JS — no surrounding quotes; the server-side json.dumps adds quotes for string values) #frontend-role
    - Presentation · 8.0h
    - needs K1, BE-1 · completes S2, S3, S4, S5, S6, S11, S13
    - **Before starting:** team must agree on vis-network version, source URL, and SHA-256 integrity hash (see Q2). This is a prerequisite for FE-1.
    - **Vendor script block:** the inlined **vis-network** library (~500 KB, no CDN) — must be wrapped in `<script id="vendor-vis-network">…</script>`. This id is a contract between FE-1 and the test suite (hash gate and external-URL boundary tests select by this id, not by "largest block" heuristic).
    - Tests
        - #unit_test — `test_viewer_html_is_loadable_as_package_resource` — `importlib.resources.files("archon_search.server").joinpath("graph_viewer.html").read_bytes()` succeeds and returns non-empty bytes
        - #unit_test — `test_viewer_html_contains_canvas_and_placeholders` — HTML file contains `<canvas`, `__ARCHON_TOKEN__`, `__ARCHON_COLLECTION__`, `__ARCHON_MAX_NODES__`, `__ARCHON_MAX_EDGES__`
        - #integration_test — `test_viewer_html_no_external_urls` — response body for a valid `/view` request: (1) strip the `<script id="vendor-vis-network">` block (selected by the stable `id` attribute — this is how FE-1 must emit it per the FE-1 contract); (2) in the remaining HTML, assert no `<script src="https://`, no `<link href="https://`, no `fetch("https://` or `fetch('https://`, no `new XMLHttpRequest` — this prevents false positives from vis-network's own internal URL strings
        - #integration_test — `test_viewer_htmx_compatible` — response body: (1) contains no `shadowRoot`; (2) contains no `customElements.define(`; (3) the document's own `<script>` elements (excluding the vendored vis-network block) contain no `type="module"` attribute — scope this check to the document structure, not the entire body, to avoid false-positive failures from the vendored library; (4) contains no `<template` element (shadow DOM slot pattern). The `<[a-z]+-[a-z]+` regex pattern is too coarse (false positives on attributes, false negatives on JS-registered components) and must NOT be used.
        - #integration_test — `test_viewer_html_overrides_math_random` — HTML source contains `Math.random` reassignment before vis-network initialization (search for `mulberry32` or `Math.random =` in response body, confirming the PRNG override is present)

- [x] **BE-2** — New `GET /graph/{collection}/view` route handler: (1) token resolution — `?token=` query param validated via the full three-source revocation-aware auth cascade from `middleware_auth.py:57–96` (using the shared `validate_token_and_get_namespace(token, request)` helper); fallback to `Authorization` header re-read (token recovery only — middleware already validated on this path); → 401 with `WWW-Authenticate: Bearer` if neither supplies a valid token; (2) graph-enabled guard → 422; (3) collection existence check via `pipeline.get_collection_meta(collection, namespace=ns)` → 404 if unknown; (4) `importlib.resources` HTML load; (5) C4 placeholder substitution; (6) `Response(content=..., media_type="text/html")`; register in `routes_graph.py` (no ordering constraint vs. `GET /graph/{collection}` — paths differ in segment count) #backend-role
    - Interface Adapters · 3.0h
    - needs BE-1, FE-1 · completes C2, C3, C4, S1, S7, S8, S9, S10, S12
    - **Requires middleware change (owned by BE-2):** modify `middleware_auth.py` to add the conditional-branch exemption described in "What does NOT change" and C3. This is an explicit BE-2 deliverable, not a side-note — it must be completed before BE-2 integration tests can run. Add to BE-2 "Done when" list: middleware exempts graph-view requests with `?token=` correctly.
    - **Stub strategy for pre-FE-1 testing:** extract the resource load into a module-private helper `_load_viewer_html() -> bytes` in the handler module. Integration tests monkeypatch this function directly (e.g. `monkeypatch.setattr("archon_search.server.routes_graph._load_viewer_html", lambda: stub_bytes)`). This is the **primary** recommended seam — monkeypatching `importlib.resources.files` directly does not work because it returns a `Traversable` interface; raw filepath return fails `.read_bytes()`. The stub bytes must contain the four C4 placeholders and `<canvas`. Without this seam, BE-2 tests are blocked on FE-1.
    - Tests
        - #unit_test — `test_view_graph_disabled_returns_422` — `config.graph.enabled=False` → 422 with exact detail string
        - #unit_test — `test_view_no_auth_checked_before_graph_disabled` — invalid `?token=` + `config.graph.enabled=False` → 401 (not 422); auth must be checked before graph-enabled guard to avoid disclosing server config to unauthenticated callers
        - #unit_test — `test_view_unknown_collection_returns_404` — unknown collection → 404
        - #unit_test — `test_view_collection_name_is_json_encoded` — a collection name containing `"` and `<` (e.g. `foo";<bar>`) appears JSON-encoded (escaped) in the response body, not raw
        - #integration_test — `test_view_token_injected_in_response` — `api_key` literal appears verbatim in `response.text`
        - #integration_test — `test_view_returns_html_content_type` — `Content-Type: text/html` header on 200 response
        - #integration_test — `test_view_invalid_token_returns_401` — invalid token in `Authorization: Bearer <bad>` header → 401 with `WWW-Authenticate: Bearer` header (covers S9)
        - #integration_test — `test_view_no_auth_returns_401_with_www_authenticate` — no auth header and no `?token=` → 401 with `WWW-Authenticate: Bearer` header (covers S8)
        - #integration_test — `test_view_query_param_token_happy_path` — valid `?token=<raw>` query param → 200 `text/html` with token in body (covers C3 path 2)
        - #integration_test — `test_view_query_param_invalid_token_returns_401` — invalid `?token=<bad>` query param → 401 with `WWW-Authenticate: Bearer` header (covers S9 on query-param path)
        - #integration_test — `test_view_query_param_keystore_revoked_token_returns_401` — a KeyStore-managed token removed from `keys.json` → 401 with `WWW-Authenticate: Bearer` header (covers `middleware_auth.py:57–62`)
        - #integration_test — `test_view_query_param_legacy_revoked_token_returns_401` — a token matching the legacy `api_key` that has been marked revoked/expired → 401 with `WWW-Authenticate: Bearer` header (covers `middleware_auth.py:80–96`)
        - #integration_test — `test_view_query_param_wrong_namespace_returns_404` — `?token=` from namespace A, collection only exists in namespace B → 404 (verifies namespace resolution is correctly set on `request.state` for the exempt path)
        - #integration_test — `test_view_header_takes_priority_over_query_param` — request with both valid `Authorization: Bearer` header and `?token=<invalid>` → 200 (header validated, invalid ?token= ignored because header takes priority)
        - #unit_test — `test_middleware_graph_view_token_param_is_exempt` — a request to `/graph/test-col/view?token=abc` bypasses the header-presence check; a request to `/graph/test-col/view` without `?token=` is NOT exempt (still requires Bearer header)
        - #unit_test — `test_middleware_exact_path_scope` — a request to `/other/view?token=abc` is NOT exempt (path pattern only matches `/graph/{collection}/view`)

- [x] **T-1** — integration: server HTTP contract for `GET /graph/{collection}/view` #tester-role
    - — · 3.0h
    - needs BE-2 · completes S1, S7, S8, S9, S10, S11, S12, S13
    - Tests
        - [x] #integration_test — `test_e2j_view_happy_path` — 200 + `text/html` + `api_key` in body + `<canvas` in body
        - [x] #integration_test — `test_e2j_view_graph_disabled_422` — 422 with exact detail string
        - [x] #integration_test — `test_e2j_view_no_auth_401` — no `Authorization` header → 401 + `WWW-Authenticate: Bearer`
        - [x] #integration_test — `test_e2j_view_collection_not_found_404` — unknown collection → 404
        - [x] #integration_test — `test_e2j_view_no_external_urls` — response body has no external URL patterns

- [x] **T-2** — Manual: JS interactive features in a real browser #tester-role
    - — · 2.0h
    - needs FE-1 · completes S2, S3, S4, S5, S6, S14
    - Tests
        - #manual_test — Node click shows inspect panel — click a known node (pre-verified via `GET /graph/{collection}`) → panel shows entity_name, entity_type, chunk_count, and the union of source_chunk_ids from all incident edges
        - #manual_test — Edge hover shows relationship type — hover an edge → tooltip displays relationship_type value (e.g. "calls", "synonym_of")
        - #manual_test — Search filters nodes — type partial name in search box → only matching nodes visible; non-matching nodes hidden; incident edges hidden
        - #manual_test — Truncation banner — **Setup:** set `max_inspection_nodes` to a small value (e.g. 3) in `archon-search.toml` and ingest a handful of documents so that `node_count` from `GET /graph/{collection}` exceeds it (this exercises the same code path as the 5000-node default without requiring a large corpus). **Test:** open `/view` → banner reads "Showing X of Y nodes and A of B edges" where X = `data.nodes.length` (number of nodes the API actually returned in the `nodes` array) and Y = `data.node_count` (total entity count from the API response).
        - #manual_test — Empty state message — open collection with no ingested documents → "No entities found — ingest some documents first" shown on canvas
        - #manual_test — Offline behavior — load page fully, disconnect server → graph remains interactive (pan, zoom, click, search)
        - #manual_test — Fetch error state (no scenario ID — bonus test) — load the page with a valid token, then revoke the token on the server; trigger a graph refresh (or reload) → page shows an error indicator (or graceful empty state) rather than a silent blank canvas

### Phase 2 · Close-out
- [ ] **T-3** — Project close-out & acceptance fact-check #tester-role
    - — · 4.0h
    - needs BE-1, BE-2, FE-1, T-1, T-2 · completes (acceptance gate)
    - Tests
    - Duties
        - Update all documentation per the "Documentation update" section — `600_api_reference`, `110_component_catalog`, `100_architecture_overview`, `CLAUDE.md`, user manual.
        - Fix all build / compiler warnings, if any.
        - Run the full test suite; fix every failing test, including any unrelated to this feature.
        - Validate every Acceptance criterion one-by-one with a fact check — no assumptions; confirm each is genuinely done.

**Critical path:** K1 → BE-1 → BE-2 → T-1 → T-3. FE-1 runs in parallel with BE-1 after K1 and gates BE-2 and T-2. T-2 runs in parallel with T-1.
