# Search Concepts

## Semantic Search vs Keyword Search

**Keyword search** matches exact words. It is fast, explainable, and works well for code identifiers and proper nouns. It fails on synonyms, paraphrases, and language variation.

**Semantic search** encodes text into a dense vector and retrieves documents with similar vectors. It understands meaning, not just tokens. It performs poorly when the exact term must be matched (e.g., function names, error codes).

**Hybrid search** combines both: semantic retrieval for broad recall, keyword boosting for precision on technical terms.

## Embeddings

An embedding is a fixed-length vector (e.g., 384 dimensions) that represents the semantic meaning of a text chunk. Texts with similar meaning cluster together in vector space.

Models differ in:
- **Dimension**: higher is usually more accurate but slower
- **Context window**: maximum input tokens; text beyond is silently truncated
- **Language support**: most models are English-focused

## Approximate Nearest Neighbour (ANN)

Exact nearest-neighbour search is O(n) per query. For large corpora, ANN indexes (HNSW, IVF) reduce this to O(log n) with a small recall trade-off. LanceDB uses HNSW by default.

## Reranking

After ANN retrieval, a cross-encoder model re-scores each (query, candidate) pair. Cross-encoders are slower but more accurate than bi-encoders. Always rerank before returning results to the user.
