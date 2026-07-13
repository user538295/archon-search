# Feature Brief: OpenAI-Compatible API Shim (G9)

## Problem

Developers who use AI coding tools — Cursor, Continue.dev, LangChain, LlamaIndex — can't point those tools at Archon without custom integration code. Every one of those tools already speaks the OpenAI API format. Archon doesn't. So even when Archon has exactly the right documents, the developer can't get the tool to search them.

## Goal

Any tool that works with OpenAI's chat API works with Archon without modification — the developer changes one URL and one model name, and their tool starts retrieving from Archon's document collections.

## Users & Context

Developers who have indexed a codebase, documentation set, or knowledge base in Archon and want their IDE or AI tool to search it automatically while they work. They're in their editor — Cursor, Zed, VS Code with Continue.dev — and they want Archon's retrieval in the loop without writing glue code.

## Core Flow

1. The developer indexes their project into an Archon collection (e.g. `my-app`).
2. In their IDE or tool, they set the API base URL to Archon's address and the model name to `archon-search/my-app`.
3. Their tool calls `GET /v1/models` on startup — Archon returns a model list that includes one entry per collection, so the tool initialises without error.
4. When the developer asks a question, the tool sends it to `POST /v1/chat/completions` with the standard OpenAI message format.
5. Archon extracts the last user message as the search query, runs retrieval against the `my-app` collection, and returns the top matching chunks as the assistant's reply — no AI generation, just the retrieved text.
6. The developer's tool receives the context and passes it to its own LLM to generate an answer.

## In Scope

- `GET /v1/models` — returns one synthetic model entry per Archon collection, plus a catch-all `archon-search` entry that uses the multi-collection router.
- `POST /v1/chat/completions` — extracts last user message as search query, returns top-k retrieved chunks as the assistant's reply content.
- Model name routing: `model="archon-search"` auto-routes across all collections; `model="archon-search/collection-name"` targets a specific collection directly.
- Path scoping via collection naming convention: ingesting a project folder into a named collection (e.g. `my-app`) makes `model="archon-search/my-app"` implicitly scope all results to that project's files.
- Streaming (`stream: true`): returns one SSE event per retrieved chunk (not token-by-token — see Key Decisions).
- Source citations appended to each chunk when `inject_citations = true` (default on).
- Same Bearer token auth as the rest of Archon — no new credentials to manage.
- OpenAI-shaped error responses on all `/v1/*` paths (`{"error": {"message": ..., "type": ...}}`).
- New `[openai_shim]` config section, disabled by default (`enabled = false`).

## Out of Scope

- **`/v1/embeddings` endpoint** — a separate, distinct feature; G9 is a chat completions shim only.
- **LLM generation** — Archon returns retrieved context, not a generated answer. The calling tool's LLM handles generation.
- **Sub-folder scoping within a single collection** — one collection per project handles the common case; monorepo sub-folder filtering deferred to a future iteration.
- **Rate-limiting headers** (`x-ratelimit-*`) — not implemented; defer unless a specific tool requires them to function.
- **A second server process or port** — `/v1` mounts on the existing REST port alongside all other routes.

## Key Decisions

- **Model name encodes the collection target**: `model="archon-search/my-collection"` routes to that collection directly; bare `model="archon-search"` uses the multi-collection router to auto-select. This makes `GET /v1/models` genuinely useful (one entry per collection = one option per "searchable library" in the tool's model picker) and requires zero extra config fields.
- **One collection per project = path scoping for free**: rather than adding a path-filter mechanism to the API, the recommended setup is to ingest each project into its own named collection. The collection name becomes the scope. Sub-folder filtering within a collection is deferred.
- **Streaming sends one SSE event per chunk, not one per token**: tools like Cursor and Continue.dev concatenate `delta.content` from SSE events and work correctly either way. Word-by-word splitting adds code complexity for no observable difference in the target tools.
- **Assistant reply content is raw chunk blocks with citations**: `\n\nContext:\n{chunk.text}\n[Source: {source_path}]` repeated per chunk. No preamble ("Here are the relevant passages:") — downstream LLMs don't need it and it wastes tokens.
- **Disabled by default**: `[openai_shim].enabled = false` — operators opt in explicitly, so existing deployments are unaffected.

## Edge Cases & Constraints

- **`model="archon-search"` with no collections**: router has nothing to select from — return a `404` with an OpenAI-shaped error ("no collections available").
- **`model="archon-search/unknown-collection"`**: return `404` error (`{"error": {"type": "invalid_request_error", "message": "Collection 'unknown-collection' not found"}}`).
- **Auth failure**: return `401` in OpenAI error shape, not Archon's standard `{"detail": ...}` shape.
- **`stream: true` with zero results**: send a single SSE event with empty content and `finish_reason: "stop"`, then `data: [DONE]`. Do not hang.
- **Namespace isolation**: G9 respects the same namespace-filtered auth as all other routes — a token scoped to namespace `ns1` cannot reach collections in `ns2`.

## Open Questions

- What should `choices[0].message.role` be in the response? Must be `"assistant"` per OpenAI spec — confirm this is what's implemented.
- `finish_reason`: use `"stop"` (standard) or a custom value like `"retrieval_complete"`? Recommendation: `"stop"` — custom values break tools that pattern-match on this field.
- Token usage fields (`prompt_tokens`, `completion_tokens`, `total_tokens`): return zeros, approximate from character count, or omit? Some tools display these; omitting may cause null-pointer errors in client libs. Recommendation: return approximate values (`len(content) // 4`) rather than zeros.
- Error format middleware: apply OpenAI error shape transformation at a `/v1/*` middleware layer (clean, one place) or per-handler (simpler, more explicit)? Engineer's call.
- `id` field format for synthetic completions: `chatcmpl-{uuid4}` matches OpenAI's format — confirm this won't collide with anything in the existing codebase.

## Future Iterations

- `/v1/embeddings` endpoint — lets tools use Archon's fastembed model as a drop-in OpenAI embeddings provider.
- Sub-folder path scoping: `x_archon_scope` custom request field for programmatic clients (LangChain, custom scripts) who need to filter within a collection by source path prefix.
- System message convention (`Working-Directory: /path`) for Continue.dev users who want path scoping without multiple collections.
- `[openai_shim].collection_aliases` config: map a friendly model name to a collection name so users can set `model="my-docs"` instead of `model="archon-search/my-docs"`.

## Recommendation

Build this now — it has the highest value-to-effort ratio of anything remaining on the roadmap (3.20), and it removes the single biggest friction for developer adoption. The hardest part is not the implementation (the existing FastAPI structure makes adding `/v1` routes straightforward) but the streaming semantics: document clearly that `stream: true` returns context chunks, not generated tokens, so users who expect word-by-word output aren't surprised. The one thing that must not be compromised is the "no LLM call" invariant — Archon is the retrieval layer, and the moment it starts generating answers it becomes a different product with different infrastructure requirements.
