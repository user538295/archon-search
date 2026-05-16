"""Standalone constants for archon-search — defined independently of archon.ai.constants."""

# Pinned dated version for internal fast-model tasks (description generation).
DEFAULT_FAST_MODEL: str = "claude-haiku-4-5-20251001"

# Default model for description generation when a more capable model is preferred.
DEFAULT_MODEL: str = "claude-sonnet-4-6"

# Default namespace used when no explicit namespace is specified.
DEFAULT_NAMESPACE: str = "default"
