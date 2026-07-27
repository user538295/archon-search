"""Tests for key_manager.py — key loading and auto-generation ."""
from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import archon_search.key_manager as km


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_HEX_64 = "a" * 64


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

    def test_invalid_env_var_warns_and_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        monkeypatch.setenv("ARCHON_SEARCH_API_KEY", "not-valid-hex!")
        key_file = tmp_path / ".search.env"
        _write_key_file(key_file, f"ARCHON_SEARCH_API_KEY={VALID_HEX_64}\n")
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
        with caplog.at_level(logging.WARNING):
            key, source = km.load_or_generate_key()
        assert key == VALID_HEX_64
        assert any("WARNING" in r.levelname for r in caplog.records)

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

        mock_write.assert_called_once_with(key_file, f"{km.ENV_VAR}={key}\n".encode(), mode=0o600)

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
        key_file = tmp_path / ".search.env"
        tmp_file = tmp_path / ".search.env.tmp"
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
        # Create orphaned tmp that the first O_EXCL open collides with.
        tmp_file.write_text("orphaned")

        real_write = km.atomic_write_bytes
        calls: list[int] = [0]

        def flaky_write(path: Path, data: bytes, mode: int = 0o600) -> None:
            calls[0] += 1
            if calls[0] == 1:
                raise FileExistsError("injected O_EXCL collision")
            real_write(path, data, mode=mode)

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
        key_file = tmp_path / ".search.env"
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
        ):
            with pytest.raises(OSError, match="injected replace failure"):
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

    def test_generate_and_write_exhausted_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Both O_EXCL attempts fail AND _load_from_file returns None → RuntimeError."""
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
        ):
            with pytest.raises(RuntimeError, match="key generation failed"):
                km._generate_and_write(key_file)


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
