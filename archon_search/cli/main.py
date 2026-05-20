from importlib.metadata import PackageNotFoundError, version as _pkg_version

import click

from archon_search.cli.collection import collection
from archon_search.cli.config_cmd import config
from archon_search.cli.ingest import ingest
from archon_search.cli.install_cmd import install, uninstall
from archon_search.cli.start import start
from archon_search.cli.status import status
from archon_search.cli.stop import stop
from archon_search.cli.sync import sync

try:
    _VERSION = _pkg_version("archon-search")
except PackageNotFoundError:
    _VERSION = "0.0.0+source"


@click.group()
@click.version_option(_VERSION, prog_name="archon-search")
def main() -> None:
    """archon-search — standalone search server for Archon."""


main.add_command(start)
main.add_command(stop)
main.add_command(status)
main.add_command(install)
main.add_command(uninstall)
main.add_command(ingest)
main.add_command(sync)
main.add_command(collection)
main.add_command(config)


if __name__ == "__main__":
    main()
