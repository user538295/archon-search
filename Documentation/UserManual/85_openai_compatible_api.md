**Purpose**: Point OpenAI-native tools (Cursor, Continue.dev, LangChain, LlamaIndex) at `archon-search` by changing one base URL and model name.
**Audience**: End users / developers
**Status**: Stable
**Last reviewed**: 2026-07-29 / **Next review**: 2027-07-29

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
top_k = 5                 # ACCEPTED but currently INERT (see caveat below)
```

Restart the server after editing. Verified in `archon_search/config.py` (`OpenAIShimConfig`) and `archon_search/server/app.py` — both the `include_router` and the `add_middleware` calls sit behind `if config.openai_shim.enabled:` guards.

> **`top_k` is inert.** The field is parsed and validated (minimum 1) but is **not forwarded** to the pipeline — retrieval uses the pipeline's construction-time `top_k` from `[database]` (`top_k_return`, default 5). It is reserved for a future release when `search()` accepts a runtime `top_k`. Setting it changes nothing today. See `[database]` in [Configuration](30_configuration.md) to actually change how many chunks are returned.

## Authentication

The shim uses the **same Bearer token** as every other route — no new credentials. Send `Authorization: Bearer <key>` (or set `ARCHON_SEARCH_API_KEY` for CLIs/SDKs that read it). Namespace isolation is preserved: a token scoped to one namespace only sees that namespace's collections.

On auth failure, `/v1/*` paths return a **401 in OpenAI error shape** rather than the standard `{"detail": ...}` body, so OpenAI client libraries parse it correctly:

```json
{"error": {"message": "Incorrect API key.", "type": "authentication_error"}}
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
| `archon-search` | Fan out across **all** namespace collections (capped at `[search] max_fanout`, default 8; extras are logged and dropped). |
| `archon-search/{collection}` | Query that **one** collection directly. |
| anything else | `404` in OpenAI error shape (`The model '...' does not exist.`). |

Path scoping comes for free from the naming convention: ingest each project into its own collection and `model="archon-search/my-app"` implicitly scopes every result to that project.

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

### Streaming

Set `stream: true` to receive Server-Sent Events. **Note: one SSE frame per retrieved chunk, not token-by-token** — `archon-search` does not generate text, so there is nothing to stream word by word. Retrieval is materialized fully first (so errors surface as clean JSON, not a broken stream), then frames are emitted, ending with a `finish_reason: "stop"` frame and `data: [DONE]`. Zero results still send one empty assistant frame plus the stop frame — the stream never hangs. Tools like Cursor and Continue.dev concatenate `delta.content` and work either way.

## Query expansion providers (G10 note)

The shim runs the same retrieval pipeline as `POST /search`, so HyDE and RAG Fusion query expansion apply if they are enabled in config — but the shim request itself carries **no** `hyde`/`rag_fusion` toggle. Expansion is governed entirely by the `[hyde]` / `[rag_fusion]` config sections and their `provider` (`anthropic` | `openai` | `ollama` | `claude_cli`). See [Searching](60_searching.md) and [Configuration](30_configuration.md) for the provider matrix and required API keys.

## Full field reference

`GET /openapi.json` on the running server is the authoritative contract for every request/response field. Exact Pydantic shapes live in `archon_search/server/schemas_openai.py`.

## Related documents

- [UserManual index](00_index.md)
- [Searching](60_searching.md) — the underlying `POST /search` surface, filters, HyDE/RAG Fusion
- [Configuration](30_configuration.md) — `[openai_shim]`, `[database]` top-k, `[hyde]` / `[rag_fusion]`
- [Running the server](40_running_the_server.md) — start/stop, host/port
- [DeveloperGuide: REST client (Python)](../DeveloperGuide/03_rest_client_python.md) — native `POST /search` client
- [DeveloperGuide: MCP integration](../DeveloperGuide/05_mcp_integration.md) — the MCP alternative to the OpenAI shim
- [SecurityGuide: Authentication and keys](../SecurityGuide/02_authentication_and_keys.md) — Bearer tokens, namespaces
- [API reference](../Architecture/600_api_reference_or_public_interface.md) — full REST + MCP + CLI surface
