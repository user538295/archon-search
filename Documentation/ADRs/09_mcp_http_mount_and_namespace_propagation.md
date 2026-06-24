# 09. MCP HTTP Mount Under FastAPI and Namespace Propagation

**Status**: Accepted
**Date**: 2026-06-24
**Deciders**: archon-search maintainers
**Spike task**: K-1 (D9 plan prerequisite)

## Context

`archon_search/server/mcp.py` contains a fully implemented FastMCP server with 17
registered tools, but `create_app()` in `archon_search/server/app.py` never calls
`create_mcp_http_app()`. The D9 plan proposes mounting the MCP app at `/mcp` on the
existing REST FastAPI app (port 8765) — same process, same Uvicorn, no second port.

Before implementation could begin, the plan required a spike (K-1) to answer four
concrete questions with working code:

1. Does `app.mount("/mcp", mcp_starlette)` work after lifespan starts — correct route
   matching, and does `/mcp` appear in the OpenAPI schema?
2. Can `TestClient` (or `httpx.AsyncClient`) complete the full `initialize` →
   `tools/list` → `tools/call` JSON-RPC sequence — are integration tests feasible?
3. How does `request.state.namespace` (set by `APIKeyMiddleware`) reach FastMCP tool
   closures? The `ctx.meta.get("namespace")` pattern in `update_collection`
   (`mcp.py:1022`) is dead code — nothing populates it.
4. Does FastMCP's own lifespan fire correctly when mounted? Does the
   `StreamableHTTPSessionManager` task group initialise?

## Spike results

All four questions were answered with working code. The findings below are the
basis for all D9 implementation tasks (BE-1 through BE-11, T-1 through T-5).

### FastMCP API change: `streamable_http_app()` removed in 3.4.2

**Critical finding**: the existing `mcp.py` code at line 1538 calls
`fastmcp_app.streamable_http_app()` which does **not exist** in FastMCP 3.4.2 (the
installed version). The correct call is `fastmcp_app.http_app(path='/')`. The default
transport is `'http'`; `'streamable-http'` is an accepted alias that routes to the same
`create_streamable_http_app()` internally, so both values are equivalent. `path='/'` is
required so the MCP endpoint is reachable at the mount point without an extra `/mcp`
suffix.

**Prerequisite for BE-1**: `fastmcp` is not listed in `pyproject.toml`'s
`[project.dependencies]`. BE-1 must add `fastmcp>=3.4` to the dependencies before any
other D9 task can proceed.

### Proof 1: Mount works; route matching correct; not in OpenAPI

```python
mcp_starlette = mcp_server.http_app(path='/')  # internal route at /

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp_starlette.router.lifespan_context(app):
        yield

fastapi_app = FastAPI(lifespan=lifespan)
fastapi_app.mount('/mcp', mcp_starlette)

with TestClient(fastapi_app) as client:
    openapi = client.get('/openapi.json').json()
    paths = list(openapi.get('paths', {}).keys())
    # Result: ['/health'] — /mcp does NOT appear
    assert not any('/mcp' in p for p in paths)  # PASSES
```

**Result**: `app.mount('/mcp', mcp_starlette)` works correctly. The MCP endpoint is
reachable at `/mcp` (via the mounted sub-app's `/` route). The `/mcp` path does NOT
appear in FastAPI's OpenAPI schema because FastAPI only includes routes it owns
directly — mounted sub-apps are opaque to the OpenAPI generator.

**Why the custom OpenAPI generator in `app.py` cannot include mount paths**: the proof
was run against a minimal FastAPI app. The claim holds more broadly because FastAPI's
`get_openapi()` only processes `APIRoute` objects, not `Mount` objects. The custom
`_configure_openapi()` in `app.py` only iterates `schema.get("paths", {})`, which is
populated by `get_openapi()`. Because `get_openapi()` cannot emit mount paths, the
custom generator cannot include them either. The OpenAPI schema will not expose `/mcp`
routes regardless of any custom generator logic.

### Proof 2: TestClient can complete the full JSON-RPC sequence

```python
with TestClient(fastapi_app, raise_server_exceptions=True) as client:
    # Step 1: initialize
    resp1 = client.post('/mcp', json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {'protocolVersion': '2024-11-05', 'capabilities': {},
                   'clientInfo': {'name': 'spike', 'version': '1.0'}}
    }, headers={'Content-Type': 'application/json',
                'Accept': 'application/json, text/event-stream'})
    # Result: 200, SSE stream with initialize result
    session_id = resp1.headers['mcp-session-id']

    # Step 2: tools/list
    resp2 = client.post('/mcp', json={
        'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}
    }, headers={'Content-Type': 'application/json',
                'Accept': 'application/json, text/event-stream',
                'mcp-session-id': session_id})
    # Result: 200, tools list contains registered tool names

    # Step 3: tools/call
    resp3 = client.post('/mcp', json={
        'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
        'params': {'name': 'hello_tool', 'arguments': {'name': 'World'}}
    }, headers={'Content-Type': 'application/json',
                'Accept': 'application/json, text/event-stream',
                'mcp-session-id': session_id})
    # Result: 200, {'content': [{'type': 'text', 'text': 'Hello World'}], ...}
```

**Result**: `TestClient` can complete the full JSON-RPC sequence including session
continuity via `mcp-session-id` header. Integration tests for BE-3, BE-5, BE-6, BE-7
are feasible without redesign.

**Note**: FastMCP uses SSE (Server-Sent Events) for responses. Each response is a
`text/event-stream` body. Test assertions must parse the `data:` lines:
```python
for line in resp.text.split('\n'):
    if line.startswith('data:'):
        data = json.loads(line[5:].strip())
        result = data.get('result', {})
```

### Proof 3: Namespace propagation via `_current_http_request` ContextVar

**Finding**: FastMCP's `RequestContextMiddleware` (in `fastmcp.server.http`) sets the
`_current_http_request` ContextVar to the current `Request` object for every HTTP
request to the MCP endpoint. Tool closures that receive a `Context` parameter can
access this ContextVar to retrieve `request.state.namespace`.

```python
from fastmcp.server.http import _current_http_request

@mcp_server.tool()
async def get_namespace(ctx: Context) -> str:
    http_req = _current_http_request.get()  # Request set by FastMCP middleware
    ns = getattr(http_req.state, 'namespace', None)  # Set by APIKeyMiddleware
    return ns or 'NO_NAMESPACE_SET'

class SetNamespaceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.namespace = 'test-namespace'  # simulates APIKeyMiddleware
        return await call_next(request)

mcp_starlette.add_middleware(SetNamespaceMiddleware)

# Result when calling get_namespace tool: 'test-namespace'
```

**Mechanism**: `APIKeyMiddleware.dispatch()` sets `request.state.namespace` (line 110
of `middleware_auth.py`). FastMCP's `RequestContextMiddleware` wraps each request with
`set_http_request(Request(scope))`, which stores the request in `_current_http_request`
ContextVar. Tool closures access the namespace via:

**Behavioral property — ContextVar is set fresh on every HTTP request**: FastMCP's
`RequestContextMiddleware.__call__()` calls `set_http_request(Request(scope))` for
**every** HTTP POST to the MCP endpoint — not just the `initialize` request. In
Streamable HTTP transport, each JSON-RPC call (`initialize`, `tools/list`, `tools/call`)
is a separate HTTP POST, so `_current_http_request` reflects the **current** request on
each invocation. Tool closures must therefore call `_get_request_namespace()` on each
invocation (not cache the result at startup) — which is the pattern shown above.

Namespace stability across a session is guaranteed at the **protocol level**, not the
ContextVar level: the MCP Streamable HTTP protocol requires every request in a session to
carry the same bearer token (the `mcp-session-id` header identifies the session; the
bearer token authenticates each individual request). A client that sends a different
token on a subsequent request fails auth for that request. Because the bearer token
determines the namespace (via `APIKeyMiddleware`), the namespace is stable for the
session's lifetime. A key rotation mid-session does NOT change the namespace — the old
token remains valid until its grace period expires, and a new session must be started
to pick up the new key.

```python
from fastmcp.server.http import _current_http_request

def _get_request_namespace() -> str:
    req = _current_http_request.get()
    if req is None:
        return DEFAULT_NAMESPACE
    return getattr(req.state, 'namespace', DEFAULT_NAMESPACE)
```

This function replaces all `DEFAULT_NAMESPACE` hardcoding in the 17 tool closures
(asymmetry fix #2, BE-5).

**Note on `ctx.meta.get("namespace")`**: Confirmed dead code. FastMCP 3.4.2's
`Context.meta` is not populated by anything in the middleware stack. The
`_current_http_request` ContextVar is the correct mechanism.

### Proof 4: FastMCP lifespan is correctly delegated and fires

The `StreamableHTTPSessionManager` task group initialises only when the MCP
sub-app's lifespan runs. When mounted naively with `app.mount('/mcp', mcp_starlette)`
without lifespan delegation, every request to `/mcp` raises:
`RuntimeError: Task group is not initialized. Make sure to use run()`.

The fix is explicit lifespan delegation via `mcp_starlette.router.lifespan_context`:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp_starlette.router.lifespan_context(app):
        # MCP session manager task group is running here
        yield  # REST app handles requests
    # MCP lifespan shuts down after yield

fastapi_app = FastAPI(lifespan=lifespan)
fastapi_app.mount('/mcp', mcp_starlette)
```

**FastMCP custom lifespan fires correctly**:

```python
@asynccontextmanager
async def custom_mcp_lifespan(server: FastMCP):
    # startup
    yield
    # shutdown

mcp_server = FastMCP('test', lifespan=custom_mcp_lifespan)
```

Observed event sequence with both FastAPI and FastMCP lifespans:
1. `fastapi_startup`
2. `mcp_lifespan_startup` (fired by `lifespan_context`)
3. `fastapi_inner` (requests are served)
4. `mcp_lifespan_shutdown`
5. `fastapi_shutdown`

**Result**: FastMCP's lifespan is NOT delegated automatically by `app.mount()`.
Explicit delegation via `mcp_starlette.router.lifespan_context(app)` is required and
works correctly. The custom lifespan (if set on the `FastMCP` instance) fires
inside the context manager.

## Decision

### Wiring approach

Use `app.mount('/mcp', mcp_starlette)` inside `create_app()`'s async lifespan context
manager — not before it — so all lifespan-constructed objects (`key_store`,
`writer`, `pipeline`, `embedder_cache`) are available when `create_mcp_http_app()` is
called.

The `create_app()` lifespan must explicitly delegate to the MCP sub-app's lifespan
via `mcp_starlette.router.lifespan_context(app)`. The delegation must happen after
all REST objects are ready (so `create_mcp_http_app()` receives fully-constructed
arguments) and must remain active until the REST lifespan yields (so the MCP session
manager task group stays alive for the duration of the server's uptime).

Skeleton:

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # ... existing startup: key_store.load_synthetic_records, search_store.connect, etc.

    if config.mcp.enabled:
        try:
            mcp_starlette = create_mcp_http_app(
                pipeline=app.state.pipeline,
                default_collection=...,
                writer=app.state.telemetry_writer,
                config=config,
                key_store=app.state.key_store,
            )
            async with mcp_starlette.router.lifespan_context(app):
                app.mount('/mcp', mcp_starlette)  # mount AFTER lifespan started
                yield  # serve requests with MCP enabled
        except Exception:
            logger.warning("MCP server failed to start; continuing without MCP")
            yield  # serve REST requests without MCP
    else:
        yield  # serve REST requests (MCP explicitly disabled)
```

### API fix required

The existing `create_mcp_http_app()` in `mcp.py` calls `streamable_http_app()` at
line 1538, which does not exist in FastMCP 3.4.2. BE-2 must replace this call with
`http_app(path='/')`. The transport default is `'http'`; `'streamable-http'` is an
equivalent alias — either is acceptable.

### Double `APIKeyMiddleware` — accepted trade-off

`create_mcp_http_app()` adds `APIKeyMiddleware` to the MCP sub-app. When mounted under
the REST `FastAPI` app — which already has `APIKeyMiddleware` as an app-level middleware
— every `/mcp` request passes through auth twice. The D9 plan explicitly prohibits
middleware changes ("no middleware changes" — BE-1 through BE-11). Decision: **keep dual
middleware** as an accepted trade-off. The redundancy is safe: `request.state.namespace`
is written twice to the same scope dict with the same value, and `active_keys()` incurs
one additional disk read per request (O(1), negligible). Removing `APIKeyMiddleware`
from `create_mcp_http_app()` would require changes outside D9's scope and would risk
breaking standalone (non-mounted) MCP usage.

### Namespace propagation mechanism

BE-5 uses `_current_http_request` from `fastmcp.server.http` to retrieve
`request.state.namespace` inside tool closures. A module-level helper:

```python
from fastmcp.server.http import _current_http_request
from archon_search.constants import DEFAULT_NAMESPACE

def _get_request_namespace() -> str:
    """Return the namespace resolved by APIKeyMiddleware for the current request."""
    req = _current_http_request.get()
    if req is None:
        return DEFAULT_NAMESPACE
    return getattr(req.state, 'namespace', DEFAULT_NAMESPACE)
```

All 17 tool closures call `_get_request_namespace()` instead of `DEFAULT_NAMESPACE`.
BE-5 must add a `namespace` parameter to `_resolve_embedder` (current signature at
`mcp.py:147`: `_resolve_embedder(pipeline, embedder_cache, collection, config)` — no
`namespace` parameter exists yet) so it can pass the namespace to
`pipeline.get_collection_meta()`.

**Tool namespace classification for BE-5**:
- **Need namespace threading** (have pipeline calls): `search`, `search_with_context`,
  `explain`, `ingest_file`, `ingest_directory`, `list_collections`, `get_collections_meta`,
  `get_collection_meta`, `list_documents`, `delete_document`, `update_collection`,
  `export_collection`, `import_collection`
- **Namespace-independent** (key management tools — no pipeline calls; use
  `DEFAULT_NAMESPACE` intentionally or take namespace from the request auth layer):
  `create_key`, `list_keys`, `revoke_key`, `rotate_key`

**Additional callsites within namespace-threaded tools** that hardcode `DEFAULT_NAMESPACE`
and must be fixed in BE-5 (in addition to the top-level tool dispatch):
- `export_collection` (`mcp.py:1132`): calls
  `pipeline.store.get_collection_meta(collection, DEFAULT_NAMESPACE)` directly — must use
  the namespace from `_get_request_namespace()`.
- `import_collection` (`mcp.py:1222`): same pattern —
  `pipeline.store.get_collection_meta(collection, DEFAULT_NAMESPACE)`.
- `get_collection_meta` (`mcp.py:952`): calls `pipeline.get_collection_meta(name)` with
  no namespace argument — must pass the namespace from `_get_request_namespace()`.

**`update_collection` is the only tool with a pre-existing (dead) namespace resolution
attempt**: `ctx.meta.get("namespace", DEFAULT_NAMESPACE)` at `mcp.py:1022`. This differs
from the other 12 namespace-threaded tools, which have no namespace resolution at all.
BE-5 must migrate `update_collection` from `ctx.meta.get` to `_get_request_namespace()`.
The other 12 tools need namespace added from scratch.

### Integration test approach

`TestClient` (synchronous) can complete the full JSON-RPC sequence. Session continuity
requires passing the `mcp-session-id` header returned by `initialize` on subsequent
requests. Tool call responses are SSE-encoded; tests parse `data:` lines.

`httpx.AsyncClient` is NOT required; `TestClient` is sufficient for BE-3, BE-5, BE-6,
BE-7. No test redesign needed.

## Consequences

### Positive
- Single Uvicorn, shared event loop, shared port (8765). No signal-handler
  coordination, no second process.
- TestClient-based integration tests cover the full JSON-RPC sequence including
  session management — no live server required in CI.
- Namespace propagation is clean: one helper function, called from all 17 closures.
  No middleware changes; no new request headers.
- FastMCP's own custom lifespan (if used) fires correctly inside the delegation
  context — no ordering issues.

### Negative / trade-offs
- **`streamable_http_app()` removal**: the existing `mcp.py` code references a
  non-existent API. BE-2 must fix this before any integration test can pass.
- **Lifespan must be explicit**: naive `app.mount()` without lifespan delegation
  causes every MCP request to fail with a `RuntimeError`. This is a non-obvious
  footgun if the mount is moved outside the lifespan context.
- **`_current_http_request` is a private FastMCP API**: it is used by FastMCP's own
  `RequestContextMiddleware` and is not documented as public. If FastMCP renames or
  removes it in a future release, namespace propagation must be reimplemented. The
  risk is low for minor releases but should be monitored on FastMCP upgrades.
  Mitigation: the `_get_request_namespace()` wrapper (see Namespace propagation
  mechanism above) isolates the private import to a single module-level function. A
  regression test that verifies the import succeeds (`from fastmcp.server.http import
  _current_http_request`) must be added alongside BE-5 so FastMCP upgrades surface the
  breakage in CI before deployment. `fastmcp` should be pinned with a `~=3.4` upper
  bound in `pyproject.toml` to prevent silent major-version upgrades.
- **SSE response format**: FastMCP returns `text/event-stream` responses even for
  unary tool calls. Tests that assert on `resp.json()` will fail; they must parse
  `data:` lines from `resp.text`. This is a non-obvious test authoring constraint for
  BE-3 through BE-11.
- **CORS for `/mcp`**: FastAPI's `CORSMiddleware` does not apply to mounted sub-apps.
  Browser-based MCP clients will encounter CORS errors. Non-browser clients (Claude
  Code CLI) are unaffected. Out of scope for D9.

## Fallback (not needed)

The plan documented a fallback: call `create_mcp_http_app()` synchronously before the
lifespan and patch `writer`/`key_store` via `app.state` in the lifespan. This fallback
is **not needed** — mount-in-lifespan works. The fallback is recorded here for
completeness but is not used.

## References

- D9 team plan: `Documentation/Backlog/mcp-wiring-team-plan.md`
- FastMCP ASGI docs: https://gofastmcp.com/deployment/asgi
- `archon_search/server/mcp.py` — `create_mcp_http_app()` (line 1505)
- `archon_search/server/app.py` — `create_app()` lifespan (line 154)
- `archon_search/server/middleware_auth.py` — `APIKeyMiddleware.dispatch()` (line 38)
- `fastmcp.server.http` — `_current_http_request` ContextVar, `RequestContextMiddleware`
