import click

from archon_search.cli.start import start
from archon_search.cli.status import status
from archon_search.cli.stop import stop


@click.group()
def main() -> None:
    """archon-search — standalone search server for Archon."""


@main.command()
def install() -> None:
    """Install archon-search service."""


@main.command()
def uninstall() -> None:
    """Uninstall archon-search service."""


main.add_command(start)
main.add_command(stop)
main.add_command(status)


@main.command()
def ingest() -> None:
    """Ingest documents into a collection."""


@main.command()
def sync() -> None:
    """Sync collections."""


@main.group()
def collection() -> None:
    """Manage collections."""


@main.group()
def config() -> None:
    """Show or edit configuration."""


if __name__ == "__main__":
    main()
