"""OpenAI-compatible Pydantic schema models for the G9 shim layer.

Pure data models — no business logic. Shapes mirror the OpenAI REST API so
clients using the OpenAI SDK can talk to archon-search without modification.

Reference: https://platform.openai.com/docs/api-reference/models
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# Known error type values (from scenarios S9–S19; the K1 contract types this as
# bare string for forward-compat — Literal is intentionally NOT used here so
# future error types added by OpenAI do not break handler construction).
_ERROR_TYPES = frozenset(
    {"invalid_request_error", "authentication_error", "server_error"}
)


class ModelObject(BaseModel):
    """One entry in the GET /v1/models response — mirrors OpenAI's Model object.

    ``created`` is always ``0`` (epoch). archon-search collections have no
    meaningful creation timestamp at the model-list level; ``0`` is the
    deliberate trade-off rather than fabricating a wall-clock value.
    """

    id: str
    object: Literal["model"] = "model"
    created: int = 0
    owned_by: str = "archon-search"


class ModelList(BaseModel):
    """GET /v1/models response envelope — mirrors OpenAI's list shape."""

    object: Literal["list"] = "list"
    data: list[ModelObject]


class OpenAIError(BaseModel):
    """Nested error detail inside an OpenAI-compatible error response."""

    message: str
    # K1 contract types this as string; Literal is intentionally NOT used —
    # see _ERROR_TYPES for the known values used in scenarios S9–S19.
    type: str
    param: str | None = None
    code: str | None = None


class OpenAIErrorResponse(BaseModel):
    """Top-level error envelope: ``{"error": {"message": ..., "type": ...}}``."""

    error: OpenAIError
