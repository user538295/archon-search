"""MultiCollectionRouter — centroid pre-ranking for RAG collection selection."""
from __future__ import annotations

import json
import logging
import math
from typing import TYPE_CHECKING, Any, Literal

import httpx

from archon_search.collection_meta import CollectionMeta
from archon_search.constants import DEFAULT_ROUTING_DESCRIPTION_WEIGHT
from archon_search.observability import record_stage

if TYPE_CHECKING:
    from archon_search.embedder import Embedder

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT = 10.0

# Fields used by the router — avoids issues with datetime deserialization
_ROUTING_FIELDS = {"name", "description", "centroid", "active_embedding_model", "doc_count", "chunk_count", "description_embedding"}


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class MultiCollectionRouter:
    """Routes a query to relevant RAG collections via centroid similarity pre-ranking."""

    def __init__(
        self,
        search_url: str,
        embedder: "Embedder",
        shortlist_size: int,
        confidence_threshold: float,
        embedding_model: str,
        initial_metadata: list[CollectionMeta] | None = None,
        strategy: Literal["centroid", "hybrid"] = "centroid",
        description_weight: float = DEFAULT_ROUTING_DESCRIPTION_WEIGHT,
    ) -> None:
        self._search_url = search_url
        self._embedder = embedder
        self._shortlist_size = shortlist_size
        self._confidence_threshold = confidence_threshold
        self._embedding_model = embedding_model
        self._strategy = strategy
        self._description_weight = description_weight

        self._cached_metadata: list[CollectionMeta] | None = (
            list(initial_metadata) if initial_metadata is not None else None
        )
        self._last_routable_names: list[str] = []
        self._decomposer_was_invoked: bool = False

    @property
    def last_routable_names(self) -> list[str]:
        """Names of routable collections from the last get_pre_context() call."""
        return self._last_routable_names

    @property
    def decomposer_was_invoked(self) -> bool:
        """Whether the decomposer was invoked in the last get_pre_context() call."""
        return self._decomposer_was_invoked

    def invalidate(self) -> None:
        """Clear cached metadata. Idempotent; safe on an already-empty cache.

        TOCTOU: if fetch_metadata() is in flight when this is called, the
        completed fetch may re-populate the cache with pre-mutation data.
        Callers must ensure the mutation completes before calling invalidate().
        """
        self._cached_metadata = None

    async def fetch_metadata(self) -> list[CollectionMeta]:
        """Fetch collection metadata via JSON-RPC; result is cached.

        Returns [] on timeout (with a debug-level warning).
        """
        if self._cached_metadata is not None:
            return self._cached_metadata

        arguments: dict[str, Any] = {}
        if self._strategy == "hybrid":
            arguments["include_description_embedding"] = True
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "get_collections_meta", "arguments": arguments},
            "id": 1,
        }
        try:
            async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT) as client:
                response = await client.post(self._search_url, json=payload)
                response.raise_for_status()
                data: dict[str, Any] = response.json()
        except httpx.TimeoutException:
            logger.debug("fetch_metadata: timed out fetching collection metadata")
            return []
        except httpx.HTTPStatusError as exc:
            logger.debug("fetch_metadata: HTTP error %s", exc.response.status_code)
            return []
        except httpx.HTTPError as exc:
            logger.debug("fetch_metadata: HTTP error: %s", exc)
            return []
        except json.JSONDecodeError as exc:
            logger.debug("fetch_metadata: JSON decode error: %s", exc)
            return []

        if "error" in data:
            logger.debug("fetch_metadata: JSON-RPC error: %s", data["error"])
            return []

        # FastMCP serialises list[dict] as a single TextContent block whose
        # .text field is the entire list JSON-encoded.
        content_blocks: list[dict[str, Any]] = (
            data.get("result", {}).get("content", [])
        )
        if not content_blocks:
            return []

        first_block = content_blocks[0]
        if first_block.get("type") != "text":
            logger.debug("fetch_metadata: unexpected content block type: %s", first_block.get("type"))
            return []

        try:
            raw_collections: list[dict[str, Any]] = json.loads(first_block["text"])
        except (json.JSONDecodeError, KeyError) as exc:
            logger.debug("fetch_metadata: failed to parse text block: %s", exc)
            return []

        result = [
            CollectionMeta(**{k: v for k, v in col.items() if k in _ROUTING_FIELDS})
            for col in raw_collections
            if isinstance(col, dict) and "name" in col
        ]
        self._cached_metadata = result
        return result

    def _score_collections(
        self,
        query_embedding: list[float],
        collections: list[CollectionMeta],
    ) -> list[tuple[CollectionMeta, float | None]]:
        """Score collections for routing. Shared by rank() and rank_with_scores().

        Scored entries (centroid present AND embedding_model matches) come first, sorted by
        similarity descending with an ascending ``meta.name`` tie-break for equal scores.
        Unscored entries (None centroid or mismatched model) are paired with ``None`` and
        appended last in ascending ``meta.name`` order.

        Under ``strategy="centroid"``: score = cos(query, centroid).
        Under ``strategy="hybrid"``: blend centroid and description_embedding when ALL hold:
          - col.description_embedding is not None
          - len(col.description_embedding) == len(query_embedding)
          - col.description_embedding is not all-zeros
          Otherwise falls back to centroid-only for that collection.
        """
        with record_stage("route"):
            scored: list[tuple[CollectionMeta, float]] = []
            unscored: list[CollectionMeta] = []

            for col in collections:
                if col.centroid is not None and col.active_embedding_model == self._embedding_model:
                    centroid_sim = _cosine_similarity(query_embedding, col.centroid)
                    if self._strategy == "hybrid":
                        desc = col.description_embedding
                        if (
                            desc is not None
                            and len(desc) == len(query_embedding)
                            and any(x != 0.0 for x in desc)
                        ):
                            desc_sim = _cosine_similarity(query_embedding, desc)
                            w = self._description_weight
                            sim = (1.0 - w) * centroid_sim + w * desc_sim
                        else:
                            sim = centroid_sim
                    else:
                        sim = centroid_sim
                    scored.append((col, sim))
                else:
                    unscored.append(col)

            scored.sort(key=lambda p: (-p[1], p[0].name))
            unscored.sort(key=lambda c: c.name)

            return [(m, s) for m, s in scored] + [(m, None) for m in unscored]

    def rank(
        self, query_embedding: list[float], collections: list[CollectionMeta]
    ) -> list[CollectionMeta]:
        """Rank collections by cosine similarity to query_embedding.

        - Collections with mismatched embedding_model are treated as centroid=None.
        - Scored entries (centroid present AND model matches) come first, sorted by similarity
          descending; ascending ``meta.name`` is the tie-break for equal scores.
        - Unscored entries (None centroid or mismatched model) always follow scored ones, sorted
          by ascending ``meta.name`` — including the all-unscored case (fully deterministic).
        - Confidence gate: if max(similarity) < threshold and there are scored collections,
          returns [].
        - All-None-centroid case: bypass gate, return up to shortlist_size.
        """
        scored_pairs = self._score_collections(query_embedding, collections)
        has_scored = any(s is not None for _, s in scored_pairs)
        ordered = [m for m, _ in scored_pairs]

        # All None/mismatched — bypass confidence gate
        if not has_scored:
            return ordered[: self._shortlist_size]

        max_sim = scored_pairs[0][1]  # first entry is the top scored
        if max_sim < self._confidence_threshold:
            return []

        return ordered[: self._shortlist_size]

    def rank_with_scores(
        self,
        query_embedding: list[float],
        collections: list[CollectionMeta],
    ) -> list[tuple[CollectionMeta, float | None]]:
        """Return every collection paired with its centroid similarity (None when unscored).

        Unlike rank(), this bypasses the confidence-threshold gate and does NOT truncate to
        shortlist_size. Intended for /explain only.
        """
        return self._score_collections(query_embedding, collections)

    async def select(self, query: str) -> list[CollectionMeta]:
        """Embed query, fetch metadata, rank, and return shortlist."""
        metadata = await self.fetch_metadata()
        if not metadata:
            return []
        vector = await self._embedder.embed_one(query)
        return self.rank(vector, metadata)

    async def get_pre_context(
        self, query: str, pinned_names: list[str], available_slots: int
    ) -> str | None:
        """Build the <search_collections> context block for the decomposer.

        Sets _last_routable_names and _decomposer_was_invoked as a side-effect.
        Returns None when the decomposer should not be involved, the block otherwise.
        """
        all_meta = await self.fetch_metadata()

        pinned_set = set(pinned_names)
        routable_meta = [m for m in all_meta if m.name not in pinned_set]

        self._last_routable_names = [m.name for m in routable_meta]

        if not all_meta:
            self._decomposer_was_invoked = False
            return None

        if available_slots <= 0:
            self._decomposer_was_invoked = False
            return None

        n_routable = len(routable_meta)

        # Tier 1: ≤3 routable — skip decomposer, search all
        if n_routable <= 3:
            self._decomposer_was_invoked = False
            return None

        # Tier 2: 4 ≤ n_routable ≤ shortlist_size — decomposer selects, no centroid ranking
        if n_routable <= self._shortlist_size:
            self._decomposer_was_invoked = True
            return self._build_block(routable_meta, available_slots)

        # Tier 3: n_routable > shortlist_size — centroid pre-ranking, then decomposer
        vector = await self._embedder.embed_one(query)
        shortlist = self.rank(vector, routable_meta)
        if not shortlist:
            # Confidence gate failed — no relevant collections found; skip decomposer
            self._last_routable_names = []
            self._decomposer_was_invoked = False
            return None
        self._last_routable_names = [m.name for m in shortlist]
        self._decomposer_was_invoked = True
        return self._build_block(shortlist, available_slots)

    def _build_block(self, collections: list[CollectionMeta], available_slots: int) -> str:
        effective_slots = max(1, available_slots)
        lines = [
            "<search_collections>",
            f"Available collections (select 1\u2013{effective_slots} most relevant for this query,"
            " output their names in",
            "<search_selected_collections>name1, name2</search_selected_collections>"
            " tags at the end of your routing decision):",
        ]
        for col in collections:
            desc = col.description if col.description else "(no description)"
            lines.append(f"- {col.name}: {desc}")
        lines.append("</search_collections>")
        return "\n".join(lines)
