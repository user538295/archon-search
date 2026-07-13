"""BE-2 (G9 OpenAI shim): Unit tests for OpenAI-compatible schema models.

TDD: these tests are written before the implementation and must fail first.
Covers: ModelObject, ModelList, OpenAIError, OpenAIErrorResponse.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from archon_search.server.schemas_openai import (
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
