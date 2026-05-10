"""Deterministic eval-only embedder and reranker backends — FEAT-039 Task 2.5.

These backends are:
- Deterministic: SHA-256-based hashing, no set() iteration, stable order.
- Query-sensitive and corpus-aware: scores vary with actual text content.
- Label-blind: encode() and predict() accept only text, no metadata.
"""
from __future__ import annotations

import hashlib
import math
import re


def _tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokenization."""
    return re.findall(r"[a-z0-9]+", text.lower())


def _token_to_vec(token: str, dim: int = 128) -> list[float]:
    """Map a token to a dim-dimensional float vector via SHA-256 with block salting."""
    token_bytes = token.encode("utf-8")
    block_size = 32  # SHA-256 produces 32 bytes
    vec: list[float] = []
    block = 0
    while len(vec) < dim:
        digest = hashlib.sha256(block.to_bytes(4, "little") + token_bytes).digest()
        for byte in digest:
            if len(vec) >= dim:
                break
            vec.append((byte - 127.5) / 127.5)
        block += 1
    return vec


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec[:]
    return [v / norm for v in vec]


_DIM = 128
_UNIFORM_UNIT = [1.0 / math.sqrt(_DIM)] * _DIM


class EvalEmbedderBackend:
    """SHA-256-based token hash embedder for eval harness use only.

    Satisfies the EmbedderBackend protocol.
    """

    model_name: str = "eval-sha256-v1"

    def encode(self, texts: list[str]) -> list[list[float]]:
        result: list[list[float]] = []
        for text in texts:
            tokens = _tokenize(text)
            if not tokens:
                result.append(_UNIFORM_UNIT[:])
                continue
            # Sum token vectors (with repetition for TF weighting)
            agg = [0.0] * _DIM
            for token in tokens:
                tvec = _token_to_vec(token)
                for i in range(_DIM):
                    agg[i] += tvec[i]
            result.append(_l2_normalize(agg))
        return result


class EvalRerankerBackend:
    """BM25-inspired lexical reranker for eval harness use only.

    Satisfies the RerankerBackend protocol.
    Higher score = more relevant.
    """

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        scores: list[float] = []
        for query, doc in pairs:
            scores.append(self._score(query, doc))
        return scores

    @staticmethod
    def _score(query: str, doc: str) -> float:
        query_tokens = _tokenize(query)
        doc_tokens = _tokenize(doc)

        if not query_tokens or not doc_tokens:
            return 0.0

        doc_tf: dict[str, int] = {}
        for t in doc_tokens:
            doc_tf[t] = doc_tf.get(t, 0) + 1

        # Deduplicate query terms while preserving first-occurrence order
        seen_query: dict[str, None] = {}
        for t in query_tokens:
            seen_query[t] = None

        k1 = 1.5
        score = 0.0
        for term in seen_query:
            tf = doc_tf.get(term, 0)
            if tf == 0:
                continue
            tf_norm = (tf * (k1 + 1)) / (tf + k1)
            score += tf_norm
        return score
