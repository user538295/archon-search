# Feature Brief: Multilingual Graph Extraction Engine (GLiNER-class, ONNX) — Entities + Relations

**ID:** 2026-08-19-035 · **Type:** Enhancement (successor work, not an incident defect)
**Status:** Committed 2026-08-19 (owner decision, incident-review follow-up of
[[2026-08-19-030-graph-spacy-download-retry-and-ingest-abort-brief.md]]) —
**unconditional engine replacement covering entities AND typed relations** (owner decisions
2026-08-21, superseding the earlier eval-gated, entities-only framing). Prioritize after the
2026-08-19 incident bug briefs (010–080) ship. **One brief, not a series.**

## Problem

Two independent gaps, both in the graph's prose path, both closed by the same model call.

**Gap 1 — entities are English-only.** With `multilingual = true`, three retrieval layers honor
the promise (multilingual embedder, multilingual reranker, per-chunk language detection) but
prose entity extraction reads English only (`en_core_web_sm`) — non-English prose (Hungarian,
for the deployment that motivated this) contributes almost nothing to the entity graph,
silently. Two structural limitations compound it:

- spaCy's fixed news-corpus label set is force-fitted to the graph schema through a lossy
  mapping (`_LABEL_TO_ENTITY_TYPE`, `graph_extractor.py:131-143`): GPE/LOC/FAC all collapse into
  `system`, and domain concepts outside the news taxonomy are never extracted.
- spaCy has no official Hungarian model at all, so per-language spaCy routing cannot close the
  gap for this deployment either.

**Gap 2 — typed relations need a server that isn't there.** Every prose edge the graph builds
on its own is an untyped `related_to` co-occurrence edge. The three meaningful types
(`uses`, `implements`, `depends_on`) exist only when the LLM enrichment AND-gate is open —
`provider is not None AND extraction_model is not None AND enrichment_client is not None`
(`graph_extractor.py:579-583`) — one HTTP call per chunk, to a provider that was **down for the
entire duration of the incident this brief series descends from**. An air-gapped or
provider-less deployment gets no typed edges at all, in any language.

## Goal

One in-process, serverless, deterministic extraction engine — the **only** prose extraction
engine in the codebase — that in a single forward pass per chunk:

- (a) extracts entities from prose in all corpus languages,
- (b) emits the graph's **own** entity types directly (`person`, `system`, `event`, `concept` —
  the mapping table is deleted),
- (c) emits the graph's **own** relationship types directly (`uses`, `implements`,
  `depends_on`) with no LLM, no network, and no provider configuration, and
- (d) leaves spaCy fully removed from the source tree, dependency set, wizard, status surface,
  and docs, and the LLM relationship-labelling path deleted along with it.

## Users & Context

Operators indexing multilingual corpora (this deployment: Hungarian + English, mostly source
repos where prose = documentation), and every air-gapped deployment — which today gets untyped
edges by construction. The code-symbol half of the graph is unaffected: it comes from the AST
path, is language-independent, and already produces typed edges (`calls`, `imports`, `defines`,
`inherits`) without an LLM. This brief brings the prose half up to that standard.

There are **no production users at this time**. That is the licence for an unconditional swap
with no dual-engine period and no data migration: everyone re-ingests after the change.

## Core Flow

1. Wizard (graph enabled): downloads the pinned GLiNER-class ONNX artifact(s) (model +
   tokenizer files) into `<data_dir>/models/`, verifies checksums, smoke-loads, and **asserts
   the checkpoint actually supports relation extraction** before reporting success (see Edge
   Cases — a non-RelEx model accepts the `relations` argument and silently returns nothing).
2. Ingest, per batch of prose chunks: one `inference(texts, labels=<EntityType names>,
   relations=<RelationshipType names>, threshold=…, relation_threshold=…, batch_size=…)` call
   returning `(entities, relations)`.
3. Entities above `threshold` → nodes / mentions / co-occurrence `related_to` edges exactly as
   today (`GraphExtractor.extract` seam and everything downstream unchanged).
4. Relations above `relation_threshold` → typed edges, `head` → `source_node_id`, `tail` →
   `target_node_id`, merged **additively** alongside the `related_to` edges — the existing merge
   semantics at `graph_extractor.py:810-813`, unchanged. Distinct `relationship_type` values
   produce distinct stable edge IDs (`make_stable_edge_id`, `graph_types.py:104`), so there is
   no key collision and no override.
5. Code-symbol chunks (`symbol_type != None`) bypass the engine entirely, as today.
6. Community summarisation still uses the LLM enricher — unchanged, still optional (see Out of
   Scope).
7. Operators re-ingest every collection after upgrading. Stated in the release notes; not
   automated, not migrated.

## In Scope

### Engine

- **Engine adapter behind the existing NER seam** (`_run_ner_sync`, `graph_extractor.py:454-469`),
  emitting `EntityType` **and** `RelationshipType` values directly. `_LABEL_TO_ENTITY_TYPE`
  deleted.
- **Call `inference()`, never `predict_entities()`** — the entities-only method cannot return
  relations, and switching later would mean re-plumbing the seam.
- **Load through the `gliner` package in ONNX mode** (owner decision 2026-08-21):
  `GLiNER.from_pretrained(<data-dir path>, load_onnx_model=True, onnx_model_file=…,
  session_options=…, revision=…, local_files_only=True)`. We keep the controls that matter —
  our own `ort.SessionOptions` pins the CPU execution provider, `revision` pins the artifact,
  `local_files_only` guarantees no run-time network call — and the package supplies span
  decoding, RelEx adjacency reconstruction (`adjacency_threshold`, `packing_config`),
  threshold handling, batching, and RelEx capability detection.
- **Batched inference** via `inference(batch_size=…)`. Not an optimization to add later:
  `_run_ner_sync` currently runs one text per call (`graph_extractor.py:463-468`), which is
  correct for spaCy and wasteful for a transformer. This is how the throughput guardrail is met.
- **CPU execution provider only** — the CoreML 146-partition lesson from
  [[2026-08-19-040-double-embedder-load-startup-brief.md]]; do not repeat it here.
- **`flat_ner=True` and `multi_label=False` pinned**, with the reason recorded in code (see Key
  Decisions) so neither is "improved" later.

### Config

- `[graph].ner_confidence` and `[graph].relation_confidence` — two knobs, both defaulted from
  the spike, both documented in `archon-search.toml.example` alongside `leiden_resolution` /
  `max_community_size`. Neither is wizard-prompted; graph tuning knobs never are.

### Provisioning

- **Generic model-artifact provisioning in the wizard** — name → pinned URL → **checksum** →
  extract → smoke-load → **capability assert**. This does **not** exist today:
  `install/extras.py:221` `_download_spacy_model` is spaCy-wheel-specific (compatibility-table
  lookup → GitHub wheel URL → unzip → wheel-layout assert) and verifies no checksum. Building it
  is part of this brief and it is critical path — with spaCy gone there is no fallback engine.
- **Publish the real download size in the wizard's pre-download prompt.** The artifact is orders
  of magnitude larger than `en_core_web_sm`'s ~12 MB; an unannounced multi-hundred-MB download
  is a bad first run.

### Deletions

- **Full spaCy removal.** Blast radius, verified: `graph_extractor.py` (resolver, disclosure,
  warnings, label map), `paths.py:94` `get_spacy_models_dir`, `install/extras.py` (downloader +
  compatibility-table fetch), `install/__init__.py:48-51` (four re-exported public symbols),
  `install/wizard.py:654`, `install/config_writer.py:409`, `model_validation.py`
  (`graph_ner_status`, `provider_notes`), `pipeline.py:646-655` (spaCy-not-importable fatal
  abort), `server/app.py` `_check_graph_deps`, and docstring references in `graph_types.py`,
  `defref_extractor.py:456`, `eval/backends.py:95`. Tests: `test_install_spacy_model.py`,
  `test_install_ui.py`, `test_graph_extractor.py`. Docs: `OperatorGuide/20`, `/60`, `/90`,
  `Architecture/110`, `archon-search.toml.example:415-435`. `pyproject.toml` drops `spacy` from
  `[graph]` (:42) and `en-core-web-sm` from the dev group (:81, :174); `[graph]` gains `gliner`.
- **LLM relationship-labelling removal** — precise, not total. Delete `label_relationships` from
  `graph_enrichment_protocol.py:61` and all four adapters (`enrichment/anthropic.py:167`,
  `openai.py:117`, `ollama.py:115`, `llama_cpp.py:150`), the `LabeledRelationship` dataclass, the
  `json_schema` response-format constraint built for it (`enrichment/llama_cpp.py:76`), the
  narrowed-3-value-subset note (`enrichment/__init__.py:9`), the enrichment AND-gate
  (`graph_extractor.py:579-583`), the per-chunk call site (`:738`), and the `llm_fallback_used`
  / `llm_edges` plumbing that exists only to serve it.
- **Removal of `GET /status.provider_notes`** (`server/schemas.py:245`, `routes_status.py:325`,
  `model_validation.py:67`) plus its `BREAKING.md` entry — see Key Decisions.

### Verification

- Throughput, memory, and determinism **regression tests** (see Guardrails).
- Spike deliverable, run **before** implementation: checkpoint selection against the three-way
  constraint below, ONNX availability, tokenizer window measurement on worst-case chunks, and a
  ~20-chunk-per-language eyeball comparison of entities and relations against current output.

## Out of Scope

- **Deleting the LLM enricher.** `summarize_community` is a separate method on the same protocol,
  consumed by `community_builder.py:384` for abstractive community summarisation. The protocol,
  the four adapters, `[graph].provider`, `[graph].extraction_model`, and the timeout / rate-limit
  / token-budget fields all survive. Only the relationship half goes.
- **Per-language model routing — rejected** (owner decision 2026-08-19): graph node identity is
  `hash(entity_type + name)` (`make_stable_entity_id`), so mixed engines/models produce split
  nodes for the same real-world entity across languages — routing fragments exactly what the
  graph exists to connect.
- **A `spacy | gliner` engine choice — rejected** (owner decision 2026-08-21): a supported second
  engine means spaCy can never be deleted and the lossy mapping table survives forever.
- **Data migration / dual-engine transition — rejected** (owner decision 2026-08-21): no
  production users exist. Re-ingest is the whole story.
- **A hand-labeled quality benchmark — dropped** (owner decision 2026-08-21): the swap is
  committed, so a quality gate cannot change the outcome. `archon_search/eval/` would have been a
  poor host anyway — it is a *retrieval* harness (`recall@k`, `MRR`, `nDCG` over `QueryEvalTrace`,
  `eval/runner.py`, `eval/types.py`), with no span-level scoring concept.
- **New relationship types.** The engine is prompted with the existing three prose types; the
  code-symbol types (`calls`, `imports`, `defines`, `inherits`) stay AST-derived, and
  `synonym_of` stays with the synonym detector.
- Community/PageRank machinery; non-prose chunks; nested or multi-label entity spans.

## Key Decisions

- **2026-08-19 (owner): GLiNER-class single multilingual engine**, over (a) spaCy
  `xx_ent_wiki_sm` (coarse 4-type model; its training set does not cover Hungarian), (b)
  per-language routing (fragmentation, above), (c) LLM extraction (rejected in 030).
- **2026-08-21 (owner): unconditional replacement, not an opt-in.** No config flag, no migration,
  no eval gate on adoption. Everyone re-ingests.
- **2026-08-21 (owner): relations ship in this brief, not a successor.** The capability comes
  from the same forward pass; deferring it would mean re-opening the same seam, re-running the
  same spike, and re-provisioning a different checkpoint.
- **Direct-to-schema labels, for both entities and relations**: the engine is prompted with the
  graph's own `EntityType` and `RelationshipType` names. The biggest single quality lever, and
  independent of the language question.
- **2026-08-21 (owner): if no single checkpoint is multilingual AND RelEx AND ONNX, load two
  models — and the memory guardrail is the go/no-go.** Full capability in every language is the
  target; a doubled resident footprint against an OOM-descended guardrail is the price, and
  guardrail 1 decides whether it ships. Named fallback if it fails: entities-only, with the LLM
  relationship path retained rather than deleted. That fallback is a *measured* retreat, not a
  planning assumption — the brief is written for the two-model outcome.
- **2026-08-21 (owner): the `gliner` package runs the model, not raw `onnxruntime`.** The only
  argument for hand-rolling inference was a lighter `[graph]` extra, and it is false:
  `transformers` 5.8.1, `tokenizers` 0.22.2, `torch` 2.13.0+cpu, `huggingface-hub` 1.26.0 and
  `onnxruntime` 1.28.0 are already resolved in `uv.lock` — `transformers` via `docling-core` /
  `docling-ibm-models`, and docling is a **base** dependency. Reimplementing span decoding and
  RelEx adjacency reconstruction would buy nothing and fail silently when subtly wrong.
- **`flat_ner=True`, `multi_label=False`.** Both defaults, both deliberate. Overlapping spans
  ("Apache Kafka" *and* "Kafka") and multi-labelled spans each become separate unlinked nodes
  under `hash(entity_type + name)`, and co-occurrence edges grow as N*(N-1)/2 over whatever NER
  emits — noise compounds quadratically.
- **`provider_notes` is removed, not kept empty** (2026-08-21): the field exists solely to carry
  the English-only disclosure (`BREAKING.md:19` records exactly that) and has no other producer.
  A permanently-empty wire field is dead surface. Removal is a REST contract change → mandatory
  `BREAKING.md` entry per CLAUDE.md.
- **Confidence thresholds are config knobs** (2026-08-21, owner — over hardcoded constants):
  GLiNER scores every span and every relation, spaCy scored nothing, so these dials are new
  surface either way, and per-corpus precision/recall tuning is the point.
- **Truncation over splitting**: chunks exceeding the model window are truncated at the window
  and the event is logged **once per process** (the `_ner_failure_logged` latch pattern,
  `graph_extractor.py:420`). Chunk boundaries are already arbitrary; a second boundary policy
  inside the extractor is complexity for a case the spike must show is rare.
- **Model confidence does not feed salience.** `graph_inspector.py:96` already derives salience
  three ways from mentions (`frequency` / `tfidf` / `importance`). Extraction confidence is a
  different quantity and would muddy a working signal.

## Guardrails (regression tests, not adoption gates)

The swap is committed, so these do not decide *whether* to swap — but guardrail 1 does decide
the one-model-vs-two question above.

1. **Memory**: steady-state addition **flat** across ≥1,000 chunks, in the style of brief 010's
   test. Extraction inputs are uniform-shape tokenized text — growth is a defect, not expected
   behavior. Highest-value guard of the three: this brief descends from an OOM, and it now
   proposes loading up to two transformers where a 12 MB statistical model used to be.
2. **Throughput**: total ingest wall-time regression ≤ 10% on the incident corpus replay,
   reference machine Apple Silicon, CPU EP. Note the baseline is generous where the LLM gate was
   open — one local forward pass replaces one network round-trip per chunk.
3. **Determinism**: greedy span decoding, no sampling — repeated runs byte-identical for both
   entities and relations, asserted.

## Edge Cases & Constraints

- **A non-RelEx checkpoint fails silently.** GLiNER's own docs: "for models that do not support
  relation extraction, the relations parameter is ignored and the response will omit the
  relations key" (`docs/serving.md`). A wrong checkpoint therefore degrades to today's untyped
  graph with no error anywhere. The wizard's smoke-load must **assert relations come back**, and
  a startup check must too — not a comment in the operator guide.
- **The ONNX artifact may not exist pre-built.** GLiNER ships `convert_to_onnx.py` and documents
  converting + quantizing yourself; published checkpoints are PyTorch. "Download a pinned ONNX
  artifact" may mean pinning a community pre-converted build or converting and hosting it
  ourselves. This shapes the provisioning step, which is already critical path — the spike
  answers it **first**, not last.
- **`transformers` version negotiation.** GLiNER's supported range may be 4.x while the lock
  resolves 5.8.1. Not a conflict: nothing in this project pins `transformers` (`pyproject.toml`
  never names it; neither docling package sets an upper bound in the lock), so the resolver can
  step it down for everyone. Confirm in the spike — a docling regression from a downgrade would
  be discovered by the existing suite, not by this brief's guardrails.
- **No fallback engine after the swap.** Today a missing spaCy model degrades to code-symbol-only
  extraction with a wire-facing warning. That degradation path is retained verbatim for the new
  artifact — but it is now the only safety net.
- **Chunk length vs model window**: `chunk_size` is **512 GPT-2 tokens**, not characters
  (`config.py:240`, `chunker.py:20` — `RecursiveChunker(tokenizer="gpt2")`). English chunks land
  near a 512-token window rather than comfortably inside it, and the entity + relation label
  prompts consume part of the window too — more of it than an entities-only prompt would.
- **Relations are directed; co-occurrence edges are not.** `make_stable_edge_id(a, b, t) !=
  make_stable_edge_id(b, a, t)` (`graph_types.py:112-113`), and the `related_to` path
  deliberately normalizes to `source_id < target_id` to stay de-facto undirected. GLiNER's
  `head`/`tail` map straight onto the directed form — do **not** apply the lexicographic
  normalization to typed edges.
- **Relation extraction is not free of the entity threshold.** Relations reference extracted
  spans, so raising `ner_confidence` silently starves `relation_confidence`. The two knobs
  interact; document it where both are defined.
- **PyPI cannot carry the model** (same constraint as the spaCy model — `pyproject.toml:78-81`
  documents it): the wizard/data-dir artifact channel is the only end-user path. Confirm the
  chosen checkpoint's license and pin model + revision + checksum.
- **Language detection is broken** ([[2026-08-19-070-fasttext-numpy2-language-detection-brief.md]]):
  every chunk is tagged `language=""`. Not a blocker (the engine is language-blind by design),
  but the spike's Hungarian sample must be selected by hand, not by language filter.
- **`_NAME_SPLIT_PATTERN`** (`graph_extractor.py:146`) splits entity names on `/`, `,`, and
  ` and ` — tuned for spaCy span text. Re-check against GLiNER's span boundaries; it may be
  redundant or actively harmful.
- Where 010's parse-worker isolation lands, decide deliberately whether extraction runs in the
  worker or in-process (uniform shapes make in-process acceptable if guardrail 1 holds).

## Open Questions

*(Implementation-level — for `/plan-maker` or the spike, not feature-level blockers.)*

- Exact checkpoint(s) against the three-way constraint: multilingual coverage (esp. Hungarian),
  RelEx architecture, ONNX availability. Candidates to evaluate: `urchade/gliner_multi-v2.1`
  (multilingual, entities only), `knowledgator/gliner-token-relex-v1.0` (RelEx, coverage
  unverified), the `knowledgator/gliner-multitask-*` line, and GLiNER2 (205M, unified NER +
  classification + structured extraction — newer, lower source reputation, different relation
  API).
- Default values for `ner_confidence` and `relation_confidence` (GLiNER's own default is 0.5 for
  both).
- Whether `concept` as a zero-shot label over-extracts on technical prose, and whether the three
  prose relation labels are the right prompt wording for a zero-shot model (`depends_on` in
  particular reads as jargon).

## Future Iterations

- Custom entity and relation types per collection — zero-shot labels make this nearly free once
  the engine is in, but `EntityType` / `RelationshipType` are closed enums (`graph_types.py:50-71`)
  and node identity hashes the type, so opening them is a schema change, not a prompt change.
- Reconsider whether community summarisation can also go local, once one transformer is already
  resident and measured.

## Recommendation

Folding relations in is the right call and it is what makes this feature worth its cost: the
multilingual fix alone helps one deployment, while local typed relations close a capability gap
for **every** air-gapped install and delete four adapter implementations, a protocol method, and
an AND-gate along the way. The hardest part is no longer the engine — it is the **checkpoint
constraint**: multilingual, RelEx-capable, and ONNX-exportable at once, which may not exist in a
single published model, which is why the spike runs before implementation rather than alongside
it. What must not be compromised: guardrail 1, and the smoke-load capability assert. This brief
descends from an OOM incident and now proposes up to two resident transformers; and a checkpoint
that silently ignores the `relations` argument would ship a feature that looks present, passes
every test that doesn't check for it, and does nothing.

## References

- **Team plan:** [2026-08-19-035-multilingual-graph-ner-team-plan.md](./2026-08-19-035-multilingual-graph-ner-team-plan.md)
- [[2026-08-19-030-graph-spacy-download-retry-and-ingest-abort-brief.md]] — decision trail, engine comparison table, the spaCy provisioning path this brief deletes
- [[archon_search/graph_extractor.py]] — `_LABEL_TO_ENTITY_TYPE` (:131-143), `_NAME_SPLIT_PATTERN` (:146), `_run_ner_sync` seam (:454-469), enrichment AND-gate (:579-583), `label_relationships` call site (:738), additive edge merge (:810-813)
- [[archon_search/graph_enrichment_protocol.py]] — `LabeledRelationship` + `label_relationships` (to delete), `summarize_community` (to keep)
- [[archon_search/graph_types.py]] — `EntityType` (:50-57), `RelationshipType` (:60-71), `make_stable_edge_id` direction semantics (:104-114)
- [[archon_search/install/extras.py]] — `_download_spacy_model` (:221), the non-generic provisioning step to replace
- [[BREAKING.md]] — `provider_notes` origin (:19), and where its removal is recorded
- GLiNER `inference()` API, flat/nested NER, RelEx capability detection, ONNX conversion: `docs/api/gliner.model.md`, `docs/usage.md`, `docs/serving.md`, `docs/convert_to_onnx.md` (github.com/urchade/gliner)
- GLiNER: Zaratiana et al., "GLiNER: Generalist Model for Named Entity Recognition using Bidirectional Transformer", NAACL 2024 — verify chosen checkpoint + license at spike time
