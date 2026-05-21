**Purpose**: Document the accessibility (a11y) and internationalization (i18n) surface of `archon-search`, a backend-only service.
**Audience**: Maintainers, integrators building CLIs/UIs on top of the HTTP and MCP APIs.
**Status**: Stable
**Last reviewed**: 2026-05-20
**Next review**: 2026-08-20

# Accessibility and Internationalization

`archon-search` ships no graphical user interface. It exposes three surfaces: a Click-based CLI (`archon-search …`), an HTTP control plane (FastAPI), and an MCP endpoint that mirrors the HTTP surface. This document records the honest, verified state of a11y and i18n across those surfaces.

## Principles

1. **No GUI, so a11y is CLI-scoped.** Frontend a11y concerns (WCAG, ARIA, screen-reader semantics, keyboard navigation) do not apply at the service boundary. They are the responsibility of any client that wraps these APIs.
2. **English only by design in v1.** All operator-facing strings — log messages, CLI output, HTTP/MCP error bodies, telemetry `error_kind` identifiers — are English. No translation layer, no locale negotiation, no `Accept-Language` handling.
3. **CLI output is plain text, machine-parseable as well as human-readable.** No ANSI color, no Unicode box-drawing, no terminal progress bars.
4. **Avoid color-only signaling.** Errors are distinguished by being written to stderr, not by color or symbols.
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

## Internationalization

- Logs, CLI messages, and HTTP/MCP error bodies are English. There is no message catalog, no gettext, no locale switch.
- Telemetry `error_kind` is a closed set of English identifiers defined in `archon_search/telemetry/entry.py`: `empty_query`, `slot_out_of_range`, `timeout`, `internal_error`, `validation_error`, `other`. These are stable API identifiers, not user-facing copy, and must not be translated.
- Timestamps are ISO 8601 in UTC (see `progress.py` and telemetry entries). This is locale-neutral by construction.
- For the broader error model and `error_kind` semantics, see `Architecture/140_error_handling_strategy.md`.

## Future work

- If telemetry ever gains an export surface (not implemented in v1 — `export_enabled = true` is accepted by the config loader but logs a warning and is silently coerced to `false`; see `archon_search/config.py`), locale-aware date formatting on the operator-facing rendering side may become relevant. The wire format would remain ISO 8601 UTC.
- If a first-party web UI is ever added (none is planned), the 300–320 frontend a11y series would be introduced and this file would link to it.

No other i18n or a11y work is on the roadmap.
