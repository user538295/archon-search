// C5 — `[graph]` engine settings: two confidence knobs plus an execution-provider list
// (internal logical seam / config contract).
//
// Layer edge: Frameworks & Drivers (TOML parsing + validation) -> three readers across three
// layers: `GraphExtractor` (Interface Adapters) reads `ner_confidence` / `relation_confidence`
// and passes them to `inference()` (C1, S21); `ProseExtractionBackend` (Frameworks & Drivers)
// reads `providers` for its own session options; the wizard (Presentation) owns `[graph].providers`
// itself — the `ProviderProbeStage` / `ProviderOffer` decision below (Q7) is what writes it.
// The two confidence knobs are NOT wizard-prompted: graph tuning knobs never are.
// `[graph].providers` IS wizard-owned (Q7). All three fields are documented in the example
// config beside the existing community-tuning knobs.
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
  /// Written by the wizard through a GENERALISED `configure_providers()` — real installer
  /// work, not reuse of an existing pattern: today it is hardcoded to the `[database]`
  /// section, takes only a `GpuType`, and no-ops for `GpuType.NONE` (`installer.py:1111-1142`);
  /// this feature adds a section parameter, a provider-list parameter and an explicit
  /// CPU-write branch. `--graph-providers` is the non-interactive CLI flag. Null means the
  /// field was never written, which the engine reads as CPU.
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

/// REMOVED by the K2g spike (Q29 closed). `adjacency_threshold` was to be a pinned
/// constant at ~0.6. The spike proved it has NO EFFECT on
/// `knowledgator/gliner-relex-multi-v1.0`, three independent ways: the checkpoint's model
/// class lacks `relations_rep_layer`, so `forward` takes the `build_all_entity_pairs`
/// branch that never receives the parameter; and sweeping it over its full range produced
/// byte-identical output on both the ONNX and PyTorch paths. It is moot a second time over
/// under the Spike gate's entities-only outcome, where no relation pairing runs locally at
/// all. A contract field for an inert dial costs a reader a day of tuning for no effect,
/// so it is deleted rather than documented in place. Reinstating it requires a checkpoint
/// whose model class actually honours it.

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
  /// sanitized message naming the offending key. `raw` is the value as `tomllib` parsed it —
  /// e.g. `graph_cfg[key]` in `config.py` — NOT a string (M12): a `string`-typed parameter can
  /// never carry a TOML boolean, so "rejects a boolean before coercion" would be unsatisfiable
  /// against the signature this contract used to declare, and S47's boolean-input case
  /// (`true`) could not be expressed by a caller typed against it.
  parseConfidence(key: string, raw: unknown): float32;
}
