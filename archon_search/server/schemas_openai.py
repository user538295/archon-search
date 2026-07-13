"""OpenAI-compatible Pydantic schema models for the G9 shim layer.

Pure data models — no business logic. Shapes mirror the OpenAI REST API so
clients using the OpenAI SDK can talk to archon-search without modification.

Reference: https://platform.openai.com/docs/api-reference/models
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

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


# ---------------------------------------------------------------------------
# Chat completion schemas (G9 OpenAI shim — BE-4)
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    """A single chat message with a role and text content.

    ``content`` is deliberately typed as ``str`` — the OpenAI spec allows an
    array of content parts, but the Archon shim does not support multipart
    content (retrieval answers are always plain text).
    """

    role: str
    content: str  # Archon simplification: str only, not str | list


class ChatCompletionRequest(BaseModel):
    """POST /v1/chat/completions request body (OpenAI-compatible subset)."""

    model: str
    messages: list[ChatMessage]
    stream: bool = False
    # ``top_k`` is accepted for forward-compatibility but has no effect at
    # runtime — the pipeline uses its construction-time top_k setting.
    top_k: int | None = None


class ChatCompletionUsage(BaseModel):
    """Token-usage envelope — always zero because Archon is retrieval-only."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionChoice(BaseModel):
    """One candidate completion inside a ``ChatCompletionResponse``."""

    index: int
    message: ChatMessage
    finish_reason: str  # typically "stop"


class ChatCompletionResponse(BaseModel):
    """POST /v1/chat/completions response envelope (OpenAI-compatible)."""

    id: str  # format: "chatcmpl-{uuid4}"
    object: Literal["chat.completion"] = "chat.completion"
    created: int  # Unix timestamp
    model: str
    choices: list[ChatCompletionChoice]
    usage: ChatCompletionUsage = Field(default_factory=ChatCompletionUsage)


# ---------------------------------------------------------------------------
# Streaming chunk schemas (BE-7)
# ---------------------------------------------------------------------------


class ChunkDelta(BaseModel):
    """Delta content for one SSE streaming frame.

    Fields are optional so the stop frame serializes as ``{}`` via
    ``model_dump(exclude_none=True)``, and frames 2..N omit ``role``.
    """

    role: str | None = None
    content: str | None = None


class ChatCompletionChunkChoice(BaseModel):
    """One choice inside a ``ChatCompletionChunk``."""

    index: int = 0
    delta: ChunkDelta
    finish_reason: str | None  # null on content frames, "stop" on stop frame


class ChatCompletionChunk(BaseModel):
    """One SSE data frame in a streaming ``POST /v1/chat/completions`` response."""

    id: str  # format: "chatcmpl-{uuid4}", same across all frames of one completion
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int  # Unix timestamp, fixed for the lifetime of the stream
    model: str
    choices: list[ChatCompletionChunkChoice]
