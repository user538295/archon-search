# 17. MCP Namespace Resolution Uses FastMCP's Public `get_http_request()`

**Status**: Accepted
**Date**: 2026-10-21
**Deciders**: archon-search maintainers
**Supersedes**: partially supersedes [ADR 09 — MCP HTTP Mount Under FastAPI and Namespace Propagation](09_mcp_http_mount_and_namespace_propagation.md) — only how `_get_request_namespace()` reads the current request: the `_current_http_request` code sketches in §"Proof 3" and §"Namespace propagation mechanism", and the Negative / Trade-offs bullet "`_current_http_request` is a private FastMCP API", including its prescribed regression test "that verifies the import succeeds (`from fastmcp.server.http import _current_http_request`)". ADR 09's mount-in-lifespan decision, its per-request (never cached) namespace resolution, its session-stability argument, and everything else in it are unchanged and still authoritative.

## Context

ADR 09 recorded the K-1 spike: FastMCP's `RequestContextMiddleware` stores the current Starlette `Request` in the private `fastmcp.server.http._current_http_request` ContextVar, and `APIKeyMiddleware` writes `request.state.namespace`. It sketched `_get_request_namespace()` as a direct read of that private ContextVar and asked for a regression test on its import.

The shipped helper never read the private ContextVar. Since BE-5 (commit `085abdb5`, 2026-06-24) `_get_request_namespace()` has called FastMCP's public `fastmcp.server.dependencies.get_http_request()`, which is listed in that module's `__all__`. In fastmcp 3.4.5 it reads the MCP SDK `request_ctx` first, falls back to `_current_http_request`, then to a Docket task snapshot, and raises `RuntimeError("No active HTTP request found.")` when none is set (`fastmcp/server/dependencies.py:361-407`). The test ADR 09 prescribed therefore guarded an import production did not use.

## Decision

**`_get_request_namespace()` (`archon_search/server/mcp.py`) resolves the namespace through the public `get_http_request()`, imported at module level, and treats only `RuntimeError` as "no active request".**

- `RuntimeError` from `get_http_request()` → `DEFAULT_NAMESPACE` (CLI, direct calls in tests).
- Any other exception propagates: a fault in the request context must not silently serve the call from the default namespace.
- A request whose `state` lacks `namespace` → `DEFAULT_NAMESPACE` (`getattr(req.state, "namespace", DEFAULT_NAMESPACE)`).
- The upgrade guard is `tests/server/test_mcp_namespace_propagation_be5.py::test_fastmcp_get_http_request_raises_runtime_error_outside_request`: it asserts `archon_search.server.mcp` uses `fastmcp.server.dependencies.get_http_request` and that the real function raises `RuntimeError` outside a request. Sibling tests in the same module pin the three branches above.

## Consequences

### Positive

- No dependency on a private FastMCP name; the import is part of FastMCP's public surface.
- The fallback depends on one upstream behaviour (the exception type), and a test fails in CI if a FastMCP upgrade changes it.

### Negative / Trade-offs

- `get_http_request()` also returns a synthetic header-only request inside Docket workers; its `state` carries no `namespace`, so such a call resolves to `DEFAULT_NAMESPACE`. archon-search runs no Docket tasks today.

## Cross-References

- [ADR 09 — MCP HTTP Mount Under FastAPI and Namespace Propagation](09_mcp_http_mount_and_namespace_propagation.md): the mount decision and the K-1 findings this decision builds on.
- `archon_search/server/mcp.py`: `_get_request_namespace`; `archon_search/server/middleware_auth.py`: `APIKeyMiddleware`.
- `tests/server/test_mcp_namespace_propagation_be5.py`.
