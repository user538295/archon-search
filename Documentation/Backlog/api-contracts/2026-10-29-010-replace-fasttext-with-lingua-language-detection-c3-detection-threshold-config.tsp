// C3 — Detection confidence-threshold configuration seam (user-facing config contract).
//
// Layer edge: Interface Adapters (`SearchConfig`, parsed from `[database]` in the TOML file)
// -> Use Cases (`SearchPipeline`, passed to the detector on every call).
//
// The key name `language_detection_confidence_threshold` and its place under `[database]` are
// unchanged. The numeric default (today 0.7, tuned to fastText probabilities) is NOT carried
// over: lingua's confidence scale is different, so the default is recalibrated on real sample
// documents including Hungarian (Q1). Today the default is written in two places (the config
// default and the `SearchPipeline` constructor default); after this change one place owns it.
// Whether a minimum distance between the top two languages is added, and whether the accepted
// range stays (0.0, 1.0], are open (Q1, Q2).
//
// Logical only: no default value is stated here because none has been decided.

namespace DetectionThresholdContract;

model DetectionSettings {
  /// `[database].multilingual` — turns language detection on.
  multilingual: boolean;

  /// `[database].language_detection_confidence_threshold` — the gate between a code and `unknown`.
  confidenceThreshold: float32;
}

/// Reported at config load, naming the key, when the value is outside the accepted range.
model InvalidDetectionThreshold {
  key: "language_detection_confidence_threshold";
  reason: string;
}

interface DetectionSettingsValidation {
  validate(settings: DetectionSettings): void | InvalidDetectionThreshold;
}
