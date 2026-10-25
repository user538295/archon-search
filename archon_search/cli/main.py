from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

import click

from archon_search.cli.backup_cmd import backup_cmd
from archon_search.cli.collection import collection
from archon_search.cli.config_cmd import config
from archon_search.cli.export_cmd import export_cmd, import_cmd
from archon_search.cli.ingest import ingest
from archon_search.cli.install_cmd import install, uninstall, wizard
from archon_search.cli.jobs_cmd import jobs
from archon_search.cli.key_cmd import key_cmd
from archon_search.cli.maintenance_cmd import maintenance_cmd
from archon_search.cli.serve import serve
from archon_search.cli.start import start
from archon_search.cli.status import status
from archon_search.cli.stop import stop
from archon_search.cli.sync import sync
from archon_search.config import ConfigError

try:
    _VERSION = _pkg_version("archon-search")
except PackageNotFoundError:
    _VERSION = "0.0.0+source"


_GRAPH_CMD_NAME = "graph"


class _LazyGraphGroup(click.Group):
    """click.Group that defers import of ``graph_cmd`` and ``httpx``.

    The import is deferred for direct subcommand invocations (e.g.
    ``archon-search config show``).  It does NOT prevent the import during
    ``--help`` rendering: Click's ``format_commands`` calls ``get_command``
    for every listed command, so the graph module is loaded then.
    ``_helpers`` and ``collection`` are already eagerly loaded by ``main.py``
    and are therefore not deferred.
    """

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        if cmd_name == _GRAPH_CMD_NAME:
            from archon_search.cli.graph_cmd import graph_cmd
            return graph_cmd
        return super().get_command(ctx, cmd_name)

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted([*super().list_commands(ctx), _GRAPH_CMD_NAME])

    def invoke(self, ctx: click.Context) -> object:
        """Print a ``ConfigError`` (e.g. a malformed ``ARCHON_SEARCH_API_KEY``) as one line and exit 1.

        Every client command resolves the key lazily, so the failure surfaces from deep inside a
        command body; handling it here keeps it a readable operator message, never a traceback.
        """
        try:
            return super().invoke(ctx)
        except ConfigError as exc:
            click.echo(f"Error: {exc}", err=True)
            raise SystemExit(1) from exc


@click.group(cls=_LazyGraphGroup)
@click.version_option(_VERSION, prog_name="archon-search")
def main() -> None:
    """archon-search — standalone RAG search server."""


main.add_command(start)
main.add_command(serve)
main.add_command(stop)
main.add_command(status)
main.add_command(wizard)
main.add_command(install)
main.add_command(uninstall)
main.add_command(ingest)
main.add_command(sync)
main.add_command(collection)
main.add_command(config)
main.add_command(export_cmd)
main.add_command(import_cmd)
main.add_command(backup_cmd)
main.add_command(maintenance_cmd)
main.add_command(key_cmd)
main.add_command(jobs)


if __name__ == "__main__":
    main()
