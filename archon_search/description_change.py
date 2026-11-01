"""Set or clear a collection's description for the caller's namespace: the one rule REST (C6) and MCP (C8) share.

The caller must own the collection: it has a meta row in the caller's namespace, and anything else, a collection of another
namespace or a name the store refuses included, is the same ``NOT_FOUND``. That is the whole rule: no admin key and no
config-path entry, so a collection that exists only as meta rows works. The shared writer embeds the text, then takes the
collection lock itself inside one guarded write, so no caller holds a lock while it calls this.
"""
from __future__ import annotations

import dataclasses
from enum import StrEnum
from typing import Protocol

from archon_search.collection_meta import CollectionMeta
from archon_search.description_write import DescriptionWriteOutcome
from archon_search.description_writer import (
    ClearChange,
    DescriptionChange,
    ManualSetChange,
    WriteResult,
)
from archon_search.store import is_valid_collection_name


class DescriptionChangeStatus(StrEnum):
    """What a set or clear did. Only ``CHANGED`` wrote the row."""

    CHANGED = "changed"
    #: The caller's namespace has no such collection, or it was dropped during the write.
    NOT_FOUND = "notFound"
    #: The collection lock was not free in time.
    STORE_BUSY = "storeBusy"


@dataclasses.dataclass(frozen=True)
class DescriptionChangeResult:
    """The status of one set or clear and, when it wrote the row, the row as stored."""

    status: DescriptionChangeStatus
    #: The stored row after the write; present exactly when ``status`` is ``CHANGED``.
    meta: CollectionMeta | None = None


class _MetaReader(Protocol):
    async def get_collection_meta(self, collection: str, namespace: str = ...) -> CollectionMeta | None: ...


class _Writer(Protocol):
    async def apply(self, collection: str, namespace: str, change: DescriptionChange) -> WriteResult: ...


async def apply_description_change(
    store: _MetaReader,
    writer: _Writer,
    name: str,
    namespace: str,
    change: ManualSetChange | ClearChange,
) -> DescriptionChangeResult:
    """Apply *change* to *name* in *namespace* and return the stored row it left.

    A name the store refuses cannot belong to any collection, so it is the same ``NOT_FOUND`` as an unknown one; it is
    checked before the read, so a ValueError from reading a real row (a corrupt one) is never mistaken for an absent
    collection.

    Raises:
        RuntimeError: the store refused an unconditional write. Set and clear carry no guard, so a refusal is a broken
            contract.
    """
    if not is_valid_collection_name(name) or await store.get_collection_meta(name, namespace=namespace) is None:
        return DescriptionChangeResult(DescriptionChangeStatus.NOT_FOUND)

    written = await writer.apply(name, namespace, change)
    match written.outcome:
        case DescriptionWriteOutcome.WRITTEN:
            pass
        case DescriptionWriteOutcome.LOCK_TIMEOUT:
            return DescriptionChangeResult(DescriptionChangeStatus.STORE_BUSY)
        case DescriptionWriteOutcome.COLLECTION_MISSING:
            return DescriptionChangeResult(DescriptionChangeStatus.NOT_FOUND)
        case refused:
            raise RuntimeError(f"an unconditional description write was refused: {refused.value}")

    meta = await store.get_collection_meta(name, namespace=namespace)
    if meta is None:  # dropped between the write and this read
        return DescriptionChangeResult(DescriptionChangeStatus.NOT_FOUND)
    return DescriptionChangeResult(DescriptionChangeStatus.CHANGED, meta)
