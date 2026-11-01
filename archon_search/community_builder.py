"""CommunityBuilder — Use Cases layer for E1b Leiden community detection.

Builds Leiden communities from the entity graph stored in ``GraphStore``.
``leidenalg`` and ``igraph`` are imported lazily inside
``_run_leiden_partition_sync`` so the module can be imported without the
``[graph]`` extras installed.

Typical usage::

    builder = CommunityBuilder(graph_store, config)
    communities = await builder.build(collection)

See E1b brief for the full design rationale and recursion policy.
"""
from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from archon_search.constants import ANSWER_RESERVE_TOKENS
from archon_search.llm_client.fit import MAX_CHARS_PER_TOKEN, describe_window
from archon_search.llm_client.once_per_process import warn_once
from archon_search.llm_client.protocol import (
    LlmCallFailure,
    LlmCallRequest,
    TokenCountingClient,
)
from archon_search.uv_tool import extra_install_hint

if TYPE_CHECKING:
    from archon_search.config import GraphConfig
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import Community, GraphEdge, GraphNode
    from archon_search.llm_client.protocol import LlmClient
    from archon_search.store import SearchStore

_logger = logging.getLogger(__name__)

#: The community-summary prompt (moved in from the Anthropic enrichment client with the rest of the summarisation
#: surface, LCD BE-15). The input budget is asked for with this template, and ``render_summary_prompt`` fills it.
SUMMARIZE_PROMPT_TEMPLATE = """\
You are a knowledge-graph summariser. Given a set of representative text passages and \
the entity names extracted from them, write a single concise paragraph that describes \
what this cluster of entities represents and how they relate.

Output only the paragraph — no preamble, no bullet points, no headings.

Entities: {entity_names}

Passages:
{chunk_texts}
"""

_ENTITY_SEPARATOR = ", "
_PASSAGE_SEPARATOR = "\n\n---\n\n"
#: ``warn_once`` keys: one WARNING per process for every failed summary, one for every budget that cannot hold a community.
_SUMMARY_FAILED_WARN_KEY = "graph_community_summary"
_SUMMARY_BUDGET_WARN_KEY = "graph_community_summary_budget"
_SUMMARY_NO_PASSAGE_WARN_KEY = "graph_community_summary_no_passage"
_SUMMARY_EMPTY_WARN_KEY = "graph_community_summary_empty"
#: ``warn_once`` key: one WARNING per process for the first community whose entity names are cut to the budget.
_SUMMARY_NAMES_CUT_WARN_KEY = "graph_community_summary_names_cut"
#: Share of the input budget (percent) the entity names may take when they do not all fit and passages follow: a community
#: whose names must be cut keeps the rest of the budget for its representative passages. Names that all fit in the budget
#: are all sent, whatever they leave.
NAMES_CUT_SHARE_PERCENT: Final = 50
_PERCENT: Final = 100


def render_summary_prompt(entity_names: Sequence[str], chunk_texts: Sequence[str]) -> str:
    """The summary prompt for one community: the template filled with its entity names and passages."""
    return SUMMARIZE_PROMPT_TEMPLATE.format(
        entity_names=_ENTITY_SEPARATOR.join(entity_names),
        chunk_texts=_PASSAGE_SEPARATOR.join(chunk_texts),
    )


def _variable_text(entity_names: Sequence[str], chunk_texts: Sequence[str]) -> str:
    """What a prompt holds beyond its template: the entity names and the passages, as sent."""
    return _PASSAGE_SEPARATOR.join([_ENTITY_SEPARATOR.join(entity_names), *chunk_texts])


@dataclass(frozen=True, slots=True)
class _FittedInput:
    """The entity names and passages one summary prompt sends, cut to the input budget."""

    entity_names: list[str]
    chunk_texts: list[str]


async def _longest_fitting_prefix(total: int, fits: Callable[[int], Awaitable[bool]]) -> int:
    """The length of a prefix of ``total`` items that ``fits`` accepts, found by bisection (``ceil(log2(total))`` calls).

    The prefix of ``total`` items is known not to fit; the empty prefix is taken to fit without asking. The answer is
    always a length ``fits`` itself accepted (or 0), so a count that is not strictly monotonic never yields a prefix that
    overruns the budget.
    """
    fitting, too_long = 0, total
    while too_long - fitting > 1:
        middle = (fitting + too_long) // 2
        if await fits(middle):
            fitting = middle
        else:
            too_long = middle
    return fitting


def _names_within_reach(entity_names: Sequence[str], room: int) -> int:
    """How many leading names join into a text of at most ``MAX_CHARS_PER_TOKEN`` characters per token of *room*.

    A longer text is taken not to fit *room* tokens, as :func:`fit_text_to_budget` takes a head longer than that.
    """
    reach = room * MAX_CHARS_PER_TOKEN
    length = -len(_ENTITY_SEPARATOR)  # a separator sits between two names only
    for index, name in enumerate(entity_names):
        length += len(name) + len(_ENTITY_SEPARATOR)
        if length > reach:
            return index
    return len(entity_names)


class CommunityDependencyError(ImportError):
    """Raised when ``leidenalg``/``igraph`` are not installed.

    Its message embeds :func:`~archon_search.uv_tool.extra_install_hint` (possibly index
    credentials in a uv tool env): log it, record ``COMMUNITY_DEPENDENCY_DETAIL`` on the job.
    """


# Wire-facing job ``error`` for ``CommunityDependencyError``.
COMMUNITY_DEPENDENCY_DETAIL = (
    "community detection requires the archon-search[graph] extra; see the server log for the install command"
)


class NoGraphNodesError(ValueError):
    """Raised by ``CommunityBuilder.build`` when the collection has no entity graph nodes."""


# Maximum recursion depth for oversized-community splitting.
_MAX_SPLIT_DEPTH: int = 5

# Leiden resolution is multiplied by this factor at each recursion level.
_RESOLUTION_GROWTH_FACTOR: int = 2

# Module-level per-(namespace, collection) rebuild-lock registry (C3).
#
# Serialises concurrent CommunityBuilder.build() calls on the same graph
# resource across ALL callers — the route's rebuild task and
# MaintenanceLoop._rebuild_communities_async each construct their own,
# separate CommunityBuilder instance, so an instance-held lock would not
# serialise anything. This registry is the single shared point every caller
# resolves through. Deliberately independent of SearchStore._collection_locks
# (different key shape — (ns, collection) vs. collection alone — and a
# different resource: this feature's writes go through GraphStore, which uses
# its own LanceDB connection, not SearchStore's).
#
# The dict itself may exist at import time; the asyncio.Lock instances inside
# it must NOT — they are created lazily, on first access, inside the running
# event loop (mirrors SearchStore.lock_for / SearchCollectionSync._get_lock).
# An import-time asyncio.Lock() binds to whichever loop is running (or none)
# at import, which breaks a later `await lock.acquire()` under a different loop.
_rebuild_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _get_rebuild_lock(ns: str, collection: str) -> asyncio.Lock:
    """Lazily create and return the rebuild lock for *(ns, collection)*."""
    key = (ns, collection)
    lock = _rebuild_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _rebuild_locks[key] = lock
    return lock


# ---------------------------------------------------------------------------
# MMR helpers
# ---------------------------------------------------------------------------


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors. Returns 0.0 for zero vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _mmr_select(chunks: list[dict], k: int) -> list[str]:
    """Greedy MMR diversity selection. Returns up to *k* chunk_id strings.

    Filters to chunks with a non-None, non-empty ``vector`` field. Picks the
    chunk closest to the centroid of all valid vectors as the first
    representative, then iteratively picks the chunk with the minimum
    max-cosine-similarity to already-selected chunks.

    Returns ``[]`` when *chunks* is empty or no chunks have valid vectors.

    Note: This is diversity-only selection — there is no relevance/λ trade-off term.
    The centroid serves as a weak relevance anchor (most "central" chunk is picked
    first), but subsequent picks maximise diversity only (λ=0 in classical MMR).
    """
    valid_chunks = [c for c in chunks if c.get("vector")]
    if not valid_chunks:
        return []
    k = min(k, len(valid_chunks))
    if k <= 0:
        return []

    dim = len(valid_chunks[0]["vector"])
    centroid = [0.0] * dim
    for c in valid_chunks:
        for i, v in enumerate(c["vector"]):
            centroid[i] += v
    n = len(valid_chunks)
    centroid = [v / n for v in centroid]

    selected_ids: list[str] = []
    selected_vectors: list[list[float]] = []
    remaining = list(valid_chunks)

    # First pick: chunk closest to centroid
    best_idx = max(range(len(remaining)), key=lambda i: _cosine_similarity(remaining[i]["vector"], centroid))
    chosen = remaining.pop(best_idx)
    selected_ids.append(str(chosen["chunk_id"]))
    selected_vectors.append(chosen["vector"])

    # Subsequent picks: minimum max-similarity to already selected
    while len(selected_ids) < k and remaining:
        best_score = float("inf")
        best_idx = 0
        for i, candidate in enumerate(remaining):
            max_sim = max(_cosine_similarity(candidate["vector"], sv) for sv in selected_vectors)
            if max_sim < best_score:
                best_score = max_sim
                best_idx = i
        chosen = remaining.pop(best_idx)
        selected_ids.append(str(chosen["chunk_id"]))
        selected_vectors.append(chosen["vector"])

    return selected_ids


# ---------------------------------------------------------------------------
# Synchronous helpers (called via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _run_leiden_partition_sync(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    resolution: float,
    seed: int | None = None,
) -> list[list[str]]:
    """Run the Leiden algorithm and return a list of entity_id groups.

    Lazy-imports ``leidenalg`` and ``igraph`` inside the function body.
    Raises ``ImportError`` with an install hint when the libraries are absent.

    Edges are treated as undirected; duplicate (src, tgt) pairs are collapsed
    before graph construction.

    Args:
        nodes: All graph nodes for the collection.
        edges: All graph edges for the collection.
        resolution: Leiden resolution_parameter (higher → more communities).
        seed: Random seed for deterministic Leiden partitioning. When None,
            uses non-deterministic behaviour (existing default). When set to
            an integer (e.g., 42), produces reproducible communities across runs.

    Returns:
        A list of groups, where each group is a list of entity IDs belonging
        to the same community.
    """
    try:
        import igraph as ig
        import leidenalg
    except (ImportError, TypeError) as exc:
        raise CommunityDependencyError(
            "leidenalg is required for community detection. "
            f"Install it with: {extra_install_hint('graph')}"
        ) from exc

    if not nodes:
        return []

    # Build vertex index
    node_ids = [n.id for n in nodes]
    id_to_idx: dict[str, int] = {nid: i for i, nid in enumerate(node_ids)}

    # Deduplicate undirected edges
    edge_pairs: set[tuple[int, int]] = set()
    for e in edges:
        src_idx = id_to_idx.get(e.source_node_id)
        tgt_idx = id_to_idx.get(e.target_node_id)
        if src_idx is None or tgt_idx is None:
            continue
        if src_idx == tgt_idx:  # Skip self-loops
            continue
        pair = (min(src_idx, tgt_idx), max(src_idx, tgt_idx))
        edge_pairs.add(pair)

    g = ig.Graph(n=len(node_ids), edges=list(edge_pairs), directed=False)
    partition = leidenalg.find_partition(
        g,
        leidenalg.RBConfigurationVertexPartition,
        resolution_parameter=resolution,
        seed=seed,
    )

    groups: list[list[str]] = []
    for community in partition:
        group = [node_ids[idx] for idx in community]
        if group:
            groups.append(group)

    return groups


def _split_oversized_communities(
    entity_id_groups: list[list[str]],
    nodes_by_id: dict[str, GraphNode],
    edges: list[GraphEdge],
    max_size: int,
    resolution: float,
    depth: int,
    seed: int | None = None,
) -> list[list[str]]:
    """Recursively split communities that exceed *max_size*.

    For each group that is larger than *max_size*:
    - If ``depth >= _MAX_SPLIT_DEPTH``: accept as-is with a WARNING log.
    - Otherwise: extract the subgraph, re-run Leiden at ``resolution * _RESOLUTION_GROWTH_FACTOR``
      (doubled each level).
      - If Leiden returns a single group (cannot split further): accept with WARNING.
      - Otherwise: recurse on each resulting sub-group.

    Args:
        entity_id_groups: Current list of entity_id groups from a Leiden run.
        nodes_by_id: Map of entity_id → GraphNode for subgraph extraction.
        edges: Full edge list for the collection (filtered per subgraph).
        max_size: Maximum allowed community size.
        resolution: Current resolution parameter.
        depth: Current recursion depth (0-indexed from the caller).
        seed: Random seed for deterministic Leiden partitioning (forwarded to all
            _run_leiden_partition_sync calls during recursion).

    Returns:
        Flattened list of groups, all with ``len <= max_size`` (or accepted
        oversized groups when the recursion limit is reached).
    """
    result: list[list[str]] = []
    for group in entity_id_groups:
        if len(group) <= max_size:
            result.append(group)
            continue

        if depth >= _MAX_SPLIT_DEPTH:
            _logger.warning(
                "community_builder: group of size %d exceeds max_size=%d "
                "at depth=%d (limit); accepting oversized community",
                len(group),
                max_size,
                depth,
            )
            result.append(group)
            continue

        # Extract subgraph
        group_set = set(group)
        sub_nodes = [nodes_by_id[nid] for nid in group if nid in nodes_by_id]
        missing = [nid for nid in group if nid not in nodes_by_id]
        if missing:
            _logger.warning(
                "community_builder: %d node ID(s) in group not found in nodes_by_id; "
                "they will be excluded from the sub-community",
                len(missing),
            )
        sub_edges = [
            e for e in edges
            if e.source_node_id in group_set and e.target_node_id in group_set
        ]

        new_resolution = resolution * _RESOLUTION_GROWTH_FACTOR
        sub_groups = _run_leiden_partition_sync(sub_nodes, sub_edges, new_resolution, seed=seed)

        if len(sub_groups) <= 1:
            # Cannot split further
            _logger.warning(
                "community_builder: Leiden could not split group of size %d "
                "at resolution=%.4f depth=%d; accepting oversized community",
                len(group),
                new_resolution,
                depth,
            )
            result.append(group)
            continue

        # Recurse on sub-groups
        result.extend(
            _split_oversized_communities(
                sub_groups, nodes_by_id, edges, max_size, new_resolution, depth + 1, seed=seed
            )
        )

    return result


def _cluster_with_size_limit(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    resolution: float,
    max_size: int,
    seed: int | None = None,
) -> list[list[str]]:
    """Run Leiden and split oversized communities as needed.

    Calls ``_run_leiden_partition_sync`` once, then delegates to
    ``_split_oversized_communities`` for any groups that exceed *max_size*.

    Args:
        nodes: All graph nodes for the collection.
        edges: All graph edges for the collection.
        resolution: Initial Leiden resolution parameter.
        max_size: Maximum allowed community size.
        seed: Random seed for deterministic Leiden partitioning (forwarded to
            _run_leiden_partition_sync and _split_oversized_communities).

    Returns:
        List of entity_id groups with all sizes ``<= max_size`` (or accepted
        oversized when the recursion limit is reached).
    """
    groups = _run_leiden_partition_sync(nodes, edges, resolution, seed=seed)
    if not any(len(g) > max_size for g in groups):
        return groups
    nodes_by_id = {n.id: n for n in nodes}
    return _split_oversized_communities(groups, nodes_by_id, edges, max_size, resolution, depth=0, seed=seed)


# ---------------------------------------------------------------------------
# CommunityBuilder
# ---------------------------------------------------------------------------


class CommunityBuilder:
    """Use Cases layer — builds Leiden communities from the entity graph (E1b).

    Lazy-imports ``leidenalg`` and ``igraph`` inside ``_run_leiden_partition_sync``
    so the module can be imported without the ``[graph]`` extras installed.

    ``build()`` orchestrates:
    1. Load all nodes from ``GraphStore.get_all_nodes``.
    2. Short-circuit to a single community when fewer than 2 nodes exist.
    3. Load all edges from ``GraphStore.get_all_edges``.
    4. Run ``_cluster_with_size_limit`` via ``asyncio.to_thread``.
    5. Fill representative_chunk_ids via MMR (when search_store is provided).
    6. Optionally summarise each community on the shared graph client (entity names and passages sized to its input
       budget); a failure keeps ``summary_text=None`` and logs one WARNING per process.
    7. Persist communities via ``GraphStore.write_communities``.
    8. Return one ``Community`` per group with a fresh UUID.
    """

    def __init__(
        self,
        graph_store: GraphStore,
        config: GraphConfig,
        *,
        search_store: SearchStore | None = None,
        graph_client: LlmClient | None = None,
    ) -> None:
        self._store = graph_store
        self._config = config
        self._search_store = search_store
        self._graph_client = graph_client

    async def _generate_llm_summary(
        self, client: LlmClient, chunk_texts: list[str], entity_names: list[str]
    ) -> str | None:
        """Summarise one community on the shared graph *client*; ``None`` when the answer is empty or nothing can be sent.

        Caller (``build()``) only invokes this when the AND-gate is open (``provider`` and ``extraction_model`` are both
        set and a client is injected); it is otherwise responsible for catching any failure raised here and substituting
        ``summary_text=None`` (the client raises, callers decide the fallback).

        The names and passages are sized to the client's input budget first (C1): the budget is the window minus the answer
        reserve minus the template. The entity names go first, in the order given (all of them when they fit the budget;
        else the longest prefix that fits, held to ``NAMES_CUT_SHARE_PERCENT`` of the budget when passages follow: a
        community can have hundreds of entities), then as many passages as fit in what is left. A cloud client has
        no budget, so everything goes. The budget holds ``ANSWER_RESERVE_TOKENS`` back for the answer: an
        ``extraction_token_budget`` above that comes out of the room for the names and passages, so the prompt and the
        answer together still fit the window.
        """
        budget = (await client.input_budget(SUMMARIZE_PROMPT_TEMPLATE)).tokens
        max_output_tokens = self._config.extraction_token_budget
        if isinstance(budget, int):
            budget = max(0, budget - max(0, max_output_tokens - ANSWER_RESERVE_TOKENS))
            fitted = await self._fit_to_budget(client, budget, entity_names, chunk_texts)
            if fitted is None:
                warn_once(
                    _logger,
                    _SUMMARY_BUDGET_WARN_KEY,
                    "community_builder: the model's input budget is %d tokens (context window: %s); not even the first entity "
                    "name of a community fits, so communities are built without summaries (later skips in this process are not "
                    "logged)",
                    budget,
                    describe_window(client.context_window()),
                )
                return None
            if len(fitted.entity_names) < len(entity_names):
                warn_once(
                    _logger,
                    _SUMMARY_NAMES_CUT_WARN_KEY,
                    "community_builder: a community has more entity names than the model's input budget of %d tokens "
                    "(context window: %s) holds; only the first names that fit are sent (%d of %d in this community; at most "
                    "%d%% of the budget when passages are sent too, so that they keep room), so its summary may leave "
                    "entities out (later cases in this process are not logged)",
                    budget,
                    describe_window(client.context_window()),
                    len(fitted.entity_names),
                    len(entity_names),
                    NAMES_CUT_SHARE_PERCENT,
                )
            if chunk_texts and not fitted.chunk_texts:
                warn_once(
                    _logger,
                    _SUMMARY_NO_PASSAGE_WARN_KEY,
                    "community_builder: no representative passage fits what the entity names leave of the model's input "
                    "budget of %d tokens; communities are summarised from their entity names alone (later cases in this "
                    "process are not logged)",
                    budget,
                )
            entity_names, chunk_texts = fitted.entity_names, fitted.chunk_texts
        request = LlmCallRequest(
            prompt=render_summary_prompt(entity_names, chunk_texts),
            max_output_tokens=max_output_tokens,
            timeout_seconds=self._config.extraction_timeout_seconds,
        )
        result = await client.call(request)
        summary = result.text.strip()
        if not summary:
            warn_once(
                _logger,
                _SUMMARY_EMPTY_WARN_KEY,
                "community_builder: the model returned no summary text (extraction_model=%r); a reasoning model may have "
                "spent its whole answer budget thinking (raise [graph].extraction_token_budget or pick another model); "
                "communities are built without summaries (later cases in this process are not logged)",
                self._config.extraction_model,
            )
            return None
        return summary

    @staticmethod
    async def _fit_to_budget(
        client: LlmClient, budget: int, entity_names: list[str], chunk_texts: list[str]
    ) -> _FittedInput | None:
        """The entity names and passages of one prompt, cut to *budget* tokens; ``None`` when not even the first name fits.

        The names go first and in the order given: all of them when they fit the budget, which leaves the passages what is
        left (possibly nothing). When they do not all fit and there are passages, the names are held to
        ``NAMES_CUT_SHARE_PERCENT`` of the budget, so the passages keep the rest; a first name larger than that share
        gives way to the whole budget. Without passages the names take the whole budget. A prefix is found with a
        logarithmic number of counts (a count is a request to the server for ``llama_cpp``), over the names that stay
        within ``MAX_CHARS_PER_TOKEN`` characters per token of the room, so no count is of a text far longer than the
        room (a community can have 100 000 names). The passages then take what is left: the longest prefix of *chunk_texts*
        that fits with those names. Tokens are counted by the client, in the unit its budget is measured in (``llama_cpp``:
        the server's tokenizer; ``ollama``: the token estimate).

        Raises:
            LlmCallFailure: the client has a budget but cannot count tokens, or the count failed.
        """
        if not isinstance(client, TokenCountingClient):
            raise LlmCallFailure("graph", "the client sizes its input to a budget but cannot count tokens")

        async def _count(names: list[str], passages: list[str]) -> int:
            return await client.count_tokens(_variable_text(names, passages))

        async def _fitting_names(room: int) -> int:
            """How many leading names *room* tokens hold."""
            reachable = _names_within_reach(entity_names, room)

            async def _names_fit(count: int) -> bool:
                return await _count(entity_names[:count], []) <= room

            if reachable == 0 or await _names_fit(reachable):
                return reachable
            return await _longest_fitting_prefix(reachable, _names_fit)

        kept = await _fitting_names(budget)
        if kept < len(entity_names) and chunk_texts:
            kept = await _fitting_names(budget * NAMES_CUT_SHARE_PERCENT // _PERCENT) or kept
        if kept == 0 and entity_names:
            return None
        names = entity_names[:kept]
        fitting: list[str] = []
        for text in chunk_texts:
            if await _count(names, [*fitting, text]) > budget:
                break
            fitting.append(text)
        return _FittedInput(entity_names=names, chunk_texts=fitting)

    async def _select_representative_chunk_ids(
        self,
        collection: str,
        entity_ids: list[str],
        nodes_by_id: dict[str, GraphNode],
    ) -> tuple[list[str], list[dict]]:
        """Select representative chunk IDs for a community via MMR.

        When search_store is None, returns ([], []) (no MMR candidates available).
        Gathers all candidate chunks for entities in this community (deduplicated
        by source_doc_id to avoid redundant store queries), then runs MMR to
        select up to ``config.community_summary_chunks`` diverse IDs.

        Returns:
            (selected_chunk_ids, all_candidate_chunks)
        """
        if self._search_store is None:
            return [], []

        seen_chunk_ids: set[str] = set()
        candidate_chunks: list[dict] = []

        # Deduplicate source_doc_ids to avoid redundant store queries
        unique_doc_ids: set[str] = set()
        for eid in entity_ids:
            node = nodes_by_id.get(eid)
            if node is not None:
                unique_doc_ids.add(node.source_doc_id)

        for doc_id in sorted(unique_doc_ids):
            doc_chunks = await self._search_store.get_chunks_for_doc(collection, doc_id)
            for chunk in doc_chunks:
                cid = chunk.get("chunk_id")
                if cid and cid not in seen_chunk_ids:
                    seen_chunk_ids.add(cid)
                    candidate_chunks.append(chunk)

        return _mmr_select(candidate_chunks, self._config.community_summary_chunks), candidate_chunks

    async def build(self, collection: str, ns: str, *, seed: int | None = None) -> list[Community]:
        """Build Leiden communities for *collection*.

        Resolves and acquires the module-level per-``(ns, collection)`` rebuild
        lock (C3) for the whole build duration, so this call never runs
        concurrently with another ``build()`` call on the same ``(ns, collection)``
        key — including a ``MaintenanceLoop`` GC-triggered rebuild, which
        constructs its own, separate ``CommunityBuilder`` instance. The lock is
        independent of ``SearchStore.lock_for``, so a rebuild never blocks or is
        blocked by document ingest on the same collection.

        Fills ``representative_chunk_ids`` via MMR when a ``search_store`` is
        provided. Attempts optional LLM summarisation through the injected
        ``graph_client`` when ``config.provider`` and ``config.extraction_model``
        are set; falls back to ``summary_text=None``
        on any failure. Persists results via ``GraphStore.write_communities``
        before returning.

        Args:
            collection: Name of the collection whose entity graph to cluster.
            ns: Namespace for the collection.
            seed: Random seed for deterministic Leiden partitioning. When None
                (default), uses non-deterministic behaviour. When set to an integer
                (e.g., 42), produces reproducible communities across runs.

        Returns:
            List of ``Community`` objects — one per detected community.
            May be empty when Leiden produces no communities on a valid graph.

        Raises:
            NoGraphNodesError: When no graph nodes exist for *collection* (ingest
                with ``graph.enabled=true`` must be run first).
            CommunityDependencyError: When ``leidenalg``/``igraph`` are not
                installed (``pip install archon-search[graph]``).
            RuntimeError: When the graph store encounters an unexpected I/O or
                storage error while loading nodes or edges.
        """
        lock = _get_rebuild_lock(ns, collection)
        async with lock:
            return await self._build_locked(collection, ns, seed=seed)

    async def _build_locked(self, collection: str, ns: str, *, seed: int | None = None) -> list[Community]:
        """Build body — caller must hold ``_get_rebuild_lock(ns, collection)``."""
        from archon_search.graph_types import Community

        nodes = await self._store.get_all_nodes(collection, ns=ns)
        if not nodes:
            raise NoGraphNodesError(
                f"No entity graph nodes found for collection {collection!r}. "
                "Run ingest with graph.enabled=true first."
            )

        now = datetime.now(tz=UTC)
        nodes_by_id = {n.id: n for n in nodes}

        if len(nodes) < 2:
            _logger.warning(
                "community_builder: collection %r has only %d node(s); "
                "returning single community without running Leiden",
                collection,
                len(nodes),
            )
            entity_ids = [n.id for n in nodes]
            rep_chunk_ids, _ = await self._select_representative_chunk_ids(
                collection, entity_ids, nodes_by_id
            )
            result = [
                Community(
                    community_id=str(uuid.uuid4()),
                    entity_ids=entity_ids,
                    representative_chunk_ids=rep_chunk_ids,
                    built_at=now,
                    summary_text=None,
                )
            ]
            await self._store.write_communities(collection, result, ns=ns)
            return result

        edges = await self._store.get_all_edges(collection, ns=ns)

        resolution = self._config.leiden_resolution
        max_size = self._config.max_community_size

        groups = await asyncio.to_thread(
            _cluster_with_size_limit, nodes, edges, resolution, max_size, seed
        )

        final_communities: list[Community] = []
        for group in groups:
            community_id = str(uuid.uuid4())

            rep_chunk_ids, candidate_chunks = await self._select_representative_chunk_ids(
                collection, group, nodes_by_id
            )

            summary_text: str | None = None
            if (
                self._config.provider is not None
                and self._config.extraction_model is not None
                and self._graph_client is not None
            ):
                # In MMR rank order (the centroid-nearest first, then the most diverse), so the passages a budget
                # drops are the later picks, and the same ones on every run.
                chunks_by_id = {chunk.get("chunk_id"): chunk for chunk in candidate_chunks}
                chunk_texts: list[str] = [
                    text for cid in rep_chunk_ids if (text := (chunks_by_id.get(cid) or {}).get("text"))
                ]

                entity_names = [
                    nodes_by_id[eid].entity_name for eid in group if eid in nodes_by_id
                ]

                try:
                    summary_text = await self._generate_llm_summary(self._graph_client, chunk_texts, entity_names)
                except LlmCallFailure as failure:
                    warn_once(
                        _logger,
                        _SUMMARY_FAILED_WARN_KEY,
                        "community_builder: LLM summary failed (extraction_model=%r): %s; communities are built "
                        "without summaries (later failures in this process are not logged)",
                        self._config.extraction_model,
                        failure,
                    )
                except Exception as exc:  # noqa: BLE001 — a summary is optional: anything that goes wrong keeps the community without one, never aborts the build
                    warn_once(
                        _logger,
                        _SUMMARY_FAILED_WARN_KEY,
                        "community_builder: LLM summary failed (extraction_model=%r): unexpected %s; communities are "
                        "built without summaries (later failures in this process are not logged)",
                        self._config.extraction_model,
                        type(exc).__name__,
                    )

            final_communities.append(
                Community(
                    community_id=community_id,
                    entity_ids=group,
                    representative_chunk_ids=rep_chunk_ids,
                    built_at=now,
                    summary_text=summary_text,
                )
            )

        await self._store.write_communities(collection, final_communities, ns=ns)
        return final_communities
