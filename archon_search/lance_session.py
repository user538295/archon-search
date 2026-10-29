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


async def list_all_table_names(db: lancedb.AsyncConnection) -> list[str]:
    """Return every table name in *db*, following LanceDB's ``page_token`` continuation.

    ``AsyncConnection.list_tables()`` returns one page (100 names by default); reading only
    ``.tables`` silently drops every table past the first page.

    Raises:
        RuntimeError: if a page hands back the token it was requested with — retrying would
            loop forever, and stopping would silently truncate the listing.
    """
    names: list[str] = []
    token: str | None = None
    while True:
        page = await db.list_tables(page_token=token)
        names.extend(page.tables)
        next_token = page.page_token
        if not next_token:
            return names
        if next_token == token:
            raise RuntimeError(f"LanceDB list_tables() returned a non-advancing page_token {token!r}")
        token = next_token
