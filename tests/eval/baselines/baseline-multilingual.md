# Multilingual Eval Baseline — C2 Task 11.1

Captures the before/after recall@5 comparison for non-English content introduced
in C2 (multilingual retrieval).

## Fixture summary

- **Collection**: `fr-docs` (French documentation)
- **Documents**: 5 French Markdown files
- **Queries**: 5 retrieval-scope queries in French targeting `fr-docs`
- **Backend**: deterministic eval backend (SHA-256 token hashing — no real model weights)

## Before/after recall@5 comparison

| Model / config           | Corpus                  | Recall@5 on fr-docs queries |
|--------------------------|-------------------------|-----------------------------|
| English-only baseline    | 33 queries / 57 docs    | N/A (fr-docs not present)   |
| Multilingual eval (C2)   | 38 queries / 62 docs    | 1.0                         |

The deterministic SHA-256 eval backend tokenizes French text the same way as
English (lowercase alphanumeric tokenization). French accented characters (é, è,
à, etc.) are not preserved by the tokenizer, but enough Latin-script tokens
remain to produce meaningful cosine similarity — all 5 French queries recall
their gold document within the top 5 results.

## Overall metric comparison

| Metric                  | Before C2 (English-only) | After C2 (with fr-docs) | Delta       |
|-------------------------|--------------------------|-------------------------|-------------|
| recall_at_1             | 0.8704                   | 0.8594                  | -0.0110     |
| recall_at_3             | 0.9630                   | 0.9688                  | +0.0058     |
| recall_at_5             | 1.0000                   | 1.0000                  | 0.0000      |
| mrr                     | 1.0000                   | 1.0000                  | 0.0000      |
| ndcg_at_5               | 0.9879                   | 0.9887                  | +0.0008     |
| ndcg_at_10              | 0.9879                   | 0.9887                  | +0.0008     |
| routing_accuracy        | 0.9394                   | 1.0000                  | +0.0606     |
| routing_mrr_centroid    | 0.6667                   | 0.7917                  | +0.1250     |
| routing_mrr_hybrid      | 0.6667                   | 0.7917                  | +0.1250     |

## Notes on recall_at_1 drop

The recall_at_1 floor dropped by 0.0110 (within the 0.05 no-waiver threshold).
This is expected: the 5 new French queries each contribute a recall@1 vote. The
SHA-256 eval backend ranks French tokens by cosine similarity — accented
characters are stripped, leaving shorter keyword sequences that create near-tie
scenarios. recall@5 remains 1.0 (all gold docs found in top 5), confirming no
coverage regression.

## Routing improvement

Adding `fr-docs` with a distinctly French centroid (French keywords produce
unique SHA-256 token vectors) improves routing MRR from 0.6667 to 0.7917. The
fr-docs collection is unambiguously distinct from English collections at the
centroid level.
