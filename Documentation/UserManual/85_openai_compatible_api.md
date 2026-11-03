**Purpose**: Point OpenAI-native tools (Cursor, Continue.dev, LangChain, LlamaIndex) at `archon-search` by changing one base URL and model name.
**Audience**: End users / developers
**Status**: Stable
**Last reviewed**: 2026-11-03 / **Next review**: 2027-07-29

# OpenAI-compatible API

## Why this exists

AI coding tools and RAG frameworks already speak the OpenAI chat API. `archon-search` does not — natively. The OpenAI-compatible shim (feature G9) closes that gap: it exposes `GET /v1/models` and `POST /v1/chat/completions` on the same REST port, so any tool that talks to OpenAI can retrieve from your collections after you change two settings — the base URL and the model name.

The shim is **retrieval only — there is no LLM generation.** A chat-completion request extracts your question, runs `archon-search` retrieval, and returns the top matching chunks *as the assistant reply text*. Your calling tool feeds that context into its own LLM. This "no LLM call" boundary is deliberate: `archon-search` is the retrieval layer.

## Enabling the shim

Disabled by default. When off it is a **true no-op** — no `/v1` routes are registered at all (not even a 404 handler), and no middleware is added. Turn it on in `~/.archon-search/archon-search.toml`:

```toml
[openai_shim]
enabled = true            # default false — opt in explicitly
inject_citations = true   # default true — append [Source: ...] per chunk
# top_k = 10              # optional; unset (default) = [database].top_k_return
```

Restart the server after editing. Verified in `archon_search/config.py` (`OpenAIShimConfig`) and `archon_search/server/app.py` — both the `include_router` and the `add_middleware` calls sit behind `if config.openai_shim.enabled:` guards.

> **`top_k` is the shim's result count.** The key is optional and unset by default. Unset, the shim returns `[database].top_k_return` results (default 5), like every other interface. Set, it is the number of result chunks for both models (`archon-search` and `archon-search/<collection>`). It must be an integer of at least 1 (a boolean, a float or a string is a `ConfigError`) and must not exceed `[search].top_k_max` (default 100): a larger value fails config load with `[openai_shim].top_k must be <= [search].top_k_max (<max>), got <value>`, even while the shim is disabled. A `top_k` field in a request body is accepted and ignored for any integer, in or out of range, and never answered with a 4xx; a non-integer value is the generic `422` of a malformed body field (`Invalid value for field 'top_k'.`). So the key is the only way to change the count. Verified in `archon_search/config.py` (`OpenAIShimConfig`, `_post_process_openai_shim`), `archon_search/server/schemas_openai.py` and `archon_search/server/routes_openai_shim.py`. See `[database]` and `[search]` in [Configuration](30_configuration.md).

## Authentication

The shim uses the **same Bearer token** as every other route — no new credentials. Send `Authorization: Bearer <key>` (or set `ARCHON_SEARCH_API_KEY` for CLIs/SDKs that read it). Namespace isolation is preserved: a token scoped to one namespace only sees that namespace's collections.

On auth failure, `/v1/*` paths return a **401 in OpenAI error shape** rather than the standard `{"detail": ...}` body, so OpenAI client libraries parse it correctly:

```json
{"error": {"message": "Incorrect API key.", "type": "authentication_error", "param": null, "code": null}}
```

This rewrite is done by `OpenAI401Middleware` (`routes_openai_shim.py`), which sits outside the normal auth middleware and rewrites its bodyless 401s. Key creation and rotation are covered in the [Security Guide](../SecurityGuide/02_authentication_and_keys.md).

## `GET /v1/models`

Returns one synthetic model per namespace-visible collection **plus a catch-all `archon-search` entry**. The list always contains at least the catch-all, even when the namespace has no collections. Tools call this on startup to populate their model picker.

```bash
curl -s http://127.0.0.1:8765/v1/models \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY"
```

```json
{
  "object": "list",
  "data": [
    {"id": "archon-search",         "object": "model", "created": 0, "owned_by": "archon-search"},
    {"id": "archon-search/my-app",  "object": "model", "created": 0, "owned_by": "archon-search"},
    {"id": "archon-search/docs",    "object": "model", "created": 0, "owned_by": "archon-search"}
  ]
}
```

`created` is always `0` — collections have no meaningful model-level creation timestamp.

## `POST /v1/chat/completions`

The shim takes the **last `role="user"` message** as the search query, runs retrieval, and returns the joined chunks as the assistant reply. The `model` field selects the target:

| `model` value | Behavior |
|---|---|
| `archon-search` | Fan out across **all** namespace collections. At most `[search] max_fanout` (default 8) are searched — the first ones of the namespace listing; the rest are left out and named in a WARNING in the server log. A collection whose embedding model differs from the server's is left out of the results too (logged). |
| `archon-search/{collection}` | Query that **one** collection directly. |
| anything else | `404` in OpenAI error shape (`The model '...' does not exist.`). |

Path scoping comes for free from the naming convention: ingest each project into its own collection and `model="archon-search/my-app"` implicitly scopes every result to that project.

### How many chunks, and what the search does

The reply holds `[openai_shim].top_k` chunks when the key is set, else `[database].top_k_return` (a request's `top_k` is ignored, see above); fewer when fewer candidates survive. The shim runs the plain hybrid search: it sends no filter, no `graph_mode`, and neither `hyde` nor `rag_fusion`, so it never applies HyDE, RAG Fusion or graph expansion, whatever `[hyde]`, `[rag_fusion]` and `[graph]` say. The reranker runs unless `[database].reranker_model` is empty or the cross-encoder is still cold (the fused ranking is used then). On `archon-search/{collection}` the wait for the collection's embedder is bounded at 0.5 s: when it expires the shim answers `200` from the FTS leg alone, a different set and order of sources, while the model loads. When telemetry is enabled, shim searches are recorded like REST and MCP ones (see [Telemetry](120_telemetry.md)).

### Worked example (OpenAI Python SDK)

Point `base_url` at your server's `/v1` and reuse your Bearer key as `api_key`:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8765/v1",
    api_key="<your-archon-search-key>",   # same Bearer token
)

resp = client.chat.completions.create(
    model="archon-search/my-app",         # one collection; use "archon-search" to fan out
    messages=[{"role": "user", "content": "How does the retry loop work?"}],
)

print(resp.choices[0].message.content)    # retrieved chunks, not a generated answer
```

The reply `content` is the concatenated chunk text. `finish_reason` is `"stop"` and token-usage fields are always zero (`archon-search` is retrieval-only).

### Citations

With `inject_citations = true` (default) each chunk is wrapped so the downstream LLM knows its source:

```
Context:
<chunk text>
[Source: /path/to/file.md]
```

Set `inject_citations = false` to return raw chunk text with no source annotation.

### Query validation

If the request has **no `role="user"` message**, you get `422` (`invalid_request_error`). If the user message is **empty or whitespace-only**, you get `400` with `code="no_user_message"` — both in OpenAI error shape, before any retrieval runs (feature 2026-07-15-340):

```json
{"error": {"message": "No user message provided", "type": "invalid_request_error", "code": "no_user_message"}}
```

### Errors

Every error is in the OpenAI shape `{"error": {"message": …, "type": …, "param": null, "code": …}}`: `param` is always `null`, and `code` is `null` except on the `400` `no_user_message` (see Query validation). A request body field of the wrong type (for example a non-integer `top_k`) is a `422` with `Invalid value for field '<field>'.` / `invalid_request_error`.

| Status | When | `message` / `type` |
|---|---|---|
| `401` | missing or wrong Bearer key | `Incorrect API key.` / `authentication_error` |
| `404` | `model` is not `archon-search` or `archon-search/<name>`, or `<name>` is empty, not a valid collection name or not in your namespace | `The model '<model>' does not exist.` / `invalid_request_error` |
| `404` | model `archon-search` and the namespace has no collection; or a collection vanished mid-request | `No collections available.` / `Collection not found.` (`invalid_request_error`) |
| `503` | the namespace's collection list or the collection metadata could not be read, an unexpected failure of the search, or an embedder load that did not finish within the cache's own 120 s wait (a defensive mapping: the search stops waiting for the embedder after 0.5 s and answers from the FTS leg, so a client does not meet it with the shipped cache); the cause is in the server log, never in the body | `Service temporarily unavailable.` / `server_error`; the embedder case adds `Retry-After: 30` |
| `504` | the search overran its budget (~30 s) | `Request timeout.` / `server_error` |

The `503` is the OpenAI envelope on both models, not the coded `metadata_store_error` body of `POST /search`; treat it as retryable.

### Streaming

Set `stream: true` to receive Server-Sent Events. **Note: one SSE frame per retrieved chunk, not token-by-token** — `archon-search` does not generate text, so there is nothing to stream word by word. Retrieval is materialized fully first (so errors surface as clean JSON, not a broken stream), then frames are emitted, ending with a `finish_reason: "stop"` frame and `data: [DONE]`. Zero results still send one empty assistant frame plus the stop frame — the stream never hangs. Tools like Cursor and Continue.dev concatenate `delta.content` and work either way.

## Query expansion providers (G10 note)

The shim never applies HyDE or RAG Fusion. It sends no `hyde` or `rag_fusion` flag and the request itself carries no such toggle, so enabling `[hyde]` or `[rag_fusion]` in config changes nothing for `/v1/chat/completions` (the flags exist on `POST /search`, the MCP `search` tool and `archon-search search`). See [Searching](60_searching.md) and [Configuration](30_configuration.md) for the providers and keys those surfaces use.

## Full field reference

`GET /openapi.json` on the running server is the authoritative contract for every request/response field. Exact Pydantic shapes live in `archon_search/server/schemas_openai.py`.

## Related documents

- [UserManual index](00_index.md)
- [Searching](60_searching.md) — the underlying `POST /search` surface, filters, HyDE/RAG Fusion
- [Configuration](30_configuration.md) — `[openai_shim]`, `[search]`, `[database]` top-k, `[hyde]` / `[rag_fusion]`
- [Running the server](40_running_the_server.md) — start/stop, host/port
- [DeveloperGuide: REST client (Python)](../DeveloperGuide/03_rest_client_python.md) — native `POST /search` client
- [DeveloperGuide: MCP integration](../DeveloperGuide/05_mcp_integration.md) — the MCP alternative to the OpenAI shim
- [SecurityGuide: Authentication and keys](../SecurityGuide/02_authentication_and_keys.md) — Bearer tokens, namespaces
- [API reference](../Architecture/600_api_reference_or_public_interface.md) — full REST + MCP + CLI surface
