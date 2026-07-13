import "@typespec/http";
using Http;

@service(#{ title: "Archon OpenAI Shim — Chat Completions API" })
namespace OpenAIShimChatApi;

model ChatMessage {
  role: string;
  content: string;
}

model ChatCompletionRequest {
  /** OpenAI model name — "archon-search" or "archon-search/{collection}" */
  @encodedName("application/json", "model")
  modelName: string;

  messages: ChatMessage[];
  stream?: boolean | null;
  /** Accepted for forward compatibility but has no effect — pipeline uses construction-time top_k. Reserved for a future release. */
  top_k?: int32 | null;
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
  object: string;
  created: int32;

  /** Echoes the requested model name */
  @encodedName("application/json", "model")
  modelName: string;

  choices: ChatCompletionChoice[];
  usage: ChatCompletionUsage;
}

// Streaming (stream=true): returns text/event-stream. Each SSE frame is a ChatCompletionChunk.
// Final frame has delta={} and finish_reason='stop'. Terminal frame is `data: [DONE]`.

model ChunkDelta {
  /** Only present on the first chunk */
  role?: string;
  content?: string;
}

model ChatCompletionChunkChoice {
  index: int32;
  delta: ChunkDelta;
  finish_reason: string | null;
}

model ChatCompletionChunk {
  id: string;
  object: string;
  created: int32;
  @encodedName("application/json", "model")
  modelName: string;
  choices: ChatCompletionChunkChoice[];
}

model OpenAIError {
  message: string;
  type: string;
  param?: string | null;
  code?: string | null;
}

model OpenAIErrorResponse {
  error: OpenAIError;
}

@route("/v1")
interface ChatCompletions {
  @post
  @route("/chat/completions")
  create(@body body: ChatCompletionRequest): ChatCompletionResponse | OpenAIErrorResponse;
}
