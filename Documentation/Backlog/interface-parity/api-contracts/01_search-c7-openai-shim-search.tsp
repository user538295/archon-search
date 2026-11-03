import "@typespec/http";

using Http;

// C7 — OpenAI-compatible shim POST /v1/chat/completions, search path only (client <-> server).
// The OpenAI envelope, the streaming format and the model-name routing stay adapter rendering; only the behaviours listed here change.
// The standing shim contract is tsp_contract/openapi-openai-shim.yaml; shim routes are absent from both app OpenAPI snapshots (disabled by default).
@service(#{ title: "Archon Search OpenAI shim search path (S1)" })
namespace SearchShimApi;

model ChatMessage {
  role: string;
  content: string;
}

model ChatCompletionRequest {
  // "archon-search" searches all of the caller's collections (capped at max_fanout, the rest dropped with a warning); "archon-search/<collection>" searches one (the name is stripped; an empty, whitespace-only or invalid collection name answers 404).
  `model`: string;
  messages: ChatMessage[];
  stream?: boolean = false;
  // Accepted and ignored, as today (decided): the shim ignores a request top_k, so no value (in or out of range) gets a 4xx or changes the result count; the field stays only for protocol compatibility. REST, MCP and the CLI honour top_k; the shim ignores the request field by decision. The result count is the `[openai_shim].top_k` key when set and `[database].top_k_return` otherwise (decided: the key becomes `int | None`, unset by default).
  top_k?: int32;
}

model ChatCompletionChoice {
  index: int32;
  message: ChatMessage;
  finish_reason: string;
}

model ChatCompletionUsage {
  prompt_tokens: int32;
  completion_tokens: int32;
  total_tokens: int32;
}

model ChatCompletionResponse {
  id: string;
  object: "chat.completion";
  created: int32;
  `model`: string;
  choices: ChatCompletionChoice[];
  usage: ChatCompletionUsage;
}

model OpenAIError {
  message: string;
  type: string;
  param?: string | null;
  code?: string | null;
}

model OpenAIErrorEnvelope {
  error: OpenAIError;
}

model Completion {
  @statusCode _: 200;
  @header contentType: "application/json";
  @body body: ChatCompletionResponse;
}

// Results are materialised first, then streamed as `data:` frames ending with `[DONE]`.
model CompletionStream {
  @statusCode _: 200;
  @header contentType: "text/event-stream";
  @body body: string;
}

// 400 no_user_message (blank last user message), unchanged. There is no out-of-range top_k 400: the shim ignores a request top_k by decision.
@error
model BadRequest {
  @statusCode _: 400;
  @body body: OpenAIErrorEnvelope;
}

@error
model Unauthorized {
  @statusCode _: 401;
  @body body: OpenAIErrorEnvelope;
}

// Unknown model or collection ("The model ... does not exist." / "Collection not found."), no collections available, unchanged.
@error
model NotFound {
  @statusCode _: 404;
  @body body: OpenAIErrorEnvelope;
}

// No user message in `messages`, unchanged.
@error
model Unprocessable {
  @statusCode _: 422;
  @body body: OpenAIErrorEnvelope;
}

// "Service temporarily unavailable." NEW: also for a failed collection-list lookup on the all-collections model (a plain 500 today) and for an unexpected failure on the fan-out branch (an unhandled 500 today).
// Embedder not ready answers it with Retry-After from the one constant (30 seconds), through the service instead of the app-level handler.
@error
model ServiceUnavailable {
  @statusCode _: 503;
  @header("Retry-After") retryAfter?: int32;
  @body body: OpenAIErrorEnvelope;
}

// "Request timeout.", unchanged.
@error
model GatewayTimeout {
  @statusCode _: 504;
  @body body: OpenAIErrorEnvelope;
}

@route("/v1/chat/completions")
interface ChatCompletions {
  @post create(@body body: ChatCompletionRequest): Completion | CompletionStream | BadRequest | Unauthorized | NotFound | Unprocessable | ServiceUnavailable | GatewayTimeout;
}
