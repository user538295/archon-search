# Evaluation Metric Functions

## NDCG@K

Normalized Discounted Cumulative Gain at position K. Rewards finding highly-graded documents early.

```python
import math

def ndcg_at_k(ranked_doc_ids: list[str], labels: dict[str, int], k: int) -> float:
    """Compute NDCG@K given ranked doc IDs and a grade dict."""
    def dcg(ids):
        return sum(
            labels.get(doc_id, 0) / math.log2(i + 2)
            for i, doc_id in enumerate(ids[:k])
        )
    actual = dcg(ranked_doc_ids)
    ideal = dcg(sorted(labels, key=lambda d: labels[d], reverse=True))
    return actual / ideal if ideal > 0 else 0.0
```

## Recall@K

Fraction of relevant documents (grade > 0) that appear in the top-K results.

```python
def recall_at_k(ranked_doc_ids: list[str], labels: dict[str, int], k: int) -> float:
    relevant = {d for d, g in labels.items() if g > 0}
    retrieved = set(ranked_doc_ids[:k])
    if not relevant:
        return 0.0
    return len(relevant & retrieved) / len(relevant)
```

## MRR

Mean Reciprocal Rank: the average of (1 / rank of first relevant result) across queries.

```python
def mrr(ranked_doc_ids: list[str], labels: dict[str, int]) -> float:
    for rank, doc_id in enumerate(ranked_doc_ids, start=1):
        if labels.get(doc_id, 0) > 0:
            return 1.0 / rank
    return 0.0
```
