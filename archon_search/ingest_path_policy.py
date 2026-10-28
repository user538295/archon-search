"""Builds the :class:`~archon_search._path_safety.IngestPathPolicy` an ingest enforces from the server configuration."""
from __future__ import annotations

import logging
from pathlib import Path

from archon_search._path_safety import IngestPathPolicy
from archon_search.config import SearchConfig, get_default_config_path
from archon_search.jobs.model import get_jobs_file
from archon_search.key_manager import KeyStore, get_key_file, get_keys_file
from archon_search.paths import get_data_dir
from archon_search.platform.service import get_secrets_env_file
from archon_search.sync_suppression import get_sync_suppressed_file

logger = logging.getLogger(__name__)


def ingest_path_policy(config: SearchConfig, config_path: Path | None = None) -> IngestPathPolicy:
    """The ingest path policy for *config*.

    Denied: the data directory and the directories the server keeps its own state in (the LanceDB directory, the
    telemetry log directory and the backup directory — they may sit outside the data directory), and the exact
    files holding secrets, the configuration or the server's state (the key file, ``keys.json``, ``.secrets.env``,
    the config file at *config_path* or its default location, the job store file, the sync-suppression sentinel,
    the log file). The data-directory files are listed one by one because a hard link to one of them, placed
    outside every denied directory, is not under *deny_roots* but is still that file's inode.
    ``[ingest].allowed_roots`` becomes the scope a client-supplied path must lie in.
    """
    state_dirs = [get_data_dir(), Path(config.db_path).expanduser(), Path(config.telemetry.log_dir).expanduser()]
    if config.backup.output_dir:
        state_dirs.append(Path(config.backup.output_dir).expanduser())
    state_files = [
        get_key_file(),
        get_keys_file(),
        get_secrets_env_file(),
        get_jobs_file(),
        get_sync_suppressed_file(),
        config_path if config_path is not None else get_default_config_path(),
    ]
    if config.log_file:
        state_files.append(Path(config.log_file).expanduser())
    return IngestPathPolicy(
        deny_roots=tuple(state_dirs),
        deny_files=tuple(state_files),
        allowed_roots=tuple(Path(root).expanduser() for root in config.ingest.allowed_roots),
    )


async def log_if_non_admin_path_ingest_is_refused(config: SearchConfig, key_store: KeyStore) -> None:
    """Log one WARNING when a non-admin key exists but ``[ingest].allowed_roots`` is empty.

    In that state a non-admin key's path-based ingest is refused (``allowed_roots_required``): with no roots the whole
    filesystem is in scope and only the operator's admin key may decide that. Inline ``documents`` are unaffected.
    A server with only the default admin key logs nothing. Keys created later are not announced here; the refusal
    itself names the setting.
    """
    if config.ingest.allowed_roots:
        return
    has_non_admin_key = bool(config.namespaces) or any(not record.admin for record in await key_store.active_keys())
    if has_non_admin_key:
        logger.warning(
            "non-admin API keys exist but [ingest].allowed_roots is empty: path-based ingest is refused for them "
            "(inline documents still work). Set [ingest].allowed_roots to the directories they may ingest from."
        )
