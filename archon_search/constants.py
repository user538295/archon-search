"""Standalone constants for archon-search — defined independently of archon.ai.constants."""

import re

# Pinned dated version for internal fast-model tasks (description generation).
DEFAULT_FAST_MODEL: str = "claude-haiku-4-5-20251001"

# Default model for description generation when a more capable model is preferred.
DEFAULT_MODEL: str = "claude-sonnet-4-6"

# Default namespace used when no explicit namespace is specified.
DEFAULT_NAMESPACE: str = "default"

_NAMESPACE_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")


def _validate_namespace(name: str) -> None:
    """Raise ValueError if name is not a valid namespace identifier."""
    if not _NAMESPACE_RE.fullmatch(name):
        raise ValueError(
            f"Invalid namespace {name!r}: must match ^[a-zA-Z0-9][a-zA-Z0-9_-]{{0,63}}$"
        )
