"""Field-level parity check (plan C9, brief Q3): the fields each interface carries, against an explicit mapping.

``field_parity.py`` holds the check (:func:`verify_request`, :func:`verify_response`) and the collectors that read the live
schemas; ``field_registry.py`` is the mapping, plain data like ``registry.py``. The contract is
``Documentation/Backlog/interface-parity/01_search-c9-field-parity.tsp``. The unit tests here run against synthetic
mappings and field sets; the real mapping is checked against the live tree at the end. The surface-level registry check
(``test_parity.py``) is unchanged.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel, Field

from tests.parity.field_parity import (
    FieldParityFailure,
    FieldParityFinding,
    LiveFields,
    Surface,
    collect_request_fields,
    collect_response_fields,
    leaf_names,
    verify_request,
    verify_response,
)
from tests.parity.field_registry import (
    REQUEST_FIELD_MAPPINGS,
    RESPONSE_FIELD_MAPPINGS,
    TRANSPORT_EXEMPTIONS,
)

_MISSING = FieldParityFailure.MISSING_ON_SURFACE
_UNMAPPED = FieldParityFailure.UNMAPPED_SURFACE_FIELD
_INVALID = FieldParityFailure.INVALID_WAIVER

# ---------------------------------------------------------------------------
# Synthetic mappings and field sets (the failure kinds are tested without the real mapping)
# ---------------------------------------------------------------------------

_ROWS: list[dict[str, Any]] = [
    {"command_field": "query", "rest_field": "query", "mcp_param": "query", "cli_option": "query"},
    {"command_field": "top_k", "rest_field": "top_k", "mcp_param": "top_k", "cli_option": "--top-k"},
]
_LIVE: LiveFields = {
    Surface.COMMAND: frozenset({"query", "top_k"}),
    Surface.REST: frozenset({"query", "top_k"}),
    Surface.MCP: frozenset({"query", "top_k"}),
    Surface.CLI: frozenset({"query", "--top-k", "--json"}),
}
_EXEMPT: dict[str, Any] = {"cli_options": ["--json"]}


def _without(live: LiveFields, surface: Surface, name: str) -> LiveFields:
    return {**live, surface: live[surface] - {name}}


def _with(live: LiveFields, surface: Surface, name: str) -> LiveFields:
    return {**live, surface: live[surface] | {name}}


def _waived(row: dict[str, Any], *surfaces: str, reason: str | None = "agreed") -> dict[str, Any]:
    """*row* with the field dropped from *surfaces* and a waiver over them."""
    keys = {"rest": "rest_field", "mcp": "mcp_param", "cli": "cli_option"}
    waived = {**row, "waived_surfaces": list(surfaces), "waiver_reason": reason}
    for surface in surfaces:
        waived.pop(keys[surface], None)
    return waived


def test_fully_mapped_fields_pass() -> None:
    """Positive control: a verifier that always fails could not satisfy the failure tests below."""
    assert verify_request(_ROWS, _LIVE, _EXEMPT) == []


@pytest.mark.parametrize("surface", [Surface.REST, Surface.MCP, Surface.CLI])
def test_field_missing_from_a_surface_fails(surface: Surface) -> None:
    name = {Surface.REST: "top_k", Surface.MCP: "top_k", Surface.CLI: "--top-k"}[surface]

    findings = verify_request(_ROWS, _without(_LIVE, surface, name), _EXEMPT)

    assert findings == [FieldParityFinding(_MISSING, surface, "top_k")]


@pytest.mark.parametrize("surface", [Surface.REST, Surface.MCP, Surface.CLI])
def test_field_with_no_name_on_a_surface_and_no_waiver_fails(surface: Surface) -> None:
    key = {Surface.REST: "rest_field", Surface.MCP: "mcp_param", Surface.CLI: "cli_option"}[surface]
    name = {Surface.REST: "top_k", Surface.MCP: "top_k", Surface.CLI: "--top-k"}[surface]
    rows = [_ROWS[0], {k: v for k, v in _ROWS[1].items() if k != key}]

    findings = verify_request(rows, _LIVE, _EXEMPT)

    # the surface carries the name, but no mapping claims it: the field is missing and the surface field maps to nothing
    assert findings == [FieldParityFinding(_MISSING, surface, "top_k"), FieldParityFinding(_UNMAPPED, surface, name)]


@pytest.mark.parametrize(
    ("surface", "name"),
    [(Surface.REST, "extra"), (Surface.MCP, "extra"), (Surface.CLI, "--extra"), (Surface.CLI, "extra_arg")],
)
def test_surface_field_that_maps_to_nothing_fails(surface: Surface, name: str) -> None:
    findings = verify_request(_ROWS, _with(_LIVE, surface, name), _EXEMPT)

    assert findings == [FieldParityFinding(_UNMAPPED, surface, name)]


def test_exempt_transport_options_map_to_nothing_without_a_finding() -> None:
    assert "--json" in _LIVE[Surface.CLI]
    assert verify_request(_ROWS, _LIVE, _EXEMPT) == []


def test_an_unlisted_transport_option_fails_and_an_exemption_covers_the_cli_only() -> None:
    live = _with(_with(_LIVE, Surface.CLI, "--verbose"), Surface.REST, "--json")

    findings = verify_request(_ROWS, live, _EXEMPT)

    assert findings == [
        FieldParityFinding(_UNMAPPED, Surface.REST, "--json"),
        FieldParityFinding(_UNMAPPED, Surface.CLI, "--verbose"),
    ]


def test_command_field_without_a_mapping_fails() -> None:
    findings = verify_request(_ROWS, _with(_LIVE, Surface.COMMAND, "hyde"), _EXEMPT)

    assert findings == [FieldParityFinding(_UNMAPPED, Surface.COMMAND, "hyde")]


def test_mapping_of_a_command_field_that_no_longer_exists_fails() -> None:
    findings = verify_request(_ROWS, _without(_LIVE, Surface.COMMAND, "top_k"), _EXEMPT)

    assert findings == [FieldParityFinding(_MISSING, Surface.COMMAND, "top_k")]


def test_a_valid_waiver_covers_the_surfaces_it_names() -> None:
    rows = [_ROWS[0], _waived(_ROWS[1], "mcp", "cli")]
    live = _without(_without(_LIVE, Surface.MCP, "top_k"), Surface.CLI, "--top-k")

    assert verify_request(rows, live, _EXEMPT) == []


def test_a_waiver_does_not_cover_a_surface_it_does_not_name() -> None:
    rows = [_ROWS[0], _waived(_ROWS[1], "mcp")]
    live = _without(_without(_LIVE, Surface.MCP, "top_k"), Surface.CLI, "--top-k")

    assert verify_request(rows, live, _EXEMPT) == [FieldParityFinding(_MISSING, Surface.CLI, "top_k")]


@pytest.mark.parametrize(
    "waiver",
    [
        pytest.param({"waived_surfaces": [], "waiver_reason": "agreed"}, id="empty-surface-list"),
        pytest.param({"waiver_reason": "agreed"}, id="no-surface-list"),
        pytest.param({"waived_surfaces": ["mcp"], "waiver_reason": ""}, id="empty-reason"),
        pytest.param({"waived_surfaces": ["mcp"], "waiver_reason": "   "}, id="blank-reason"),
        pytest.param({"waived_surfaces": ["mcp"]}, id="no-reason"),
        pytest.param({"waived_surfaces": ["mcp"], "waiver_reason": None}, id="null-reason"),
    ],
)
def test_waiver_without_surfaces_or_without_a_reason_is_invalid_and_covers_nothing(waiver: dict[str, Any]) -> None:
    row = {k: v for k, v in _ROWS[1].items() if k != "mcp_param"} | waiver
    live = _without(_LIVE, Surface.MCP, "top_k")

    findings = verify_request([_ROWS[0], row], live, _EXEMPT)

    assert FieldParityFinding(_INVALID, Surface.COMMAND, "top_k") in findings
    assert FieldParityFinding(_MISSING, Surface.MCP, "top_k") in findings


def test_waiver_naming_a_surface_that_carries_the_field_is_invalid() -> None:
    row = {**_ROWS[1], "waived_surfaces": ["rest"], "waiver_reason": "agreed"}

    assert verify_request([_ROWS[0], row], _LIVE, _EXEMPT) == [FieldParityFinding(_INVALID, Surface.REST, "top_k")]


def test_waiver_naming_the_command_surface_is_invalid() -> None:
    row = {**_ROWS[1], "waived_surfaces": ["command"], "waiver_reason": "agreed"}

    assert verify_request([_ROWS[0], row], _LIVE, _EXEMPT) == [FieldParityFinding(_INVALID, Surface.COMMAND, "top_k")]


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        pytest.param(
            [{**_ROWS[0], "waived_surfaces": ["shim"], "waiver_reason": "agreed"}], "unknown surface", id="unknown-surface"
        ),
        pytest.param([{**_ROWS[0], "rest_fied": "top_k"}], "unknown key", id="misspelled-key"),
        pytest.param([{"rest_field": "query"}], "missing key", id="no-source-field"),
        pytest.param([_ROWS[0], dict(_ROWS[0])], "duplicate", id="duplicate-source-field"),
    ],
)
def test_malformed_mapping_is_an_error_not_a_finding(rows: list[dict[str, Any]], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        verify_request(rows, _LIVE, _EXEMPT)


def test_waiver_with_an_empty_surface_list_and_no_reason_is_still_an_invalid_waiver() -> None:
    row = {k: v for k, v in _ROWS[1].items() if k != "mcp_param"} | {"waived_surfaces": []}
    live = _without(_LIVE, Surface.MCP, "top_k")

    findings = verify_request([_ROWS[0], row], live, _EXEMPT)

    assert findings == [
        FieldParityFinding(_INVALID, Surface.COMMAND, "top_k"),
        FieldParityFinding(_MISSING, Surface.MCP, "top_k"),
    ]


# ---------------------------------------------------------------------------
# The response side
# ---------------------------------------------------------------------------

_RESPONSE_ROWS: list[dict[str, Any]] = [
    {"result_field": "acl_filtered", "rest_field": "acl_filtered", "mcp_field": "acl_filtered"},
    {"result_field": "include_metadata", "waived_surfaces": ["rest", "mcp"], "waiver_reason": "request flag, not echoed"},
]
_RESPONSE_LIVE: LiveFields = {
    Surface.COMMAND: frozenset({"acl_filtered", "include_metadata"}),
    Surface.REST: frozenset({"acl_filtered"}),
    Surface.MCP: frozenset({"acl_filtered"}),
}


def test_fully_mapped_response_fields_pass() -> None:
    assert verify_response(_RESPONSE_ROWS, _RESPONSE_LIVE) == []


@pytest.mark.parametrize("surface", [Surface.REST, Surface.MCP])
def test_result_field_missing_from_a_response_fails(surface: Surface) -> None:
    findings = verify_response(_RESPONSE_ROWS, _without(_RESPONSE_LIVE, surface, "acl_filtered"))

    assert findings == [FieldParityFinding(_MISSING, surface, "acl_filtered")]


@pytest.mark.parametrize("surface", [Surface.REST, Surface.MCP])
def test_response_field_that_maps_to_nothing_fails(surface: Surface) -> None:
    findings = verify_response(_RESPONSE_ROWS, _with(_RESPONSE_LIVE, surface, "extra"))

    assert findings == [FieldParityFinding(_UNMAPPED, surface, "extra")]


def test_result_field_without_a_mapping_fails() -> None:
    findings = verify_response(_RESPONSE_ROWS, _with(_RESPONSE_LIVE, Surface.COMMAND, "applied_filters"))

    assert findings == [FieldParityFinding(_UNMAPPED, Surface.COMMAND, "applied_filters")]


@pytest.mark.parametrize("waiver", [{"waived_surfaces": ["rest"]}, {"waived_surfaces": [], "waiver_reason": "agreed"}])
def test_response_waiver_without_a_reason_or_surfaces_is_invalid(waiver: dict[str, Any]) -> None:
    rows = [_RESPONSE_ROWS[0], {"result_field": "include_metadata", **waiver}]

    assert FieldParityFinding(_INVALID, Surface.COMMAND, "include_metadata") in verify_response(rows, _RESPONSE_LIVE)


def test_response_waiver_naming_the_cli_is_invalid() -> None:
    """The CLI's ``--json`` prints the REST body, so it is no response surface of its own."""
    rows = [_RESPONSE_ROWS[0], {**_RESPONSE_ROWS[1], "waived_surfaces": ["rest", "mcp", "cli"]}]

    assert verify_response(rows, _RESPONSE_LIVE) == [FieldParityFinding(_INVALID, Surface.CLI, "include_metadata")]


# ---------------------------------------------------------------------------
# The walker that names a model's fields
# ---------------------------------------------------------------------------


@dataclass
class _Leg:
    name: str
    weight: float


class _Envelope(BaseModel):
    legs: list[_Leg]
    best: _Leg | None = None
    note: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)


@dataclass
class _Empty:
    pass


@dataclass
class _HoldsEmpty:
    empty: _Empty


@dataclass
class _TwoModels:
    either: _Leg | _Empty


class _Aliased(BaseModel):
    wire: str = Field(default="", serialization_alias="renamed")


def test_leaf_names_are_dotted_paths_with_a_list_mark_and_plain_containers_are_leaves() -> None:
    assert leaf_names(_Envelope) == {"legs[].name", "legs[].weight", "best.name", "best.weight", "note", "tags"}


def test_a_nested_model_without_fields_is_named_by_its_container() -> None:
    assert leaf_names(_HoldsEmpty) == {"empty"}


def test_a_field_holding_two_models_is_an_error_not_a_silently_dropped_one() -> None:
    with pytest.raises(ValueError, match="more than one model"):
        leaf_names(_TwoModels)


def test_a_field_with_an_alias_is_an_error_because_the_wire_name_is_not_the_attribute_name() -> None:
    with pytest.raises(ValueError, match="alias"):
        leaf_names(_Aliased)


# ---------------------------------------------------------------------------
# The live schemas
# ---------------------------------------------------------------------------

_LIVE_GROUP = pytest.mark.xdist_group("parity-live")  # one worker builds the live surfaces once (``--dist=loadgroup``)


@pytest.fixture(scope="module")
def live_request_fields() -> LiveFields:
    return collect_request_fields()


@pytest.fixture(scope="module")
def live_response_fields() -> LiveFields:
    return collect_response_fields()


@_LIVE_GROUP
def test_live_command_fields_are_the_leaves_of_the_command_with_its_filters_nested(live_request_fields: LiveFields) -> None:
    assert live_request_fields[Surface.COMMAND] == {
        "query",
        "collection",
        "collections",
        "top_k",
        "filters.file_type",
        "filters.source_path_prefix",
        "filters.source_path_glob",
        "filters.indexed_after",
        "filters.indexed_before",
        "filters.language",
        "filters.include_metadata",
        "include_metadata",
        "hyde",
        "rag_fusion",
        "graph_mode",
        "scope_filter",
        "acl_context",
    }


@_LIVE_GROUP
def test_live_rest_fields_nest_the_filters_and_mcp_fields_are_flat(live_request_fields: LiveFields) -> None:
    rest = live_request_fields[Surface.REST]
    mcp = live_request_fields[Surface.MCP]

    assert {"filters.file_type", "filters.include_metadata", "include_metadata", "top_k"} <= rest
    assert "filters" not in rest
    assert {"file_type", "include_metadata", "top_k", "acl_context", "query"} <= mcp
    assert not any(name.startswith("filters") for name in mcp)


@_LIVE_GROUP
def test_live_cli_fields_are_the_options_and_the_query_argument_without_help(live_request_fields: LiveFields) -> None:
    cli = live_request_fields[Surface.CLI]

    assert {"query", "--collection", "--top-k", "--include-metadata", "--json", "--api-url", "--api-key"} <= cli
    assert "--help" not in cli


@_LIVE_GROUP
def test_live_response_fields_nest_results_excluded_collections_and_applied_filters(
    live_response_fields: LiveFields,
) -> None:
    for surface in (Surface.REST, Surface.MCP):
        names = live_response_fields[surface]
        assert {
            "acl_filtered",
            "results[].doc_id",
            "results[].acl_gate.source",
            "excluded_collections[].name",
            "applied_filters.file_type",
        } <= names, surface
        assert not {"results", "excluded_collections", "applied_filters", "results[].acl_gate"} & names, surface
    assert {"results[].result.doc_id", "results[].acl_gate.warnings", "include_metadata"} <= live_response_fields[
        Surface.COMMAND
    ]


# ---------------------------------------------------------------------------
# The real mapping against the live tree
# ---------------------------------------------------------------------------


@_LIVE_GROUP
def test_field_parity_passes_on_the_real_tree(live_request_fields: LiveFields, live_response_fields: LiveFields) -> None:
    """Every command and result field is mapped or waived, and no surface field maps to nothing, bar the transport options (S41)."""
    assert verify_request(REQUEST_FIELD_MAPPINGS, live_request_fields, TRANSPORT_EXEMPTIONS) == []
    assert verify_response(RESPONSE_FIELD_MAPPINGS, live_response_fields) == []


@_LIVE_GROUP
@pytest.mark.parametrize(("surface", "name"), [(Surface.REST, "top_k"), (Surface.MCP, "top_k"), (Surface.CLI, "--top-k")])
def test_field_parity_fails_on_a_field_missing_from_a_surface(
    live_request_fields: LiveFields, surface: Surface, name: str
) -> None:
    """A fake surface without ``top_k``."""
    findings = verify_request(
        REQUEST_FIELD_MAPPINGS, _without(live_request_fields, surface, name), TRANSPORT_EXEMPTIONS
    )

    assert findings == [FieldParityFinding(_MISSING, surface, "top_k")]


@_LIVE_GROUP
def test_field_parity_fails_on_a_response_field_missing_from_a_surface(live_response_fields: LiveFields) -> None:
    findings = verify_response(
        RESPONSE_FIELD_MAPPINGS, _without(live_response_fields, Surface.MCP, "results[].acl_gate.source")
    )

    assert findings == [FieldParityFinding(_MISSING, Surface.MCP, "results[].acl_gate.source")]


@_LIVE_GROUP
@pytest.mark.parametrize(
    ("surface", "name"),
    [(Surface.REST, "extra"), (Surface.MCP, "extra"), (Surface.CLI, "--extra"), (Surface.CLI, "--json-lines")],
)
def test_field_parity_fails_on_a_surface_field_that_maps_to_nothing(
    live_request_fields: LiveFields, surface: Surface, name: str
) -> None:
    """Any new surface field fails, except the exempt transport options (``--json``, ``--api-url``, ``--api-key``)."""
    findings = verify_request(REQUEST_FIELD_MAPPINGS, _with(live_request_fields, surface, name), TRANSPORT_EXEMPTIONS)

    assert findings == [FieldParityFinding(_UNMAPPED, surface, name)]


@_LIVE_GROUP
def test_field_parity_fails_on_a_response_field_that_maps_to_nothing(live_response_fields: LiveFields) -> None:
    findings = verify_response(RESPONSE_FIELD_MAPPINGS, _with(live_response_fields, Surface.REST, "keyword_only"))

    assert findings == [FieldParityFinding(_UNMAPPED, Surface.REST, "keyword_only")]


def _row(rows: list[dict[str, Any]], key: str, field: str) -> dict[str, Any]:
    return next(row for row in rows if row[key] == field)


@_LIVE_GROUP
@pytest.mark.parametrize(
    "damage",
    [
        pytest.param({"waived_surfaces": []}, id="no-surfaces"),
        pytest.param({"waiver_reason": ""}, id="no-reason"),
        pytest.param({"waiver_reason": None}, id="null-reason"),
    ],
)
def test_field_parity_fails_on_an_invalid_waiver(live_request_fields: LiveFields, damage: dict[str, Any]) -> None:
    """No ``waived_surfaces`` or no reason on the real ``filters.include_metadata`` waiver."""
    rows = copy.deepcopy(REQUEST_FIELD_MAPPINGS)
    _row(rows, "command_field", "filters.include_metadata").update(damage)

    findings = verify_request(rows, live_request_fields, TRANSPORT_EXEMPTIONS)

    assert FieldParityFinding(_INVALID, Surface.COMMAND, "filters.include_metadata") in findings


@_LIVE_GROUP
def test_field_parity_fails_on_an_invalid_response_waiver(live_response_fields: LiveFields) -> None:
    rows = copy.deepcopy(RESPONSE_FIELD_MAPPINGS)
    _row(rows, "result_field", "include_metadata")["waiver_reason"] = ""

    findings = verify_response(rows, live_response_fields)

    assert FieldParityFinding(_INVALID, Surface.COMMAND, "include_metadata") in findings


def test_nested_filters_are_mapped_explicitly_to_nested_rest_properties_and_flat_mcp_and_cli_names() -> None:
    for name, cli in [
        ("file_type", "--file-type"),
        ("source_path_prefix", "--source-path-prefix"),
        ("source_path_glob", "--source-path-glob"),
        ("indexed_after", "--indexed-after"),
        ("indexed_before", "--indexed-before"),
        ("language", "--language"),
    ]:
        row = _row(REQUEST_FIELD_MAPPINGS, "command_field", f"filters.{name}")
        assert (row["rest_field"], row["mcp_param"], row["cli_option"]) == (f"filters.{name}", name, cli)


def test_the_two_include_metadata_flags_are_mapped_as_c9_says() -> None:
    """The top-level flag reaches REST, MCP and the CLI; ``filters.include_metadata`` reaches REST only (S41, S50)."""
    top = _row(REQUEST_FIELD_MAPPINGS, "command_field", "include_metadata")
    nested = _row(REQUEST_FIELD_MAPPINGS, "command_field", "filters.include_metadata")

    assert (top["rest_field"], top["mcp_param"], top["cli_option"]) == (
        "include_metadata",
        "include_metadata",
        "--include-metadata",
    )
    assert nested["rest_field"] == "filters.include_metadata"
    assert "mcp_param" not in nested and "cli_option" not in nested
    assert sorted(nested["waived_surfaces"]) == ["cli", "mcp"]
    assert "one include-metadata flag" in nested["waiver_reason"]


def test_the_one_cli_waiver_is_filters_include_metadata() -> None:
    """S50: the field-level check records exactly that one CLI waiver."""
    cli_waivers = [row["command_field"] for row in REQUEST_FIELD_MAPPINGS if "cli" in row.get("waived_surfaces", [])]

    assert cli_waivers == ["filters.include_metadata"]


def test_the_transport_exemptions_are_exactly_the_three_cli_options() -> None:
    assert TRANSPORT_EXEMPTIONS == {"cli_options": ["--json", "--api-url", "--api-key"]}
