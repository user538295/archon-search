import "@typespec/http";
using Http;

@service(#{ title: "Archon Search — GET /status provider fields (G10)" })
namespace G10StatusProviderApi;

// Boundary: HTTP/API seam — GET /status response.
// Shows only the G10-modified sub-objects (HydeStatusDetail and RagFusionStatusDetail).
// Full StatusResponse has additional top-level fields not shown here.
// key_available semantics by provider:
//   anthropic → True iff ANTHROPIC_API_KEY is set in env
//   openai    → True iff OPENAI_API_KEY is set in env
//   ollama    → always True (no API key required)

model HydeStatusDetail {
  /** True iff the configured provider's auth key is available.
   *  anthropic → ANTHROPIC_API_KEY set in env.
   *  openai → OPENAI_API_KEY set in env.
   *  ollama → always True (no API key required). */
  key_available: boolean;

  /** Active provider for this feature: "anthropic" | "openai" | "ollama" */
  provider: string;
}

model RagFusionStatusDetail {
  /** True iff the configured provider's auth key is available.
   *  anthropic → ANTHROPIC_API_KEY set in env.
   *  openai → OPENAI_API_KEY set in env.
   *  ollama → always True (no API key required). */
  key_available: boolean;

  /** Active provider for this feature: "anthropic" | "openai" | "ollama" */
  provider: string;
}

/** Slice of StatusResponse showing G10-modified fields only. */
model StatusResponseSlice {
  /** null when hyde.enabled = false */
  hyde?: HydeStatusDetail | null;

  /** null when rag_fusion.enabled = false */
  rag_fusion?: RagFusionStatusDetail | null;
}

@route("/status")
interface StatusRoutes {
  @get
  getStatus(): StatusResponseSlice;
}
