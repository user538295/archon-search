"""The description sample port (contract C2/C3): what description generation reads of the store, and how that read fails.

Owned by the Use Cases side: ``DescriptionGenerator`` depends on :class:`DescriptionSampleStore`, and the store
(Frameworks & Drivers) implements it and raises :class:`StoreReadError`, so the dependency points inward. Stdlib only, so
the store imports it at no cost.
"""
from __future__ import annotations

from typing import Protocol


class StoreReadError(Exception):
    """Raised by ``list_chunk_ids`` and ``read_excerpts`` when the store fails to read *collection*'s chunks.

    Never an empty result: ``[]`` from those reads means the read worked and found nothing, which a description
    generation settles differently (``noUnrestrictedChunks``) from a failed read (``sampleReadFailed``). The LanceDB or
    I/O error that caused it is chained as ``__cause__``; ``str(exc)`` names the collection only, never a chunk text.
    """

    def __init__(self, collection: str) -> None:
        super().__init__(f"could not read the chunks of collection {collection!r} from the store")
        self.collection = collection


class DescriptionSampleStore(Protocol):
    """The two reads of the sample (the store implements them, C3). Both raise :class:`StoreReadError` when the store
    fails: ``[]`` means the read worked and found nothing. The SAME ``include_restricted`` goes to both."""

    async def list_chunk_ids(self, collection: str, namespace: str, include_restricted: bool = False) -> list[str]: ...

    async def read_excerpts(
        self, collection: str, namespace: str, chunk_ids: list[str], include_restricted: bool = False
    ) -> list[str]: ...
