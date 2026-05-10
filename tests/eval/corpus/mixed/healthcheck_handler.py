"""FastAPI health check endpoint with sub-system probes."""
from __future__ import annotations
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import asyncio


app = FastAPI()


async def _check_db(db_url: str) -> str:
    """Return 'ok' if the database is reachable, else an error message."""
    try:
        # In real code this would open a connection and run SELECT 1
        await asyncio.sleep(0)  # placeholder
        return "ok"
    except Exception as exc:
        return str(exc)


async def _check_index(index_dir: str) -> str:
    """Return 'ok' if the vector index files exist."""
    from pathlib import Path
    return "ok" if Path(index_dir).exists() else "missing"


@app.get("/health")
async def health(db_url: str = "sqlite:///:memory:", index_dir: str = "/tmp/index") -> JSONResponse:
    db_status, index_status = await asyncio.gather(
        _check_db(db_url), _check_index(index_dir)
    )
    overall = "ok" if db_status == "ok" and index_status == "ok" else "degraded"
    code = 200 if overall == "ok" else 503
    return JSONResponse(
        {"status": overall, "db": db_status, "index": index_status}, status_code=code
    )
