# Feature Brief: E2j — Graph Viewer (Single-File Local UI)

## Problem
Operators and developers running archon-search have no way to visually explore their knowledge graph — they can only download raw data files and open them in separate tools, which is too slow for day-to-day debugging or exploration.

## Goal
Opening `GET /graph/{collection}/view` in a browser renders an interactive, self-contained graph — nodes, edges, search, and click-through inspection — with no installation, no external tools, and no extra steps.

## Users & Context
Developers and operators who have archon-search running locally (or on a private server), have a Bearer token in hand, and want to understand what's in their graph — which entities exist, how they connect, which ones dominate.

## Core Flow
1. User navigates to `GET /graph/{collection}/view` in a browser, presenting their Bearer token.
2. The server responds with a single, self-contained HTML page — the token is injected into the page automatically so no second login is needed.
3. The page fetches the collection's graph data (`GET /graph/{collection}`) and renders a force-directed canvas: nodes sized by salience, colored by entity type, edges weighted by co-occurrence count.
4. If the graph was truncated (too large to show in full), a banner appears at the top explaining how many nodes/edges were omitted.
5. User types in the search box to highlight or filter nodes by name.
6. User clicks a node to open a side panel showing: entity name, type, chunk count, and the source chunk IDs that mention it.
7. If `graph.enabled = false`, the endpoint returns the same 422 error as the underlying data endpoints — no special handling needed.

## In Scope
- Single-file HTML served directly by the archon-search server — no separate process, no static file hosting
- Force-directed layout using an inlined force-simulation library (~15 KB embedded in the file — no CDN call, no npm, no build step)
- Bearer token embedded in the page at render time so the graph loads without a second login prompt
- Nodes: sized by salience score, colored by entity type (`person`, `concept`, `system`, `event`, `code_symbol`)
- Edges: thickness proportional to co-occurrence weight; relationship type visible on hover
- Click-to-inspect side panel: shows `entity_name`, `entity_type`, `chunk_count`, `source_chunk_ids`
- Text search: filters visible nodes by name as the user types
- Truncated banner: displayed when the graph hit the server's node/edge cap, showing total vs. displayed counts
- `graph.enabled = false` handling: returns the same 422 the data endpoints return — no custom error page needed
- HTMX-compatible markup throughout (required for E8 admin UI reuse)
- Zero external network requests — works offline once the page loads
- Bundled into the Python package as a resource file — no separate deployment

## Out of Scope
- **Cross-collection merged view** — the cross-collection API exists but adding a collection-picker and merged-graph rendering doubles the scope; defer to a follow-on
- **Salience mode selector** — the viewer always uses the server's default (frequency) salience; switching modes is a follow-on
- **Impact / blast-radius drill-down** — clicking a `code_symbol` node to see callers/callees is a natural extension but not part of the minimum acceptance bar
- **Community highlighting** — visualizing Leiden communities as node clusters is deferred
- **PNG / SVG export** from the viewer — users can use the existing `GET /graph/{collection}?format=graphml` for export
- **E8 admin UI integration** — E2j ships as a standalone viewer; E8 will reuse its markup, but that wiring is E8's scope

## Key Decisions
- **Inline a proven force-simulation library, not hand-rolled physics**: Writing a force-directed layout from scratch risks weeks of tuning for a mediocre result. Embedding a well-tested ~15 KB simulation library satisfies every stated constraint (no CDN, no npm, no build step, single file) while shipping faster and looking better.
- **Token embedded at render time, not entered by the user**: The server injects the caller's Bearer token into the HTML as a script-tag variable. This is the only flow that loads the graph without any extra steps, and the security trade-off (key visible in page source) is acceptable for a local developer tool.
- **Single collection only**: The spec defines one endpoint (`GET /graph/{collection}/view`). Cross-collection viewer adds a collection-picker UI and a different URL shape — out of scope for v1.

## Edge Cases & Constraints
- **Empty graph** (graph enabled, no ingest yet): `GET /graph/{collection}` returns `nodes: [], edges: [], truncated: false`. The viewer renders an empty canvas with a "No entities found — ingest some documents first" message.
- **Truncated graph**: A banner at the top of the page reads "Showing X of Y nodes and A of B edges — graph is too large to display in full." No interactive way to load more (that's a follow-on).
- **`graph.enabled = false`**: `GET /graph/{collection}/view` returns 422 — same response as the underlying data endpoints. No special viewer error state needed.
- **Large token in page source**: The Bearer token is a short string (a few dozen characters). Injecting it into a `<script>` tag is safe for local/private deployments. The brief notes this is not appropriate for a publicly accessible or multi-tenant deployment.
- **HTMX compatibility**: The canvas element and surrounding layout must use standard HTML attributes and event handling compatible with HTMX partial-page replacement. No custom Web Components or Shadow DOM that would break HTMX swaps.

## Open Questions
- Which specific d3-force build to inline: the standalone `d3-force` ESM bundle, or a hand-selected subset? Affects file size and whether `import` syntax needs a shim.
- How to bundle the HTML file into the Python package: `importlib.resources` (Python 3.9+, clean) vs. embedding as a multi-line string literal in the route handler. The former is cleaner; the latter avoids any packaging edge cases.
- Node layout stability: should the force simulation use a fixed random seed so the graph looks the same on every reload, or is a fresh layout each load acceptable?
- Which `salience` query parameter value should the view endpoint request from `GET /graph/{collection}`? Default (frequency) is fine for v1; document the choice.

## Future Iterations
- **Cross-collection merged view** — a `GET /graph/cross-collection/view?collections=a,b` endpoint using the same HTML template with a collection-selector dropdown
- **Salience mode selector** — a toggle in the UI to switch between `frequency`, `tfidf`, and `importance` modes and re-fetch
- **Impact / blast-radius drill-down** — click a `code_symbol` node to fetch and overlay its callers/callees from `GET /graph/{collection}/impact/{symbol}`
- **Community overlay** — color nodes by Leiden community membership; toggle community boundaries on/off
- **E8 admin UI panel** — this viewer becomes the graph tab in the E8 admin panel; same markup, HTMX-wired into the shell

## Recommendation
Build this now — every competing tool ships a visual graph surface and archon-search has none. The hardest part is the force-layout feel: a graph that clumps or jitters looks broken even if the data is right, so the inline-library choice is load-bearing. The one thing that must not be compromised is the zero-external-requests constraint — if the file ever loads from a CDN, it breaks offline use and violates the E8 integration contract.
