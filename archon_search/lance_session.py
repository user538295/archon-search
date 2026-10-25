"""The bounded LanceDB session every archon-search connection opens its database on."""
from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import lancedb

_MIB: Final[int] = 1024 * 1024

#: Ceiling on the session's index cache (FTS and vector index partitions). LanceDB's default is
#: 6 GiB, more than a container running the embedder and reranker has to give.
LANCE_INDEX_CACHE_BYTES: Final[int] = 512 * _MIB

#: Ceiling on the session's metadata cache (manifests and transactions of every table version
#: read or written). LanceDB's default is 1 GiB, and one meta-table write adds ~12 KiB to it
#: (measured), so the default let an ingest-heavy server's memory climb without bound. Entries of
#: superseded versions are never read again; evicting them costs nothing.
LANCE_METADATA_CACHE_BYTES: Final[int] = 64 * _MIB


def new_lance_session() -> lancedb.Session:
    """Return a session with the cache ceilings above; one per connection."""
    import lancedb  # lazy: keeps module import free of pyarrow and the Rust extension

    return lancedb.Session(
        index_cache_size_bytes=LANCE_INDEX_CACHE_BYTES,
        metadata_cache_size_bytes=LANCE_METADATA_CACHE_BYTES,
    )
