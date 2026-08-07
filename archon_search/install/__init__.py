"""Installer package — install, configure, and manage the search service.

Split into a read-only ``BaseInstaller`` (ABC) with two concrete strategies:
``RealInstaller`` executes destructive actions, ``DryRunInstaller`` stubs them.
Use ``create_installer(config_file, dry_run)`` to obtain the right one.

This ``__init__`` re-exports the entire pre-split top-level surface so that
``from archon_search.install import <anything>`` and
``patch("archon_search.install.<name>")`` keep resolving. The redundant
``as`` aliases mark these as intentional re-exports.
"""
from __future__ import annotations

# Stdlib / external module names re-bound onto the package namespace so tests
# that monkeypatch via ``archon_search.install.<X>`` keep resolving.
import importlib as importlib
import os as os
import shutil as shutil
import subprocess as subprocess
import sys as sys
import threading as threading
import urllib.error
import urllib.request  # noqa: F401 — re-export for archon_search.install.urllib (both submodules)

from archon_search._durable_io import atomic_write_bytes as atomic_write_bytes
from archon_search.config import get_default_config_path as get_default_config_path
from archon_search.config import load_config as load_config
from archon_search.key_manager import get_key_file as get_key_file
from archon_search.key_manager import load_or_generate_key as load_or_generate_key
from archon_search.paths import get_data_dir as get_data_dir
from archon_search.pipeline import create_pipeline as create_pipeline
from archon_search.platform.runtime import get_runtime as get_runtime
from archon_search.platform.runtime import get_search_service as get_search_service

from .config_writer import WizardFeatures as WizardFeatures
from .config_writer import _apply_wizard_features_to_toml as _apply_wizard_features_to_toml
from .config_writer import _assert_features_persisted as _assert_features_persisted
from .config_writer import _detect_config_hand_edits as _detect_config_hand_edits
from .config_writer import _profile_toml as _profile_toml
from .config_writer import _reconcile_ollama_base_url as _reconcile_ollama_base_url
from .config_writer import _revert_graph_enabled_flag as _revert_graph_enabled_flag
from .config_writer import _revert_multilingual_flag as _revert_multilingual_flag
from .config_writer import _write_profile_config as _write_profile_config
from .errors import InstallError as InstallError
from .errors import InstallLockError as InstallLockError
from .errors import NeedsForceDeleteError as NeedsForceDeleteError
from .extras import _PROVIDER_EXTRA as _PROVIDER_EXTRA
from .extras import _install_code_extra as _install_code_extra
from .extras import _install_cuda_torch as _install_cuda_torch
from .extras import _install_extra as _install_extra
from .extras import _install_graph_extra as _install_graph_extra
from .extras import _install_multilingual_extra as _install_multilingual_extra
from .extras import _install_query_expansion_extras as _install_query_expansion_extras
from .extras import _revert_graph_enrichment_flags as _revert_graph_enrichment_flags
from .extras import _revert_query_expansion_flags as _revert_query_expansion_flags
from .installer import _SEARCH_PACKAGES as _SEARCH_PACKAGES
from .installer import _WAIT_FOR_SERVICE_TIMEOUT as _WAIT_FOR_SERVICE_TIMEOUT
from .installer import BaseInstaller as BaseInstaller
from .installer import DryRunInstaller as DryRunInstaller
from .installer import InstallerProtocol as InstallerProtocol
from .installer import RealInstaller as RealInstaller
from .installer import _compute_svc_timeout as _compute_svc_timeout
from .installer import create_installer as create_installer
from .licenses import FASTTEXT_MODEL_URL as FASTTEXT_MODEL_URL
from .licenses import _download_fasttext_model as _download_fasttext_model
from .licenses import _prompt_fasttext_license as _prompt_fasttext_license
from .licenses import _prompt_jina_license as _prompt_jina_license
from .licenses import _requires_jina_license as _requires_jina_license
from .lock import _acquire_install_lock as _acquire_install_lock
from .lock import _install_lock_path as _install_lock_path
from .lock import _pid_is_alive as _pid_is_alive
from .prewarm import _check_disk_space as _check_disk_space
from .prewarm import _check_reinstall_guard as _check_reinstall_guard
from .prewarm import _execute_force_reinstall as _execute_force_reinstall
from .prewarm import _prewarm_models as _prewarm_models
from .prewarm import _prewarm_timeout as _prewarm_timeout
from .render import _KEY_FILE_PLACEHOLDER as _KEY_FILE_PLACEHOLDER
from .render import _PROFILE_CAPS as _PROFILE_CAPS
from .render import _PROFILE_ORDER as _PROFILE_ORDER
from .render import _mask_api_key as _mask_api_key
from .render import _print_next_steps as _print_next_steps
from .render import _render_profile_table as _render_profile_table
from .render import _render_summary as _render_summary
from .service_ops import _create_secrets_env as _create_secrets_env
from .service_ops import _legacy_service_path as _legacy_service_path
from .service_ops import _remove_legacy_service as _remove_legacy_service
from .wizard import _CHOICE_MAP as _CHOICE_MAP
from .wizard import _CLAUDE_MODEL_ALIASES as _CLAUDE_MODEL_ALIASES
from .wizard import _OLLAMA_FETCH_TIMEOUT_SECONDS as _OLLAMA_FETCH_TIMEOUT_SECONDS
from .wizard import _check_claude_cli_present as _check_claude_cli_present
from .wizard import _fetch_llama_cpp_models as _fetch_llama_cpp_models
from .wizard import _fetch_ollama_models as _fetch_ollama_models
from .wizard import _pick_claude_model as _pick_claude_model
from .wizard import _pick_llama_cpp_model as _pick_llama_cpp_model
from .wizard import _pick_ollama_model as _pick_ollama_model
from .wizard import _prompt_gpu_confirm as _prompt_gpu_confirm
from .wizard import _prompt_graph_provider as _prompt_graph_provider
from .wizard import _prompt_llama_cpp_model as _prompt_llama_cpp_model
from .wizard import _prompt_model_freetext as _prompt_model_freetext
from .wizard import _prompt_multilingual as _prompt_multilingual
from .wizard import _prompt_ollama_model as _prompt_ollama_model
from .wizard import _prompt_optional_features as _prompt_optional_features
from .wizard import _prompt_provider as _prompt_provider
from .wizard import _select_profile as _select_profile

__all__ = [
    "InstallError",
    "InstallLockError",
    "InstallerProtocol",
    "NeedsForceDeleteError",
    "WizardFeatures",
    "create_installer",
]
