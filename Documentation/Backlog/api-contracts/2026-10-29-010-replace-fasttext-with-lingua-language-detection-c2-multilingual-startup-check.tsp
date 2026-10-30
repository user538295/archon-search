// C2 — Multilingual startup check seam (internal logical seam).
//
// Layer edge: Presentation (`create_app` and its `_check_multilingual_deps` startup guard)
// -> Interface Adapters (the detector package, resolved by import).
//
// Today the guard makes two checks: the fasttext-predict package is importable, and the
// `lid.176.ftz` model file exists. After this change there is one check: the lingua package is
// importable. There is no model file, so there is no second failure and no model-path helper.
// Both composition roots (`create_app` and `create_pipeline`) must build the detector the same
// way, without a model path.
//
// Logical only: the exact wording of the message is an implementation detail, but it MUST name
// the `multilingual` extra as the way to fix the problem and MUST NOT name fasttext.

namespace MultilingualStartupContract;

/// The only failure the guard reports.
model MultilingualDependencyMissing {
  /// The install extra that provides the missing package. Kept as `multilingual` unless Q5
  /// renames it.
  extra: "multilingual";

  /// Operator-facing remedy: the install command for the extra.
  remedy: string;
}

interface MultilingualStartupCheck {
  /// A no-op when multilingual is off. When on, passes silently if the lingua package is importable.
  check(multilingualEnabled: boolean): void | MultilingualDependencyMissing;
}
