"""Example ingestion script: bulk-ingest a directory into archon-search."""
import asyncio
from pathlib import Path
import httpx


SEARCH_URL = "http://127.0.0.1:8765"
COLLECTION = "docs"
SOURCE_DIR = Path("docs/")


async def ingest_file(client: httpx.AsyncClient, path: Path) -> None:
    relative = str(path.relative_to(SOURCE_DIR))
    content = path.read_text(encoding="utf-8")
    resp = await client.post(
        f"{SEARCH_URL}/ingest",
        json={
            "collection": COLLECTION,
            "source_path": relative,
            "content": content,
            "metadata": {"filename": path.name},
        },
    )
    resp.raise_for_status()
    print(f"Ingested {relative}: doc_id={resp.json()['doc_id']}")


async def main() -> None:
    async with httpx.AsyncClient(timeout=30.0) as client:
        files = list(SOURCE_DIR.rglob("*.md"))
        print(f"Ingesting {len(files)} files into collection={COLLECTION!r}")
        for path in files:
            await ingest_file(client, path)
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
