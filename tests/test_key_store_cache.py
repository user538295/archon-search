"""KeyStore serves ``keys.json`` from a (mtime, size, inode)-validated cache (SecReview #1b).

A request used to re-read and re-parse ``keys.json`` synchronously; now an unchanged file costs one
``stat``. Every change — through this instance, a second instance, or another process replacing the file —
must be visible on the very next call: a revoke or a rotation never lingers in a stale cache.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from archon_search.key_manager import KeyRecord, KeyStore

_STALE_MTIME_NS = 1_000_000_000  # 1970: any real write has a different mtime


def _record(key_id: str, *, status: str = "active", namespace: str = "team-a") -> KeyRecord:
    return KeyRecord(
        id=key_id,
        token_hash=f"hash-{key_id}",
        namespace=namespace,
        created_at=datetime.now(UTC),
        status=status,  # type: ignore[arg-type]
    )


@pytest.fixture
def keys_path(tmp_path: Path) -> Path:
    return tmp_path / "keys.json"


@pytest.fixture
def parse_count(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count how often keys.json is actually read and parsed."""
    calls: list[int] = []
    original = KeyStore._load_for_update

    def counting(self: KeyStore) -> list[KeyRecord]:
        calls.append(1)
        return original(self)

    monkeypatch.setattr(KeyStore, "_load_for_update", counting)
    return calls


async def test_an_unchanged_file_is_parsed_once_for_many_reads(keys_path: Path, parse_count: list[int]) -> None:
    store = KeyStore(keys_path)
    await store.create("team-a", None, None)
    parse_count.clear()

    for _ in range(25):
        await store.active_keys()
        await store.load()

    assert len(parse_count) == 1


async def test_a_revoke_through_the_same_instance_is_seen_immediately(keys_path: Path) -> None:
    store = KeyStore(keys_path)
    created = await store.create("team-a", None, None)
    assert [r.id for r in await store.active_keys()] == [created.id]

    await store.revoke(str(created.id))

    assert await store.active_keys() == []


async def test_a_revoke_by_a_second_instance_is_seen_immediately(keys_path: Path) -> None:
    reader, writer = KeyStore(keys_path), KeyStore(keys_path)
    created = await writer.create("team-a", None, None)
    assert len(await reader.active_keys()) == 1  # primes the reader's cache

    await writer.revoke(str(created.id))

    assert await reader.active_keys() == []


async def test_a_creation_by_a_second_instance_is_seen_immediately(keys_path: Path) -> None:
    reader, writer = KeyStore(keys_path), KeyStore(keys_path)
    assert await reader.active_keys() == []
    await writer.create("team-a", None, None)
    assert len(await reader.active_keys()) == 1

    await writer.create("team-a", None, None)

    assert len(await reader.active_keys()) == 2


async def test_a_rotation_by_a_second_instance_is_seen_immediately(keys_path: Path) -> None:
    reader, writer = KeyStore(keys_path), KeyStore(keys_path)
    old = await writer.create("default", None, None)
    assert len(await reader.active_keys()) == 1
    old_token = str(old.token)

    await writer.record_rotation(old_token, 0, "f" * 64)

    active = await reader.active_keys()
    assert len(active) == 1 and active[0].id != old.id


async def test_another_process_replacing_the_file_is_seen_immediately(keys_path: Path) -> None:
    """A different process rewrites keys.json (atomic replace): the next read sees it, no stale window."""
    store = KeyStore(keys_path)
    created = await store.create("team-a", None, None)
    assert len(await store.active_keys()) == 1

    revoked = [{**r, "status": "revoked"} for r in json.loads(keys_path.read_text())]
    script = (
        "import json, os, sys\n"
        "path = sys.argv[1]\n"
        "tmp = path + '.other'\n"
        "open(tmp, 'w').write(sys.argv[2])\n"
        "os.replace(tmp, path)\n"
    )
    await asyncio.to_thread(
        subprocess.run, [sys.executable, "-c", script, str(keys_path), json.dumps(revoked)], check=True
    )

    assert await store.active_keys() == [], f"revoke of {created.id} by another process was not seen"


async def test_an_in_place_rewrite_with_new_mtime_is_seen(keys_path: Path) -> None:
    """Even a non-atomic in-place edit is seen when the mtime moves."""
    store = KeyStore(keys_path)
    await store.create("team-a", None, None)
    assert len(await store.active_keys()) == 1

    payload = [{**r, "status": "revoked"} for r in json.loads(keys_path.read_text())]
    with keys_path.open("r+") as handle:
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(payload))
    stat = keys_path.stat()
    os.utime(keys_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + _STALE_MTIME_NS))

    assert await store.active_keys() == []


async def test_deleting_the_file_empties_the_view(keys_path: Path) -> None:
    store = KeyStore(keys_path)
    await store.create("team-a", None, None)
    assert len(await store.active_keys()) == 1

    keys_path.unlink()

    assert await store.active_keys() == []


async def test_a_corrupted_file_is_read_as_empty_once_and_recovers_when_fixed(
    keys_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = KeyStore(keys_path)
    await store.create("team-a", None, None)
    good = keys_path.read_text()
    keys_path.write_text("{not json")

    with caplog.at_level("ERROR", logger="archon_search.key_manager"):
        for _ in range(5):
            assert await store.active_keys() == []
    assert len([r for r in caplog.records if "not valid JSON" in r.getMessage()]) == 1, "logged once per file state"

    keys_path.write_text(good)
    stat = keys_path.stat()
    os.utime(keys_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + _STALE_MTIME_NS))
    assert len(await store.active_keys()) == 1


async def test_mutations_never_work_from_cached_objects(keys_path: Path) -> None:
    """revoke() mutates a fresh parse, so a caller holding a cached record never sees it change under it."""
    store = KeyStore(keys_path)
    created = await store.create("team-a", None, None)
    held = (await store.load())[0]

    await store.revoke(str(created.id))

    assert held.status == "active", "revoke must not mutate the record object the cache handed out"
    assert (await store.load())[0].status == "revoked"


async def test_records_missing_the_admin_field_follow_the_compatibility_rule(keys_path: Path) -> None:
    def entry(key_id: str | None, namespace: str) -> dict:
        return {
            "id": key_id,
            "token_hash": f"h-{key_id}-{namespace}",
            "namespace": namespace,
            "label": None,
            "created_at": datetime.now(UTC).isoformat(),
            "expires_at": None,
            "status": "active",
        }

    keys_path.write_text(
        json.dumps(
            [
                entry("managed-default", "default"),
                entry("managed-other", "team-a"),
                entry(None, "default"),  # TOML synthetic record mapped to the default namespace
                {**entry("explicit", "default"), "admin": False},
            ]
        )
    )

    admin_by_id = {r.id: r.admin for r in await KeyStore(keys_path).load()}

    assert admin_by_id == {"managed-default": True, "managed-other": False, None: False, "explicit": False}


async def test_a_transient_read_error_is_not_cached_as_an_empty_store(
    keys_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An EIO on one read must not pin ``[]`` to the unchanged file signature."""
    store = KeyStore(keys_path)
    await store.create("team-a", None, None)
    fresh = KeyStore(keys_path)
    real_read_bytes = Path.read_bytes
    failures = [1]

    def flaky_read_bytes(self: Path) -> bytes:
        if self == keys_path and failures:
            failures.pop()
            raise OSError(5, "Input/output error")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", flaky_read_bytes)

    assert await fresh.load() == []
    assert [record.namespace for record in await fresh.load()] == ["team-a"]


async def test_mutating_a_loaded_record_does_not_poison_the_cache(keys_path: Path) -> None:
    store = KeyStore(keys_path)
    await store.create("team-a", None, None)

    first = await store.load()
    first[0].status = "revoked"
    first[0].admin = True
    (await store.active_keys())[0].namespace = "evil"
    (await store.list_keys())[0].namespace = "evil"

    again = await store.load()
    assert [(r.status, r.admin, r.namespace) for r in again] == [("active", False, "team-a")]
