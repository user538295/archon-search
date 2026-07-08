# archon-search notes

## Feature ideas

### One Major lifecycle gap remains: def/ref cleanup on TTL/maintenance-only chunk expiry (not on explicit delete or sync/watcher delete).

### Handle more file type without file size limit.

**Resolved by E0d.** The 1 MB limitation no longer exists. Large files (PDF and all other supported formats) ingest at any size when no size guard is configured (`[ingest].max_file_mb = 0`, the default). Operators can set a size ceiling via `[ingest].max_file_mb` in `archon-search.toml`; exceeding it returns HTTP 413 / MCP `code="file_too_large"` with an actionable message. See `Documentation/Backlog/e0d-pdf-large-file-support-team-plan.md` for full details.

### The user can search between the visited websites and if he asks questions in a topic, then these websites also could help him to recall and use those information.

A chrome extension could be written to use it to ingest the current website content and link to be able to use later and be indexed. In this case a new type of sources could be added to the archon-search. A challange can be if it is stored in one collections, because many various type of contents would be stored in one collection (from cooking recipes to technical deep dive in documentation and other researches). It could be hard to find the right collection for the app.

### Link frontier model chat history into archon-search

It would be nice if the user could search in the cloud AI chat history as well. Many times the users use more AI providers in the same topic and in this case we could connect the similar topic into one merged search. For example is the user develops an app like Financialwell.app and he has chats in two providers like Claude and ChatGPT, it would be powerfull if the user or another LLM could use this search and could have a hollistic view and access to these contens in this topic. The user don't need to remember wher does this topic have discussed before, just talk wiht his LLM and the LLM will remember (find) the right conversation and use it.
We should ingest the whole chat history from the beginning. Need to solve the sync and handle rate limits as well.
First we should support ChatGPT, Claude and Perplexity. Is there any services to be able to have access all of them via one MCP, API interface?

### Handle 30+ collections better

If we have more collections in the future, then we should think over the handling of the search between collections. Now we have a heuristic, to determine in which collections should be search (not all of them), but later we should handle it even better. Eg.: What if we put another layer to collection search? The collections should have a larger more detailed description about what are in that collection and run a very fast and effective search on these descriptions to determine which collections will fit the best. We should define what does the more detailed collection mean. I expect that the 

### multilanguage support 

### video trans-scripting with video frame linking. Finf the word or expression and show me the related video image (or seek the video there and pause it (and the user can play it))

### cloud drive support

sync, then index them, remove the file form the disk but keep the indexes on the drive and let check the search with the rag is it usable or not (check drive online to check file change, then pull  index and remove local copy?)

### connect to be able to search in apple notes too with automatic re-ingesting feature

### Ultra tier

Honest answer: no ultra tier within fastembed's registry. The ceiling is ~2.3 GB for embedders and ~1.1 GB for rerankers. An M4 Pro 48 GB won't be RAM-constrained by any of these — the constraint is fastembed's
  ONNX model catalog, not hardware.

  However, two genuinely different options exist that could form an "ultra" tier — but they require architectural changes, not just bigger models:

  ---
  Option 1: ColBERT late-interaction — jinaai/jina-colbert-v2

  This is a fundamentally different retrieval mechanism. Instead of one vector per document, it stores per-token vectors and scores with MaxSim at query time. fastembed has it (jinaai/jina-colbert-v2, 2.24 GB,
  multilingual, 8192 token context), but archon-search's store layer is built around single dense vectors — supporting ColBERT would need LanceDB multi-vector index support added to store.py.

  Benefit: much better precision on long documents and complex queries.

  Option 2: Hybrid sparse+dense — SPLADE/BM25 + dense

  fastembed has prithivida/Splade_PP_en_v1 (SPLADE++, 532 MB) and Qdrant/bm25 (BM25 multi-language). archon-search already does FTS (BM25) + dense RRF fusion, but SPLADE is learned sparse — much stronger than BM25.
   Requires a sparse vector column in the LanceDB schema.

  Benefit: better recall on exact-match and out-of-domain queries.

  For the M4 Pro specifically

  The real advantage on that machine isn't model size — it's CoreML acceleration. The existing max tier models running on the Neural Engine will be significantly faster than the same models on CPU. The current
  configure_providers() already handles this.

  Summary recommendation:

  ┌──────────────────────────┬──────────────────────────────────────────────────────┬───────────────────────────┐
  │           Tier           │                     Feasibility                      │       What it needs       │
  ├──────────────────────────┼──────────────────────────────────────────────────────┼───────────────────────────┤
  │ Minimal / Balanced / Max │ Works today, just config changes                     │ --profile flag in install │
  ├──────────────────────────┼──────────────────────────────────────────────────────┼───────────────────────────┤
  │ Ultra-ColBERT            │ Requires store.py multi-vector index work            │ Significant               │
  ├──────────────────────────┼──────────────────────────────────────────────────────┼───────────────────────────┤
  │ Ultra-Hybrid SPLADE      │ Requires sparse vector schema + SPLADE embedder path │ Moderate                  │
  └──────────────────────────┴──────────────────────────────────────────────────────┴───────────────────────────┘

  If you want an "ultra" install option that works without architecture changes today, the answer is: there isn't one within fastembed. The max tier is the hardware ceiling for the current design.
  
### Pointer-based memory plus retrieval keys

Description. Store full artefacts (transcripts, notes, KB entries) externally. Inject into the LLM prompt only:

- a compact query,
- constraints (time range, project, participants),
- top‑k pointers (document IDs + minimal metadata),
- optionally 1–3 verbatim “evidence” spans.

This can beat any dialect because you stop paying repeated tokens for the same history.

This is aligned with the MemPalace architecture itself: it stores verbatim content and uses summaries/metadata primarily as a routing layer; AAAK is explicitly framed as a separate compression layer, not the storage default. 

Expected reduction. Effective reduction is dominated by “how much text you don’t send”. In steady-state agent systems, 10×–1000× reductions vs naïvely pasting full history are common in principle (highly workload-dependent).

- Pros. Best token economy; high fidelity if retrieval is correct; supports audits with verbatim evidence.

- Cons. Requires retrieval infra; failure mode is “missed evidence” rather than “bad compression”.

- Complexity. Medium–High.

- Compatibility. High with tool calling / RAG pipelines.

- Recommended use cases. Meeting transcripts; large KB; long-term agent memory; compliance contexts.

#### Implementation steps.

Normalise artefacts into segments (turns/paragraphs) with stable IDs.
Index with embeddings + metadata filters (project/date/participants).
At query time: retrieve top‑k segments; optionally re-rank.
Provide LLM with (a) IDs + (b) minimal snippets.
Only fetch verbatim spans after the model commits to which IDs are needed.
Example encoding (prompt injection).

