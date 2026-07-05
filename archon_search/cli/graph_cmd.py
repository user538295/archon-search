"""``archon-search graph`` CLI group (E1b BE-4).

Provides:

* ``archon-search graph build-communities <collection>``
  Run Leiden community detection on the entity graph for *collection*, select
  MMR representative chunks, and persist the result to the
  ``_archon_graph_{col}_communities`` LanceDB table.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import click

from archon_search.config import ConfigError, load_config
from archon_search.community_builder import CommunityBuilder
from archon_search.graph_store import GraphStore
from archon_search.store import SearchStore


@click.group("graph")
def graph_cmd() -> None:
    """Graph management commands (E1b)."""


@graph_cmd.command("build-communities")
@click.argument("collection")
@click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(path_type=Path),
    help="Path to archon-search.toml (defaults to ~/.archon-search/archon-search.toml).",
)
def build_communities_cmd(collection: str, config_path: Path | None) -> None:
    """Build Leiden communities for COLLECTION.

    Loads the entity graph from the ``_archon_graph_{col}_nodes`` /
    ``_archon_graph_{col}_edges`` tables (created by ``ingest`` with
    ``graph.enabled=true``), runs Leiden clustering, selects MMR representative
    chunks per community, and writes results to
    ``_archon_graph_{col}_communities``.

    Exit codes:
    - 0 on success
    - 1 on any error (missing graph data, leidenalg absent, config/IO failure)
    """
    try:
        cfg = load_config(config_path)
    except (ConfigError, OSError, ValueError) as exc:
        click.echo(f"Error loading config: {exc}", err=True)
        raise SystemExit(1) from exc

    if not cfg.graph.enabled:
        click.echo(
            "graph.enabled is false in config. Set graph.enabled = true in "
            "archon-search.toml and re-run ingest before building communities.",
            err=True,
        )
        raise SystemExit(1)

    async def _run() -> None:
        graph_store = GraphStore(cfg.db_path)
        search_store = SearchStore(cfg.db_path, config=cfg)

        try:
            await graph_store.connect()
            await search_store.connect()
            builder = CommunityBuilder(graph_store, cfg.graph, search_store=search_store)
            from archon_search.constants import DEFAULT_NAMESPACE  # noqa: PLC0415
            # Note: the graph CLI commands operate on the default namespace only.
            # Multi-namespace deployments have no REST or CLI path to build communities
            # for non-default namespaces — this is a known E2d limitation.
            communities = await builder.build(collection, ns=DEFAULT_NAMESPACE)
            click.echo(
                f"Built {len(communities)} communities for collection {collection!r}."
            )
        except (ValueError, ImportError, RuntimeError, OSError) as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(1) from exc
        finally:
            await graph_store.disconnect()
            await search_store.disconnect()

    asyncio.run(_run())
