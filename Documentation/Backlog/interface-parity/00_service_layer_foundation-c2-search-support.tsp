// C2 — Shared search-support modules moved to services/ (search budget, request HyDE, scope validation).
namespace SearchSupportContract;

// Core types, opaque here.
model SearchPipeline {}

model Embedder {}

model SearchWarmup {
  rerank: boolean;
  ftsOnly: boolean;
}

model HyDEOutcome {
  vector: float64[] | null;
  applied: boolean;
  expansionWarning: string | null;
}

// Stable wire-visible text that every adapter imports instead of restating.
model SharedWireText {
  hydeExpansionFailedWarning: string;
}

// Raised by runWithinBudget, and only by it, when the overall budget itself elapsed; a plain exception with no fields.
// Adapters map it to their own timeout response.
model SearchBudgetExceeded {}

// Module constants adapters import: SEARCH_TIMEOUT_SECONDS is bound to each adapter's own `_SEARCH_TIMEOUT_SECONDS` alias,
// EMBEDDER_WARMUP_WAIT_SECONDS is read by routes_search for its embedder-load wait.
const SEARCH_TIMEOUT_SECONDS = 30.0;
const EMBEDDER_WARMUP_WAIT_SECONDS = 0.5;

interface SearchBudget {
  // Never raises. Pass the embedder serving the request; fan-out callers omit it.
  warmupForSearch(pipeline: SearchPipeline, embedder?: Embedder): SearchWarmup;

  // Returns what the awaited coroutine returns (T). The default timeout is the module constant
  // SEARCH_TIMEOUT_SECONDS bound when the function is defined, so adapters pass their own alias explicitly.
  op runWithinBudget<T>(awaitable: T, timeoutSeconds?: float64): T;
}

// Opaque core types the HyDE inputs carry.
model HyDEGeneratorHandle {}

model HyDEConfigHandle {}

// One request's HyDE inputs (the RequestHyDE dataclass). `suppressed` is computed by forRequest, never by an adapter.
model RequestHyDEInputs {
  query: string;
  requested: boolean;
  suppressed: boolean;
  generator: HyDEGeneratorHandle | null;
  config: HyDEConfigHandle | null;
  timeoutSeconds: float64;
}

interface RequestHyDE {
  // Class method used by every adapter (all arguments but the query are keyword-only in code). Computes `suppressed` via hydeSuppressed.
  forRequest(
    query: string,
    requested: boolean,
    ragFusion: boolean,
    ragFusionEnabled: boolean,
    graphMode: string | null,
    generator: HyDEGeneratorHandle | null,
    config: HyDEConfigHandle | null,
    timeoutSeconds: float64
  ): RequestHyDEInputs;

  // Instance method on the inputs. Embeds the hypothesis with the embedder serving the request;
  // embedderWarm is keyword-only in code and is false when the request degrades to FTS-only.
  resolve(inputs: RequestHyDEInputs, embedder: Embedder, embedderWarm: boolean): HyDEOutcome;
}

// Module-level functions that keep their names because tests import them and patch resolve_hyde_within_budget by string.
interface HyDEBudget {
  // True when RAG Fusion (and its kill-switch) or any graph mode wins over HyDE, so no LLM call is made and no warning is reported.
  hydeSuppressed(ragFusion: boolean, ragFusionEnabled: boolean, graphMode: string | null): boolean;

  // Awaits one HyDE generation under the search budget; returns null when the budget elapsed. Other exceptions propagate.
  op resolveHydeWithinBudget(generation: float64[] | null, timeoutSeconds: float64): float64[] | null;
}

interface ScopeFilterValidation {
  // Returns an error message, or null when the filter is acceptable (a null filter is acceptable).
  validateScopeFilter(scopeFilter: string | null): string | null;
}
