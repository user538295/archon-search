"""RAG collection synchronisation utilities."""
from __future__ import annotations

import re
from pathlib import Path


def path_to_collection_name(path: str) -> str:
    """Derive a deterministic, sanitized LanceDB collection name from a filesystem path.

    Rules:
    - Expand ``~`` and resolve to absolute path.
    - Use the last path component (``Path.name``) as the raw name.
    - Sanitize: lowercase, replace non-alphanumeric runs with ``_``,
      strip leading/trailing ``_``.
    - Fall back to ``"collection"`` if the result is empty.

    This function is collision-unaware by design.  Collision resolution is
    applied in :class:`RagCollectionSync`.
    """
    resolved = Path(path).expanduser().resolve()
    name = resolved.name.lower()
    name = re.sub(r"[^a-z0-9]+", "_", name).strip("_")
    return name or "collection"
