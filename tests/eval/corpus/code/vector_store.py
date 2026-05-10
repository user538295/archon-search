"""Simple in-memory vector store with cosine similarity search."""
import math
from dataclasses import dataclass, field


@dataclass
class VectorEntry:
    id: str
    vector: list[float]
    payload: dict


@dataclass
class VectorStore:
    _entries: list[VectorEntry] = field(default_factory=list)

    def add(self, entry: VectorEntry) -> None:
        self._entries.append(entry)

    def search(self, query: list[float], top_k: int = 5) -> list[tuple[float, VectorEntry]]:
        scored = [(self._cosine(query, e.vector), e) for e in self._entries]
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:top_k]

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
