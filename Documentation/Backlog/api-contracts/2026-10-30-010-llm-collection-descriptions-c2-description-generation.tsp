// C2 — Description generation seam (internal logical seam).
//
// Layer edge: Use Cases (ingest pipeline, describe job, startup backfill) -> Use Cases
// (description generation), which depends on the shared LLM client (C1) and writes through the
// shared write-description operation (C11).
//
// What changes: today one module-level call returns text or nothing, and every failure looks the
// same. The new seam returns a typed outcome so the caller can record `ok` or `failed`.
// What stays: the 20% regeneration rule that decides WHEN generation runs (it lives with the caller).
// The sample is drawn across the whole collection (not the first rows in insertion order), sized to
// the window and capped by `max_sample_chunks` (`defaultMaxSampleChunks` in C4, 100). Only open chunks
// (no ACL) are sampled unless the operator switch for local providers is on (C4, C5).
// Sampling rule (deterministic, so S10, S55 and S82 are reproducible), read through the
// `DescriptionStore` port (C3), not through a concrete store: the port lists the chunk ids of the
// open rows (`acl` null, through the store's SQL filter helpers, never an f-string; with the switch on
// for a local provider, restricted rows join the pool) SORTED BY `chunk_id` ascending, never in stored
// row order (stored order shifts with compaction and re-ingest, so "the same rows" would not stay the
// same). Let M be that count and K = `max_sample_chunks` (`defaultMaxSampleChunks` in C4, 100). Fit
// algorithm: start with N = min(K, M); the picks are the ids at positions floor((j + 0.5) * M / N)
// for j = 0 .. N-1, spaced over ALL M rows; read their excerpts (port `readExcerpts`) and cost them
// with the client's counting rule (C1: exact, estimate plus margin, or nothing for a cloud provider,
// where N stays min(K, M)). If the cost exceeds the input budget (C1, sized for the template WITH the
// retry reminder), set N = min(N - 1, floor(N * budget / cost)) and draw again, re-spacing over all M
// rows; N strictly decreases, so it ends. A small window therefore yields fewer excerpts from the whole
// collection and never the head of it, and no tail is cut afterwards. If not even N = 1 fits, the
// outcome is `failed` / `callFailed` with a detail that names the window (the startup WARNING already
// says it is small). Today `sample_chunk_texts` (store.py lines 2543-2568) does none of this: it takes
// the first N rows with no ACL filter, only shuffles that window, and returns `[]` on ANY error
// (tests/test_e0c_be1_sample_shuffle.py pins that shuffle and is rewritten; the helper is replaced
// by the two port calls). An error reading the rows is NOT an empty result: the port raises, and the
// generation settles `failed` / `sampleReadFailed`, never `noUnrestrictedChunks` (which means the
// read worked and found zero open rows).
// Order of checks: the source is read FIRST; a collection whose source is `manual` makes no provider
// call at all (the lifecycle answers `refusedManual`), and a collection with no open chunk (switch off)
// makes none either (`noUnrestrictedChunks`). The sample is read AFTER the lifecycle's generation slot
// is held (C5), so a describe that waited for the slot samples the collection as it is then. The write
// re-checks the source inside the lock (C3, C11).
// Output rules: the prompt asks for one line of plain text, at most 600 characters (`maxDescriptionChars`, C4). The result is
// cleaned (thinking tags removed, whitespace collapsed to one line) and accepted when non-empty and
// within 600 characters. Otherwise ONE automatic retry with the same single stateless message plus
// a reminder of the limit; provider errors are never retried. If the retry is still too long it is
// trimmed to the last full sentence within 600 characters and stored. If nothing usable remains
// (empty or thinking tags only) nothing is stored, the status is `failed`, an earlier description is
// kept and a WARNING is logged (pointing to the thinking setting). On a cloud provider a retry sends
// the excerpts twice. Each attempt has its own timeout (`timeout_seconds`).
//
// Logical only.

namespace DescriptionGenerationContract;

/// Excerpts already sized to the model's input budget (C1).
model DescriptionSample {
  collectionName: string;
  excerpts: string[];
}

enum DescriptionFailureReason {
  /// Provider unreachable, key missing, timeout, prompt rejected, or any call failure (not retried).
  callFailed,
  /// Output was empty or thinking tags only, even after the retry.
  unusableOutput,
  /// The collection has no open chunk and restricted chunks are not allowed (the read worked).
  noUnrestrictedChunks,
  /// The chunks could not be read (store error); distinct from `noUnrestrictedChunks`.
  sampleReadFailed,
  /// The description could not be saved, for example the collection lock timed out.
  saveFailed,
}

model DescriptionGenerated {
  text: string;
  /// True when the text was trimmed to the last full sentence after the retry.
  trimmed: boolean;
}

model DescriptionFailed {
  reason: DescriptionFailureReason;
  /// Operator-readable detail, free of document text.
  detail: string;
}

/// Generation never raises to its caller: failure is a value that becomes `description_status = failed`.
/// A failed outcome never replaces an existing description.
union DescriptionOutcome {
  generated: DescriptionGenerated,
  failed: DescriptionFailed,
}

interface DescriptionGenerator {
  generate(sample: DescriptionSample): DescriptionOutcome;
}
