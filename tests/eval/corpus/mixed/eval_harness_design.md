# Evaluation Harness Design

## Purpose

The eval harness measures retrieval quality offline, without a running search server, so changes to chunking, embedding, or ranking strategies can be tested before deployment.

## Corpus Layout

```
tests/eval/
  documents.jsonl   # manifest: {doc_id, collection, relative_path}
  queries.jsonl     # benchmarks: {query_id, text, collection, metric_scope}
  labels.jsonl      # relevance: {query_id, doc_id, grade}
  corpus/           # actual text files
  routing/          # routing-specific fixtures
    collections.jsonl
```

## Metrics

- **NDCG@K**: Normalized Discounted Cumulative Gain — primary ranking metric.
- **Recall@K**: Fraction of relevant documents found in top-K results.
- **Precision@K**: Fraction of top-K results that are relevant.
- **MRR**: Mean Reciprocal Rank — how high is the first relevant result?

## Relevance Grades

| Grade | Meaning |
|-------|---------|
| 0 | Not relevant |
| 1 | Relevant |
| 2 | Highly relevant (preferred result) |

## Running the Harness

```python
from archon_search.eval.fixtures import load_eval_corpus
from archon_search.eval.runner import run_eval

corpus = load_eval_corpus(Path("tests/eval"))
report = await run_eval(corpus, search_client)
print(report.ndcg_at_5)
```
