**Purpose**: Explain `archon-search`'s versioning scheme, how to read `BREAKING.md`, and how to pin a client across releases.
**Audience**: Engineers maintaining a long-lived integration who need to know what guarantees they have between two `archon-search` versions.
**Status**: Draft
**Last reviewed**: 2026-10-12 / **Next review**: 2027-10-12

# Versioning and Breaking Changes

`archon-search` uses CalVer: the version is `YY.M.<rev-count>` (e.g. `26.5.123`), derived from git tags via `hatch-vcs`. CalVer encodes **time only**. It does not signal API compatibility. The compatibility contract lives in `BREAKING.md`; CalVer segments tell you when, not what.

## Principles

1. **CalVer ≠ SemVer.** A bump from `26.5.x` to `26.6.x` is not a minor release in the SemVer sense. The third segment is the **total** commit count across the repository's history (`git rev-list --count HEAD`, see `release.sh`), so it is monotonic across months — not a per-month counter. Read `BREAKING.md` before upgrading.
2. **`BREAKING.md` is the contract.** Every release that removes or alters an existing API contract (REST, MCP, CLI, config, on-disk layout) adds an entry there with `Surface`, `Change`, `Migration`, and `Announced in`. If a change is not in `BREAKING.md`, it was not intentional — file a bug.
3. **OpenAPI is the machine-readable shape.** Pin to a captured `/openapi.json` snapshot if your CI must catch contract drift automatically. The `info.version` field in that document is the running server's CalVer string (`server/app.py::_configure_openapi`).
4. **MCP shapes are Pydantic-gated as of C7.** The 11 search, ingest, collection, and document MCP tools validate return values through explicit Pydantic schemas (`archon_search/server/mcp_schemas.py`, or `ExplainResponse` in `routes_explain.py` for `explain`; `extra='forbid'`). The `mcp_schemas.py` schemas copy each field from the domain dataclass by name in an explicit `from_result` mapping, so a field added to a dataclass never reaches the MCP wire until its schema adds it, and a dataclass field the mapping still reads but that was removed raises `AttributeError`, which the tool's catch-all returns as `{"error": "...", "code": "internal_error"}`. `schema_validation_error` is returned only when a mapped value fails the schema's own validation (e.g. a wrong type). Debt item `API-4` is resolved. See `BREAKING.md` C7 entries.

## Reading the version

The running server reports its version on the authenticated `GET /status` (`GET /health` returns only `{"status": "running"}`):

```json
{"version": "26.5.123", "...": "..."}
```

The package version is resolved via `importlib.metadata` at import time (`server/routes_status.py`) and re-used as the OpenAPI `info.version`. The string never reflects compatibility — only the release count.

## Reading `BREAKING.md`

Each section heading is the release name. Each entry has the shape:

```
### [next release] — short headline

**Surface**: REST | MCP | CLI | config
**Change**: what changed.
**Migration**: how to adapt the client.
**Announced in**: the release where the deprecation was first published (often the same release that ships the change — there is no formal deprecation period yet).
```

Two illustrative entries — **not** the full list. `BREAKING.md` carries 99 `### ` headings, all of them `[next release]`; the two below are the ones `MigrationGuide/03_breaking_changes_index.md` indexes as NR-1 and NR-2 (that index's NR rows cover only some of the headings). Never treat this section, or that index, as an inventory: read `BREAKING.md` itself before an upgrade.

- **MCP `search` response shape.** Now `{"results": [...], "acl_filtered": bool}`; previously a bare list. Update consumers to read `response["results"]`.
- **REST `/search` `top_k` ignored.** Request schema still accepts the field (Pydantic validates `top_k ≥ 1`; the route rejects a value above `[search].top_k_max` with `422`), but the route does not pass it to the pipeline. Configure `[database] top_k_return` in `archon-search.toml` instead.

When upgrading, diff `BREAKING.md` between the version you have and the version you want; everything in that diff applies to you.

## Client pinning strategy

For libraries with a stable user base:

1. **Pin a minimum server version.** Read the running server's `GET /status` at startup, compare against your floor, and fail fast if too old.
2. **Snapshot `/openapi.json` in CI.** Save the schema as a fixture and diff it on each server upgrade. Anything new is opportunity; anything removed is breakage.
3. **Treat MCP tool names as part of the contract.** A renamed tool is a breaking change even if no schema field moves.
4. **Don't pin to CalVer segments in dependency declarations.** Pin to a tag or commit; CalVer values do not communicate compatibility to package managers.

### Example: startup compatibility check

```python
import os

import httpx

REQUIRED_MIN = (26, 5, 0)  # year, month, rev-count

def parse_calver(v: str) -> tuple[int, int, int]:
    # Note: between tags, hatch-vcs emits dev versions like "26.5.0.post{N}"
    # (4 segments). Strip or handle that suffix before splitting in production.
    yy, mm, rev = v.split(".")
    return int(yy), int(mm), int(rev)

r = httpx.get(
    "http://127.0.0.1:8765/status",
    headers={"Authorization": f"Bearer {os.environ['ARCHON_SEARCH_API_KEY']}"},
    timeout=2.0,
)
r.raise_for_status()
running = parse_calver(r.json()["version"])
if running < REQUIRED_MIN:
    raise RuntimeError(f"archon-search {running} is older than required {REQUIRED_MIN}")
```

## What is and isn't covered by `BREAKING.md`

| Change | In `BREAKING.md`? |
| --- | --- |
| Renaming a REST route | Yes |
| Removing a field from a `response_model` | Yes |
| Adding a required field to a request body | Yes |
| Adding an optional field to a request body | No |
| Adding a new REST endpoint or MCP tool | No |
| Renaming an MCP tool | Yes |
| Removing a CLI subcommand or flag | Yes |
| Changing the on-disk layout under `~/.archon-search/` | Yes |
| Changing default config values that affect retrieval behaviour | Yes (if observable on the wire) |
| Internal refactors with no surface change | No |

Telemetry's "no raw query" invariant is structural (the factory has no `query` parameter); breaking it would be a `BREAKING.md` entry, but the type system would reject the change first.

## OpenAPI as authoritative

If this guide, `Architecture/600_api_reference_or_public_interface.md`, and `/openapi.json` ever disagree:

1. `/openapi.json` wins for request and response shapes.
2. `mcp.py` wins for MCP tool names and arguments.
3. `BREAKING.md` wins for migration guidance.
4. The mismatch in this guide is a bug — open a PR or an issue.

For machine-generated client types, run `openapi-typescript`, `datamodel-code-generator`, or your tool of choice against the live `/openapi.json` of the target server, not the version pinned in a fixture.

## Related documents

- [`../../BREAKING.md`](../../BREAKING.md) — compatibility contract.
- [`../Architecture/520_api_design_and_contracts.md`](../Architecture/520_api_design_and_contracts.md) — design rules and `BREAKING.md` discipline.
- [`../Architecture/510_release_and_environment_strategy.md`](../Architecture/510_release_and_environment_strategy.md) — `release.sh`, CalVer, CI/CD.
- [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) — endpoint reference.
- [`06_error_handling.md`](./06_error_handling.md) — what failure modes mean to clients.
