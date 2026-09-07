// C1 — Prose extraction engine seam (internal logical seam).
//
// Layer edge: Interface Adapters (GraphExtractor) -> Frameworks & Drivers (PyTorch engine backend).
// Replaces the private per-text spaCy seam `GraphExtractor._run_ner_sync(nlp, texts)`, which
// returns one `(entity_text, spacy_label)` list per text and loops one model call per text.
//
// ONE checkpoint, decided (Q1): `knowledgator/gliner-relex-multi-v1.0`, driven through
// `gliner>=0.2.26` and loaded with `GLiNER.from_pretrained(model_name, revision=...)` — fetched
// from HuggingFace at the pinned revision on first use, the literal mirror of
// `embedder.py:37-45`'s lazy-load-behind-a-lock. There is no second model and no
// per-checkpoint fan-out anywhere in this seam.
//
// SUPERSEDED (2026-08-24, team plan Scope -> Provisioning): an earlier resolution of the Spike
// gate had us hosting this checkpoint ourselves as a tarball of its 7-file PyTorch snapshot,
// verified and published by C4's `ArtifactProvisioner`, loaded with
// `GLiNER.from_pretrained(local_dir, local_files_only=True)`. That design was superseded the
// same day, before any of it shipped: there is nothing for us to build, host or hand-verify —
// the checkpoint's files already sit on HuggingFace at a pinned revision — so the graph model
// now uses the house fastembed pattern instead, never `ArtifactProvisioner`. See `load()`,
// below, for the corrected load path.
//
// NOT an ONNX export, and this is a resolved decision, not an open one (Spike gate — RESOLVED
// 2026-08-24, team plan). Trace-based ONNX export cannot carry this model's relations at all:
// it freezes gliner's data-dependent pair-building to whatever the export-time dummy input
// happened to produce. The PyTorch path also measured LOWER resident and peak memory.
//
// Lives in its own module, `archon_search/prose_extraction_backend.py`, separate from
// `graph_extractor.py` — C4's installer-import constraint (the installer must not import
// `archon_search.graph_extractor`) applies to the latter, not this module. The installer no
// longer imports this module for a provisioner's smoke-load or capability assert — neither
// exists for the graph model any more (C4) — but it MAY still import it for the graph engine's
// own provider-validation probe (C5), via `ProvisioningBackendFactory`, below.
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
/// A bare `other` DECOY member was prompted here (Q3) until 2026-09-06: measured against
/// the pinned checkpoint it absorbed nearly every real span, all of which the real-label
/// filter then discarded, leaving real corpora with zero entities. It is gone; the four
/// real labels are prompted with their descriptions. The discard filter it motivated is
/// retained (see `BatchExtraction` below) and still drops any off-vocabulary span.
enum ProseEntityLabel {
  person,
  concept,
  system,
  event,
}

/// The graph's own relation vocabulary, handed to the engine as zero-shot labels — BARE,
/// without descriptions (the descriptive form returns zero relations from the pinned
/// checkpoint). `synonym_of` is excluded: it comes from embedding similarity (E2f), not RelEx.
///
/// As shipped this is every `RelationshipType` except `synonym_of`, so the code-symbol types
/// are prompted too — an earlier draft of this contract said they were NEVER prompted. Both
/// vocabularies were measured on the pinned checkpoint and the typed yield is a wash (3 typed
/// relations either way, differing in WHICH), so the shipped set stands rather than churn
/// extraction output for no measured gain. `related_to` earns its slot as the decoy the
/// removed `other` failed to be: dropping it costs a typed edge. Prompted here, still never
/// persisted — `_build_typed_relation_edge` discards it so the co-occurrence loop owns it.
enum ProseRelationLabel {
  uses,
  implements,
  depends_on,
  related_to,
  calls,
  imports,
  defines,
  inherits,
}

/// A prompted label paired with the one-line natural-language description the model is
/// actually conditioned on (Q3). The schema value is what gets stored; the description is
/// what disambiguates it. `depends_on` in particular reads as jargon on its own.
///
/// Only the ENTITY prompt set is built from these pairs. Relation labels are prompted bare
/// (the descriptive form returns zero relations from the pinned checkpoint); their
/// descriptions are retained solely as the documented meaning of each label. Where a
/// description IS prompted it is part of the contract: changing one changes extraction output
/// and therefore node identity, so they stay pinned in code beside the labels, never
/// assembled ad hoc at the call site.
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
/// spans decoded under any label outside the real entity schema are already dropped. (This
/// once named the `"other"` decoy specifically; the decoy was removed 2026-09-06 — see Q3 in
/// the team plan — but the filter it motivated is retained.)
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
/// `relationsSupported` was the structural answer to the silent-degradation hazard: a
/// checkpoint without relation-extraction heads accepts the relation labels and simply
/// omits relations, which is indistinguishable from "found none" without this flag.
///
/// AS SHIPPED (`archon_search/prose_extraction_backend.py:130-168`) this seam differs from
/// the logical model below in two ways, both deliberate:
///   - `relationsSupported` DOES NOT EXIST. It was only ever consumed by the provisioner's
///     capability assert, which was dropped with `ArtifactProvisioner` (C4/C6). With nothing
///     recording the result, the flag had no reader, so it was not built. The hazard it
///     named is therefore uncovered — recorded as such, not silently dropped.
///   - Chunk attribution is STRUCTURAL, not an index field. `BatchExtraction` carries
///     `chunks: ChunkExtraction[]`, aligned index-for-index with the input `texts`, and each
///     `ChunkExtraction` holds that chunk's own `entities` / `relations`. The `textIndex`
///     field on the two DTOs below is how this contract expresses the same attribution
///     logically; the shipped code expresses it by position instead. Likewise shipped
///     `ExtractedRelation` carries `head` / `tail` as the span TEXT, not as nested entity
///     objects. This file is logical-only by its own header, so those are shape choices, not
///     contradictions — the flag's absence is the real one.
model BatchExtraction {
  entities: ExtractedEntity[];
  relations: ExtractedRelation[];

  /// True when at least one input text exceeded the model window and was truncated.
  /// The caller logs this at most once per process.
  truncated: boolean;
}

/// What the engine reports about itself once loaded.
///
/// AS SHIPPED, only `loadCount` survives as real surface, and as a property rather than a
/// return value: `ProseExtractionBackend.load()` returns nothing, and the memory/concurrency
/// guards read `backend.load_count` / `backend.is_loaded` directly
/// (`prose_extraction_backend.py:229-239`, cited by name from the code). There is no
/// `EngineCapability` object. Its other three fields died with their only reader — the
/// provisioner's smoke-load and capability assert, dropped with `ArtifactProvisioner` for
/// this artifact (C4/C6): `relationsSupported` (see `BatchExtraction`, above),
/// `artifactName` / `artifactRevision` (the pinned identity now lives in exactly one place,
/// `paths.GRAPH_NER_MODEL_NAME` / `GRAPH_NER_MODEL_REVISION`, so re-reporting it off a loaded
/// model would be a second source of truth), and `activeProviders`. The device the model
/// actually landed on IS still observed, but at install time only, by `_prewarm_graph_model`
/// reading `model.device.type` and handing it to stage 2 of the device probe (C5) — never
/// off a runtime instance. This model is retained as the logical statement of what a load
/// would report if a reader for it were ever reintroduced.
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
  /// Load the pinned artifact via `GLiNER.from_pretrained(model_name, revision=...)` —
  /// `huggingface_hub` fetches it from HuggingFace on first use, mirroring
  /// `embedder.py:37-45`'s lazy-load-behind-a-lock (Scope -> Provisioning, team plan,
  /// 2026-08-24) — SUPERSEDING an earlier resolution of the Spike gate that had this load a
  /// pre-verified, self-hosted tree from `paths.get_graph_models_dir()` (Q6) via
  /// `ArtifactProvisioner` (C4). That accessor is NOT gone: it is now passed as the explicit
  /// `cache_dir` argument to `GLiNER.from_pretrained`, so `ARCHON_SEARCH_DATA_DIR` still
  /// relocates the graph model — only its role changed, from a pre-verified publish target C4
  /// wrote to, to a plain `huggingface_hub` cache directory this call manages itself. Runs at
  /// most once per process, behind a lock, off the event loop, and never inside lifespan
  /// startup's awaited path. Device placement is built from `[graph].providers` for the shared
  /// instance, or from the explicit list for a `ProvisioningBackendFactory`-built instance —
  /// that factory's caller changed too (see `ProvisioningBackendFactory`, below): no longer
  /// `ArtifactProvisioner`'s smoke-load, which no longer exists for this artifact, but the
  /// graph engine's own provider-validation probe (C5). Cancellation during
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
  /// knobs: flat single-label spans (pinned), and the relation adjacency threshold. On the
  /// latter this contract's earlier "pinned constant at the model card's midpoint (Q29)"
  /// reading was SUPERSEDED by the K2g spike and by C5: `adjacency_threshold` is inert on the
  /// pinned checkpoint, so nothing pins it — it is simply never passed
  /// (`prose_extraction_backend.py:417`).
  ///
  /// AS SHIPPED the two label lists are not parameters either: `inference(texts,
  /// ner_confidence, relation_confidence)` reads the entity and relation prompt sets from
  /// module-level constants, so a caller cannot vary node identity per call. The logical
  /// parameters below record what the call is conditioned on, not the Python signature —
  /// this file is logical-only by its own header.
  inference(
    texts: string[],
    entityLabels: LabelPrompt[],
    relationLabels: LabelPrompt[],
    entityThreshold: float32,
    relationThreshold: float32,
  ): BatchExtraction;
}

/// A second construction path (C1, C5), used ONLY by the graph engine's own
/// provider-validation probe — never by `GraphExtractor`. RECHARACTERISED 2026-08-24 (Scope ->
/// Provisioning, team plan): this factory originally existed for `ArtifactProvisioner`'s (C4)
/// mandatory post-download smoke-load, which no longer exists for the graph model — that
/// design was superseded before it shipped. The construction need it serves did not go away
/// with it: something must still build a backend instance against an explicit candidate
/// provider list, bypassing the process-wide shared instance, its load lock and its
/// once-per-process latch, so an accelerator can be validated against the real artifact before
/// the wizard offers it (Q7) without disturbing the shared instance the rest of the process
/// will load later. That caller is now the graph provider-validation probe (C5) — a
/// `model_validation.py`-resident check mirroring the shape of the embedder/reranker's own
/// post-download reprobe (`installer.py:247-261`, `_fe1_reprobe`) rather than riding a
/// smoke-load `ArtifactProvisioner` no longer performs.
///
/// AS SHIPPED THIS FACTORY DOES NOT EXIST — no symbol named `ProvisioningBackendFactory` or
/// `for_providers` is in the tree. The construction need it was invented for evaporated with
/// its second caller: stage 2 of the device probe does NOT build a backend under a candidate
/// device. It rides the optional pre-warm instead, which loads the checkpoint through
/// `GLiNER.from_pretrained` directly and reports `model.device.type` (`prewarm.py:178-212`,
/// graded by `device_probe.probe_device_validation` — see C5). The shared instance is never
/// disturbed because pre-warm never touches it: it runs in the wizard process, not as a
/// second instance inside the server. Retained here as the logical record of a construction
/// path that was designed and then not needed.
interface ProvisioningBackendFactory {
  forProviders(providers: string[]): ProseExtractionBackend;
}
