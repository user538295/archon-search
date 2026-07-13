"""BE-2 + BE-4 (G9 OpenAI shim): Unit tests for OpenAI-compatible schema models.

TDD: these tests are written before the implementation and must fail first.
Covers: ModelObject, ModelList, OpenAIError, OpenAIErrorResponse (BE-2);
        ChatMessage, ChatCompletionRequest, ChatCompletionChoice,
        ChatCompletionUsage, ChatCompletionResponse (BE-4).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from archon_search.server.schemas_openai import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionUsage,
    ChatMessage,
    ModelList,
    ModelObject,
    OpenAIError,
    OpenAIErrorResponse,
    _ERROR_TYPES,
)


# ---------------------------------------------------------------------------
# ModelObject
# ---------------------------------------------------------------------------


def test_model_object_serialization() -> None:
    """ModelObject serialises to correct OpenAI-compatible JSON keys."""
    obj = ModelObject(id="archon-search/docs")
    data = json.loads(obj.model_dump_json())
    assert data["id"] == "archon-search/docs"
    assert data["object"] == "model"
    assert "created" in data
    assert data["owned_by"] == "archon-search"


def test_model_object_created_is_zero() -> None:
    """ModelObject.created is always 0 — deliberate epoch trade-off, not a real timestamp."""
    obj = ModelObject(id="archon-search/docs")
    data = json.loads(obj.model_dump_json())
    assert data["created"] == 0


# ---------------------------------------------------------------------------
# ModelList
# ---------------------------------------------------------------------------


def test_model_list_shape() -> None:
    """ModelList serialises as {"object": "list", "data": [...]}."""
    entry = ModelObject(id="archon-search/docs")
    lst = ModelList(data=[entry])
    data = json.loads(lst.model_dump_json())
    assert data["object"] == "list"
    assert isinstance(data["data"], list)
    assert data["data"][0]["id"] == "archon-search/docs"


# ---------------------------------------------------------------------------
# OpenAIErrorResponse
# ---------------------------------------------------------------------------


def test_openai_error_response_shape() -> None:
    """OpenAIErrorResponse serialises as {"error": {"message": ..., "type": ...}}."""
    err = OpenAIErrorResponse(
        error=OpenAIError(message="not found", type="invalid_request_error")
    )
    data = json.loads(err.model_dump_json())
    assert "error" in data
    assert data["error"]["message"] == "not found"
    assert data["error"]["type"] == "invalid_request_error"


def test_openai_error_optional_fields_excluded_when_none() -> None:
    """OpenAIError optional param/code fields are absent when not set."""
    err = OpenAIErrorResponse(
        error=OpenAIError(message="oops", type="server_error")
    )
    data = json.loads(err.model_dump_json(exclude_none=True))
    assert "param" not in data["error"]
    assert "code" not in data["error"]


def test_openai_error_optional_fields_present_when_set() -> None:
    """OpenAIError param and code fields are included when explicitly set."""
    err = OpenAIErrorResponse(
        error=OpenAIError(
            message="bad param", type="invalid_request_error", param="model", code="42"
        )
    )
    data = json.loads(err.model_dump_json())
    assert data["error"]["param"] == "model"
    assert data["error"]["code"] == "42"


def test_openai_error_default_serialization_includes_null_fields() -> None:
    """Default model_dump_json() (no exclude_none) emits param/code as null.

    Documents the real wire behavior callers must handle when not passing
    exclude_none=True explicitly.
    """
    err = OpenAIErrorResponse(
        error=OpenAIError(message="oops", type="server_error")
    )
    data = json.loads(err.model_dump_json())
    assert "param" in data["error"]
    assert data["error"]["param"] is None
    assert "code" in data["error"]
    assert data["error"]["code"] is None


def test_openai_error_type_known_values() -> None:
    """OpenAIError.type accepts the three documented scenario values.

    type is typed as str (not Literal) to match the K1 contract and preserve
    forward-compatibility with future OpenAI error types.  The known values are
    documented in _ERROR_TYPES and used across scenarios S9–S19.
    """
    assert _ERROR_TYPES == {"invalid_request_error", "authentication_error", "server_error"}
    for valid_type in _ERROR_TYPES:
        err = OpenAIError(message="test", type=valid_type)
        assert err.type == valid_type

    # Any string is accepted — type is str per the K1 contract
    err = OpenAIError(message="test", type="future_openai_error_type")
    assert err.type == "future_openai_error_type"


def test_model_object_literal_rejection() -> None:
    """ModelObject rejects object values other than 'model'."""
    with pytest.raises(ValidationError):
        ModelObject(id="x", object="not-a-model")  # type: ignore[call-arg]


def test_model_list_literal_rejection() -> None:
    """ModelList rejects object values other than 'list'."""
    with pytest.raises(ValidationError):
        ModelList(object="not-a-list", data=[])  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# BE-4: ChatMessage, ChatCompletionRequest, ChatCompletionChoice,
#        ChatCompletionUsage, ChatCompletionResponse
# ---------------------------------------------------------------------------


def test_chat_completion_response_serialization() -> None:
    """ChatCompletionResponse serialises with expected field values."""
    msg = ChatMessage(role="assistant", content="hello")
    choice = ChatCompletionChoice(index=0, message=msg, finish_reason="stop")
    usage = ChatCompletionUsage()
    resp = ChatCompletionResponse(
        id="chatcmpl-abc123",
        created=1700000000,
        model="archon-search/docs",
        choices=[choice],
        usage=usage,
    )
    data = json.loads(resp.model_dump_json())

    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["role"] == "assistant"
    # usage must have exactly these three zero fields
    assert data["usage"]["prompt_tokens"] == 0
    assert data["usage"]["completion_tokens"] == 0
    assert data["usage"]["total_tokens"] == 0


def test_chat_completion_request_validation() -> None:
    """ChatCompletionRequest accepts empty messages list; missing messages raises ValidationError."""
    # Valid: empty messages list is allowed at the schema level (validation happens in handler)
    req = ChatCompletionRequest(model="archon-search", messages=[])
    assert req.model == "archon-search"
    assert req.messages == []

    # Missing messages field raises ValidationError
    with pytest.raises(ValidationError):
        ChatCompletionRequest(model="archon-search")  # type: ignore[call-arg]


def test_chat_message_content_string_only() -> None:
    """ChatMessage.content must be typed as str (not str | list).

    The OpenAI spec allows array content, but Archon's shim does not support it
    (deliberate simplification). Passing a list must raise ValidationError.
    """
    # String content works
    msg = ChatMessage(role="user", content="hello")
    assert msg.content == "hello"

    # List content raises ValidationError — Archon simplification
    with pytest.raises(ValidationError):
        ChatMessage(role="user", content=["part1", "part2"])  # type: ignore[arg-type]

    # Confirm the annotation is str, not a union
    annotation = ChatMessage.model_fields["content"].annotation
    assert annotation is str


def test_user_message_extraction_role_case() -> None:
    """role is stored case-sensitively; 'user' != 'User'.

    Documents that extraction logic must use exact ``role == "user"`` comparison.
    """
    lower = ChatMessage(role="user", content="hello")
    upper = ChatMessage(role="User", content="hello")

    assert lower.role == "user"
    assert upper.role == "User"
    # Prove case-sensitivity: these are not equal
    assert lower.role != upper.role
