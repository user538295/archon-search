// C2 — Request normalisation for search with context and the context-window rule (Use Cases, internal).
// The normaliser stays the ONE place a request is validated (services/search_command.py). The context-window rule is a function of that module beside the `top_k` maximum rule (`top_k_exceeded_detail` in services/service_error.py), and it takes the cap as a parameter read from the `[search]` config the service already holds.
namespace SearchWithContextNormalisationContract;

// Validation order follows `search` (brief Edge Cases): collection, then query, then the top_k lower bound, then the limits that need configuration in the order top_k above top_k_max, context_window outside its range, scope-filter syntax. The window check sits beside the top_k maximum, before the syntax rule; one characterization test fixes the order.
enum ValidationRule {
  collection_missing_or_blank,
  query_blank,
  top_k_below_minimum,
  top_k_above_maximum,
  context_window_out_of_range,
  scope_filter_syntax,
}

// The operator's limits the normaliser is given; none is a product constant.
// `context_window_max` comes from [search].context_window_max (config floor 1, default 10) and is read from the config the service already holds, no longer from the pipeline. `top_k_max` and `context_window_max` stay independent (brief Key Decisions); the 30 s budget is the backstop.
model NormalisationLimits {
  top_k_max: int32;
  context_window_max: int32;
}

// Fixed, sanitised detail text (ServiceError detail never carries an exception's text). The context-window text keeps today's wording from the pipeline (`context_window_error`, pipeline.py line 383): `context_window must be in [0, {max}]; got {n}`. REST answers it as `422 {"detail": "<text>"}` (the `_error_response` branch of the validation kind, routes_search.py line 165) for ANY out-of-range value, below 0 included, because the REST schema carries no minimum on `context_window`; MCP answers `validation_error` (the tool has no ge/le either); the CLI prints it after `Error:`.
model ContextWindowViolation {
  requested: int32;
  maximum: int32;
}

// A window of 0 is valid and means hits only. A window above `maximum`, or below 0, is a `validation` ServiceError naming the bound. The default call (window omitted = 1) can never be rejected: the config floor keeps the maximum >= 1.
// A missing (None) or blank collection raises the existing text `collection must not be empty`. Raised as a `validation` ServiceError whose detail is the fixed text of the first failing rule (in the order above).
model NormaliserInput {
  query: string;
  collection?: string;
  top_k?: int32;
  context_window?: int32;
  scope_filter?: string;
}

interface SearchWithContextNormaliser {
  normalise(input: NormaliserInput, limits: NormalisationLimits): ValidationRule | void;
}
