// C2 — Shared search-support modules moved to services/ (search budget, request HyDE, scope validation).
namespace SearchSupportContract;

model SearchWarmup {
  rerank: boolean;
  ftsOnly: boolean;
}

model HyDEOutcome {
  vector: float32[] | null;
  applied: boolean;
  expansionWarning: string | null;
}

// Stable wire-visible text that every adapter imports instead of restating.
model SharedWireText {
  hydeExpansionFailedWarning: string;
}

// Raised when a search exceeds its time budget; adapters map it to their own timeout response.
model SearchBudgetExceeded {
  budgetSeconds: float64;
}

interface SearchBudget {
  warmupForSearch(embedderPresent: boolean): SearchWarmup;
  runWithinBudget(timeoutSeconds: float64): void;
}

interface RequestHyDE {
  resolve(): HyDEOutcome;
}

interface ScopeFilterValidation {
  // Returns an error message, or null when the filter is acceptable.
  validateScopeFilter(scopeFilter: string): string | null;
}
