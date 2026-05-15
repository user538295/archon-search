"""Tests for key_manager.py — key loading and auto-generation (Task 1.1)."""
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
        monkeypatch.setattr(km, "KEY_FILE", tmp_path / ".search.env")
        key, source = km.load_or_generate_key()
        assert key == VALID_HEX_64
        assert source == "env var"
        # File must not have been read (it doesn't exist)
        assert not (tmp_path / ".search.env").exists()

    def test_empty_env_falls_back_to_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("ARCHON_SEARCH_API_KEY", "")
        key_file = tmp_path / ".search.env"
        _write_key_file(key_file, f"ARCHON_SEARCH_API_KEY={VALID_HEX_64}\n")
        monkeypatch.setattr(km, "KEY_FILE", key_file)
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
        monkeypatch.setattr(km, "KEY_FILE", key_file)
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
        monkeypatch.setattr(km, "KEY_FILE", key_file)
        key, source = km.load_or_generate_key()
        assert key == env_key
        assert source == "env var"


class TestLoadFromFile:
    def test_load_from_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        _write_key_file(key_file, f"ARCHON_SEARCH_API_KEY={VALID_HEX_64}\n")
        monkeypatch.setattr(km, "KEY_FILE", key_file)
        key, source = km.load_or_generate_key()
        assert key == VALID_HEX_64
        assert "file" in source

    def test_malformed_file_non_hex(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        _write_key_file(key_file, "ARCHON_SEARCH_API_KEY=not-hex\n")
        monkeypatch.setattr(km, "KEY_FILE", key_file)
        import logging

        with caplog.at_level(logging.ERROR):
            result = km._load_from_file()
        assert result is None
        assert any("ERROR" in r.levelname for r in caplog.records)

    def test_malformed_file_empty_value(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        _write_key_file(key_file, "ARCHON_SEARCH_API_KEY=\n")
        monkeypatch.setattr(km, "KEY_FILE", key_file)
        import logging

        with caplog.at_level(logging.ERROR):
            result = km._load_from_file()
        assert result is None
        assert any("ERROR" in r.levelname for r in caplog.records)

    def test_key_with_whitespace_stripped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        # Leading and trailing spaces
        _write_key_file(key_file, f"ARCHON_SEARCH_API_KEY=  {VALID_HEX_64}  \n")
        monkeypatch.setattr(km, "KEY_FILE", key_file)
        result = km._load_from_file()
        assert result == VALID_HEX_64

    def test_key_with_crlf_line_endings(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        _write_key_file(key_file, f"ARCHON_SEARCH_API_KEY={VALID_HEX_64}\r\n")
        monkeypatch.setattr(km, "KEY_FILE", key_file)
        result = km._load_from_file()
        assert result == VALID_HEX_64

    def test_key_file_missing_prefix(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        _write_key_file(key_file, "SOME_OTHER_VAR=abc123\n")
        monkeypatch.setattr(km, "KEY_FILE", key_file)
        result = km._load_from_file()
        assert result is None

    def test_chmod_on_wide_perms(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        if sys.platform == "win32":
            pytest.skip("chmod not relevant on Windows")
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        _write_key_file(key_file, f"ARCHON_SEARCH_API_KEY={VALID_HEX_64}\n")
        key_file.chmod(0o644)
        monkeypatch.setattr(km, "KEY_FILE", key_file)
        result = km._load_from_file()
        assert result == VALID_HEX_64
        # Should have been chmod'd to 600
        mode = stat.S_IMODE(key_file.stat().st_mode)
        assert mode == 0o600


class TestAutoGenerate:
    def test_auto_generate(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        monkeypatch.setattr(km, "KEY_FILE", key_file)
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
        monkeypatch.setattr(km, "KEY_FILE", key_file)
        km.load_or_generate_key()
        mode = stat.S_IMODE(key_file.stat().st_mode)
        assert mode == 0o600

    def test_atomic_write(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        monkeypatch.setattr(km, "KEY_FILE", key_file)
        replace_calls: list[tuple[str, str]] = []
        real_replace = os.replace

        def tracking_replace(src: str, dst: str) -> None:
            replace_calls.append((src, dst))
            return real_replace(src, dst)

        with patch("os.replace", side_effect=tracking_replace):
            km.load_or_generate_key()

        assert len(replace_calls) == 1
        src, dst = replace_calls[0]
        assert src.endswith(".search.env.tmp")
        assert dst == str(key_file)

    def test_concurrent_race_loses(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """FileExistsError on O_EXCL + .search.env already present → reads existing file."""
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        existing_key = "d" * 64
        _write_key_file(key_file, f"ARCHON_SEARCH_API_KEY={existing_key}\n")
        monkeypatch.setattr(km, "KEY_FILE", key_file)

        real_os_open = os.open
        call_count = 0

        def fake_os_open(path: str, flags: int, mode: int = 0o777) -> int:
            nonlocal call_count
            if ".search.env.tmp" in path and (flags & os.O_EXCL):
                call_count += 1
                raise FileExistsError("injected")
            return real_os_open(path, flags, mode)

        with patch("os.open", side_effect=fake_os_open):
            result = km._generate_and_write()

        assert result == existing_key

    def test_orphaned_tmp(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Orphaned .search.env.tmp + no .search.env → deletes tmp, retries, succeeds."""
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        tmp_file = tmp_path / ".search.env.tmp"
        monkeypatch.setattr(km, "KEY_FILE", key_file)
        # Create orphaned tmp
        tmp_file.write_text("orphaned")

        real_os_open = os.open
        excl_calls: list[int] = [0]

        def fake_os_open(path: str, flags: int, mode: int = 0o777) -> int:
            if ".search.env.tmp" in path and (flags & os.O_EXCL):
                excl_calls[0] += 1
                if excl_calls[0] == 1:
                    raise FileExistsError("injected")
            return real_os_open(path, flags, mode)

        with patch("os.open", side_effect=fake_os_open):
            key = km._generate_and_write()

        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)
        assert not tmp_file.exists()

    def test_key_never_appears_in_logs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        monkeypatch.setattr(km, "KEY_FILE", key_file)
        with caplog.at_level(logging.DEBUG):
            key, _ = km.load_or_generate_key()
        # The key value must not appear in any log record
        for record in caplog.records:
            assert key not in record.getMessage(), f"Key appeared in log: {record.getMessage()}"

    def test_os_replace_failure_cleans_up(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        tmp_file = tmp_path / ".search.env.tmp"
        monkeypatch.setattr(km, "KEY_FILE", key_file)

        def failing_replace(src: str, dst: str) -> None:
            raise OSError("injected replace failure")

        with patch("os.replace", side_effect=failing_replace):
            with pytest.raises(OSError):
                km._generate_and_write()

        # tmp file must have been cleaned up
        assert not tmp_file.exists()

    def test_generate_and_write_exhausted_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Both O_EXCL attempts fail AND _load_from_file returns None → RuntimeError."""
        monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
        key_file = tmp_path / ".search.env"
        monkeypatch.setattr(km, "KEY_FILE", key_file)

        def always_raises(path: str, flags: int, mode: int = 0o777) -> int:
            if flags & os.O_EXCL:
                raise FileExistsError("injected")
            return os.open.__wrapped__(path, flags, mode)  # type: ignore[attr-defined]

        with (
            patch("os.open", side_effect=always_raises),
            patch.object(km, "_load_from_file", return_value=None),
        ):
            with pytest.raises(RuntimeError, match="key generation failed"):
                km._generate_and_write()
