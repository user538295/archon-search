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
//   - `provider_warnings` survives unchanged in SHAPE, but its graph-engine CONTENT narrows,
//     2026-08-24 (Scope -> Provisioning, team plan) — see field doc, below, for what an
//     earlier draft of this contract described versus what actually has a producer now. Its
//     grading semantics remain load-bearing and unchanged: `GET /ready` -> `checks.models` is
//     graded on this field alone, so only actionable strings may go here.
//   - A missing `[graph]` extra or an unfetched graph model IS actionable (install the extra;
//     let the next real use trigger the lazy fetch, or re-run the wizard's optional pre-warm)
//     and belongs here — the successor to today's spaCy-era `graph_ner_status`'s
//     package/model-presence check (`model_validation.py:134`), now probing `gliner`
//     importability and a cheap, non-loading cache-presence check under
//     `get_graph_models_dir()`, mirroring `resolve_spacy_model`'s SHAPE, not its content.
//   - `relations_not_supported` and a `smoke_load_failed` sentinel are GONE from this field,
//     not merely unreachable-but-rendered-elsewhere: both were rendered from
//     `capability_status.json` (C4, `CapabilityStatusRecord`), written by
//     `ArtifactProvisioner`'s post-placement smoke-load/capability-assert stage for the graph
//     model. That stage no longer exists — the graph model is fetched lazily via
//     `GLiNER.from_pretrained` (C1), never through `ArtifactProvisioner` — so nothing writes
//     the sentinel this field's old rendering rule read. Q9 forbids the background probe from
//     loading the model itself to find out some other way, so there is no replacement source
//     for either category; this seam has nothing honest to report for them any more.
//   - A graph device step-down (`[graph].providers` configured above what was actually
//     validated) is likewise GONE from this field, for the same reason: the persisted
//     `activeProviders` record it was graded against was part of the same dead sentinel.
//     Device-accelerator validation still happens (C5's post-download reprobe, now a
//     standalone wizard-time call rather than a step inside `provision()`), but its result is
//     not persisted anywhere the LATER background probe can read — the same gap Known
//     limitations already names for a driver that disappears after install (team plan, "A
//     graph execution-provider step-down is detected only at provision time..."), now widened
//     to cover every step-down, install-time included, not only a post-install one.
//   - `conflicting_onnx_runtimes` stays OUT for the graph engine, as before this change: under
//     the PyTorch decision the graph engine never creates an `onnxruntime.InferenceSession`,
//     so there is no resolved session for two importable runtimes to make unpredictable. C4
//     retains it in the frozen enum for interface stability but declares it unreachable there
//     (`c4.tsp:222-251`), exactly as `license_declined` is retained and unreachable. The probe
//     must never render it for the graph model.
//   - No replacement channel is introduced for any of the removed categories (Q10). The
//     absence is deliberate and is recorded as such in ADR 12, so a future reader does not
//     "restore" any of them as an oversight.
//
// Where the graph engine's capability result comes from (Q9): the existing BACKGROUND model
// validation probe — the same non-blocking task that already fills `provider_warnings`.
// Nothing about the socket bind changes, the probe still never raises, and a failed
// relation-capability assert degrades to one actionable warning rather than failing startup.
// A graph-engine device step-down (`[graph].providers`, Q7 — torch device placement, not ONNX
// Runtime execution providers) surfaces the same way.
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
  // Newly carries ONE graph-engine category as of 2026-08-24 (Scope -> Provisioning, team
  // plan; see header for the full accounting) — narrowed from the four an earlier draft of
  // this contract described: the `[graph]` extra or the graph model itself is missing (a
  // cheap package-importability / cache-presence check, never a model load), produced by the
  // background validation probe exactly as today's spaCy-era `graph_ner_status` check is.
  // "Artifact unusable" (a `smoke_load_failed` sentinel), the recorded `relations_not_supported`
  // capability result, and a graph device step-down are all GONE, not merely unreachable: each
  // was rendered from `capability_status.json` (C4), which nothing writes for the graph model
  // any more — see header for why, and why there is no replacement source. `conflicting_onnx_runtimes`
  // is deliberately NOT among them — it is unreachable for the graph engine under torch
  // (`c4.tsp:222-251`), so rendering it here would advertise a fault this seam can never
  // actually observe.
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
