// C1 — Prose extraction engine seam (internal logical seam).
//
// Layer edge: Interface Adapters (GraphExtractor) -> Frameworks & Drivers (ONNX engine backend).
// Replaces the private per-text spaCy seam `GraphExtractor._run_ner_sync(nlp, texts)`, which
// returns one `(entity_text, spacy_label)` list per text and loops one model call per text.
//
// ONE checkpoint, decided (Q1): `knowledgator/gliner-relex-multi-v1.0`, self-exported to ONNX
// and hosted by us, driven through `gliner>=0.2.26`. There is no second model and no
// per-checkpoint fan-out anywhere in this seam.
//
// Logical only: no signatures, no ONNX session options, no tokenizer detail.

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

/// What the engine reports about itself once loaded — consumed by the background model
/// validation probe (Q9) and by the wizard's post-provision capability assert.
model EngineCapability {
  relationsSupported: boolean;

  /// Pinned artifact identity: checkpoint name plus the revision the artifact was pinned at.
  /// One artifact, always — see the header.
  artifactName: string;
  artifactRevision: string;

  /// The ONNX execution providers the loaded session actually resolved to. The configured
  /// list is `[graph].providers` (Q7); a step-down to CPU at load time must be visible here
  /// rather than inferred.
  activeProviders: string[];
}

/// The seam two roles agree on. One call per batch of prose texts; never per text.
/// Code-symbol chunks never reach this interface.
interface ProseExtractionBackend {
  /// Load the pinned artifact from `get_graph_models_dir()` (Q6). Runs at most once per
  /// process, behind a lock, off the event loop, and never inside lifespan startup's awaited
  /// path. Session options are built from `[graph].providers`; cancellation during load is
  /// re-raised and never latched.
  load(): EngineCapability;

  /// Report capability without running a forward pass over corpus text.
  capability(): EngineCapability;

  /// One forward pass over the whole batch.
  ///
  /// Two decoding parameters are deliberately NOT arguments here and NOT config knobs:
  /// flat single-label spans (pinned), and the relation adjacency threshold, which is a
  /// pinned constant at the model card's midpoint (Q29) because its effect is indirect and
  /// unmeasurable without span-level scoring the eval harness does not have.
  inference(
    texts: string[],
    entityLabels: LabelPrompt[],
    relationLabels: LabelPrompt[],
    entityThreshold: float32,
    relationThreshold: float32,
    batchSize: int32,
  ): BatchExtraction;
}
