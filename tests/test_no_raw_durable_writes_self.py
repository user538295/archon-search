"""Self-tests for the durable-write gate's logic, run against synthetic
fixtures (NOT the real tree) so the gate's detection/allow-listing rules are
exercised in isolation without recursion against production source."""

from pathlib import Path

from tests.test_no_raw_durable_writes import find_violations


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_helper_file_is_excluded(tmp_path: Path) -> None:
    """A file named _durable_io.py is the sanctioned home and is skipped."""
    _write(
        tmp_path / "_durable_io.py",
        "import os\n\n\ndef move(a, b):\n    os.replace(a, b)\n",
    )
    violations = find_violations(tmp_path)
    assert violations == [], violations


def test_raw_write_text_is_reported(tmp_path: Path) -> None:
    """A raw path.write_text(...) without a noqa is flagged."""
    _write(
        tmp_path / "mod.py",
        "from pathlib import Path\n\n\ndef save(path: Path, data):\n"
        "    path.write_text(data)\n",
    )
    violations = find_violations(tmp_path)
    assert len(violations) == 1, violations
    rel, lineno, line = violations[0]
    assert rel == "mod.py"
    assert lineno == 5
    assert "write_text" in line


def test_noqa_suppresses_violation(tmp_path: Path) -> None:
    """The same line with a durable-write noqa is not reported."""
    _write(
        tmp_path / "mod.py",
        "from pathlib import Path\n\n\ndef save(path: Path, data):\n"
        "    path.write_text(data)  # noqa: durable-write\n",
    )
    violations = find_violations(tmp_path)
    assert violations == [], violations


def test_os_open_o_creat_split_across_lines_is_reported(tmp_path: Path) -> None:
    """The AST detector catches os.open(..., O_CREAT, ...) wrapped over lines."""
    _write(
        tmp_path / "mod.py",
        "import os\n\n\ndef save(path, data):\n"
        "    fd = os.open(\n"
        "        path,\n"
        "        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,\n"
        "        0o600,\n"
        "    )\n"
        "    return fd\n",
    )
    violations = find_violations(tmp_path)
    assert len(violations) == 1, violations
    rel, lineno, _line = violations[0]
    assert rel == "mod.py"
    # Reported at the call's lineno (the os.open( line).
    assert lineno == 5


def test_os_open_o_creat_noqa_on_token_line_suppresses(tmp_path: Path) -> None:
    """A noqa on the O_CREAT line suppresses the split os.open violation."""
    _write(
        tmp_path / "mod.py",
        "import os\n\n\ndef save(path, data):\n"
        "    fd = os.open(\n"
        "        path,\n"
        "        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,  # noqa: durable-write\n"
        "        0o600,\n"
        "    )\n"
        "    return fd\n",
    )
    violations = find_violations(tmp_path)
    assert violations == [], violations


def test_os_open_o_creat_noqa_on_call_line_suppresses(tmp_path: Path) -> None:
    """A noqa on the os.open( call line also suppresses the violation."""
    _write(
        tmp_path / "mod.py",
        "import os\n\n\ndef save(path, data):\n"
        "    fd = os.open(  # noqa: durable-write\n"
        "        path,\n"
        "        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,\n"
        "        0o600,\n"
        "    )\n"
        "    return fd\n",
    )
    violations = find_violations(tmp_path)
    assert violations == [], violations
