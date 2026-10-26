"""Tests for key_manager.py — key loading and auto-generation ."""
from __future__ import annotations

import asyncio
import os
import stat
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import archon_search.key_manager as km
from archon_search._durable_io import DurabilityUnconfirmedError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_HEX_64 = "a" * 64
_THREAD_WAIT_SECONDS = 10
_LOOP_TURNS = 5
# Past CPython's 4300-digit int-conversion limit: json.loads raises a plain ValueError.
_OVERSIZED_INT_DIGITS = 5000
# Past the recursion limit: json.loads raises RecursionError.
_EXCESSIVE_NESTING_DEPTH = 100_000


def _write_key_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLoadFromEnv:
    def test_load_from_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("ARCHON_SEARCH_API_KEY", VALID_HEX_64)
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
        key, source = km.load_or_generate_key()
        assert key == VALID_HEX_64
        assert source == "env var"
        # Key file is written so CLI processes in the same environment can
        # authenticate without needing the env var propagated explicitly.
        assert (tmp_path / ".search.env").exists()

    def test_empty_env_falls_back_to_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("ARCHON_SEARCH_API_KEY", "")
        key_file = tmp_path / ".search.env"
        _write_key_file(key_file, f"ARCHON_SEARCH_API_KEY={VALID_HEX_64}\n")
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
        key, source = km.load_or_generate_key()
        assert key == VALID_HEX_64
        assert "file" in source

    def test_invalid_env_var_is_refused_not_silently_replaced_by_the_key_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from archon_search.config import ConfigError

        monkeypatch.setenv("ARCHON_SEARCH_API_KEY", "not-valid-hex!")
        key_file = tmp_path / ".search.env"
        _write_key_file(key_file, f"ARCHON_SEARCH_API_KEY={VALID_HEX_64}\n")
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
        with pytest.raises(ConfigError, match="ARCHON_SEARCH_API_KEY is not a valid API key") as excinfo:
            km.load_or_generate_key()
        assert "not-valid-hex" not in str(excinfo.value)

    def test_env_priority_over_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        env_key = "b" * 64
        file_key = "c" * 64
        monkeypatch.setenv("ARCHON_SEARCH_API_KEY", env_key)
        key_file = tmp_path / ".search.env"
        _write_key_file(key_file, f"ARCHON_SEARCH_API_KEY={file_key}\n")
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
        key, source = km.load_or_generate_key()
        assert key == env_key
        assert source == "env var"


class TestLoadFromFile:
    def test_load_from_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        _write_key_file(key_file, f"ARCHON_SEARCH_API_KEY={VALID_HEX_64}\n")
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
        key, source = km.load_or_generate_key()
        assert key == VALID_HEX_64
        assert "file" in source

    def test_malformed_file_non_hex(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        _write_key_file(key_file, "ARCHON_SEARCH_API_KEY=not-hex\n")
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
        import logging

        with caplog.at_level(logging.ERROR):
            result = km._load_from_file(key_file)
        assert result is None
        assert any("ERROR" in r.levelname for r in caplog.records)

    def test_malformed_file_empty_value(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        _write_key_file(key_file, "ARCHON_SEARCH_API_KEY=\n")
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
        import logging

        with caplog.at_level(logging.ERROR):
            result = km._load_from_file(key_file)
        assert result is None
        assert any("ERROR" in r.levelname for r in caplog.records)

    def test_key_with_whitespace_stripped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        # Leading and trailing spaces
        _write_key_file(key_file, f"ARCHON_SEARCH_API_KEY=  {VALID_HEX_64}  \n")
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
        result = km._load_from_file(key_file)
        assert result == VALID_HEX_64

    def test_key_with_crlf_line_endings(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        _write_key_file(key_file, f"ARCHON_SEARCH_API_KEY={VALID_HEX_64}\r\n")
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
        result = km._load_from_file(key_file)
        assert result == VALID_HEX_64

    def test_key_file_missing_prefix(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        _write_key_file(key_file, "SOME_OTHER_VAR=abc123\n")
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
        result = km._load_from_file(key_file)
        assert result is None

    def test_chmod_on_wide_perms(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        if sys.platform == "win32":
            pytest.skip("chmod not relevant on Windows")
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        _write_key_file(key_file, f"ARCHON_SEARCH_API_KEY={VALID_HEX_64}\n")
        key_file.chmod(0o644)
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
        result = km._load_from_file(key_file)
        assert result == VALID_HEX_64
        # Should have been chmod'd to 600
        mode = stat.S_IMODE(key_file.stat().st_mode)
        assert mode == 0o600


class TestAutoGenerate:
    def test_auto_generate(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
        key, source = km.load_or_generate_key()
        assert source == "auto-generated"
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)
        # File content must be exactly "ARCHON_SEARCH_API_KEY=<key>\n"
        content = key_file.read_text()
        assert content == f"ARCHON_SEARCH_API_KEY={key}\n"

    def test_generated_file_permissions_600(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        if sys.platform == "win32":
            pytest.skip("chmod not relevant on Windows")
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
        km.load_or_generate_key()
        mode = stat.S_IMODE(key_file.stat().st_mode)
        assert mode == 0o600

    def test_atomic_write(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

        with patch("archon_search.key_manager.atomic_write_bytes") as mock_write:
            key = km._generate_and_write(key_file)

        mock_write.assert_called_once_with(key_file, f"{km.ENV_VAR}={key}\n".encode(), replace=False)

    def test_concurrent_race_loses(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """FileExistsError on O_EXCL + .search.env already present → reads existing file."""
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        existing_key = "d" * 64
        _write_key_file(key_file, f"ARCHON_SEARCH_API_KEY={existing_key}\n")
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

        real_os_open = os.open
        call_count = 0

        def fake_os_open(path: str, flags: int, mode: int = 0o777) -> int:
            nonlocal call_count
            if ".search.env.tmp" in path and (flags & os.O_EXCL):
                call_count += 1
                raise FileExistsError("injected")
            return real_os_open(path, flags, mode)

        with patch("os.open", side_effect=fake_os_open):
            result = km._generate_and_write(key_file)

        assert result == existing_key

    def test_orphaned_tmp(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Orphaned .search.env.tmp causes FileExistsError on attempt 0 → unlink tmp, retry, succeed."""
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        monkeypatch.setattr(km, "KEY_WRITE_CONFLICT_WAIT_SECONDS", 0.0)  # an orphan is not waited for here
        key_file = tmp_path / ".search.env"
        tmp_file = tmp_path / ".search.env.tmp"
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
        # Create orphaned tmp that the first O_EXCL open collides with.
        tmp_file.write_text("orphaned")

        real_write = km.atomic_write_bytes
        calls: list[int] = [0]

        def flaky_write(path: Path, data: bytes, mode: int = 0o600, *, replace: bool = True) -> None:
            calls[0] += 1
            if calls[0] == 1:
                raise FileExistsError("injected O_EXCL collision")
            real_write(path, data, mode=mode, replace=replace)

        with patch("archon_search.key_manager.atomic_write_bytes", side_effect=flaky_write):
            key = km._generate_and_write(key_file)

        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)
        assert not tmp_file.exists()

    def test_key_never_appears_in_logs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
        with caplog.at_level(logging.DEBUG):
            key, _ = km.load_or_generate_key()
        # The key value must not appear in any log record
        for record in caplog.records:
            assert key not in record.getMessage(), f"Key appeared in log: {record.getMessage()}"

    def test_os_replace_failure_cleans_up(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A non-FileExists OSError from the helper propagates out of _generate_and_write."""
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

        with patch(
            "archon_search.key_manager.atomic_write_bytes",
            side_effect=OSError("injected replace failure"),
        ), pytest.raises(OSError, match="injected replace failure"):
            km._generate_and_write(key_file)

    def test_concurrent_bootstrap_retry_still_works(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """FileExistsError on both attempts, with a concurrent writer's key visible on retry."""
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
        existing_key = "c" * 64

        with (
            patch(
                "archon_search.key_manager.atomic_write_bytes",
                side_effect=FileExistsError("injected"),
            ),
            patch.object(km, "_load_from_file", side_effect=[None, existing_key]),
        ):
            result = km._generate_and_write(key_file)

        assert result == existing_key

    def test_generate_and_write_exhausted_raises_key_write_conflict_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Both O_EXCL attempts fail AND _load_from_file returns None → KeyWriteConflictError."""
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

        def always_raises(path: str, flags: int, mode: int = 0o777) -> int:
            if flags & os.O_EXCL:
                raise FileExistsError("injected")
            return os.open.__wrapped__(path, flags, mode)  # type: ignore[attr-defined]

        with (
            patch("os.open", side_effect=always_raises),
            patch.object(km, "_load_from_file", return_value=None),
            pytest.raises(km.KeyWriteConflictError, match="key generation failed"),
        ):
            km._generate_and_write(key_file)

    def test_a_loser_waits_for_a_live_writer_instead_of_removing_its_temp_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A temp file whose writer is still running (a slow fsync) is not an orphan: the loser must not delete it
        and publish a key of its own, or the writer's key is lost."""
        import threading
        import time

        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        tmp_file = tmp_path / ".search.env.tmp"
        writers_key = "a" * 64
        tmp_file.write_text(f"{km.ENV_VAR}={writers_key}\n")
        publish_after = km.KEY_WRITE_CONFLICT_RETRY_SECONDS * 3  # well past the loser's first look

        def publish() -> None:
            time.sleep(publish_after)
            key_file.write_text(f"{km.ENV_VAR}={writers_key}\n")
            tmp_file.unlink()

        writer = threading.Thread(target=publish)
        writer.start()
        try:
            adopted = km._generate_and_write(key_file)
        finally:
            writer.join()

        assert adopted == writers_key
        assert key_file.read_text() == f"{km.ENV_VAR}={writers_key}\n"

    @pytest.mark.parametrize("fsync_seconds", [0.0, 0.25], ids=["fast-disk", "slow-fsync"])
    def test_two_starters_adopt_one_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fsync_seconds: float
    ) -> None:
        """Concurrent first starts all return the key the file ends up holding: none lost, none replaced.

        The slow fsync stretches the winner's publish past the losers' first look, as a loaded CI disk does."""
        import threading
        import time

        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        starters = 8
        barrier = threading.Barrier(starters)
        keys: list[str] = []
        failures: list[BaseException] = []
        real_fsync = os.fsync

        def slow_fsync(fd: int) -> None:
            time.sleep(fsync_seconds)
            real_fsync(fd)

        def start() -> None:
            barrier.wait()
            try:
                keys.append(km._generate_and_write(key_file))
            except BaseException as exc:  # noqa: BLE001 — a thread's failure must reach the assertion below
                failures.append(exc)

        monkeypatch.setattr(os, "fsync", slow_fsync)
        threads = [threading.Thread(target=start) for _ in range(starters)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        on_disk = key_file.read_text().strip().removeprefix(f"{km.ENV_VAR}=")
        assert not failures
        assert len(keys) == starters
        assert set(keys) == {on_disk}

    def test_a_file_with_no_key_line_is_replaced_by_a_generated_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        key_file.write_text("# nothing here\n")

        key = km._generate_and_write(key_file)

        assert key_file.read_text() == f"{km.ENV_VAR}={key}\n"

    @pytest.mark.skipif(sys.platform == "win32", reason="file modes")
    def test_an_unreadable_key_file_is_refused_not_overwritten(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from archon_search.config import ConfigError

        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
        key_file = tmp_path / ".search.env"
        original = "ARCHON_SEARCH_API_KEY=" + "e" * 64 + "\n"
        key_file.write_text(original)
        real_read_text = Path.read_text

        def unreadable(self: Path, *args: object, **kwargs: object) -> str:
            if self == key_file:
                raise PermissionError(13, "Permission denied")
            return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", unreadable)

        with pytest.raises(ConfigError) as excinfo:
            km.load_or_generate_key()

        assert str(key_file) in str(excinfo.value)
        assert "e" * 64 not in str(excinfo.value)
        assert key_file.read_bytes().decode() == original


# ---------------------------------------------------------------------------
# path migration tests
# ---------------------------------------------------------------------------


class TestGetKeyFile:
    """Tests for `get_key_file()` — the lazy replacement for the old module-level
    `KEY_FILE` constant (C9 Task 2.3)."""

    @pytest.mark.archon_unset_data_dir
    def test_get_key_file_default(self) -> None:
        """No env vars set → ``Path.home() / ".archon-search" / ".search.env"``."""
        assert km.get_key_file() == Path.home() / ".archon-search" / ".search.env"

    def test_get_key_file_key_file_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`ARCHON_SEARCH_KEY_FILE="/custom/.env"` → ``Path("/custom/.env")``."""
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", "/custom/.env")
        assert km.get_key_file() == Path("/custom/.env")

    def test_get_key_file_data_dir_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`ARCHON_SEARCH_DATA_DIR="/data"` → ``Path("/data/.search.env")``."""
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "/data")
        assert km.get_key_file() == Path("/data/.search.env")

    def test_key_file_env_overrides_data_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both env vars set → ``ARCHON_SEARCH_KEY_FILE`` wins."""
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", "/explicit/.env")
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "/data")
        assert km.get_key_file() == Path("/explicit/.env")

    def test_no_module_level_key_file_constant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Behavioral guard: setting ``ARCHON_SEARCH_DATA_DIR`` AFTER import
        must still influence the resolved key file path. Any module-level
        capture (e.g. ``KEY_FILE = ...`` or ``_key_file_env = os.environ.get(...)``)
        evaluated at import time would be stale here and break the assertion."""
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "/tmp/guard-test")
        result = km.get_key_file()
        assert str(result).startswith("/tmp/guard-test"), (
            f"Expected result under /tmp/guard-test, got: {result}"
        )

    def test_load_or_generate_key_uses_key_file_env_over_data_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Integration: both env vars set → key is written to ``ARCHON_SEARCH_KEY_FILE``,
        not ``ARCHON_SEARCH_DATA_DIR / .search.env``."""
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        explicit = tmp_path / "explicit.env"
        data_dir = tmp_path / "data"
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", str(explicit))
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(data_dir))
        key, source = km.load_or_generate_key()
        assert source == "auto-generated"
        assert explicit.exists(), "Key must be written to ARCHON_SEARCH_KEY_FILE"
        assert not (data_dir / ".search.env").exists(), (
            "Key must NOT fall through to ARCHON_SEARCH_DATA_DIR when "
            "ARCHON_SEARCH_KEY_FILE is set"
        )
        assert explicit.read_text() == f"ARCHON_SEARCH_API_KEY={key}\n"

    def test_get_key_file_relative_path_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Relative `ARCHON_SEARCH_KEY_FILE` is rejected — CWD inside the
        container is implementation-dependent and a relative key path would
        leak the secret to whatever directory the process happens to be in.
        Parity with `get_data_dir()`'s same guard."""
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", "secrets/.env")
        with pytest.raises(ValueError, match="absolute path"):
            km.get_key_file()

    def test_get_key_file_tilde_with_home_unset_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`ARCHON_SEARCH_KEY_FILE="~/x"` + HOME unset → `Path.expanduser`
        raises `RuntimeError`. `get_key_file()` translates it into the same
        `ValueError` callers of `get_data_dir()` already expect."""
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", "~/keys/.env")

        def _raise(_self: Path) -> Path:
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(Path, "expanduser", _raise)
        with pytest.raises(ValueError, match="HOME is not set"):
            km.get_key_file()

    def test_get_key_file_whitespace_padding_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Leading/trailing whitespace from copy-paste must be stripped
        before Path construction — `get_data_dir()` already does this, and
        `get_key_file()` matches for consistency."""
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", "  /custom/.env  ")
        assert km.get_key_file() == Path("/custom/.env")

    def test_get_key_file_empty_env_falls_through_to_data_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty `ARCHON_SEARCH_KEY_FILE` is intentionally lenient — it
        falls through to `get_data_dir()` rather than raising. This
        documents the deliberate asymmetry with `ARCHON_SEARCH_DATA_DIR`
        (which raises on empty): operators may want to unset a `KEY_FILE`
        override by emptying the env var without redefining the rest of
        their config."""
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", "")
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "/data")
        assert km.get_key_file() == Path("/data/.search.env")

    def test_get_key_file_whitespace_only_env_falls_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whitespace-only `ARCHON_SEARCH_KEY_FILE` behaves like empty —
        falls through to `get_data_dir()`. Same rationale as
        `test_get_key_file_empty_env_falls_through_to_data_dir`."""
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", "   ")
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", "/data")
        assert km.get_key_file() == Path("/data/.search.env")

    def test_get_key_file_whitespace_padded_relative_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whitespace padding is stripped BEFORE the absolute-path check —
        a relative payload like ``"  secrets/.env  "`` must still raise
        ``ValueError``, not silently slip through because of the surrounding
        spaces."""
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", "  secrets/.env  ")
        with pytest.raises(ValueError, match="absolute path"):
            km.get_key_file()

    def test_load_or_generate_key_file_source_matches_resolved_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """TOCTOU regression guard: when ``load_or_generate_key()`` finds an
        existing key on disk, the reported source string must report the
        path actually read — i.e. the path resolved once at the top of the
        function and threaded through, not a fresh re-resolution."""
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        explicit = tmp_path / "explicit.env"
        _write_key_file(explicit, f"ARCHON_SEARCH_API_KEY={VALID_HEX_64}\n")
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", str(explicit))
        key, source = km.load_or_generate_key()
        assert key == VALID_HEX_64
        assert source == f"file: {explicit}"


# ---------------------------------------------------------------------------
# persist_key
# ---------------------------------------------------------------------------


class TestPersistKey:
    def test_persist_key_writes_key_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """persist_key() writes the key to the key file so CLI can read it."""
        key_file = tmp_path / ".search.env"
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", str(key_file))
        km.persist_key(VALID_HEX_64)
        assert key_file.exists()
        content = key_file.read_text()
        assert f"ARCHON_SEARCH_API_KEY={VALID_HEX_64}" in content

    def test_persist_key_overwrites_existing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """persist_key() replaces a stale key file with the current key."""
        old_key = "b" * 64
        new_key = VALID_HEX_64
        key_file = tmp_path / ".search.env"
        _write_key_file(key_file, f"ARCHON_SEARCH_API_KEY={old_key}\n")
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", str(key_file))
        km.persist_key(new_key)
        content = key_file.read_text()
        assert new_key in content
        assert old_key not in content

    def test_persist_key_creates_parent_dirs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """persist_key() creates the parent directory when it does not exist."""
        key_file = tmp_path / "subdir" / ".search.env"
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", str(key_file))
        km.persist_key(VALID_HEX_64)
        assert key_file.exists()

    def test_load_or_generate_key_env_var_writes_key_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When source is env var, load_or_generate_key() writes the key to
        the key file so that CLI commands in the same environment (Docker
        terminal, docker exec) can authenticate without manual setup.

        Regression: server started with ARCHON_SEARCH_API_KEY env var never
        wrote the key file, causing CLI to fail with PermissionError when it
        tried to generate the key itself.
        """
        key_file = tmp_path / ".search.env"
        monkeypatch.setenv("ARCHON_SEARCH_API_KEY", VALID_HEX_64)
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", str(key_file))
        key, source = km.load_or_generate_key()
        assert key == VALID_HEX_64
        assert source == "env var"
        assert key_file.exists(), "key file must be written so CLI can authenticate"
        assert VALID_HEX_64 in key_file.read_text()

    def test_persist_key_swallows_oserror(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """OSError (e.g. EROFS on read-only mount) must not propagate — startup
        must not crash just because the convenience write fails."""
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", str(tmp_path / ".search.env"))
        with patch.object(km, "_generate_and_write", side_effect=OSError(30, "EROFS")):
            km.persist_key(VALID_HEX_64)  # must not raise

    def test_persist_key_swallows_permission_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """PermissionError (EACCES) must be swallowed — it is a subclass of OSError."""
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", str(tmp_path / ".search.env"))
        with patch.object(km, "_generate_and_write", side_effect=PermissionError(13, "EACCES")):
            km.persist_key(VALID_HEX_64)  # must not raise


# ---------------------------------------------------------------------------
# load_key
# ---------------------------------------------------------------------------


class TestLoadKey:
    def test_returns_env_key(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Env var set → returns it; key file is not consulted."""
        monkeypatch.setenv("ARCHON_SEARCH_API_KEY", VALID_HEX_64)
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", str(tmp_path / "missing.env"))
        assert km.load_key() == VALID_HEX_64

    def test_returns_file_key_when_env_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No env var → reads key file."""
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        _write_key_file(key_file, f"ARCHON_SEARCH_API_KEY={VALID_HEX_64}\n")
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", str(key_file))
        assert km.load_key() == VALID_HEX_64

    def test_returns_none_when_nothing_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No env var, no key file → None."""
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", str(tmp_path / "missing.env"))
        assert km.load_key() is None


# ---------------------------------------------------------------------------
# rotate_and_persist_default_key — the rotate use case shared by REST and MCP
# ---------------------------------------------------------------------------


def _reader(token: str) -> AsyncMock:
    """A ``read_current_token`` stand-in answering *token*."""
    return AsyncMock(return_value=token)


class TestRotateAndPersistDefaultKey:
    @pytest.fixture
    def key_file(self, tmp_path: Path) -> Path:
        return tmp_path / "keys" / ".search.env"

    @pytest.fixture
    def store(self, tmp_path: Path) -> km.KeyStore:
        return km.KeyStore(path=tmp_path / "keys.json")

    @staticmethod
    async def _rotate(
        store: km.KeyStore,
        key_file: Path,
        *,
        token: str = VALID_HEX_64,
        grace: int = 0,
        reader: AsyncMock | None = None,
        publish: list[str] | None = None,
    ) -> km.RotationOutcome:
        return await km.rotate_and_persist_default_key(
            store,
            key_file=key_file,
            read_current_token=reader if reader is not None else _reader(token),
            grace_seconds=grace,
            publish_new_token=publish.append if publish is not None else None,
        )

    async def test_writes_the_new_key_file_then_records_it(self, store: km.KeyStore, key_file: Path) -> None:
        published: list[str] = []
        result = await self._rotate(store, key_file, publish=published)

        new_token = result.new_token
        assert key_file.read_text() == f"{km.ENV_VAR}={new_token}\n"
        assert [r.id for r in await store.active_keys()] == [result.new_key_id]
        assert result.old_record is None  # unmanaged old key, no grace window
        assert published == [new_token]

    async def test_blank_current_token_is_rejected_before_anything_is_written(
        self, store: km.KeyStore, key_file: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            patch.object(km, "atomic_write_bytes") as write,
            caplog.at_level("ERROR"),
            pytest.raises(km.BlankCurrentKeyError, match="current_token must not be empty"),
        ):
            await self._rotate(store, key_file, token="   ")

        write.assert_not_called()
        assert not (tmp_path / "keys.json").exists()
        assert "rotate: rejected before writing .search.env: current_token must not be empty" in caplog.text

    @pytest.mark.parametrize("grace", [-1, km.MAX_ROTATE_GRACE_SECONDS + 1])
    async def test_out_of_range_grace_is_rejected_before_the_current_key_is_read(
        self, store: km.KeyStore, key_file: Path, tmp_path: Path, grace: int, caplog: pytest.LogCaptureFixture
    ) -> None:
        reader = _reader(VALID_HEX_64)
        with (
            patch.object(km, "atomic_write_bytes") as write,
            caplog.at_level("WARNING"),
            pytest.raises(km.KeyRotationInputError, match="grace_seconds must be") as exc_info,
        ):
            await self._rotate(store, key_file, grace=grace, reader=reader)

        rejected = [r for r in caplog.records if r.getMessage().startswith("rotate: rejected before writing .search.env")]
        assert [r.levelname for r in rejected] == ["WARNING"], "an out-of-range grace is logged WARNING, not ERROR"
        assert not isinstance(exc_info.value, km.BlankCurrentKeyError)
        reader.assert_not_awaited()
        write.assert_not_called()
        assert not key_file.parent.exists()
        assert not (tmp_path / "keys.json").exists()

    async def test_key_file_write_failure_raises_key_file_write_error_and_leaves_keys_json(
        self, store: km.KeyStore, key_file: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cause = PermissionError(13, "Permission denied", str(key_file))
        with patch.object(km, "atomic_write_bytes", side_effect=cause), caplog.at_level("ERROR"), pytest.raises(km.KeyFileWriteError) as exc_info:
            await self._rotate(store, key_file)

        assert str(exc_info.value) == km.ROTATE_WRITE_FAILED_DETAIL
        assert exc_info.value.__cause__ is cause
        assert any(r.exc_info is not None and r.exc_info[1] is cause for r in caplog.records)
        assert not (tmp_path / "keys.json").exists()

    async def test_key_file_dir_creation_failure_raises_key_file_write_error(
        self, store: km.KeyStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("a regular file where the key file's directory belongs")
        published: list[str] = []

        with caplog.at_level("ERROR"), pytest.raises(km.KeyFileWriteError) as exc_info:
            await self._rotate(store, blocker / "sub" / ".search.env", publish=published)

        assert isinstance(exc_info.value.__cause__, OSError)
        assert str(blocker) in caplog.text and "Traceback" in caplog.text
        assert not (tmp_path / "keys.json").exists()
        assert published == []

    async def test_key_file_durability_unconfirmed_counts_as_written(
        self, store: km.KeyStore, key_file: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """os.replace landed the new key: a failed dir fsync must not claim "rotation aborted"."""
        real_write = km.atomic_write_bytes

        def write_then_fail_dir_fsync(path: Path, data: bytes, mode: int = 0o600) -> None:
            real_write(path, data, mode=mode)
            if path == key_file:
                raise DurabilityUnconfirmedError(5, "dir fsync failed", str(path.parent))

        with patch.object(km, "atomic_write_bytes", side_effect=write_then_fail_dir_fsync), caplog.at_level("WARNING"):
            result = await self._rotate(store, key_file)

        assert key_file.read_text() == f"{km.ENV_VAR}={result.new_token}\n"
        assert [r.id for r in await store.active_keys()] == [result.new_key_id]
        assert any(r.levelname == "WARNING" and "durability" in r.getMessage() for r in caplog.records)

    async def test_unreadable_keys_json_fails_closed_before_the_key_file_is_written(
        self, store: km.KeyStore, key_file: Path, tmp_path: Path
    ) -> None:
        corrupt = b"{ not json"
        (tmp_path / "keys.json").write_bytes(corrupt)
        published: list[str] = []

        with pytest.raises(km.KeyStoreUnreadableError) as exc_info:
            await self._rotate(store, key_file, publish=published)

        assert str(exc_info.value) == km.KEY_STORE_UNREADABLE_DETAIL
        assert not key_file.exists()
        assert (tmp_path / "keys.json").read_bytes() == corrupt
        assert published == []

    async def test_keys_json_failure_after_the_write_raises_rotation_incomplete_error(
        self, store: km.KeyStore, key_file: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cause = km.KeyStoreUnreadableError(km.KEY_STORE_UNREADABLE_DETAIL)  # keys.json broke after verify_readable
        with patch.object(store, "record_rotation", side_effect=cause), caplog.at_level("ERROR"), pytest.raises(km.RotationIncompleteError) as exc_info:
            await self._rotate(store, key_file)

        assert str(exc_info.value) == km.ROTATE_KEYS_JSON_FAILED_DETAIL
        assert exc_info.value.__cause__ is cause
        assert any(r.exc_info is not None and r.exc_info[1] is cause for r in caplog.records)
        assert key_file.exists(), "the new key was written before keys.json"

    async def test_keys_json_write_oserror_is_an_incomplete_rotation(
        self, store: km.KeyStore, key_file: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A real keys.json write failure, after .search.env landed, never claims nothing changed."""
        real_write = km.atomic_write_bytes
        cause = PermissionError(13, "Permission denied", str(tmp_path / "keys.json"))
        published: list[str] = []

        def fail_keys_json(path: Path, data: bytes, mode: int = 0o600) -> None:
            if path == tmp_path / "keys.json":
                raise cause
            real_write(path, data, mode=mode)

        with (
            patch.object(km, "atomic_write_bytes", side_effect=fail_keys_json),
            caplog.at_level("ERROR"),
            pytest.raises(km.RotationIncompleteError) as exc_info,
        ):
            await self._rotate(store, key_file, publish=published)

        assert exc_info.value.__cause__ is cause
        assert key_file.read_text().startswith(f"{km.ENV_VAR}=")
        assert not (tmp_path / "keys.json").exists()
        assert published == []

    async def test_keys_json_durability_unconfirmed_counts_as_written(
        self, store: km.KeyStore, key_file: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        real_write = km.atomic_write_bytes

        def write_then_fail_dir_fsync(path: Path, data: bytes, mode: int = 0o600) -> None:
            real_write(path, data, mode=mode)
            if path == tmp_path / "keys.json":
                raise DurabilityUnconfirmedError(5, "dir fsync failed", str(path.parent))

        with patch.object(km, "atomic_write_bytes", side_effect=write_then_fail_dir_fsync), caplog.at_level("WARNING"):
            result = await self._rotate(store, key_file)

        assert [r.id for r in await store.active_keys()] == [result.new_key_id]
        assert any(r.levelname == "WARNING" and "durability" in r.getMessage() for r in caplog.records)

    async def test_token_mismatch_after_both_writes_is_an_incomplete_rotation(
        self, store: km.KeyStore, key_file: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        recorded = km.RotationOutcome(new_key_id="id", new_token="f" * 64, old_record=None)
        published: list[str] = []
        with (
            patch.object(store, "record_rotation", AsyncMock(return_value=recorded)),
            caplog.at_level("ERROR"),
            pytest.raises(km.RotationIncompleteError) as exc_info,
        ):
            await self._rotate(store, key_file, publish=published)

        assert str(exc_info.value) == km.ROTATE_KEYS_JSON_FAILED_DETAIL
        assert "unexpected token" in caplog.text
        assert published == []

    async def test_concurrent_rotations_are_serialised(self, store: km.KeyStore, key_file: Path) -> None:
        """Each rotation reads the key the previous one published: no orphaned active key."""
        state = {"current": VALID_HEX_64}

        async def read() -> str:
            current = state["current"]
            await asyncio.sleep(0)  # let the other rotation run if nothing serialises them
            return current

        def publish(token: str) -> None:
            state["current"] = token

        async def rotate() -> km.RotationOutcome:
            return await km.rotate_and_persist_default_key(
                store, key_file=key_file, read_current_token=read, grace_seconds=0, publish_new_token=publish
            )

        await asyncio.gather(rotate(), rotate())

        active = await store.active_keys()
        assert len(active) == 1, "the first rotation's key must be revoked by the second"
        assert key_file.read_text() == f"{km.ENV_VAR}={state['current']}\n"

    async def _cancel_during_the_key_file_write(
        self, store: km.KeyStore, key_file: Path, published: list[str], *, cancels: int = 1
    ) -> None:
        """Cancel a rotation *cancels* times while its key-file write blocks.

        After each cancel, assert the caller still waits under the lock; then it sees the cancel.
        """
        real_write = km.atomic_write_bytes
        write_started = threading.Event()
        release_write = threading.Event()

        def blocking_write(path: Path, data: bytes, mode: int = 0o600) -> None:
            if path == key_file:
                write_started.set()
                release_write.wait(timeout=_THREAD_WAIT_SECONDS)
            real_write(path, data, mode=mode)

        with patch.object(km, "atomic_write_bytes", side_effect=blocking_write):
            rotation = asyncio.create_task(self._rotate(store, key_file, publish=published))
            try:
                assert await asyncio.to_thread(write_started.wait, _THREAD_WAIT_SECONDS)
                for _ in range(cancels):
                    rotation.cancel()
                    for _ in range(_LOOP_TURNS):
                        await asyncio.sleep(0)
                    assert not rotation.done(), "the caller must wait for the in-flight rotation"
                    assert store.rotation_lock.locked(), "the lock must be held until the rotation ends"
            finally:
                release_write.set()
            with pytest.raises(asyncio.CancelledError):
                await rotation

    async def test_a_cancel_during_the_key_file_write_still_completes_the_rotation_under_the_lock(
        self, store: km.KeyStore, key_file: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The write thread cannot be cancelled: the rotation finishes, then the cancel propagates (C5-I-2)."""
        published: list[str] = []
        with caplog.at_level("WARNING"):
            await self._cancel_during_the_key_file_write(store, key_file, published)

        assert len(published) == 1
        assert key_file.read_text() == f"{km.ENV_VAR}={published[0]}\n"
        assert len(await store.active_keys()) == 1, "keys.json must record the key .search.env now holds"
        assert not store.rotation_lock.locked()
        assert f"rotate: cancelled mid-rotation, but it completed: {key_file} holds the new key" in caplog.text

    async def test_repeated_cancels_during_the_key_file_write_still_complete_the_rotation_under_the_lock(
        self, store: km.KeyStore, key_file: Path
    ) -> None:
        """A second cancel, delivered while the caller waits for the commit, is ignored too (C7-I-202)."""
        published: list[str] = []
        await self._cancel_during_the_key_file_write(store, key_file, published, cancels=2)

        assert len(published) == 1
        assert key_file.read_text() == f"{km.ENV_VAR}={published[0]}\n"
        assert len(await store.active_keys()) == 1, "keys.json must record the key .search.env now holds"
        assert not store.rotation_lock.locked()

    async def test_a_cancel_during_the_key_file_write_then_a_keys_json_failure_logs_the_failed_rotation(
        self, store: km.KeyStore, key_file: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        published: list[str] = []
        cause = OSError("keys.json write failed")
        with patch.object(store, "record_rotation", side_effect=cause), caplog.at_level("ERROR"):
            await self._cancel_during_the_key_file_write(store, key_file, published)

        failed = [r for r in caplog.records if r.getMessage() == "rotate: cancelled mid-rotation, and the rotation failed"]
        assert len(failed) == 1 and failed[0].levelname == "ERROR"
        assert isinstance(failed[0].exc_info[1], km.RotationIncompleteError)
        assert failed[0].exc_info[1].__cause__ is cause
        assert published == [], "a failed rotation must publish nothing"
        assert not (tmp_path / "keys.json").exists()
        assert not store.rotation_lock.locked()

    async def test_a_cancel_during_the_key_file_write_then_a_cancelled_rotation_task_logs_an_unknown_outcome(
        self, store: km.KeyStore, key_file: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        published: list[str] = []
        with patch.object(store, "record_rotation", side_effect=asyncio.CancelledError), caplog.at_level("ERROR"):
            await self._cancel_during_the_key_file_write(store, key_file, published)

        message = f"rotate: cancelled mid-rotation and the rotation task was cancelled too; check {key_file} and keys.json"
        assert [r.levelname for r in caplog.records if r.getMessage() == message] == ["ERROR"]
        assert published == []
        assert not (tmp_path / "keys.json").exists()
        assert not store.rotation_lock.locked()


class TestResolveRotationKeyFile:
    def test_returns_the_key_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", str(tmp_path / ".search.env"))
        assert km.resolve_rotation_key_file() == tmp_path / ".search.env"

    def test_bad_key_file_env_answers_the_fixed_write_failure(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", "relative/.search.env")
        with caplog.at_level("ERROR"), pytest.raises(km.KeyFileWriteError) as exc_info:
            km.resolve_rotation_key_file()

        assert str(exc_info.value) == km.ROTATE_WRITE_FAILED_DETAIL
        assert isinstance(exc_info.value.__cause__, ValueError)
        assert "relative/.search.env" in caplog.text and "Traceback" in caplog.text


class TestKeyStoreFailsClosed:
    """A keys.json that exists but cannot be read is never overwritten by a mutation (C4-I-102)."""

    @pytest.fixture
    def path(self, tmp_path: Path) -> Path:
        return tmp_path / "keys.json"

    @staticmethod
    def _mutate(store: km.KeyStore, mutation: str):
        if mutation == "create":
            return store.create(ns="default", label=None, expires_at=None)
        if mutation == "revoke":
            return store.revoke("some-id")
        if mutation == "record_rotation":
            return store.record_rotation(current_token=VALID_HEX_64, grace_seconds=0, new_token="b" * 64)
        return store.load_synthetic_records([])

    @pytest.mark.parametrize("mutation", ["create", "revoke", "record_rotation", "load_synthetic_records"])
    @pytest.mark.parametrize(
        "content",
        [
            b"{ not json",
            b"",
            b'{"a": 1}',
            b'[{"id": "x"}]',
            b"\xff",
            b"[" + b"1" * _OVERSIZED_INT_DIGITS + b"]",
            b"[" * _EXCESSIVE_NESTING_DEPTH,
        ],
        ids=["invalid-json", "empty", "not-an-array", "invalid-record", "not-utf8", "oversized-int", "deep-nesting"],
    )
    async def test_corrupt_keys_json_is_never_overwritten(self, path: Path, mutation: str, content: bytes) -> None:
        path.write_bytes(content)
        store = km.KeyStore(path)

        with pytest.raises(km.KeyStoreUnreadableError) as exc_info:
            await self._mutate(store, mutation)

        assert str(exc_info.value) == km.KEY_STORE_UNREADABLE_DETAIL
        assert path.read_bytes() == content
        assert await store.load() == [], "auth reads keep degrading to an empty key list"

    async def test_unreadable_keys_json_is_never_overwritten(self, path: Path) -> None:
        path.write_bytes(b"[]")
        store = km.KeyStore(path)
        cause = PermissionError(13, "Permission denied", str(path))

        with patch.object(Path, "read_bytes", side_effect=cause):
            with pytest.raises(km.KeyStoreUnreadableError) as exc_info:
                await store.create(ns="default", label=None, expires_at=None)
            assert await store.load() == []

        assert exc_info.value.__cause__ is cause
        assert path.read_bytes() == b"[]"

    async def test_missing_keys_json_is_created(self, path: Path) -> None:
        store = km.KeyStore(path)
        created = await store.create(ns="default", label=None, expires_at=None)
        assert [r.id for r in await store.load()] == [created.id]

    async def test_write_durability_unconfirmed_is_logged_and_the_mutation_succeeds(
        self, path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        real_write = km.atomic_write_bytes

        def write_then_fail_dir_fsync(target: Path, data: bytes, mode: int = 0o600) -> None:
            real_write(target, data, mode=mode)
            raise DurabilityUnconfirmedError(5, "dir fsync failed", str(target.parent))

        store = km.KeyStore(path)
        with patch.object(km, "atomic_write_bytes", side_effect=write_then_fail_dir_fsync), caplog.at_level("WARNING"):
            created = await store.create(ns="default", label=None, expires_at=None)

        assert [r.id for r in await store.load()] == [created.id]
        assert any(r.levelname == "WARNING" and "durability" in r.getMessage() for r in caplog.records)


class TestRotationErrorTypes:
    def test_blank_current_key_and_input_errors_are_unrelated_siblings(self) -> None:
        """A server fault and a caller input error: neither handler may catch the other (C5-B-3)."""
        assert not issubclass(km.BlankCurrentKeyError, km.KeyRotationInputError)
        assert not issubclass(km.KeyRotationInputError, km.BlankCurrentKeyError)


class TestValidateRotationGrace:
    @pytest.mark.parametrize("grace", [-1, km.MAX_ROTATE_GRACE_SECONDS + 1])
    def test_rejects_out_of_range_grace(self, grace: int) -> None:
        with pytest.raises(km.KeyRotationInputError, match="grace_seconds must be"):
            km.validate_rotation_grace(grace)

    @pytest.mark.parametrize("grace", [0, km.MAX_ROTATE_GRACE_SECONDS])
    def test_accepts_in_range_grace(self, grace: int) -> None:
        km.validate_rotation_grace(grace)
