import "@typespec/http";
using Http;

// C6 — `GET /status` -> `model_validation`, after the removal of `provider_notes`.
//
// HTTP/API seam. `GET /openapi.json` is the authoritative HTTP contract for this project,
// so this file models only the sub-object this feature changes, not the whole `/status`
// response. The removal is a REST contract change and requires a `BREAKING.md` entry.
//
// What changes:
//   - `provider_notes: list[str]` is REMOVED. Its only producer was the English-only prose
//     disclosure, which disappears with the engine that caused it. A permanently-empty wire
//     field is dead surface, so it is removed rather than kept empty.
//   - `provider_warnings` survives unchanged in shape. Its grading semantics are load-bearing
//     and unchanged: `GET /ready` -> `checks.models` is graded on this field alone, so only
//     actionable strings may go here. A failed relation-extraction capability assert IS
//     actionable (re-run the wizard) and belongs here; a permanent, unactionable property
//     does not, and after this change there is no longer a channel for one.
//   - No replacement channel is introduced (Q10). The absence is deliberate and is recorded
//     as such in ADR 12, so a future reader does not "restore" the field as an oversight.
//
// Where the graph engine's capability result comes from (Q9): the existing BACKGROUND model
// validation probe — the same non-blocking task that already fills `provider_warnings`.
// Nothing about the socket bind changes, the probe still never raises, and a failed
// relation-capability assert degrades to one actionable warning rather than failing startup.
// A graph-engine execution-provider step-down (`[graph].providers`, Q7) surfaces the same way.
//
// Not modelled and not changed by this feature: `embedder_ok`, `reranker_ok`,
// `llama_cpp_ok`, `validated_at` — carried through verbatim so the diff is legible.

@service(#{ title: "Archon Search Status — Model Validation" })
namespace StatusModelValidationApi;

// The `model_validation` sub-object of the `GET /status` response, in its post-change shape.
model ModelValidationStatus {
  // Null while validation is pending.
  embedder_ok: boolean | null;

  reranker_ok: boolean | null;

  // Null when llama_cpp is not the configured provider for any feature.
  llama_cpp_ok: boolean | null;

  // Actionable operator-facing strings only. Graded into `GET /ready` -> `checks.models`.
  // Newly carries the graph engine categories: artifact missing, artifact unusable,
  // relation-extraction capability not confirmed, and graph execution-provider step-down.
  // All four are produced by the background validation probe, never by a blocking check.
  provider_warnings: string[];

  // `provider_notes: string[]` was here. Removed by this feature.

  validated_at: utcDateTime | null;
}

// Trimmed to the field under contract; the real response carries many more sub-objects.
model StatusResponse {
  @statusCode statusCode: 200;
  model_validation: ModelValidationStatus;
}

@route("/status")
interface Status {
  @get status(): StatusResponse;
}
