// C5 — Stored `language` value seam (internal logical seam; no wire-shape change).
//
// Layer edge: Interface Adapters (the detector's result, written by `SearchPipeline` onto
// `ChunkRecord.language`) -> Frameworks & Drivers (`SearchStore`: the LanceDB `language` column,
// the `language` search filter, and the dominant-language lookup that picks the FTS tokenizer).
//
// The column stays a free-form UTF-8 string and keeps its three states. Only the value domain of
// NEW ingests narrows: always two-letter ISO 639-1 (Norwegian as `nb` / `nn`). Rows written
// earlier by fasttext-predict keep their codes until re-ingested — they may be three-letter codes
// or `no` — and every reader (filter, dominant-language vote, tokenizer map) must keep working on
// a collection that mixes old and new codes. No schema version bump, no backfill.
//
// Logical only.

namespace StoredLanguageContract;

scalar Iso6391Code extends string;

union StoredLanguage {
  /// Legacy or detection-off state: the document was never tagged. Excluded by explicit
  /// `language` filters and by the dominant-language vote; surfaced by the /status warning.
  untagged: "",

  /// Detection ran and did not produce a confident supported language.
  unknownLanguage: "unknown",

  /// A new-ingest tag.
  code: Iso6391Code,

  /// A tag written before this change; read-only legacy, never written again.
  legacyCode: string,
}

model ChunkLanguage {
  language: StoredLanguage;
}

interface TokenizerSelection {
  /// The most common tag among a collection's chunks, ignoring `untagged` and `unknownLanguage`,
  /// that selects the FTS tokenizer; a code outside the tokenizer map falls back to English.
  dominantLanguage(collection: string): StoredLanguage | void;
}
