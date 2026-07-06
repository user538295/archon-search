"""Deterministic eval-only embedder and reranker backends.

These backends are:
- Deterministic: SHA-256-based hashing, no set() iteration, stable order.
- Query-sensitive and corpus-aware: scores vary with actual text content.
- Label-blind: encode() and predict() accept only text, no metadata.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re

logger = logging.getLogger(__name__)


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

    is_warm: bool = False

    def __init__(self, model_name: str = "eval-sha256-v1") -> None:
        self.model_name = model_name

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


# ---------------------------------------------------------------------------
# Eval graph expansion stub (BE-9)
# ---------------------------------------------------------------------------


#: Deterministic entity→neighbours map for graph-mode eval queries.
#: Covers the ``graph`` collection fixtures (auth_service.md, token_validator.md).
EVAL_GRAPH_ENTITY_MAP: dict[str, list[str]] = {
    "authservice": ["TokenValidator"],
    "tokenvalidator": ["AuthService"],
    "auth": ["AuthService", "TokenValidator"],
}


class StubGraphExpander:
    """Deterministic stub GraphExpander for eval harness use only.

    Uses a fixed entity→neighbours dict (no LanceDB, no spaCy).
    Satisfies the GraphExpander protocol (``expand(query, collection) → ExpandedQuery``).
    """

    def __init__(self, entity_map: dict[str, list[str]]) -> None:
        self._entity_map = {k.lower(): v for k, v in entity_map.items()}

    async def expand(self, query: str, collection: str, ns: str = "default") -> "ExpandedQuery":  # type: ignore[name-defined]  # noqa: F821
        from archon_search.graph_expander import (
            ExpandedQuery,
            build_expanded_text,
            tokenize_and_generate_ngrams,
        )

        ngrams = tokenize_and_generate_ngrams(query, 3)
        neighbour_names: list[str] = []
        entity_names_found: list[str] = []
        for ngram in ngrams:
            neighbours = self._entity_map.get(ngram.lower())
            if neighbours:
                entity_names_found.append(ngram)
                neighbour_names.extend(neighbours)

        if not neighbour_names:
            return ExpandedQuery(original_query=query, expanded_text=query)

        expanded, appended = build_expanded_text(query, neighbour_names)
        return ExpandedQuery(
            original_query=query,
            expanded_text=expanded,
            expansion_applied=bool(appended),
            entity_names_found=entity_names_found,
            neighbour_names_added=appended,
        )


# ---------------------------------------------------------------------------
# Eval community store stub (BE-10)
# ---------------------------------------------------------------------------


#: Eval collection that has community fixtures.
_EVAL_COMMUNITY_COLLECTION = "graph"

#: Entity names (lowercased) recognised by the stub for local-mode entity lookup.
_EVAL_ENTITY_NAMES: frozenset[str] = frozenset(
    {"authservice", "tokenvalidator", "auth", "token"}
)

#: Stub community id returned by the eval community store.
_STUB_COMMUNITY_ID = "eval-community-01"

#: Stub chunk IDs — intentionally non-existent in the real store.
#: When the pipeline calls store.get_chunks_by_ids() with these IDs, it gets []
#: and falls back to standard hybrid search (Q6 resolution).
_STUB_CHUNK_IDS = ["stub-eval-chunk-id-1", "stub-eval-chunk-id-2"]


class CommunityStoreStub:
    """Deterministic stub GraphStore for eval harness use only.

    Returns non-empty community fixtures for the ``graph`` collection so the
    pipeline does not raise ``GraphCommunitiesNotBuiltError`` during eval.
    All returned ``representative_chunk_ids`` are intentionally fake — they
    are absent from the real eval LanceDB store — so ``store.get_chunks_by_ids``
    returns ``[]`` and the pipeline falls back to standard hybrid search (Q6
    resolution).  This lets ``graph_mode=local`` and ``graph_mode=global``
    queries produce real MRR values (from standard hybrid search) without
    requiring a real community table.

    Implements the subset of ``GraphStore`` methods called by
    ``SearchPipeline._search_graph_mode()`` and ``SearchPipeline._search_local_mode()``.
    """

    async def communities_table_exists(self, collection: str, ns: str = "default") -> bool:
        """Return True only for the graph collection used in eval fixtures."""
        return collection == _EVAL_COMMUNITY_COLLECTION

    async def list_community_representatives(
        self, collection: str, ns: str = "default"
    ) -> list:  # list[Community]
        """Return one stub community for the graph collection; empty otherwise."""
        from datetime import datetime, timezone

        from archon_search.graph_types import Community

        if collection != _EVAL_COMMUNITY_COLLECTION:
            return []
        return [
            Community(
                community_id=_STUB_COMMUNITY_ID,
                entity_ids=["stub-entity-01", "stub-entity-02"],
                representative_chunk_ids=list(_STUB_CHUNK_IDS),
                built_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                summary_text=None,
            )
        ]

    async def find_nodes_by_name(
        self, collection: str, names: list[str], ns: str = "default"
    ) -> list:  # list[GraphNode]
        """Return a stub GraphNode when any recognised entity name is present."""
        from archon_search.graph_types import EntityType, GraphNode

        if collection != _EVAL_COMMUNITY_COLLECTION:
            return []
        lower_names = {n.lower() for n in names}
        if not lower_names & _EVAL_ENTITY_NAMES:
            return []
        return [
            GraphNode(
                id="stub-entity-01",
                entity_name="AuthService",
                entity_type=EntityType.system,
                source_doc_id="graph-001",
                collection_name=collection,
            )
        ]

    async def get_communities_for_entities(
        self, collection: str, entity_ids: list[str], ns: str = "default"
    ) -> list:  # list[Community]
        """Return one stub community when entity_ids are non-empty for graph collection."""
        from datetime import datetime, timezone

        from archon_search.graph_types import Community

        if collection != _EVAL_COMMUNITY_COLLECTION or not entity_ids:
            return []
        return [
            Community(
                community_id=_STUB_COMMUNITY_ID,
                entity_ids=list(entity_ids),
                representative_chunk_ids=list(_STUB_CHUNK_IDS),
                built_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                summary_text=None,
            )
        ]


class RealCommunityEvalBackend:
    """Real eval backend wrapping a GraphStore for multi-hop eval collections.

    Implements the four-method CommunityBackend protocol:
    - communities_table_exists
    - list_community_representatives
    - find_nodes_by_name
    - get_communities_for_entities

    Used for multi-hop eval collections (multihop-musique, multihop-2wiki, hotpotqa)
    where real community detection has pre-built communities.
    """

    def __init__(self, graph_store: "GraphStore") -> None:  # type: ignore[name-defined]  # noqa: F821
        self._graph_store = graph_store

    async def communities_table_exists(self, collection: str, ns: str = "default") -> bool:
        """Check if communities table exists for the collection."""
        return await self._graph_store.communities_table_exists(collection, ns=ns)

    async def list_community_representatives(
        self, collection: str, ns: str = "default"
    ) -> list:  # list[Community]
        """List all communities for the collection."""
        return await self._graph_store.list_community_representatives(collection, ns=ns)

    async def find_nodes_by_name(
        self, collection: str, names: list[str], ns: str = "default"
    ) -> list:  # list[GraphNode]
        """Find graph nodes matching the given entity names."""
        try:
            return await self._graph_store.find_nodes_by_name(collection, names, ns=ns)
        except (FileNotFoundError, ValueError):
            return []

    async def get_communities_for_entities(
        self, collection: str, entity_ids: list[str], ns: str = "default"
    ) -> list:  # list[Community]
        """Get communities containing the given entity IDs."""
        return await self._graph_store.get_communities_for_entities(
            collection, entity_ids, ns=ns
        )


class DispatchingCommunityStore:
    """Proxy that dispatches community backend calls by collection name.

    Implements the four-method CommunityBackend protocol by routing each call
    to the appropriate backend based on collection name.
    """

    def __init__(self, backend_map: dict[str, "RealCommunityEvalBackend | CommunityStoreStub"]) -> None:  # type: ignore[name-defined]  # noqa: F821
        self._backend_map = backend_map

    def _get_backend(self, collection: str) -> "RealCommunityEvalBackend | CommunityStoreStub":  # type: ignore[name-defined]  # noqa: F821
        """Get the backend for a collection, raising KeyError if not found."""
        try:
            return self._backend_map[collection]
        except KeyError:
            raise KeyError(f"no community backend configured for collection {collection!r}; configured: {sorted(self._backend_map)}")  # noqa: B904

    async def communities_table_exists(self, collection: str, ns: str = "default") -> bool:
        """Dispatch to the appropriate backend."""
        backend = self._get_backend(collection)
        return await backend.communities_table_exists(collection, ns=ns)

    async def list_community_representatives(
        self, collection: str, ns: str = "default"
    ) -> list:  # list[Community]
        """Dispatch to the appropriate backend."""
        backend = self._get_backend(collection)
        return await backend.list_community_representatives(collection, ns=ns)

    async def find_nodes_by_name(
        self, collection: str, names: list[str], ns: str = "default"
    ) -> list:  # list[GraphNode]
        """Dispatch to the appropriate backend."""
        backend = self._get_backend(collection)
        return await backend.find_nodes_by_name(collection, names, ns=ns)

    async def get_communities_for_entities(
        self, collection: str, entity_ids: list[str], ns: str = "default"
    ) -> list:  # list[Community]
        """Dispatch to the appropriate backend."""
        backend = self._get_backend(collection)
        return await backend.get_communities_for_entities(collection, entity_ids, ns=ns)


class RealGraphExpander:
    """Real GraphExpander for eval harness that reads entity names from GraphStore.

    Implements the GraphExpander protocol by querying real graph nodes
    from a GraphStore (as opposed to the stub which uses a fixed dictionary).
    """

    def __init__(self, graph_store: "GraphStore") -> None:  # type: ignore[name-defined]  # noqa: F821
        self._graph_store = graph_store

    async def expand(self, query: str, collection: str, ns: str = "default") -> "ExpandedQuery":  # type: ignore[name-defined]  # noqa: F821
        """Expand query with first-degree graph-neighbour entity names.

        Reads entity names from the eval GraphStore's node table and
        retrieves their neighbors (same protocol as production GraphExpander).
        """
        import asyncio

        from archon_search.graph_expander import (
            ExpandedQuery,
            build_expanded_text,
            tokenize_and_generate_ngrams,
        )

        # Step 1: tokenise and generate N-gram candidates
        ngram_candidates: list[str] = await asyncio.to_thread(
            tokenize_and_generate_ngrams, query, 3
        )

        if not ngram_candidates:
            return ExpandedQuery(original_query=query, expanded_text=query)

        # Step 2: look up matched graph nodes
        try:
            matched_nodes = await self._graph_store.find_nodes_by_name(
                collection, ngram_candidates, ns=ns
            )
        except (FileNotFoundError, ValueError) as e:
            # Fallback to original query on expected errors
            logger.warning("RealGraphExpander: %s", e)
            return ExpandedQuery(original_query=query, expanded_text=query)

        if not matched_nodes:
            return ExpandedQuery(original_query=query, expanded_text=query)

        # Step 3: retrieve neighbours for matched nodes
        entity_ids = [node.id for node in matched_nodes]
        try:
            neighbour_nodes = await self._graph_store.get_neighbours(
                collection, entity_ids, ns=ns
            )
        except (FileNotFoundError, ValueError) as e:
            logger.warning("RealGraphExpander: %s", e)
            return ExpandedQuery(original_query=query, expanded_text=query)

        if not neighbour_nodes:
            return ExpandedQuery(original_query=query, expanded_text=query)

        # Step 4: build expanded text
        neighbour_names = [node.entity_name for node in neighbour_nodes]
        entity_names_found = [node.entity_name for node in matched_nodes]
        expanded, appended = build_expanded_text(query, neighbour_names)

        return ExpandedQuery(
            original_query=query,
            expanded_text=expanded,
            expansion_applied=bool(appended),
            entity_names_found=entity_names_found,
            neighbour_names_added=appended,
        )


class EvalRerankerBackend:
    """BM25-inspired lexical reranker for eval harness use only.

    Satisfies the RerankerBackend protocol.
    Higher score = more relevant.
    """

    is_warm: bool = False

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
