# ADR-002: Sentence-Aware Chunking with Fixed Token Overlap

**Status**: Accepted  
**Date**: 2026-01-15

## Context

Documents must be split into chunks before embedding. Three strategies were evaluated:

1. **Fixed-size character splits** — fast but splits mid-sentence.
2. **Sentence splits** — clean semantics but chunk length varies wildly (1–2000 tokens).
3. **Fixed-size token windows with sentence alignment** — predictable size, clean boundaries.

## Decision

Use 512-token windows with 64-token overlap, aligned to sentence boundaries.

```python
def chunk_text(text, chunk_size=512, overlap=64):
    sentences = split_sentences(text)
    return sliding_window(sentences, chunk_size, overlap)
```

## Consequences

- Chunks near the configured size boundary may be slightly over/under if the next sentence is long.
- Code files use a different splitter (function-boundary aware).
- Average chunk count ≈ document_tokens / (chunk_size - overlap/2).
