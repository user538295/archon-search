// C1 — Prose extraction engine seam (internal logical seam).
//
// Layer edge: Interface Adapters (GraphExtractor) -> Frameworks & Drivers (PyTorch engine backend).
// Replaces the private per-text spaCy seam `GraphExtractor._run_ner_sync(nlp, texts)`, which
// returns one `(entity_text, spacy_label)` list per text and loops one model call per text.
//
// ONE checkpoint, decided (Q1): `knowledgator/gliner-relex-multi-v1.0`, hosted by us as a
// tarball of its 7-file PyTorch snapshot at the pinned revision (C4), driven through
// `gliner>=0.2.26` and loaded with `GLiNER.from_pretrained(local_dir, local_files_only=True)`.
// There is no second model and no per-checkpoint fan-out anywhere in this seam.
//
// NOT an ONNX export, and this is a resolved decision, not an open one (Spike gate — RESOLVED
// 2026-08-24, team plan). Trace-based ONNX export cannot carry this model's relations at all:
// it freezes gliner's data-dependent pair-building to whatever the export-time dummy input
// happened to produce. The PyTorch path also measured LOWER resident and peak memory.
//
// Lives in its own module, `archon_search/prose_extraction_backend.py`, separate from
// `graph_extractor.py` — C4's installer-import constraint (the installer must not import
// `archon_search.graph_extractor`) applies to the latter, not this module; the installer
// imports this module directly for the provisioner's smoke-load and capability assert.
//
// The module does NOT import `gliner`, `torch` or `transformers` at module level (K12) — those
// live inside `load()`'s body. Merely importing this module must stay cheap, for two different
// reasons at two different call sites: `GraphExtractor`'s module-level reference to
// `ProseExtractionBackend` is paid during `create_app`'s synchronous construction (`app.py:507`,
// before lifespan); the installer's reference to the same module, for the provisioner, is paid
// on every wizard run instead — `create_app` never imports the installer package. The cold
// multi-second import cost either site would otherwise pay is deferred entirely into `load()`'s
// body, off the event loop.
//
// Logical only: no signatures, no engine thread/device configuration, no tokenizer detail.

namespace ProseExtractionEngine;

/// The graph's own entity vocabulary, handed to the engine as zero-shot labels.
/// `code_symbol` is AST-derived and is NEVER part of the prompt set.
///
/// `other` is a DECOY (Q3): it is prompted so the model has somewhere to put spans that
/// belong to none of the four real types, and every span returned under it is DISCARDED.
/// Without it a zero-shot span decoder pushes those spans onto the nearest real label —
/// `concept` in practice — and over-extracts on technical prose.
enum ProseEntityLabel {
  person,
  concept,
  system,
  event,
  other,
}

/// The graph's own prose relation vocabulary, handed to the engine as zero-shot labels.
/// The code-symbol types (calls/imports/defines/inherits) and `synonym_of` are produced
/// elsewhere and are NEVER part of the prompt set.
enum ProseRelationLabel {
  uses,
  implements,
  depends_on,
}

/// A prompted label paired with the one-line natural-language description the model is
/// actually conditioned on (Q3). The schema value is what gets stored; the description is
/// what disambiguates it. `depends_on` in particular reads as jargon on its own.
///
/// Both prompt sets are built from these pairs. The descriptions are part of the contract:
/// changing one changes extraction output and therefore node and edge identity, so they are
/// pinned in code beside the labels, never assembled ad hoc at the call site.
model LabelPrompt {
  /// The stored schema value — a `ProseEntityLabel` or a `ProseRelationLabel` member name.
  label: string;

  /// One line, no examples, no enumeration of sub-cases.
  description: string;
}

/// One span the engine asserts, already carrying a graph entity type — there is no
/// intermediate label vocabulary and no mapping table.
model ExtractedEntity {
  /// Index into the batch's input texts; identifies which chunk the span came from.
  textIndex: int32;

  /// Span text exactly as the engine returned it — a real substring of the input, never a
  /// reconstruction. Node identity hashes this with `label`. No name-splitting or
  /// name-repair heuristic is applied on this path (Q27); the pattern that did so served
  /// the deleted LLM repair helper only.
  name: string;

  /// Character offsets of the span in its input text. Present because the engine returns
  /// real substrings; they make the span reviewable against the source during the spike.
  startChar: int32;
  endChar: int32;

  label: ProseEntityLabel;

  /// Engine confidence for the span. Compared against `[graph].ner_confidence`.
  score: float32;
}

/// One directed relation the engine asserts between two spans it also returned.
/// Direction is significant and MUST NOT be lexicographically normalised — unlike the
/// co-occurrence path, which sorts its endpoint ids to stay de-facto undirected.
model ExtractedRelation {
  textIndex: int32;

  /// The relation's source span. Refers to an entity in the same batch result.
  head: ExtractedEntity;

  /// The relation's target span. Refers to an entity in the same batch result.
  tail: ExtractedEntity;

  label: ProseRelationLabel;

  /// Engine confidence for the relation. Compared against `[graph].relation_confidence`.
  score: float32;
}

/// The batch result. Both collections are already threshold-filtered by the engine, and
/// spans decoded under the `other` decoy label are already dropped.
///
/// **`relations` is deduplicated by the adapter before it is returned (measured, not
/// hypothetical).** K2d recorded gliner's real PyTorch return shape verbatim and found the raw
/// relation list carries EXACT-duplicate `(head, tail, relation)` triples — the same triple 2x
/// to 4x, with byte-identical scores. That is the engine's own behaviour, not a defect to fix
/// upstream, so the adapter collapses identical triples and this collection never presents the
/// same `(head, tail, label)` twice. Deduplication happens here, at the seam, rather than being
/// left to each consumer: C2's edge-id hash would incidentally absorb duplicates on upsert, but
/// relying on that would make correctness depend on an accident nobody asserted, and any
/// consumer counting raw relation objects instead of persisted edges would silently inflate by
/// gliner's variable duplication factor.
/// `relationsSupported` is the structural answer to the silent-degradation hazard: a
/// checkpoint without relation-extraction heads accepts the relation labels and simply
/// omits relations, which is indistinguishable from "found none" without this flag.
model BatchExtraction {
  entities: ExtractedEntity[];
  relations: ExtractedRelation[];
  relationsSupported: boolean;

  /// True when at least one input text exceeded the model window and was truncated.
  /// The caller logs this at most once per process.
  truncated: boolean;
}

/// What the engine reports about itself once loaded — read by the provisioner's smoke-load
/// and capability assert (C4), and by the memory/concurrency guards that read `loadCount`
/// below (S26, S48). The background probe (Q9) never holds one of these: it reads
/// `capability_status.json` instead.
model EngineCapability {
  relationsSupported: boolean;

  /// Pinned artifact identity: checkpoint name plus the revision the artifact was pinned at.
  /// One artifact, always — see the header.
  artifactName: string;
  artifactRevision: string;

  /// The torch devices the loaded model actually resolved to (`cpu` / `cuda` / `mps`). The
  /// configured list is `[graph].providers` (Q7); a step-down to CPU at load time must be
  /// visible here rather than inferred. Shape is unchanged from the superseded ONNX reading —
  /// only the meaning of the strings moved from ONNX Runtime execution providers to torch
  /// device placement (Spike gate — RESOLVED 2026-08-24). C4's twin field carries the same
  /// wording (`86d088d5`).
  activeProviders: string[];

  /// Monotonic count of how many times `load()` has actually executed a real model load in
  /// this process — NOT the number of callers that reached the load lock (K10). This is
  /// backend surface, declared here so S26/S48's "exactly one load occurred across N
  /// concurrent callers" assertion has something to read: `activeProviders` alone is
  /// computable from `[graph].providers` with no model ever loaded, so it cannot discriminate
  /// "loaded once" from "never loaded."
  loadCount: int32;
}

/// The seam two roles agree on. One call per batch of prose texts; never per text.
/// Code-symbol chunks never reach this interface.
interface ProseExtractionBackend {
  /// Load the pinned artifact from `get_graph_models_dir()` (Q6). Runs at most once per
  /// process, behind a lock, off the event loop, and never inside lifespan startup's awaited
  /// path. Device placement is built from `[graph].providers` for the shared instance, or from
  /// the explicit list for a `ProvisioningBackendFactory`-built instance; cancellation during
  /// load is re-raised and never latched. **The intra-op and inter-op thread counts are pinned
  /// explicitly via `torch.set_num_threads` / `torch.set_num_interop_threads`, never left to
  /// torch's defaults** (K10, S28) — CPU reductions vary with thread count, so leaving it
  /// unpinned would make cross-process determinism (S28) undecidable: a difference could be the
  /// model or the thread count, and two runs sharing a process cannot even see the confound at
  /// all. The pin itself is unchanged in intent; only the API moved off `onnxruntime`'s
  /// `SessionOptions`, which does not exist on this path (Spike gate — RESOLVED 2026-08-24).
  /// **S42 clause 4 cites these lines as its source of truth — it must assert on the torch
  /// thread-count calls, not on a constructed `SessionOptions` object.**
  load(): EngineCapability;

  /// One forward pass over the whole batch. The caller (`GraphExtractor`) hands in the whole
  /// per-document batch of texts; sub-batching against `GRAPH_NER_SUB_BATCH_SIZE` happens
  /// INSIDE this call, against a pinned module constant — NOT a parameter here and NOT a
  /// `[graph]` config key (S53), so a caller cannot inadvertently bypass the throughput
  /// guardrail by passing an oversized batch straight through to one forward pass.
  ///
  /// Two decoding parameters are likewise deliberately NOT arguments here and NOT config
  /// knobs: flat single-label spans (pinned), and the relation adjacency threshold, which is
  /// a pinned constant at the model card's midpoint (Q29) because its effect is indirect and
  /// unmeasurable without span-level scoring the eval harness does not have.
  inference(
    texts: string[],
    entityLabels: LabelPrompt[],
    relationLabels: LabelPrompt[],
    entityThreshold: float32,
    relationThreshold: float32,
  ): BatchExtraction;
}

/// A second construction path (C1, C4), used ONLY by the artifact provisioner's smoke-load
/// and capability assert — never by `GraphExtractor`. It builds a backend instance against an
/// explicit candidate provider list, bypassing the process-wide shared instance, its load
/// lock and its once-per-process latch entirely. This is what lets the provisioner validate a
/// not-yet-configured backend — before the wizard has written `[graph].providers` — without
/// disturbing the shared instance the rest of the process will load later.
interface ProvisioningBackendFactory {
  forProviders(providers: string[]): ProseExtractionBackend;
}
