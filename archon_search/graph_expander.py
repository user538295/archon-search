"""GraphExpander — Use Cases layer for GraphRAG naive query expansion (E1a).

Implements ``graph_mode=naive``: tokenizes the search query, generates all
contiguous N-grams (N=1,2,3), performs a single batched case-insensitive
lookup in the graph node table, retrieves first-degree neighbours of matched
nodes, then appends their entity names to the original query string.

Privacy invariant: the raw query string is NEVER passed to any logging call.
All log messages use ``_query_fingerprint(query)`` for correlation.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from archon_search._privacy import _query_fingerprint

if TYPE_CHECKING:
    from archon_search.graph_store import GraphStore

_logger = logging.getLogger(__name__)

# Maximum N-gram size for query-time entity matching.
# Larger values produce more LanceDB candidates without meaningful recall gains.
_MAX_NGRAM_SIZE = 3


# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------


@dataclass
class ExpandedQuery:
    """Result of a ``GraphExpander.expand()`` call.

    ``expanded_text`` replaces the original query in the hybrid search
    pipeline when ``expansion_applied`` is ``True``.  When ``False``,
    ``expanded_text == original_query`` (no-op).
    """

    original_query: str
    """The unmodified search query passed to ``expand()``."""
    expanded_text: str
    """Original query with neighbour entity names appended (space-separated).
    Equals ``original_query`` when ``expansion_applied`` is ``False``.
    """
    expansion_applied: bool = False
    """``True`` only when at least one neighbour name was appended."""
    entity_names_found: list[str] = field(default_factory=list)
    """Names of graph nodes matched against the query N-grams."""
    neighbour_names_added: list[str] = field(default_factory=list)
    """Names of first-degree neighbours actually appended to the query."""


# ---------------------------------------------------------------------------
# Tokenisation helpers (CPU-bound → must be called via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _generate_ngrams(tokens: list[str], max_n: int) -> list[str]:
    """Return all unique lowercased N-grams (N=1..max_n) from *tokens*.

    Produces phrases like ``"authservice"``, ``"token validator"``,
    ``"machine learning model"`` — used as lookup keys for
    ``GraphStore.find_nodes_by_name``.
    """
    candidates: list[str] = []
    seen: set[str] = set()
    for n in range(1, max_n + 1):
        for i in range(len(tokens) - n + 1):
            phrase = " ".join(tokens[i : i + n]).lower()
            if phrase not in seen:
                candidates.append(phrase)
                seen.add(phrase)
    return candidates


def _tokenize_and_generate_ngrams(query: str, max_n: int) -> list[str]:
    """Split *query* by whitespace, then return all N-gram candidates."""
    tokens = query.split()
    if not tokens:
        return []
    return _generate_ngrams(tokens, max_n)


def _build_expanded_text(original_query: str, neighbour_names: list[str]) -> tuple[str, list[str]]:
    """Append *neighbour_names* to *original_query*, skipping duplicates.

    Deduplication strategy:
    - **Single-word names**: check token-set membership so ``"Auth"`` is NOT
      suppressed merely because the query contains ``"AuthService"``.
    - **Multi-word names**: check phrase-substring presence (``"token validator"
      in lower_query``) since multi-word phrases cannot produce false positives
      from partial-word matches.

    Also deduplicates within *neighbour_names* when two matched nodes share the
    same neighbour.

    Returns:
        A ``(expanded_text, actually_appended_names)`` tuple.
    """
    query_tokens = set(original_query.lower().split())
    lower_query = original_query.lower()
    appended: list[str] = []
    seen_lower: set[str] = set()
    for name in neighbour_names:
        name_lower = name.lower()
        if name_lower in seen_lower:
            continue
        # Single-word: token-set check avoids false positives from substrings.
        # Multi-word: phrase substring check (no partial-word false positives).
        name_tokens = name_lower.split()
        if len(name_tokens) == 1:
            already_present = name_lower in query_tokens
        else:
            already_present = name_lower in lower_query
        if not already_present:
            appended.append(name)
            seen_lower.add(name_lower)
    if not appended:
        return original_query, []
    return original_query + " " + " ".join(appended), appended


# ---------------------------------------------------------------------------
# GraphExpander
# ---------------------------------------------------------------------------


class GraphExpander:
    """Use Cases component that expands a query with first-degree graph neighbours.

    Queries the graph node table for any entity names that match N-grams in
    the search query (case-insensitive, exact match), then retrieves their
    first-degree neighbours.  The neighbour entity names are appended to the
    original query text.

    ``expand()`` is safe to call concurrently — it holds no mutable state.
    """

    def __init__(self, graph_store: "GraphStore") -> None:
        self._store = graph_store

    async def expand(self, query: str, collection: str) -> ExpandedQuery:
        """Expand *query* with first-degree graph-neighbour entity names.

        Args:
            query: The original search query string.
            collection: The collection whose graph tables to query.

        Returns:
            An ``ExpandedQuery`` with ``expansion_applied=True`` when at least
            one neighbour name was appended, ``False`` otherwise.
        """
        # Step 1: tokenise and generate N-gram candidates (CPU-bound).
        ngram_candidates: list[str] = await asyncio.to_thread(
            _tokenize_and_generate_ngrams, query, _MAX_NGRAM_SIZE
        )

        if not ngram_candidates:
            _logger.debug(
                "graph_expander: empty query (fp=%s); skipping expansion",
                _query_fingerprint(query),
            )
            return ExpandedQuery(original_query=query, expanded_text=query)

        # Step 2: single batched lookup — all N-gram candidates in one call.
        try:
            matched_nodes = await self._store.find_nodes_by_name(collection, ngram_candidates)
        except Exception:
            _logger.warning(
                "graph_expander: store lookup failed for collection %r (fp=%s); skipping expansion",
                collection,
                _query_fingerprint(query),
                exc_info=True,
            )
            return ExpandedQuery(original_query=query, expanded_text=query)

        if not matched_nodes:
            _logger.debug(
                "graph_expander: no graph nodes matched query (fp=%s)",
                _query_fingerprint(query),
            )
            return ExpandedQuery(original_query=query, expanded_text=query)

        # Step 3: retrieve first-degree neighbours.
        matched_ids = [n.id for n in matched_nodes]
        try:
            neighbour_nodes = await self._store.get_neighbours(collection, matched_ids)
        except Exception:
            _logger.warning(
                "graph_expander: neighbour lookup failed for collection %r (fp=%s); skipping expansion",
                collection,
                _query_fingerprint(query),
                exc_info=True,
            )
            return ExpandedQuery(original_query=query, expanded_text=query)

        if not neighbour_nodes:
            _logger.debug(
                "graph_expander: matched nodes have no neighbours (fp=%s)",
                _query_fingerprint(query),
            )
            return ExpandedQuery(
                original_query=query,
                expanded_text=query,
                entity_names_found=[n.entity_name for n in matched_nodes],
            )

        # Step 4: build expanded text (CPU-bound, but trivially fast; no thread needed).
        neighbour_names = [n.entity_name for n in neighbour_nodes]
        expanded, appended_names = _build_expanded_text(query, neighbour_names)

        entity_names_found = [n.entity_name for n in matched_nodes]

        if not appended_names:
            # All neighbour names were already in the query.
            return ExpandedQuery(
                original_query=query,
                expanded_text=query,
                entity_names_found=entity_names_found,
            )

        _logger.debug(
            "graph_expander: expanded query (fp=%s) with %d neighbour(s)",
            _query_fingerprint(query),
            len(appended_names),
        )
        return ExpandedQuery(
            original_query=query,
            expanded_text=expanded,
            expansion_applied=True,
            entity_names_found=entity_names_found,
            neighbour_names_added=appended_names,
        )
