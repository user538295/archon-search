# Multi-Collection Routing Design

## Problem

When a user's query could match documents in multiple collections, which collection should be searched? Searching all collections is accurate but slow and expensive.

## Solution: Query Routing

A lightweight router classifies the query and selects the best collection before performing retrieval. This keeps per-query latency low while maintaining good recall.

## Routing Strategies

### Keyword-Based Routing
Fast. Assign each collection a list of trigger keywords. Route to the first match. Fails on ambiguous or paraphrased queries.

### Embedding-Based Routing
Each collection has a centroid vector computed from its document embeddings. Route to the collection whose centroid is closest to the query vector. Slower but more robust.

### LLM-Based Routing
An LLM (e.g., Claude Haiku) reads the collection descriptions and the query, then selects the best collection. Most accurate; adds ~300ms latency.

## POST /route API

```json
{
  "query": "how to configure rate limiting",
  "collections": ["code", "docs"],
  "strategy": "embedding"
}
```

Response:
```json
{
  "collection": "docs",
  "confidence": 0.87
}
```

## Evaluation

Routing accuracy is measured by checking whether the selected collection contains at least one relevant document for the query (from the ground-truth label set).
