"""The ingest path policy on real files in tmp dirs (SecReview #3): deny list, ``allowed_roots``, symlink escapes."""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from archon_search import _path_safety
from archon_search._path_safety import (
    ALLOWED_ROOTS_REQUIRED,
    DENIED_LOCATION,
    OUTSIDE_ALLOWED_ROOTS,
    IngestPathPolicy,
    PathUnsafeError,
    unsafe_path_detail,
    validate_glob_pattern,
    validate_ingest_source,
)
from archon_search.config import SearchConfig
from archon_search.ingest_path_policy import ingest_path_policy


def _write(path: Path, text: str = "content") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(directory))
    monkeypatch.delenv("ARCHON_SEARCH_KEY_FILE", raising=False)
    monkeypatch.delenv("ARCHON_SEARCH_CONFIG", raising=False)
    return directory


@pytest.fixture
def policy(data_dir: Path) -> IngestPathPolicy:
    config = SearchConfig()
    config.db_path = str(data_dir / "search")
    config.telemetry.log_dir = str(data_dir / "search-logs")
    return ingest_path_policy(config)


def _denied(policy: IngestPathPolicy, path: Path, *, admin: bool = True) -> str | None:
    try:
        validate_ingest_source(str(path), policy, admin=admin)
    except PathUnsafeError as exc:
        return exc.reason
    return None


# ---------------------------------------------------------------------------
# The always-on deny list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [".search.env", ".secrets.env", "keys.json", "archon-search.toml", "jobs.json", "logs/archon-search.log", "search/t.lance"],
)
def test_data_dir_files_are_denied(policy: IngestPathPolicy, data_dir: Path, relative: str) -> None:
    secret = _write(data_dir / relative)

    assert _denied(policy, secret) == DENIED_LOCATION


def test_data_dir_itself_is_denied(policy: IngestPathPolicy, data_dir: Path) -> None:
    assert _denied(policy, data_dir) == DENIED_LOCATION


def test_a_file_that_does_not_exist_under_the_data_dir_is_denied_the_same_way(
    policy: IngestPathPolicy, data_dir: Path
) -> None:
    """No existence oracle: the refusal does not depend on whether the denied path exists."""
    assert _denied(policy, data_dir / "never-created.txt") == DENIED_LOCATION


def test_state_directories_outside_the_data_dir_are_denied(tmp_path: Path, data_dir: Path) -> None:
    config = SearchConfig()
    config.db_path = str(tmp_path / "elsewhere-db")
    config.backup.output_dir = str(tmp_path / "elsewhere-backups")
    config.telemetry.log_dir = str(tmp_path / "elsewhere-logs")
    state_policy = ingest_path_policy(config)
    for directory in ("elsewhere-db", "elsewhere-backups", "elsewhere-logs"):
        _write(tmp_path / directory / "file.txt")

        assert _denied(state_policy, tmp_path / directory / "file.txt") == DENIED_LOCATION


def test_a_key_file_and_config_file_outside_the_data_dir_are_denied(
    tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file = _write(tmp_path / "keys" / "api.env", "ARCHON_SEARCH_API_KEY=" + "a" * 64)
    config_file = _write(tmp_path / "etc" / "archon.toml", "[namespaces]\n")
    monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", str(key_file))
    monkeypatch.setenv("ARCHON_SEARCH_CONFIG", str(config_file))
    other_policy = ingest_path_policy(SearchConfig())

    assert _denied(other_policy, key_file) == DENIED_LOCATION
    assert _denied(other_policy, config_file) == DENIED_LOCATION
    assert _denied(other_policy, _write(tmp_path / "etc" / "notes.md")) is None


@pytest.mark.parametrize("system_path", ["/dev/null", "/dev/zero"])
def test_device_files_are_denied(policy: IngestPathPolicy, system_path: str) -> None:
    assert _denied(policy, Path(system_path)) == DENIED_LOCATION


@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="no /proc on this platform")
def test_proc_environ_is_denied(policy: IngestPathPolicy) -> None:
    assert _denied(policy, Path("/proc/self/environ")) == DENIED_LOCATION


@pytest.mark.parametrize(
    "relative",
    [
        ".ssh/id_ed25519",
        ".ssh/config",
        ".aws/credentials",
        ".gnupg/pubring.kbx",
        "id_rsa",
        "id_rsa.pub",
        "id_ed25519",
        ".env",
        ".env.production",
        "tls/server.pem",
        ".netrc",
        ".SSH/config",
        ".ENV",
        "Server.PEM",
    ],
)
def test_credential_names_are_denied_anywhere(policy: IngestPathPolicy, tmp_path: Path, relative: str) -> None:
    assert _denied(policy, _write(tmp_path / "home" / relative)) == DENIED_LOCATION


@pytest.mark.parametrize("relative", ["notes.md", "docs/readme.txt", ".environment-notes.md", "keyboard.md", "pem.txt"])
def test_ordinary_documents_are_allowed(policy: IngestPathPolicy, tmp_path: Path, relative: str) -> None:
    assert _denied(policy, _write(tmp_path / "docs" / relative)) is None


def test_a_dot_env_directory_is_not_a_secret_file(policy: IngestPathPolicy, tmp_path: Path) -> None:
    """A virtualenv named ``.env`` holds ordinary files: only a *file* named ``.env`` is a credential."""
    assert _denied(policy, _write(tmp_path / "project" / ".env" / "lib" / "notes.md")) is None


# ---------------------------------------------------------------------------
# Symlink and hard-link escapes
# ---------------------------------------------------------------------------


def test_a_symlink_into_the_data_dir_is_denied(policy: IngestPathPolicy, data_dir: Path, tmp_path: Path) -> None:
    secret = _write(data_dir / ".search.env", "ARCHON_SEARCH_API_KEY=" + "b" * 64)
    link = tmp_path / "docs" / "innocent.md"
    link.parent.mkdir()
    link.symlink_to(secret)

    assert _denied(policy, link) == DENIED_LOCATION


def test_a_symlinked_directory_into_the_data_dir_is_denied(policy: IngestPathPolicy, data_dir: Path, tmp_path: Path) -> None:
    _write(data_dir / "notes.md")
    link = tmp_path / "docs-link"
    link.symlink_to(data_dir, target_is_directory=True)

    assert _denied(policy, link / "notes.md") == DENIED_LOCATION
    assert _denied(policy, link) == DENIED_LOCATION


def test_a_symlink_to_a_credential_file_is_denied(policy: IngestPathPolicy, tmp_path: Path) -> None:
    key = _write(tmp_path / "home" / ".ssh" / "id_rsa")
    link = _write(tmp_path / "docs" / "placeholder.md")
    link.unlink()
    link.symlink_to(key)

    assert _denied(policy, link) == DENIED_LOCATION


def test_a_hard_link_to_a_denied_file_is_denied(policy: IngestPathPolicy, data_dir: Path, tmp_path: Path) -> None:
    secret = _write(data_dir / ".search.env", "ARCHON_SEARCH_API_KEY=" + "c" * 64)
    hard = tmp_path / "docs" / "copy.md"
    hard.parent.mkdir()
    os.link(secret, hard)

    assert _denied(policy, hard) == DENIED_LOCATION


# ---------------------------------------------------------------------------
# [ingest].allowed_roots
# ---------------------------------------------------------------------------


@pytest.fixture
def scoped(tmp_path: Path, data_dir: Path) -> IngestPathPolicy:
    config = SearchConfig()
    config.ingest.allowed_roots = [str(tmp_path / "corpus")]
    (tmp_path / "corpus").mkdir()
    return ingest_path_policy(config)


def test_a_path_inside_an_allowed_root_passes(scoped: IngestPathPolicy, tmp_path: Path) -> None:
    assert _denied(scoped, _write(tmp_path / "corpus" / "sub" / "a.md")) is None
    assert _denied(scoped, tmp_path / "corpus") is None


def test_a_path_outside_every_allowed_root_is_refused(scoped: IngestPathPolicy, tmp_path: Path) -> None:
    assert _denied(scoped, _write(tmp_path / "other" / "a.md")) == OUTSIDE_ALLOWED_ROOTS


def test_a_sibling_sharing_the_root_name_prefix_is_refused(scoped: IngestPathPolicy, tmp_path: Path) -> None:
    assert _denied(scoped, _write(tmp_path / "corpus-private" / "a.md")) == OUTSIDE_ALLOWED_ROOTS


def test_a_symlink_inside_a_root_pointing_outside_is_refused(scoped: IngestPathPolicy, tmp_path: Path) -> None:
    outside = _write(tmp_path / "other" / "secret.md")
    link = tmp_path / "corpus" / "escape.md"
    link.symlink_to(outside)

    assert _denied(scoped, link) == OUTSIDE_ALLOWED_ROOTS


def test_the_deny_list_still_applies_inside_an_allowed_root(scoped: IngestPathPolicy, tmp_path: Path) -> None:
    assert _denied(scoped, _write(tmp_path / "corpus" / ".env")) == DENIED_LOCATION


def test_no_allowed_roots_means_no_scope_restriction(policy: IngestPathPolicy, tmp_path: Path) -> None:
    assert _denied(policy, _write(tmp_path / "anywhere" / "a.md")) is None



# ---------------------------------------------------------------------------
# H4: the widened always-on deny list (system secrets, CLI/cloud credentials, browser profiles)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not Path("/etc/passwd").exists(), reason="no /etc/passwd on this platform")
def test_etc_passwd_is_denied(policy: IngestPathPolicy) -> None:
    assert _denied(policy, Path("/etc/passwd")) == DENIED_LOCATION


def test_system_secret_files_and_directories_are_denied_by_identity(
    policy: IngestPathPolicy, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The system list is compared by file identity, so a symlink to ``/etc/shadow`` is ``/etc/shadow``."""
    shadow = _write(tmp_path / "fake-etc" / "shadow")
    private_dir = tmp_path / "fake-etc" / "ssl-private"
    _write(private_dir / "server-cert-material")
    root_home = tmp_path / "fake-root"
    _write(root_home / "notes.md")
    monkeypatch.setattr(_path_safety, "_SYSTEM_DENY_FILES", (shadow,))
    monkeypatch.setattr(_path_safety, "_SYSTEM_DENY_DIRS", (private_dir, root_home))
    link = tmp_path / "innocent.txt"
    link.symlink_to(shadow)

    assert _denied(policy, shadow) == DENIED_LOCATION
    assert _denied(policy, link) == DENIED_LOCATION
    assert _denied(policy, private_dir / "server-cert-material") == DENIED_LOCATION
    assert _denied(policy, root_home / "notes.md") == DENIED_LOCATION
    assert _denied(policy, _write(tmp_path / "fake-etc" / "hostname")) is None


@pytest.mark.parametrize(
    "relative",
    [
        ".kube/config",
        ".docker/config.json",
        ".azure/accessTokens.json",
        ".config/gcloud/credentials.db",
        ".config/gh/hosts.yml",
        ".oci/config",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        ".pgpass",
        ".NPMRC",
        "Library/Application Support/Google/Chrome/Default/Login Data",
        "Library/Keychains/login.keychain-db",
        ".mozilla/firefox/abc.default/logins.json",
        ".config/google-chrome/Default/Cookies",
        ".config/BraveSoftware/Brave-Browser/Default/Login Data",
    ],
)
def test_cli_cloud_and_browser_credentials_are_denied(policy: IngestPathPolicy, tmp_path: Path, relative: str) -> None:
    assert _denied(policy, _write(tmp_path / "home" / "me" / relative)) == DENIED_LOCATION


@pytest.mark.parametrize(
    "relative",
    [".config/app/settings.json", "Library/Application Support/Notes/a.md", "docs/docker-config.md", "docs/kube.md"],
)
def test_neighbours_of_the_credential_directories_stay_allowed(
    policy: IngestPathPolicy, tmp_path: Path, relative: str
) -> None:
    assert _denied(policy, _write(tmp_path / "home" / "me" / relative)) is None


# ---------------------------------------------------------------------------
# L2: key-like names are denied; ``*.key`` only when the file is a PEM (a Keynote deck is not a private key)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["identity.p12", "identity.PFX", "store.keystore", "store.jks", "deploy_rsa", "backup_rsa.pub", "id_work", "id_x.txt"],
)
def test_key_like_names_are_denied_whatever_their_content(policy: IngestPathPolicy, tmp_path: Path, name: str) -> None:
    assert _denied(policy, _write(tmp_path / "home" / name, "not a pem")) == DENIED_LOCATION


@pytest.mark.parametrize(
    "pem_header",
    ["-----BEGIN PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----", "-----BEGIN OPENSSH PRIVATE KEY-----", "-----BEGIN CERTIFICATE-----"],
)
def test_a_dot_key_file_with_a_pem_header_is_denied(policy: IngestPathPolicy, tmp_path: Path, pem_header: str) -> None:
    key = _write(tmp_path / "tls" / "server.key", f"{pem_header}\nMIIEvQIBADANBgkq\n")

    assert _denied(policy, key) == DENIED_LOCATION


def test_a_dot_key_file_with_leading_whitespace_before_the_pem_header_is_denied(
    policy: IngestPathPolicy, tmp_path: Path
) -> None:
    assert _denied(policy, _write(tmp_path / "tls" / "padded.KEY", "\n  -----BEGIN PRIVATE KEY-----\nabc")) == DENIED_LOCATION


def test_a_keynote_deck_named_dot_key_is_allowed(policy: IngestPathPolicy, tmp_path: Path) -> None:
    deck = tmp_path / "talks" / "keynote.key"
    deck.parent.mkdir(parents=True)
    deck.write_bytes(b"PK\x03\x04" + bytes(range(256)))

    assert _denied(policy, deck) is None


def test_a_text_file_named_dot_key_without_a_pem_header_is_allowed(policy: IngestPathPolicy, tmp_path: Path) -> None:
    assert _denied(policy, _write(tmp_path / "talks" / "outline.key", "Slide 1: intro")) is None


def test_a_directory_named_dot_key_is_allowed_and_a_pem_inside_it_is_not(
    policy: IngestPathPolicy, tmp_path: Path
) -> None:
    """A Keynote bundle is a directory; its files are judged by their own names and content."""
    bundle = tmp_path / "talks" / "deck.key"
    _write(bundle / "Index" / "Document.iwa", "slides")
    _write(bundle / "Data" / "leaf.pem", "x")

    assert _denied(policy, bundle) is None
    assert _denied(policy, bundle / "Index" / "Document.iwa") is None
    assert _denied(policy, bundle / "Data" / "leaf.pem") == DENIED_LOCATION


def test_an_unreadable_dot_key_file_is_denied(policy: IngestPathPolicy, tmp_path: Path) -> None:
    key = _write(tmp_path / "tls" / "locked.key", "-----BEGIN PRIVATE KEY-----")
    key.chmod(0)
    try:
        if os.access(key, os.R_OK):
            pytest.skip("running with privileges that ignore file modes")
        assert _denied(policy, key) == DENIED_LOCATION
    finally:
        key.chmod(0o600)


def test_a_dot_key_path_that_does_not_exist_is_not_a_secret(policy: IngestPathPolicy, tmp_path: Path) -> None:
    """Nothing to leak, and the ingest answers ``not found`` as for any other missing path."""
    assert _denied(policy, tmp_path / "missing.key") is None


def test_an_out_of_scope_pem_key_is_refused_like_any_out_of_scope_path(scoped: IngestPathPolicy, tmp_path: Path) -> None:
    """The PEM probe must not run before the scope check: no existence/content oracle for outside files."""
    pem = _write(tmp_path / "other" / "server.key", "-----BEGIN PRIVATE KEY-----\n")
    plain = _write(tmp_path / "other" / "deck.key", "Slide 1")

    assert _denied(scoped, pem) == OUTSIDE_ALLOWED_ROOTS
    assert _denied(scoped, plain) == OUTSIDE_ALLOWED_ROOTS
    assert _denied(scoped, tmp_path / "other" / "missing.key") == OUTSIDE_ALLOWED_ROOTS


def test_a_pem_key_is_not_probed_for_a_non_admin_caller_without_allowed_roots(
    policy: IngestPathPolicy, tmp_path: Path
) -> None:
    pem = _write(tmp_path / "tls" / "server.key", "-----BEGIN PRIVATE KEY-----\n")

    assert _denied(policy, pem, admin=False) == ALLOWED_ROOTS_REQUIRED


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
def test_a_fifo_named_dot_key_does_not_block_the_probe(policy: IngestPathPolicy, tmp_path: Path) -> None:
    fifo = tmp_path / "pipe.key"
    os.mkfifo(fifo)
    outcome: list[str | None] = []
    worker = threading.Thread(target=lambda: outcome.append(_denied(policy, fifo)), daemon=True)
    worker.start()
    worker.join(timeout=5)
    if worker.is_alive():
        # Release the blocked open so the thread does not outlive the test.
        os.close(os.open(fifo, os.O_WRONLY))
        pytest.fail("the *.key probe hung on a FIFO")
    assert outcome == [DENIED_LOCATION]


# ---------------------------------------------------------------------------
# H4: path-based ingest by a non-admin caller needs ``[ingest].allowed_roots``
# ---------------------------------------------------------------------------


def test_a_non_admin_caller_cannot_ingest_a_path_while_allowed_roots_is_empty(
    policy: IngestPathPolicy, tmp_path: Path
) -> None:
    document = _write(tmp_path / "docs" / "a.md")

    assert _denied(policy, document, admin=False) == ALLOWED_ROOTS_REQUIRED


def test_an_admin_caller_can_ingest_a_path_while_allowed_roots_is_empty(policy: IngestPathPolicy, tmp_path: Path) -> None:
    assert _denied(policy, _write(tmp_path / "docs" / "a.md"), admin=True) is None


def test_a_non_admin_caller_ingests_inside_the_allowed_roots_and_not_outside(
    scoped: IngestPathPolicy, tmp_path: Path
) -> None:
    assert _denied(scoped, _write(tmp_path / "corpus" / "a.md"), admin=False) is None
    assert _denied(scoped, _write(tmp_path / "other" / "a.md"), admin=False) == OUTSIDE_ALLOWED_ROOTS


def test_a_secret_is_denied_before_the_missing_allowed_roots_are_reported(
    policy: IngestPathPolicy, tmp_path: Path
) -> None:
    assert _denied(policy, _write(tmp_path / "home" / ".env"), admin=False) == DENIED_LOCATION


def test_the_missing_allowed_roots_detail_is_fixed_and_names_the_setting() -> None:
    detail = unsafe_path_detail(ALLOWED_ROOTS_REQUIRED)

    assert detail.startswith("path is unsafe:")
    assert "[ingest].allowed_roots" in detail
    assert unsafe_path_detail("empty") == "path is unsafe: empty"

# ---------------------------------------------------------------------------
# glob patterns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern", ["**/*", "*.md", "docs/**/*.txt", "**/src/**/*.py"])
def test_safe_glob_patterns_pass(pattern: str) -> None:
    assert validate_glob_pattern(pattern) == pattern


@pytest.mark.parametrize(
    ("pattern", "reason"),
    [
        ("", "glob_empty"),
        ("   ", "glob_empty"),
        ("/etc/*", "glob_absolute"),
        ("**/../../*", "contains_dotdot"),
        ("../*", "contains_dotdot"),
        ("a\x00b", "nul_byte"),
        ("x" * 300, "glob_too_long"),
        ("/".join(["**"] * 5) + "/*", "glob_too_recursive"),
    ],
)
def test_unsafe_glob_patterns_are_refused(pattern: str, reason: str) -> None:
    with pytest.raises(PathUnsafeError) as raised:
        validate_glob_pattern(pattern)

    assert raised.value.reason == reason
