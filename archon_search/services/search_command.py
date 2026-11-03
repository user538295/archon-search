"""``SearchCommand`` and the one normaliser of a search request.

Every interface (REST, MCP, the OpenAI shim, through the service) hands the service the same transport-free command.
The rules live here, in REST's actual check order and with REST's exact texts, because clients and tests pin those
strings.  :func:`normalize_request_shape` holds the rules that need no configuration (REST's request model validators
are to call it); :func:`normalize_search_command` adds the operator's limits: the fan-out limit (counted after
de-duplication), ``top_k`` above the maximum, the ``scope_filter`` syntax, ``scope_filter`` with ``graph_mode``, ``ppr``
with any ``collections`` and the ``[graph] enabled`` switch.

Nothing here imports a transport (``fastapi``, ``fastmcp``, ``click``): a failure is a :class:`ServiceError`.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Final

from pydantic import ValidationError

from archon_search.constants import TOP_K_MIN, VALID_GRAPH_MODES
from archon_search.filters import SearchFilters
from archon_search.services.service_error import (
    INVALID_SEARCH_FILTERS_DETAIL,
    ServiceError,
    ServiceErrorKind,
    fanout_exceeded_detail,
    top_k_exceeded_detail,
)
from archon_search.services.validators import validate_scope_filter

_QUERY_EMPTY_DETAIL: Final[str] = "query must not be empty"
_COLLECTION_EMPTY_DETAIL: Final[str] = "collection must not be empty"
_COLLECTION_AND_COLLECTIONS_DETAIL: Final[str] = "supply either collection or collections, not both"
_COLLECTION_MISSING_DETAIL: Final[str] = "supply either collection or collections"
_COLLECTIONS_EMPTY_DETAIL: Final[str] = "collections must not be empty"
_COLLECTION_NAME_EMPTY_DETAIL: Final[str] = "collection names must not be empty or whitespace"
_TOP_K_BELOW_MINIMUM_DETAIL: Final[str] = f"top_k must be at least {TOP_K_MIN}"
_INVALID_GRAPH_MODE_DETAIL: Final[str] = f"graph_mode must be one of {list(VALID_GRAPH_MODES)!r}"
_SCOPE_FILTER_GRAPH_MODE_DETAIL: Final[str] = "scope_filter is not supported with graph_mode"
_PPR_WITH_COLLECTIONS_DETAIL: Final[str] = (
    "graph_mode is not supported with multi-collection fanout; use a single collection"
)
_GRAPH_DISABLED_DETAIL: Final[str] = "graph_mode requires [graph] enabled=true in server config"


@dataclass(frozen=True, slots=True)
class SearchCommand:
    """What an adapter asks of the search service; the fields REST's request carries, under the same names.

    Exactly one of ``collection`` (single path) or ``collections`` (multi path) is set once normalised.  ``top_k`` omitted
    means the configured ``top_k_return``.  ``filters`` is ``None`` when the caller gave no filter, and its own
    ``include_metadata`` is the nested flag REST accepts; ``include_metadata`` is the top-level flag.
    """

    query: str
    collection: str | None = None
    collections: tuple[str, ...] | None = None
    top_k: int | None = None
    filters: SearchFilters | None = None
    include_metadata: bool = False
    hyde: bool = False
    rag_fusion: bool = False
    graph_mode: str | None = None
    scope_filter: str | None = None
    acl_context: bool = False

    @property
    def wants_metadata(self) -> bool:
        """The two include-metadata flags, ORed (REST's rule); the one place they are combined."""
        return self.include_metadata or (self.filters is not None and self.filters.include_metadata)


def build_search_filters(
    *,
    file_type: str | None = None,
    source_path_prefix: str | None = None,
    source_path_glob: str | None = None,
    indexed_after: datetime | date | str | None = None,
    indexed_before: datetime | date | str | None = None,
    language: str | None = None,
) -> SearchFilters | None:
    """Build the one :class:`SearchFilters` from flat filter fields (MCP, CLI); ``None`` when no filter was given.

    No filter means no filter: with every field absent the result is ``None``, never an all-defaults model, because the
    pipeline treats the two differently.  The include-metadata flag is not a filter: MCP and the CLI set it on the
    command.  A rejected value is a ``validation`` error with a fixed detail.
    """
    values = (file_type, source_path_prefix, source_path_glob, indexed_after, indexed_before, language)
    if all(v is None for v in values):
        return None
    try:
        return SearchFilters(
            file_type=file_type,
            source_path_prefix=source_path_prefix,
            source_path_glob=source_path_glob,
            indexed_after=indexed_after,
            indexed_before=indexed_before,
            language=language,
        )
    except ValidationError as exc:
        raise ServiceError(ServiceErrorKind.VALIDATION, INVALID_SEARCH_FILTERS_DETAIL) from exc


def normalize_request_shape(command: SearchCommand) -> SearchCommand:
    """The rules that need no server configuration, in order (REST's request model calls the single rules; other adapters call this).

    Strips the collection name and the query, rejects a ``top_k`` below one and a ``graph_mode`` outside the valid set,
    then checks the collection selection and returns the command with the names stripped and de-duplicated.  Order is
    REST's: the field checks (collection, query, ``top_k``, ``graph_mode``), then the model-level selection check.
    """
    collection = normalize_collection(command.collection)
    query = normalize_query(command.query)
    if command.top_k is not None and command.top_k < TOP_K_MIN:
        raise ServiceError(ServiceErrorKind.VALIDATION, _TOP_K_BELOW_MINIMUM_DETAIL)
    if command.graph_mode is not None and command.graph_mode not in VALID_GRAPH_MODES:
        raise ServiceError(ServiceErrorKind.INVALID_GRAPH_MODE, _INVALID_GRAPH_MODE_DETAIL)
    collections = select_collections(collection, command.collections)
    return replace(command, query=query, collection=collection, collections=collections)


def normalize_search_command(
    command: SearchCommand,
    *,
    max_fanout: int,
    top_k_max: int,
    graph_enabled: bool,
) -> SearchCommand:
    """Return *command* normalised and checked against the operator's limits, or raise ``ServiceError``.

    Runs :func:`normalize_request_shape`, then the handler checks of REST in order: the fan-out limit (counted after
    de-duplication), ``top_k`` above the maximum, the ``scope_filter`` syntax, ``scope_filter`` with ``graph_mode``,
    ``ppr`` with ``collections`` and the ``[graph] enabled`` switch.  The first failure wins.
    """
    normalized = normalize_request_shape(command)
    collections = normalized.collections
    if collections is not None and len(collections) > max_fanout:
        raise ServiceError(ServiceErrorKind.VALIDATION, fanout_exceeded_detail(max_fanout))
    if normalized.top_k is not None and normalized.top_k > top_k_max:
        raise ServiceError(ServiceErrorKind.VALIDATION, top_k_exceeded_detail(normalized.top_k, top_k_max))

    scope_filter_error = validate_scope_filter(normalized.scope_filter)
    if scope_filter_error is not None:
        raise ServiceError(ServiceErrorKind.INVALID_SCOPE_FILTER, scope_filter_error)
    if normalized.scope_filter is not None and normalized.graph_mode is not None:
        raise ServiceError(ServiceErrorKind.SCOPE_FILTER_GRAPH_MODE_INCOMPATIBLE, _SCOPE_FILTER_GRAPH_MODE_DETAIL)
    if normalized.graph_mode == "ppr" and collections is not None:
        raise ServiceError(ServiceErrorKind.VALIDATION, _PPR_WITH_COLLECTIONS_DETAIL)
    if normalized.graph_mode is not None and not graph_enabled:
        raise ServiceError(ServiceErrorKind.GRAPH_DISABLED, _GRAPH_DISABLED_DETAIL)
    return normalized


def normalize_query(query: str) -> str:
    """The stripped query; an empty or blank one is a ``validation`` error (one of the rules REST's request model calls)."""
    stripped = query.strip()
    if not stripped:
        raise ServiceError(ServiceErrorKind.VALIDATION, _QUERY_EMPTY_DETAIL)
    return stripped


def normalize_collection(collection: str | None) -> str | None:
    """The stripped collection name (``None`` stays ``None``); an empty or blank one is a ``validation`` error."""
    if collection is None:
        return None
    stripped = collection.strip()
    if not stripped:
        raise ServiceError(ServiceErrorKind.VALIDATION, _COLLECTION_EMPTY_DETAIL)
    return stripped


def select_collections(collection: str | None, collections: Sequence[str] | None) -> tuple[str, ...] | None:
    """Validate that exactly one selection is given; return the stripped, de-duplicated names."""
    if collection is not None and collections is not None:
        raise ServiceError(ServiceErrorKind.VALIDATION, _COLLECTION_AND_COLLECTIONS_DETAIL)
    if collection is None and collections is None:
        raise ServiceError(ServiceErrorKind.VALIDATION, _COLLECTION_MISSING_DETAIL)
    if collections is None:
        return None
    if not collections:
        raise ServiceError(ServiceErrorKind.VALIDATION, _COLLECTIONS_EMPTY_DETAIL)
    deduped: list[str] = []
    for name in collections:
        stripped = name.strip()
        if not stripped:
            raise ServiceError(ServiceErrorKind.VALIDATION, _COLLECTION_NAME_EMPTY_DETAIL)
        if stripped not in deduped:
            deduped.append(stripped)
    return tuple(deduped)
