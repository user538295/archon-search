**Purpose**: Document the accessibility (a11y) and internationalization (i18n) surface of `archon-search`, a backend-only service.
**Audience**: Maintainers, integrators building CLIs/UIs on top of the HTTP and MCP APIs.
**Status**: Stable
**Last reviewed**: 2026-09-07
**Next review**: 2026-12-07

# Accessibility and Internationalization

`archon-search` ships no graphical user interface. It exposes three surfaces: a Click-based CLI (`archon-search …`), an HTTP control plane (FastAPI), and an MCP endpoint that mirrors the HTTP surface. This document records the honest, verified state of a11y and i18n across those surfaces.

## Principles

1. **No GUI, so a11y is CLI-scoped.** Frontend a11y concerns (WCAG, ARIA, screen-reader semantics, keyboard navigation) do not apply at the service boundary. They are the responsibility of any client that wraps these APIs.
2. **English only by design in v1.** All operator-facing strings — log messages, CLI output, HTTP/MCP error bodies, telemetry `error_kind` identifiers — are English. No translation layer, no locale negotiation, no `Accept-Language` handling.
3. **CLI output is plain text, machine-parseable as well as human-readable.** No ANSI color, no Unicode box-drawing, no terminal progress bars.
4. **Avoid color-only signaling — a CLI-scoped guarantee today.** Errors are distinguished by being written to stderr, not by color or symbols. This principle is *enforced* only on the CLI; the one browser-rendered surface this service serves (the graph viewer) does not meet it — see "Graph viewer: colour-only relationship signalling" below.
5. **Structured over decorated.** Status, errors, and progress are exposed as structured data (JSON state file, JSON HTTP bodies) rather than visual cues.

## Scope

This file is the placeholder for the 220 slot in the documentation series. The frontend a11y series (300–320) is **not applicable** to this project and is intentionally absent.

## CLI accessibility

Verified against `archon_search/cli/*.py`:

- All CLI output goes through `click.echo(...)`. Searching the CLI tree shows zero uses of `click.style`, `click.secho`, `fg=`, `bold=`, or `tqdm`. The CLI is not styled and emits no ANSI escape sequences.
- Errors are routed to stderr via `click.echo(..., err=True)` (see e.g. `cli/ingest.py`, `cli/config_cmd.py`, `cli/sync.py`, `cli/install_cmd.py`). stdout carries success/result output only, which keeps output pipeable and parseable.
- Because no styling is applied, output is identical on a TTY and when redirected — there is no TTY-only rendering path to disable.
- There is no terminal progress bar. Long-running ingest/reindex jobs report progress via the indexing state file managed by `archon_search/progress.py` (`IndexingStateStore` writes `~/.archon-search/.indexing_state.json`) and via the HTTP `/indexing-state` and `/jobs/{job_id}` endpoints. Clients — including screen readers driving a shell — can poll those endpoints without parsing animated terminal output.

The consequence for assistive technology: any screen reader or accessibility tool that can read plain stdout/stderr can consume the CLI without special handling.

## HTTP and MCP accessibility

The HTTP and MCP surfaces return structured JSON (see `Architecture/520_api_design_and_contracts.md` and `Architecture/600_api_reference_or_public_interface.md`). Accessibility for end users happens entirely in the calling client; the service has no presentation layer to make accessible. Error responses from the `routes_*.py` modules are JSON objects with English `detail` strings (FastAPI serialises raised `HTTPException` instances). The auth middleware (`server/middleware_auth.py`) is the one exception: it returns bare-body `401` responses with only a `WWW-Authenticate: Bearer` header and no JSON body. These responses are intended for developers and operators, not end users.

## Graph viewer: colour-only relationship signalling

`GET /graph/{collection}/view` serves `archon_search/server/graph_viewer.html`, a vis-network page — the one browser-rendered surface in the tree, and the reason principle 1's "no GUI" is a near-truth rather than a fact.

The viewer distinguishes the nine `relationship_type` values **by colour** (`RELATIONSHIP_COLORS`, with `DEFAULT_EDGE_COLOR` for `related_to` and for a missing type). The only non-colour cues are an arrowhead on directional edges — which separates directional from undirected (`related_to`, `synonym_of`), *not* one type from another — and the per-edge hover tooltip carrying the raw `relationship_type` string, which is the honest text fallback and the only cue that fully disambiguates.

**A mandatory per-type dash pattern was considered and rejected.** The multilingual-graph-extraction change that introduced type colouring specified the dash as an *optional* secondary cue, and the shipped viewer took the option: colour plus arrowhead, no dash. Making the dash mandatory would satisfy principle 4 for this surface, but it is a viewer-wide accessibility decision — it changes how every edge renders, including the def/ref code-graph edges that predate that change — so it does not belong as a side effect of an edge-colouring change owned by a different feature. Deferred deliberately, recorded here rather than dropped.

Consequences for a reader with a colour-vision deficiency, stated plainly: all nine types are distinguishable only by hovering each edge individually — the arrowhead splits directional from undirected but never identifies which type within either group. There is no legend and no relationship-type filter (both were explicitly out of scope). The remedy, when it is taken up, is a per-type dash pattern plus a legend — not a colour-palette swap, which would move the problem rather than remove it.

## Internationalization

- Logs, CLI messages, and HTTP/MCP error bodies are English. There is no message catalog, no gettext, no locale switch.
- Telemetry `error_kind` is a closed set of English identifiers defined in `archon_search/telemetry/entry.py`: `empty_query`, `slot_out_of_range`, `timeout`, `internal_error`, `validation_error`, `other`. These are stable API identifiers, not user-facing copy, and must not be translated.
- Timestamps are ISO 8601 in UTC (see `progress.py` and telemetry entries). This is locale-neutral by construction.
- For the broader error model and `error_kind` semantics, see `Architecture/140_error_handling_strategy.md`.

### Corpus language vs interface language

These are two different things and only one of them is English. Conflating them is the mistake this section exists to prevent, and it is the section the two graph guides — [`OperatorGuide/60_graph_operations.md`](../OperatorGuide/60_graph_operations.md) and [`UserManual/65_graph_search.md`](../UserManual/65_graph_search.md) — cross-reference for the distinction.

- **The interface is English by design.** Everything above this heading — logs, CLI copy, HTTP/MCP error bodies, telemetry `error_kind` identifiers — is English, with no message catalog and no locale negotiation. That is a deliberate v1 scope decision, not a gap awaiting work.
- **The corpus is unrestricted.** Nothing in the ingest path rejects, downgrades or warns about a non-English document. Language detection (fasttext `lid.176.ftz`, enabled by `[database].multilingual = true`) *tags* a document's language; it never gates it. Prose graph extraction runs through a multilingual checkpoint — `paths.GRAPH_NER_MODEL_NAME` = `knowledgator/gliner-relex-multi-v1.0` — so a Hungarian or French document produces entity nodes, mentions and typed relations exactly as an English one does. There is no English-only restriction anywhere in the graph path and, correspondingly, no English-only disclosure on any surface.
- **Quality varies by language, and that is a quality gap, not a supported/unsupported line.** Span quality, entity recall and relation precision are all a property of the checkpoint's training distribution and differ per language. A language that extracts measurably worse than English ships with a *recorded* gap rather than a blocked feature: the engine still works, the benefit is narrower. Per-language findings are recorded in the review notes at the end of `tests/test_graph_ner_real_artifact_lane.py`, not on the wire.
- **There is no runtime channel for that gap, deliberately.** `provider_notes` — the field that once carried permanent, non-actionable disclosures on `GET /status` — was removed and has no successor (ADR 12). `provider_warnings`, the only remaining channel, is the sole input to `checks.models` on `GET /ready`, so anything permanent and unactionable placed there would pin that check to `warn` for the life of the deployment. A per-language quality gap is exactly that shape, so it lives in documentation and in test records instead. Restoring a disclosure channel is a deliberate decision for a later reader to make, not an oversight.
- **The choice of embedding model is a separate axis.** Corpus language being unrestricted for *extraction* says nothing about retrieval quality: that follows the per-collection embedding model, which is configurable and may or may not be multilingual. See `Architecture/130_data_architecture_and_persistence.md`.

## Future work

- If telemetry ever gains an export surface (not implemented in v1 — `export_enabled = true` is accepted by the config loader but logs a warning and is silently coerced to `false`; see `archon_search/config.py`), locale-aware date formatting on the operator-facing rendering side may become relevant. The wire format would remain ISO 8601 UTC.
- If a first-party web UI is ever added (none is planned), the 300–320 frontend a11y series would be introduced and this file would link to it.

No other i18n or a11y work is on the roadmap.
