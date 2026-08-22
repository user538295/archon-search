// C5 — `[graph]` engine settings: two confidence knobs plus an execution-provider list
// (internal logical seam / config contract).
//
// Layer edge: Frameworks & Drivers (TOML parsing + validation) -> Interface Adapters
// (the extraction engine that reads them). The two confidence knobs are NOT wizard-prompted:
// graph tuning knobs never are. `[graph].providers` IS wizard-owned (Q7). All three are
// documented in the example config beside the existing community-tuning knobs.
//
// The two confidence knobs interact and the interaction is not obvious: relations reference
// extracted spans, so raising the entity threshold starves the relation threshold of
// candidates. The documentation for both must say so in the same place.

namespace GraphConfidenceConfig;

/// The fields this feature adds to the `[graph]` config block.
model GraphEngineSettings {
  /// Minimum engine score for a span to become a node and a mention.
  /// Float in the half-open range (0.0, 1.0]; a boolean is rejected before coercion,
  /// matching the existing per-field validation idiom for the sibling threshold knobs.
  /// Default 0.5 (Q2) — the model card's precision-leaning end.
  nerConfidence: float32;

  /// Minimum engine score for a relation to become a typed edge. Only relations between
  /// spans that already cleared `nerConfidence` are candidates. Same range and validation.
  /// Default 0.75 (Q2) — deliberately stricter than the entity knob.
  relationConfidence: float32;

  /// ONNX execution providers for the graph engine, as its OWN setting (Q7) rather than a
  /// hardcoded CPU pin. This FOLLOWS the split-providers ADR (per-model-role provider
  /// configuration, probed at install time) instead of departing from it.
  ///
  /// Written by the wizard through the same `configure_providers()` pattern the embedder and
  /// reranker settings use; `--graph-providers` is the non-interactive CLI flag. Null means
  /// the field was never written, which the engine reads as CPU.
  providers: string[] | null;
}

/// Two-stage provider selection (Q7). Neither stage is an extra wizard step the operator has
/// to sit through: stage 1 is free, stage 2 rides work that is mandatory anyway.
enum ProviderProbeStage {
  /// (1) BEFORE download: host availability only — `_available_providers()` / GPU detection.
  /// No accelerator on the host means no offer, CPU written silently, zero cost.
  availability_precheck,

  /// (2) AFTER download: the MANDATORY smoke-load and capability assert, pointed at the
  /// accelerator instead of at CPU — real validation against the real artifact, in the shape
  /// of the existing post-download re-probe. Two importable ONNX runtimes is a FAILURE here,
  /// never a swallowed exception (Q26).
  post_download_validation,
}

/// What the wizard does with the probe result. Only after stage 2 passes is anything offered.
enum ProviderOffer {
  /// Either stage failed — no prompt at all, CPU written silently. The operator is never
  /// asked to adjudicate a probe they cannot act on.
  none_cpu_written,

  /// Both stages passed — the wizard OFFERS the accelerator with the accelerator as the
  /// default, and the operator may step down to CPU.
  accelerator_offered_default_on,
}

/// Deliberately NOT config (Q29), recorded so it is not "promoted" later by accident.
/// The relation adjacency threshold is a PINNED CONSTANT at the model card's midpoint
/// (~0.6), with its reason next to it in code. Its effect on output is indirect and cannot
/// be measured without span-level scoring the retrieval-scoped eval harness does not have,
/// so a knob would be an unfalsifiable dial. The spike confirms the value; promotion to a
/// setting requires evidence.
model PinnedNonConfigurable {
  adjacencyThreshold: float32;
}

/// Fields explicitly retained on the same config block, so the deletion's boundary is
/// unambiguous: they now gate community summarisation only, not relationship labelling.
model RetainedEnrichmentKnobs {
  provider: string | null;
  extractionModel: string | null;
  extractionTimeoutSeconds: int32;
  extractionRateLimitRpm: int32;
  extractionTokenBudget: int32;
}

interface GraphConfigParsing {
  /// Rejects a boolean, coerces to float, then range-checks; raises a config error with a
  /// sanitized message naming the offending key.
  parseConfidence(key: string, raw: string): float32;
}
