import click


@click.group()
def main() -> None:
    """archon-search — standalone search server for Archon."""


@main.command()
def install() -> None:
    """Install archon-search service."""


@main.command()
def uninstall() -> None:
    """Uninstall archon-search service."""


@main.command()
def start() -> None:
    """Start the archon-search service."""


@main.command()
def stop() -> None:
    """Stop the archon-search service."""


@main.command()
def status() -> None:
    """Show archon-search service status."""


@main.command()
def ingest() -> None:
    """Ingest documents into a collection."""


@main.command()
def sync() -> None:
    """Sync collections."""


@main.command()
def collection() -> None:
    """Manage collections."""


@main.command()
def config() -> None:
    """Show or edit configuration."""


if __name__ == "__main__":
    main()
