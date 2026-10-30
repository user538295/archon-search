// C1 — Language detector seam (internal logical seam).
//
// Layer edge: Use Cases (`SearchPipeline.ingest_file`, built by `create_pipeline` and `create_app`)
// -> Interface Adapters (`LanguageDetector`).
//
// What stays: the call shape the pipeline already depends on — one asynchronous call per document,
// the first 2000 characters of the text decide, the result is an ISO 639-1 code or `unknown`.
// What changes: the engine behind it (lingua instead of fasttext-predict) and construction — the
// detector no longer takes a model path, because the language models ship inside the package.
// What is deliberately NOT here: the confidence rule that decides `unknown` (Q1), the accuracy mode
// (Q4) and the language subset (Q3). Each is an open question in the team plan; this seam only
// states that a rule exists and is configurable (see C3).
//
// Logical only: no signatures beyond what two roles must agree on, no engine API.

namespace LanguageDetectorContract;

/// A two-letter ISO 639-1 code in the key space of the store's FTS tokenizer map
/// (for example `en`, `fr`, `hu`; Norwegian Bokmål is `nb`, Nynorsk is `nn`).
/// Never a three-letter ISO 639-3 code: every language the engine supports has an ISO 639-1 code.
scalar Iso6391Code extends string;

/// What a detection returns. `unknownLanguage` covers all of: empty or whitespace-only text,
/// confidence below the configured gate, and a language the engine does not support — an
/// unsupported language is never mapped to the nearest supported one.
union DetectionResult {
  code: Iso6391Code,
  unknownLanguage: "unknown",
}

/// The seam two roles agree on.
///
/// Behavioural promises:
///   - Async: the engine call runs off the event loop; the caller is never blocked.
///   - Only the first 2000 characters of `text` are examined; the rest is ignored.
///   - The detector is built once per process and reused; concurrent calls are safe.
///   - A detector that cannot run raises; the caller (the pipeline) turns that into a warning and
///     untagged chunks, never an aborted ingest.
interface LanguageDetector {
  detect(text: string, confidenceThreshold: float32): DetectionResult;
}
