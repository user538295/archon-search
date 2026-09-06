"""Installer exception types, extracted so every other module imports them without cycles."""
from __future__ import annotations

from .provisioning import ProvisionFailureKind


class InstallLockError(Exception):
    """Raised when the install lock is already held by another process."""


class InstallError(Exception):
    """Raised to abort an install due to a pre-flight check failure.

    ``kind`` carries the failure category structurally, so a call site can render
    a sanitized, remedy-bearing message without parsing (or leaking) the message
    text — several of these embed a filesystem path. ``None`` for the raise sites
    that are not model-artifact provisioning failures.
    """

    def __init__(self, *args: object, kind: ProvisionFailureKind | None = None) -> None:
        super().__init__(*args)
        self.kind = kind


class NeedsForceDeleteError(InstallError):
    """Raised when a model or chunk_size conflict requires --force --delete-db to resolve."""
