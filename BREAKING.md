# BREAKING CHANGES

## Compatibility Policy
archon-search uses CalVer (`YY.M.<commit-count>`). CalVer segments encode **time only** —
they do not signal compatibility. This file IS the compatibility contract.

**Rule**: every release that removes or changes an existing API contract MUST add an entry
here describing: what changed, the migration path, and from which release the deprecated
form was announced. Consumers should subscribe to changes in this file, not interpret
CalVer segments.

## Changelog

### [next release] — MCP `search` tool response shape
**Surface**: MCP (`mcp.py` `search` tool)
**Change**: `search` tool now returns `{"results": [...], "acl_filtered": bool}` instead
of `[{...}, {...}]` (bare list of result dicts).
**Migration**: Update consumers to access `response["results"]` instead of iterating the
response directly. `response["acl_filtered"]` provides the ACL filter flag previously
unavailable on the MCP surface.
**Announced in**: this release (no prior deprecation period — the old shape was never
documented as stable).

### [next release] — REST `/search` per-request `top_k` no longer honored
**Surface**: REST (`/search` POST)
**Change**: The `top_k` field in `SearchRequest` is now ignored at the route level; the pipeline uses
`config.top_k_return` instead. Previously, each request could specify its own `top_k`.
**Migration**: Configure `[search] top_k_return` in `archon-search.toml` to set the desired result count.
**Announced in**: this release (the behavior was supported but never documented as stable).
