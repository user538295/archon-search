# Ingestion Pipeline

## Overview

Documents flow through five stages before they are searchable:

```
Input → Parse → Chunk → Embed → Store → Index
```

## Stage 1: Parse

Supported input formats: plain text (`.txt`), Markdown (`.md`), Python source (`.py`), PDF (`.pdf`), HTML.

PDF and HTML are converted to Markdown via Docling before chunking. Binary formats not in this list are rejected with `400 Unsupported media type`.

## Stage 2: Chunk

Text is split into overlapping windows (default: 512 tokens, 64-token overlap). Sentence boundaries are respected—a chunk never starts mid-sentence.

Code files use a code-aware splitter that splits at function/class boundaries rather than token count.

## Stage 3: Embed

Each chunk is encoded into a dense vector using the collection's configured embedding model. Embedding is batched (32 chunks per call) for throughput.

## Stage 4: Store

Chunks and their vectors are written to LanceDB in a single batch transaction. The document record is inserted into the relational metadata store.

## Stage 5: Index

LanceDB rebuilds the HNSW index after each batch. For large ingestions, pass `defer_index=true` to skip this step and call `POST /collections/{name}/reindex` manually when done.
