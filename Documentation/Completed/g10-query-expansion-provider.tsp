namespace G10QueryExpansion;

// Constructor parameters (set once at factory time): max_tokens: int, timeout_seconds: float.
// Each adapter method also accepts max_tokens and timeout_seconds as per-call kwargs,
// allowing the generator to override per-request (e.g. RAG Fusion scales max_tokens by num_queries).
// These parameters are not expressed in the TypeSpec signature because they are Python-only kwargs;
// see DA-ARCH-C1-I-7 in the team plan for the authoritative description.

// Boundary: Use Cases ↔ Interface Adapters
// Implementations: AnthropicQueryExpansionProvider (refactored from HyDEGenerator/RAGFusionGenerator),
//                  OllamaQueryExpansionProvider, OpenAIQueryExpansionProvider.
// All methods are async in Python; this contract captures shape only.
// Failure contract: implementations return null / [] on any error — callers fall back to plain search silently.

interface QueryExpansionProvider {
  /** Generate a hypothetical document for the query. Returns null on any failure (timeout, API error, missing key). */
  generateHypotheticalDoc(query: string): string | null;

  /** Decompose the query into N variant queries. Returns [] on any failure. */
  decomposeQuery(query: string): string[];
}
