"""Installer exception types, extracted so every other module imports them without cycles."""
from __future__ import annotations


class InstallLockError(Exception):
    """Raised when the install lock is already held by another process."""


class InstallError(Exception):
    """Raised to abort an install due to a pre-flight check failure."""


class NeedsForceDeleteError(InstallError):
    """Raised when a model or chunk_size conflict requires --force --delete-db to resolve."""
