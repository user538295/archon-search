"""The field-level parity check (plan C9, brief Q3): the fields each interface carries, against an explicit mapping.

:func:`verify_request` compares the search command's fields with the REST request model, the MCP tool's input schema and
the Click command's parameters; :func:`verify_response` compares the service result's fields with the REST and MCP response
models (the CLI's ``--json`` prints the REST body, so the CLI has no response surface). Both return the
:class:`FieldParityFinding` list; an empty list means parity holds. The collectors (:func:`collect_request_fields`,
:func:`collect_response_fields`) read the live schemas. The contract is
``Documentation/Backlog/interface-parity/01_search-c9-field-parity.tsp``; the mapping data is ``field_registry.py``.

Field names. A field is a leaf of its model, named by its dotted path: the nested ``filters`` of the command and of REST's
request give ``filters.file_type``, a list of models adds ``[]`` (``results[].doc_id``, ``excluded_collections[].name``), MCP
and the CLI are flat (``file_type``, ``--file-type``). A CLI option is its longest-form flag (``--top-k``) and the positional
argument is its bare name (``query``). :data:`Surface.COMMAND` stands for the source side: the command for a request, the
service result for a response.

Mapping shape. A mapping is a plain dict (so a test can copy and mutate it), one per source field::

    {"command_field": "top_k", "rest_field": "top_k", "mcp_param": "top_k", "cli_option": "--top-k"}
    {"result_field": "include_metadata", "rest_field": ..., "mcp_field": ...,
     "waived_surfaces": ["rest", "mcp"], "waiver_reason": "why"}

``None`` or an absent name means the surface does not carry the field, which needs a waiver naming that surface. Any waiver key
makes the row a waiver; it is invalid, and covers nothing, without a surface list or without a non-blank reason, and one
surface it names is invalid when it is not a checked surface (``command``; ``cli`` on a response) or when it does carry the field.

Three failure kinds (:class:`FieldParityFailure`): a source field a surface does not carry and no waiver covers, or a
mapping row for a source field that no longer exists (``missing_on_surface``); a surface field no mapping names, or a source
field with no mapping row, bar the CLI transport options of the exemption set (``unmapped_surface_field``); an invalid waiver
(``invalid_waiver``). An unknown surface or key, a missing source key or a second row for one source field is a malformed
mapping and raises :class:`ValueError`, as does a model field with an alias or one that holds two models (its dotted name would
be wrong or ambiguous). Values are not compared: a name is either carried or not, and no contract failure kind names a value difference.
"""
from __future__ import annotations

import types
import typing
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from enum import StrEnum
from typing import Any
from unittest.mock import MagicMock

import click
from pydantic import BaseModel

from archon_search.cli.main import main as cli_main
from archon_search.key_manager import KeyStore
from archon_search.server.mcp_schemas import McpSearchResponse
from archon_search.server.routes_search import SearchRequest, SearchResponse
from archon_search.services.search_command import SearchCommand
from archon_search.services.search_service import SearchOutcome
from tests._mcp_tools import input_properties, registered_tools

_SEQUENCE_PATH_MARK = "[]"
_MCP_SEARCH_TOOL = "search"
_CLI_SEARCH_COMMAND = "search"


class Surface(StrEnum):
    COMMAND = "command"
    REST = "rest"
    MCP = "mcp"
    CLI = "cli"


class FieldParityFailure(StrEnum):
    MISSING_ON_SURFACE = "missing_on_surface"
    UNMAPPED_SURFACE_FIELD = "unmapped_surface_field"
    INVALID_WAIVER = "invalid_waiver"


@dataclass(frozen=True)
class FieldParityFinding:
    failure: FieldParityFailure
    surface: Surface
    field: str


LiveFields = Mapping[Surface, frozenset[str]]
"""The field names each surface carries now, keyed by surface (``COMMAND`` is the source side)."""

_REQUEST_SURFACES = (Surface.REST, Surface.MCP, Surface.CLI)
_RESPONSE_SURFACES = (Surface.REST, Surface.MCP)

_COMMON_KEYS = frozenset({"waived_surfaces", "waiver_reason"})
_REQUEST_KEYS = {
    "command_field": None,
    "rest_field": Surface.REST,
    "mcp_param": Surface.MCP,
    "cli_option": Surface.CLI,
}
_RESPONSE_KEYS = {"result_field": None, "rest_field": Surface.REST, "mcp_field": Surface.MCP}


@dataclass(frozen=True)
class _Mapping:
    source: str
    names: Mapping[Surface, str | None]
    has_waiver: bool
    waived_surfaces: tuple[Surface, ...]
    waiver_reason: str | None

    @property
    def waiver_reason_is_blank(self) -> bool:
        return not (self.waiver_reason or "").strip()


def _parsed_mappings(rows: Sequence[Mapping[str, Any]], keys: Mapping[str, Surface | None]) -> list[_Mapping]:
    source_key = next(key for key, surface in keys.items() if surface is None)
    parsed: list[_Mapping] = []
    for row in rows:
        unknown = set(row) - keys.keys() - _COMMON_KEYS
        if unknown:
            raise ValueError(f"unknown key {sorted(unknown)} in a field mapping")
        if source_key not in row:
            raise ValueError(f"missing key {source_key!r} in a field mapping")
        if any(mapping.source == row[source_key] for mapping in parsed):
            raise ValueError(f"duplicate mapping for {row[source_key]!r}")
        parsed.append(
            _Mapping(
                source=row[source_key],
                names={surface: row.get(key) for key, surface in keys.items() if surface is not None},
                has_waiver=not _COMMON_KEYS.isdisjoint(row),
                waived_surfaces=tuple(_parsed_surface(value) for value in row.get("waived_surfaces") or ()),
                waiver_reason=row.get("waiver_reason"),
            )
        )
    return parsed


def _parsed_surface(value: object) -> Surface:
    try:
        return Surface(value)
    except ValueError:
        raise ValueError(f"unknown surface {value!r} in a field mapping") from None


def _carries(mapping: _Mapping, surface: Surface, live: LiveFields) -> bool:
    name = mapping.names[surface]
    return name is not None and name in live[surface]


def _checked_waiver(
    mapping: _Mapping, live: LiveFields, checked: Sequence[Surface]
) -> tuple[list[FieldParityFinding], frozenset[Surface]]:
    """The invalid-waiver findings of one mapping and the surfaces its waiver validly covers."""
    if not mapping.has_waiver:
        return [], frozenset()
    findings: list[FieldParityFinding] = []
    if not mapping.waived_surfaces or mapping.waiver_reason_is_blank:
        findings.append(FieldParityFinding(FieldParityFailure.INVALID_WAIVER, Surface.COMMAND, mapping.source))
    covered = set()
    for surface in mapping.waived_surfaces:
        if surface not in checked or _carries(mapping, surface, live):
            findings.append(FieldParityFinding(FieldParityFailure.INVALID_WAIVER, surface, mapping.source))
        else:
            covered.add(surface)
    unique = list(dict.fromkeys(findings))
    return unique, frozenset() if mapping.waiver_reason_is_blank else frozenset(covered)


def _verify(
    mappings: Sequence[_Mapping],
    live: LiveFields,
    checked: Sequence[Surface],
    exempt: Mapping[Surface, Collection[str]],
) -> list[FieldParityFinding]:
    findings = [
        FieldParityFinding(FieldParityFailure.UNMAPPED_SURFACE_FIELD, Surface.COMMAND, name)
        for name in sorted(live[Surface.COMMAND] - {mapping.source for mapping in mappings})
    ]
    for mapping in mappings:
        if mapping.source not in live[Surface.COMMAND]:
            findings.append(FieldParityFinding(FieldParityFailure.MISSING_ON_SURFACE, Surface.COMMAND, mapping.source))
        waiver_findings, covered = _checked_waiver(mapping, live, checked)
        findings += waiver_findings
        findings += [
            FieldParityFinding(FieldParityFailure.MISSING_ON_SURFACE, surface, mapping.source)
            for surface in checked
            if surface not in covered and not _carries(mapping, surface, live)
        ]
    for surface in checked:
        mapped = {mapping.names[surface] for mapping in mappings} - {None}
        findings += [
            FieldParityFinding(FieldParityFailure.UNMAPPED_SURFACE_FIELD, surface, name)
            for name in sorted(live[surface] - mapped - set(exempt.get(surface, ())))
        ]
    return findings


def verify_request(
    mappings: Sequence[Mapping[str, Any]], live: LiveFields, exemptions: Mapping[str, Collection[str]]
) -> list[FieldParityFinding]:
    """Compare the command-field mappings with the live command, REST, MCP and CLI fields.

    *exemptions* lists the CLI ``cli_options`` that carry no command field (the transport options); they are the only
    surface fields allowed to map to nothing.
    """
    exempt = {Surface.CLI: frozenset(exemptions.get("cli_options", ()))}
    return _verify(_parsed_mappings(mappings, _REQUEST_KEYS), live, _REQUEST_SURFACES, exempt)


def verify_response(mappings: Sequence[Mapping[str, Any]], live: LiveFields) -> list[FieldParityFinding]:
    """Compare the result-field mappings with the live service result, REST response and MCP response fields."""
    return _verify(_parsed_mappings(mappings, _RESPONSE_KEYS), live, _RESPONSE_SURFACES, {})


# ---------------------------------------------------------------------------
# Collectors: the live schemas
# ---------------------------------------------------------------------------


def _fields_of(model: type) -> dict[str, Any]:
    """Field name to annotation of a Pydantic model or a dataclass; a field with an alias is refused (its wire name differs)."""
    if issubclass(model, BaseModel):
        for name, field in model.model_fields.items():
            if field.alias or field.validation_alias or field.serialization_alias:
                raise ValueError(f"{model.__name__}.{name} has an alias, so its attribute name is not its wire name")
        return {name: field.annotation for name, field in model.model_fields.items()}
    hints = typing.get_type_hints(model)
    return {field.name: hints[field.name] for field in fields(model)}


def _nested_model(annotation: object) -> tuple[type | None, bool]:
    """The model or dataclass an annotation holds, directly or inside an optional or a sequence, and whether it is a sequence."""
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType, list, tuple):
        is_sequence = origin in (list, tuple)
        found = []
        for argument in typing.get_args(annotation):
            if argument is Ellipsis or argument is type(None):
                continue
            model, nested_sequence = _nested_model(argument)
            if model is not None:
                found.append((model, is_sequence or nested_sequence))
        if len(found) > 1:
            raise ValueError(f"{annotation!r} holds more than one model, so its leaf names are ambiguous")
        return found[0] if found else (None, False)
    if isinstance(annotation, type) and (is_dataclass(annotation) or issubclass(annotation, BaseModel)):
        return annotation, False
    return None, False


def leaf_names(model: type, prefix: str = "") -> frozenset[str]:
    """The dotted leaf field names of a Pydantic model or dataclass (a list of models adds ``[]``)."""
    names: set[str] = set()
    for name, annotation in _fields_of(model).items():
        nested, is_sequence = _nested_model(annotation)
        if nested is None:
            names.add(f"{prefix}{name}")
        else:
            mark = _SEQUENCE_PATH_MARK if is_sequence else ""
            names |= leaf_names(nested, f"{prefix}{name}{mark}.") or {f"{prefix}{name}"}
    return frozenset(names)


def _mcp_search_params() -> frozenset[str]:
    """The input properties the real FastMCP advertises for the ``search`` tool."""
    tool = registered_tools(MagicMock(spec=KeyStore))[_MCP_SEARCH_TOOL]
    return frozenset(input_properties(tool))


def _cli_search_fields() -> frozenset[str]:
    """The ``search`` command's options by their longest ``--`` flag, and its arguments by name."""
    command = cli_main.get_command(click.Context(cli_main, info_name=cli_main.name), _CLI_SEARCH_COMMAND)
    if command is None:
        raise LookupError(f"the CLI has no {_CLI_SEARCH_COMMAND!r} command")
    names = set()
    for parameter in command.params:
        if isinstance(parameter, click.Option):
            names.add(max((flag for flag in parameter.opts if flag.startswith("--")), key=len, default=parameter.opts[0]))
        else:
            names.add(parameter.name)
    return frozenset(names)


def collect_request_fields() -> LiveFields:
    """The live request fields: the command, REST's request model, the MCP tool's inputs and the Click parameters."""
    return {
        Surface.COMMAND: leaf_names(SearchCommand),
        Surface.REST: leaf_names(SearchRequest),
        Surface.MCP: _mcp_search_params(),
        Surface.CLI: _cli_search_fields(),
    }


def collect_response_fields() -> LiveFields:
    """The live response fields: the service result, REST's ``SearchResponse`` and the MCP ``search`` response model."""
    return {
        Surface.COMMAND: leaf_names(SearchOutcome),
        Surface.REST: leaf_names(SearchResponse),
        Surface.MCP: leaf_names(McpSearchResponse),
    }
