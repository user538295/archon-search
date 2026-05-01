import click


@click.group()
def main() -> None:
    """archon-search — standalone search server for Archon."""


if __name__ == "__main__":
    main()
