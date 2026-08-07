// C1 — QueryExpansionProvider (INTERNAL LOGICAL SEAM)
// Consumer-owned protocol in the Use Cases layer
// (archon_search/query_expansion_protocol.py). HyDE and RAG Fusion depend
// on it; every query-expansion adapter — including the new
// LlamaCppQueryExpansionProvider — implements it.
//
// Load-bearing contract: these operations MUST NEVER raise. Any failure
// (transport error, timeout, unreachable llama-server, malformed response,
// or a non-2xx HTTP status / HTTPStatusError such as a 503 with a
// well-formed-but-error body — S8) is surfaced as `null` / empty list so
// the caller silently falls back to plain search. This preserves ADR
// C4/C5's silent-fallback invariant.
//
// No change to the protocol shape itself — the seam is documented here so
// the new adapter is held to the exact same signature and never-raise rule.
//
// Privacy: the ADR 05 no-raw-query / fingerprint-only logging invariant
// applies to this contract — the raw QUERY guarantee lives here (this is
// the actual query-expansion contract); only `_query_fingerprint(query)`
// may appear in logs, never the raw query string (S15a).

namespace ArchonSearch.QueryExpansion;

// A hypothetical document generated for HyDE, or null on any failure.
alias HypotheticalDoc = string | null;

model GenerateHypotheticalDocParams {
  query: string;
  maxTokens?: int32 = 200;
  timeoutSeconds?: float64 = 10.0;
}

model DecomposeQueryParams {
  query: string;
  numQueries?: int32 = 3;
  maxTokens?: int32 = 450;
  timeoutSeconds?: float64 = 10.0;
}

// The seam both llama_cpp and every other provider must satisfy.
interface QueryExpansionProvider {
  // Never raises. Returns a hypothetical document, or null on failure.
  generateHypotheticalDoc(params: GenerateHypotheticalDocParams): HypotheticalDoc;

  // Never raises. Returns decomposed sub-queries, or [] on failure.
  decomposeQuery(params: DecomposeQueryParams): string[];
}

// Module helper (query_expansion_protocol.py:provider_key_available).
// Gains a `llama_cpp` branch that always reports true — a local provider
// needs no API key, exactly like `ollama` and `claude_cli`.
model ProviderKeyAvailability {
  provider: string;
  keyAvailable: boolean;
}
