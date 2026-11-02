"""Interface parity check (brief C12, plan C4): what the registry claims against what the live surfaces expose.

:func:`verify` compares a registry with a :class:`LiveSurfaces` and returns the :class:`ParityFinding` list; the
collectors (:func:`rest_identifiers`, :func:`mcp_tool_names`, :func:`cli_identifiers`, :func:`collect_live_surfaces`)
read the three live surfaces. The contract is
``Documentation/Backlog/interface-parity/00_service_layer_foundation-c4-parity-registry.tsp``. The unit tests here run
against synthetic registries; the real registry is the data module ``registry.py``, checked against the live surfaces at the end.

Registry shape. A registry is a plain list of plain dicts, with no imports and no behaviour, so it can be copied,
serialised and mutated (the e2e guard test deletes a row from a copy)::

    REGISTRY = [
        {
            "name": "Search",                         # the capability
            "service_method": "SearchService.search",  # or None while no service method exists
            "claims": [                               # one claim per interface, or more
                {"surface": "rest", "identifier": "POST /search", "waiver": None},
                {"surface": "mcp", "identifier": "search", "waiver": None},
                {
                    "surface": "cli",
                    "identifier": None,               # a gap: the capability has no surface here
                    "waiver": {"kind": "pending", "reason": "why", "pending_slice": "S2"},
                },
            ],
        },
    ]

* ``surface`` is ``"rest"``, ``"mcp"`` or ``"cli"``.
* ``identifier`` is a live surface, or ``None`` for a gap. REST: ``METHOD path`` for each method of a route, with
  ``HEAD`` dropped (Starlette adds it to every plain ``Route`` that serves ``GET``, such as ``/openapi.json`` and the docs
  routes); a ``Mount``, which has no methods, is its path alone (``/mcp``). MCP: the tool name. CLI: the space-joined
  command path (``collection list``, ``backup status``); a Click group is a surface only when it is invokable without a
  subcommand (today only ``backup``), every other group is a namespace.
* ``waiver`` is ``None`` or ``{"kind": "permanent" | "pending", "reason": str, "pending_slice": str | None}``.
  A claim may carry both an identifier and a waiver (a live surface the roadmap waives, such as ``GET /health``).
  ``reason`` is required. ``pending_slice`` names the roadmap slice that will close the gap, for example ``"S2"``, and is
  required for a ``pending`` waiver.

Four failure kinds (:class:`ParityFailure`): a live surface no claim names (``unclaimedSurface``); a claimed identifier
that is not live (``claimedSurfaceMissing``), except a lifespan-only REST surface; a claim with neither identifier nor
waiver (``gapWithoutWaiver``); a ``pending`` waiver that names no slice (``pendingWaiverWithoutSlice``). There is no
stale-waiver rule, and the same identifier may be claimed more than once. An unknown ``surface`` or waiver ``kind``, or a
waiver without a ``reason``, is a malformed registry and raises :class:`ValueError`.

Live surfaces. REST comes from the app built with docs and the OpenAI shim enabled, without running its lifespan, so
``/docs``, ``/redoc``, ``/docs/oauth2-redirect`` and ``/v1/*`` are live and ``/mcp`` (mounted inside the lifespan) is the
one lifespan-only surface, exempt from the missing check. MCP comes from the real FastMCP built with a key store
stand-in, so the four key-management tools are included. The Click tree is walked through ``list_commands`` and
``get_command``, which resolves the lazily registered ``graph`` group.

Nothing here needs the repo conftest: :func:`collect_live_surfaces` sets its own data dir and key, and the only helper
imported is ``tests/_mcp_tools.py``.
"""
from __future__ import annotations

import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import click
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import iter_route_contexts
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from archon_search.cli.main import main as cli_main
from archon_search.config import SearchConfig
from archon_search.constants import MIN_API_KEY_LENGTH
from archon_search.jobs.store import JobStore
from archon_search.key_manager import KeyStore
from archon_search.server.app import create_app
from tests._mcp_tools import registered_tools
from tests.parity.registry import REGISTRY

_HEAD = "HEAD"
# Mounted inside the app lifespan (``app.py``, ``create_mcp_http_app``), which the parity test does not run.
_LIFESPAN_ONLY_REST_SURFACES = frozenset({"/mcp"})
_FAKE_API_KEY = "0" * MIN_API_KEY_LENGTH  # lowercase hex of the shortest length the server accepts

Registry = Sequence[Mapping[str, Any]]


class Surface(StrEnum):
    REST = "rest"
    MCP = "mcp"
    CLI = "cli"


class WaiverKind(StrEnum):
    PERMANENT = "permanent"
    PENDING = "pending"


class ParityFailure(StrEnum):
    UNCLAIMED_SURFACE = "unclaimedSurface"
    CLAIMED_SURFACE_MISSING = "claimedSurfaceMissing"
    GAP_WITHOUT_WAIVER = "gapWithoutWaiver"
    PENDING_WAIVER_WITHOUT_SLICE = "pendingWaiverWithoutSlice"


@dataclass(frozen=True)
class ParityFinding:
    """One failure: ``capability`` is ``None`` for an unclaimed surface; ``identifier`` is ``None`` for a gap-level one."""

    failure: ParityFailure
    capability: str | None
    surface: Surface
    identifier: str | None


@dataclass(frozen=True)
class LiveSurfaces:
    rest_routes: frozenset[str]
    lifespan_only_rest_surfaces: frozenset[str]
    mcp_tools: frozenset[str]
    cli_commands: frozenset[str]

    def identifiers(self, surface: Surface) -> frozenset[str]:
        return {
            Surface.REST: self.rest_routes,
            Surface.MCP: self.mcp_tools,
            Surface.CLI: self.cli_commands,
        }[surface]


def _parsed[E: StrEnum](enum_type: type[E], value: object, what: str) -> E:
    try:
        return enum_type(value)
    except ValueError:
        raise ValueError(f"unknown {what} {value!r} in a registry claim") from None


def _waiver_kind(waiver: Mapping[str, Any]) -> WaiverKind:
    kind = _parsed(WaiverKind, waiver["kind"], "waiver kind")
    if not (waiver.get("reason") or "").strip():
        raise ValueError("a registry waiver needs a reason")
    return kind


def _names_a_slice(waiver: Mapping[str, Any]) -> bool:
    return bool((waiver.get("pending_slice") or "").strip())


def verify(registry: Registry, live: LiveSurfaces) -> list[ParityFinding]:
    """Compare the registry's claims with the live surfaces; an empty list means parity holds."""
    findings: list[ParityFinding] = []
    claimed: dict[Surface, set[str]] = {surface: set() for surface in Surface}
    for capability in registry:
        name = capability["name"]
        for claim in capability["claims"]:
            surface = _parsed(Surface, claim["surface"], "surface")
            identifier = claim["identifier"]
            waiver = claim["waiver"]
            if waiver is not None and _waiver_kind(waiver) is WaiverKind.PENDING and not _names_a_slice(waiver):
                findings.append(ParityFinding(ParityFailure.PENDING_WAIVER_WITHOUT_SLICE, name, surface, identifier))
            if identifier is None:
                if waiver is None:
                    findings.append(ParityFinding(ParityFailure.GAP_WITHOUT_WAIVER, name, surface, None))
                continue
            claimed[surface].add(identifier)
            lifespan_only = surface is Surface.REST and identifier in live.lifespan_only_rest_surfaces
            if identifier not in live.identifiers(surface) and not lifespan_only:
                findings.append(ParityFinding(ParityFailure.CLAIMED_SURFACE_MISSING, name, surface, identifier))
    for surface in Surface:
        findings.extend(
            ParityFinding(ParityFailure.UNCLAIMED_SURFACE, None, surface, identifier)
            for identifier in sorted(live.identifiers(surface) - claimed[surface])
        )
    return findings


def rest_identifiers(app: FastAPI) -> frozenset[str]:
    """``METHOD path`` per method of every route (``HEAD`` dropped); a route with no methods, a ``Mount``, is its path."""
    identifiers: set[str] = set()
    for route in iter_route_contexts(app.routes):
        if route.methods is None:
            identifiers.add(route.path)
        else:
            identifiers.update(f"{method} {route.path}" for method in route.methods if method != _HEAD)
    return frozenset(identifiers)


def mcp_tool_names() -> frozenset[str]:
    """The tools the real FastMCP advertises, built with a key store stand-in so the key-management tools exist."""
    return frozenset(registered_tools(MagicMock(spec=KeyStore)))


def cli_identifiers(root: click.Group) -> frozenset[str]:
    """Space-joined command paths; a group is listed only when invokable without a subcommand."""
    identifiers: set[str] = set()

    def walk(command: click.Command, ctx: click.Context, path: str) -> None:
        if not isinstance(command, click.Group):
            identifiers.add(path)
            return
        if command.invoke_without_command and path:
            identifiers.add(path)
        for name in command.list_commands(ctx):
            child = command.get_command(ctx, name)
            if child is None:
                raise LookupError(f"{path or 'root'} lists {name!r} but get_command does not resolve it")
            walk(child, click.Context(child, parent=ctx, info_name=name), f"{path} {name}".strip())

    walk(root, click.Context(root, info_name=root.name), "")
    return frozenset(identifiers)


def collect_live_surfaces() -> LiveSurfaces:
    """Build the three live surfaces; the REST app is built with docs and the OpenAI shim on and never started.

    Isolated from the operator's data dir and key (``create_app`` persists the key it is given), so it needs no conftest.
    """
    with pytest.MonkeyPatch.context() as monkeypatch, tempfile.TemporaryDirectory() as workdir:
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", workdir)
        monkeypatch.setenv("ARCHON_SEARCH_API_KEY", _FAKE_API_KEY)
        monkeypatch.delenv("ARCHON_SEARCH_KEY_FILE", raising=False)
        config = SearchConfig()
        config.db_path = str(Path(workdir) / "search")
        config.docs_enabled = True
        config.openai_shim.enabled = True
        app = create_app(config, JobStore(path=Path(workdir) / "jobs.json"))
        return LiveSurfaces(
            rest_routes=rest_identifiers(app),
            lifespan_only_rest_surfaces=_LIFESPAN_ONLY_REST_SURFACES,
            mcp_tools=mcp_tool_names(),
            cli_commands=cli_identifiers(cli_main),
        )


# ---------------------------------------------------------------------------
# Synthetic registries and live surfaces (the failure kinds are tested without the real registry)
# ---------------------------------------------------------------------------


def _live(
    rest: Sequence[str] = (),
    mcp: Sequence[str] = (),
    cli: Sequence[str] = (),
    lifespan_only: Sequence[str] = (),
) -> LiveSurfaces:
    return LiveSurfaces(
        rest_routes=frozenset(rest),
        lifespan_only_rest_surfaces=frozenset(lifespan_only),
        mcp_tools=frozenset(mcp),
        cli_commands=frozenset(cli),
    )


def _claim(surface: str, identifier: str | None = None, waiver: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"surface": surface, "identifier": identifier, "waiver": waiver}


def _waiver(kind: str, reason: str = "agreed", pending_slice: str | None = None) -> dict[str, Any]:
    return {"kind": kind, "reason": reason, "pending_slice": pending_slice}


def _capability(name: str, *claims: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "service_method": None, "claims": list(claims)}


_SEARCH = _capability(
    "Search",
    _claim("rest", "POST /search"),
    _claim("mcp", "search"),
    _claim("cli", "status"),
)
_SEARCH_LIVE = {"rest": ["POST /search"], "mcp": ["search"], "cli": ["status"]}


def test_fully_claimed_surfaces_pass() -> None:
    """Positive control: a verifier that always fails could not satisfy the failure tests below."""
    assert verify([_SEARCH], _live(**_SEARCH_LIVE)) == []


@pytest.mark.parametrize(
    ("surface", "identifier"),
    [("rest", "GET /extra"), ("mcp", "extra_tool"), ("cli", "extra command")],
)
def test_unclaimed_surface_fails(surface: str, identifier: str) -> None:
    live = {key: [*values, identifier] if key == surface else values for key, values in _SEARCH_LIVE.items()}

    findings = verify([_SEARCH], _live(**live))

    assert findings == [ParityFinding(ParityFailure.UNCLAIMED_SURFACE, None, Surface(surface), identifier)]


def test_a_claim_on_one_surface_does_not_cover_the_same_name_on_another() -> None:
    live = _live(rest=["POST /search"], mcp=["search"], cli=["status", "search"])  # a CLI ``search`` next to the MCP tool

    assert verify([_SEARCH], live) == [ParityFinding(ParityFailure.UNCLAIMED_SURFACE, None, Surface.CLI, "search")]


def test_unclaimed_surface_fails_with_an_empty_registry() -> None:
    findings = verify([], _live(**_SEARCH_LIVE))

    assert {(f.failure, f.surface, f.identifier) for f in findings} == {
        (ParityFailure.UNCLAIMED_SURFACE, Surface.REST, "POST /search"),
        (ParityFailure.UNCLAIMED_SURFACE, Surface.MCP, "search"),
        (ParityFailure.UNCLAIMED_SURFACE, Surface.CLI, "status"),
    }


def test_a_waived_live_surface_counts_as_claimed() -> None:
    registry = [_capability("Health", _claim("rest", "GET /health", _waiver("permanent", "probe")))]

    assert verify(registry, _live(rest=["GET /health"])) == []


@pytest.mark.parametrize(
    ("surface", "identifier"),
    [("rest", "GET /gone"), ("mcp", "gone_tool"), ("cli", "gone command")],
)
def test_claimed_surface_that_does_not_exist_fails(surface: str, identifier: str) -> None:
    registry = [_SEARCH, _capability("Gone", _claim(surface, identifier))]

    findings = verify(registry, _live(**_SEARCH_LIVE))

    assert findings == [ParityFinding(ParityFailure.CLAIMED_SURFACE_MISSING, "Gone", Surface(surface), identifier)]


def test_claimed_surface_that_does_not_exist_fails_even_when_waived() -> None:
    registry = [_SEARCH, _capability("Waived", _claim("rest", "GET /gone", _waiver("permanent")))]

    assert verify(registry, _live(**_SEARCH_LIVE)) == [
        ParityFinding(ParityFailure.CLAIMED_SURFACE_MISSING, "Waived", Surface.REST, "GET /gone")
    ]


def test_claimed_surface_that_does_not_exist_fails_except_the_lifespan_only_rest_mount() -> None:
    mount = _capability("MCP transport", _claim("rest", "/mcp", _waiver("permanent", "transport endpoint")))
    live = _live(**_SEARCH_LIVE, lifespan_only=["/mcp"])

    assert verify([_SEARCH, mount], live) == []

    # The exemption is what the live surfaces declare: without it the same claim fails.
    assert verify([_SEARCH, mount], _live(**_SEARCH_LIVE)) == [
        ParityFinding(ParityFailure.CLAIMED_SURFACE_MISSING, "MCP transport", Surface.REST, "/mcp")
    ]

    # It names that one surface: another missing REST claim still fails, and so does the same
    # identifier claimed on a surface other than REST.
    other = _capability("Other", _claim("rest", "GET /other"), _claim("mcp", "/mcp"))
    assert {(f.surface, f.identifier) for f in verify([_SEARCH, mount, other], live)} == {
        (Surface.REST, "GET /other"),
        (Surface.MCP, "/mcp"),
    }


@pytest.mark.parametrize("surface", ["rest", "mcp", "cli"])
def test_gap_without_waiver_and_pending_waiver_without_slice_fail(surface: str) -> None:
    gap = _capability("Gap", _claim(surface))
    assert verify([_SEARCH, gap], _live(**_SEARCH_LIVE)) == [
        ParityFinding(ParityFailure.GAP_WITHOUT_WAIVER, "Gap", Surface(surface), None)
    ]

    pending = _capability("Pending", _claim(surface, None, _waiver("pending")))
    assert verify([_SEARCH, pending], _live(**_SEARCH_LIVE)) == [
        ParityFinding(ParityFailure.PENDING_WAIVER_WITHOUT_SLICE, "Pending", Surface(surface), None)
    ]


@pytest.mark.parametrize("waiver", [_waiver("pending", pending_slice=""), _waiver("pending", pending_slice="   ")])
def test_pending_waiver_with_a_blank_slice_fails(waiver: dict[str, Any]) -> None:
    pending = _capability("Pending", _claim("cli", None, waiver))

    assert verify([_SEARCH, pending], _live(**_SEARCH_LIVE)) == [
        ParityFinding(ParityFailure.PENDING_WAIVER_WITHOUT_SLICE, "Pending", Surface.CLI, None)
    ]


def test_pending_waiver_without_a_slice_key_fails() -> None:
    waiver = {"kind": "pending", "reason": "agreed"}

    assert verify([_capability("Pending", _claim("cli", None, waiver))], _live()) == [
        ParityFinding(ParityFailure.PENDING_WAIVER_WITHOUT_SLICE, "Pending", Surface.CLI, None)
    ]


def test_pending_waiver_without_slice_on_a_live_surface_keeps_the_identifier() -> None:
    registry = [_capability("Search", _claim("rest", "POST /search", _waiver("pending")))]

    assert verify(registry, _live(rest=["POST /search"])) == [
        ParityFinding(ParityFailure.PENDING_WAIVER_WITHOUT_SLICE, "Search", Surface.REST, "POST /search")
    ]


def test_waived_gaps_pass() -> None:
    """Positive control for the gap checks: a permanent waiver and a pending waiver naming a slice are accepted."""
    registry = [
        _SEARCH,
        _capability(
            "Describe",
            _claim("mcp", None, _waiver("permanent", "agents get no rewrite tool")),
            _claim("cli", None, _waiver("pending", "lands in S2", "S2")),
        ),
    ]

    assert verify(registry, _live(**_SEARCH_LIVE)) == []


@pytest.mark.parametrize(
    ("claim", "message"),
    [
        (_claim("grpc", "x"), "unknown surface 'grpc'"),
        (_claim("rest", None, _waiver("temporary", pending_slice="S2")), "unknown waiver kind 'temporary'"),
        (_claim("rest", None, _waiver("permanent", reason="")), "needs a reason"),
        (_claim("rest", None, _waiver("permanent", reason="   ")), "needs a reason"),
        (_claim("rest", None, {"kind": "permanent", "pending_slice": None}), "needs a reason"),
    ],
)
def test_malformed_registry_is_an_error_not_a_finding(claim: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        verify([_capability("Bad", claim)], _live())


# ---------------------------------------------------------------------------
# Collectors, on synthetic apps and trees
# ---------------------------------------------------------------------------


async def _plain(request: Request) -> PlainTextResponse:
    return PlainTextResponse("")


def test_rest_identifiers_are_method_and_path_without_head_and_a_mount_is_its_path() -> None:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    router = APIRouter()

    @router.get("/items")
    async def list_items() -> list[str]:
        return []

    @router.post("/items")
    async def add_item() -> None:
        return None

    app.include_router(router, prefix="/v2")
    plain = Route("/plain", _plain, methods=["GET"])
    app.router.routes.append(plain)
    app.mount("/mounted", Starlette())

    assert "HEAD" in (plain.methods or set()), "premise: Starlette adds HEAD to a plain GET route"
    assert rest_identifiers(app) == frozenset({"GET /v2/items", "POST /v2/items", "GET /plain", "/mounted"})


class _LazyGroup(click.Group):
    """Resolves one command only through ``get_command``, as ``main._LazyGraphGroup`` does for ``graph``."""

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        if cmd_name == "lazy":
            return _group_with_commands("lazy", "inner")
        return super().get_command(ctx, cmd_name)

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted([*super().list_commands(ctx), "lazy"])


def _group_with_commands(name: str, *children: str, invoke_without_command: bool = False) -> click.Group:
    group = click.Group(name, invoke_without_command=invoke_without_command)
    for child in children:
        group.add_command(click.Command(child))
    return group


def test_cli_identifiers_walk_nested_lazy_and_invokable_groups() -> None:
    root = _LazyGroup("root")
    root.add_command(click.Command("top"))
    root.add_command(_group_with_commands("nested", "one", "two"))  # a namespace: only its commands are surfaces
    root.add_command(_group_with_commands("invokable", "child", invoke_without_command=True))  # a surface and a namespace
    deep = click.Group("outer")
    deep.add_command(_group_with_commands("inner", "leaf"))
    root.add_command(deep)

    assert cli_identifiers(root) == frozenset(
        {"top", "nested one", "nested two", "invokable", "invokable child", "outer inner leaf", "lazy inner"}
    )


def test_cli_identifiers_never_list_the_root_group_itself() -> None:
    root = _group_with_commands("root", "top", invoke_without_command=True)

    assert cli_identifiers(root) == frozenset({"top"})


def test_cli_identifiers_fail_on_a_listed_command_that_does_not_resolve() -> None:
    class _Broken(click.Group):
        def list_commands(self, ctx: click.Context) -> list[str]:
            return ["ghost"]

    with pytest.raises(LookupError, match="ghost"):
        cli_identifiers(_Broken("root"))


# ---------------------------------------------------------------------------
# Collectors, on the live surfaces
# ---------------------------------------------------------------------------


_LIVE_GROUP = pytest.mark.xdist_group("parity-live")  # one worker builds the live surfaces once (``--dist=loadgroup``)


@pytest.fixture(scope="module")
def live_surfaces() -> LiveSurfaces:
    return collect_live_surfaces()


@_LIVE_GROUP
def test_live_rest_routes_include_docs_shim_and_health_without_head_or_the_lifespan_mount(
    live_surfaces: LiveSurfaces,
) -> None:
    routes = live_surfaces.rest_routes

    assert {
        "GET /openapi.json",
        "GET /docs",
        "GET /redoc",
        "GET /docs/oauth2-redirect",
        "POST /v1/chat/completions",
        "GET /v1/models",
        "GET /health",
        "POST /search",
    } <= routes
    assert not [route for route in routes if route.startswith("HEAD ")]
    assert "/mcp" not in routes, "the MCP transport is mounted inside the lifespan, which this test does not run"


@_LIVE_GROUP
def test_live_mcp_tools_include_the_key_management_tools(live_surfaces: LiveSurfaces) -> None:
    assert {"search", "list_keys", "create_key", "revoke_key", "rotate_key"} <= live_surfaces.mcp_tools


@_LIVE_GROUP
def test_live_cli_commands_resolve_lazy_nested_and_invokable_groups(live_surfaces: LiveSurfaces) -> None:
    commands = live_surfaces.cli_commands

    assert {"graph build-communities", "collection list", "key create", "backup", "backup status", "status"} <= commands
    # Namespaces are not surfaces: only a group invokable without a subcommand counts, and today only ``backup`` is.
    assert not {"collection", "graph", "jobs", "key", "config", "maintenance"} & commands


@_LIVE_GROUP
def test_parity_passes_on_live_surfaces(live_surfaces: LiveSurfaces) -> None:
    """The real registry claims or waives every live surface (S11, S15) and claims no surface that is not live, bar ``/mcp``."""
    assert verify(REGISTRY, live_surfaces) == []


_SLICE_ROW = re.compile(r"S(\d+) ")


def test_registry_has_one_row_per_roadmap_slice_and_pending_waivers_name_their_own_slice() -> None:
    """S16: rows S1 to S32 are all present, and a pending waiver in a slice row names that slice."""
    slice_rows = {match.group(0).strip(): row for row in REGISTRY if (match := _SLICE_ROW.match(row["name"]))}

    assert list(slice_rows) == [f"S{number}" for number in range(1, 33)]
    for slice_id, row in slice_rows.items():
        pending = {
            claim["waiver"]["pending_slice"] for claim in row["claims"] if (claim["waiver"] or {}).get("kind") == "pending"
        }
        assert pending <= {slice_id}, f"{row['name']} has a pending waiver naming {pending - {slice_id}}"


def test_registry_has_the_descriptions_row_with_the_permanent_mcp_describe_waiver() -> None:
    """D9: the missing MCP describe tool is a permanent waiver that cites descriptions plan C8 and ADR 19."""
    row = next(row for row in REGISTRY if row["name"].startswith("Descriptions"))
    waiver = next(claim["waiver"] for claim in row["claims"] if claim["surface"] == "mcp" and claim["identifier"] is None)

    assert waiver["kind"] == "permanent"
    assert "C8" in waiver["reason"]
    assert "ADR 19" in waiver["reason"]
