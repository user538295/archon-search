"""Standalone constants for archon-search — defined independently of archon.ai.constants."""

import re
from typing import Final

# Pre-A1 sentinel for ``ingested_by``. Stored on legacy rows; normalized to
# ``"cli"`` at read/header-parse boundaries (see _types.IngestedBy). Reindex
# (Task 6.2) rewrites legacy stored values to ``"reindex"``.
LEGACY_INGESTED_BY: Final[str] = "archon-search-cli"

# Canonical ``ingested_by`` values (must mirror the ``IngestedBy`` Literal in
# ``_types.py`` — drift is pinned by tests/test_types_ingested_by.py). Legacy
# is intentionally NOT included.
INGESTED_BY_VALUES: Final[tuple[str, ...]] = ("cli", "http", "watcher", "reindex")

# Per-collection ingest-lock acquisition timeout (Task 6.1). Hardcoded for v1;
# the only externally-visible knob is the 503 ``Retry-After`` header derived
# from this value (rounded up to integer seconds per RFC 7231).
INGEST_LOCK_TIMEOUT_S: Final[float] = 30.0

# asyncio.wait_for timeout used by SearchStore.ping() (B2 Task 1.1).
PING_TIMEOUT_SECONDS: Final[float] = 1.0

# In-process cache TTL for the ping result (B2 Task 1.1).
PING_TTL_SECONDS: Final[float] = 1.0

# Pinned dated version for internal fast-model tasks (description generation).
DEFAULT_FAST_MODEL: str = "claude-haiku-4-5-20251001"

# Default model for description generation when a more capable model is preferred.
DEFAULT_MODEL: str = "claude-sonnet-4-6"

# Default namespace used when no explicit namespace is specified.
DEFAULT_NAMESPACE: str = "default"

# Default blend weight for description-embedding cosine in hybrid routing.
# Shared by router.py, config.py, and eval/runner.py to avoid hardcoding 0.3.
DEFAULT_ROUTING_DESCRIPTION_WEIGHT: Final[float] = 0.3

# Maximum number of chunks passed to a single store.ingest_chunks() call during
# ingest_file() batch-emit (D4). At ~4 KB per chunk, 512 chunks ≈ 2 MB per
# batch — safe on any memory-constrained host.
# ponytail: profile on large PDFs before changing
# ponytail: monitor for LanceDB fragmentation on large corpora
_INGEST_CHUNK_BATCH_SIZE: Final[int] = 512

_NAMESPACE_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")


def _validate_segment_safe(name: str, label: str) -> None:
    """Raise ValueError if *name* contains patterns that break the __ table-name separator."""
    if name.endswith("_"):
        raise ValueError(
            f"Invalid {label} {name!r}: must not end with '_'"
        )
    if "__" in name:
        raise ValueError(
            f"Invalid {label} {name!r}: must not contain consecutive underscores '__'"
        )


def _validate_namespace(name: str) -> None:
    """Raise ValueError if name is not a valid namespace identifier."""
    if name == "deny-all":
        raise ValueError("Namespace name 'deny-all' is reserved and cannot be used.")
    if not _NAMESPACE_RE.fullmatch(name):
        raise ValueError(
            f"Invalid namespace {name!r}: must match ^[a-zA-Z0-9][a-zA-Z0-9_-]{{0,63}}$"
        )
    _validate_segment_safe(name, "namespace")
