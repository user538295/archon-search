"""License gates (Jina, fasttext) plus the fasttext model download."""
from __future__ import annotations

import logging
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from archon_search.profiles import JINA_RERANKER_MODEL, InstallProfile

from .errors import InstallError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Jina license gate (Task C0-3.2)
# ---------------------------------------------------------------------------

def _requires_jina_license(profile: InstallProfile) -> bool:
    """Return True if *profile* uses the Jina reranker model (CC-BY-NC-4.0)."""
    return profile.reranker == JINA_RERANKER_MODEL


def _prompt_jina_license(non_interactive: bool, accept_jina_license: bool = False) -> None:
    """Print the Jina CC-BY-NC-4.0 warning and gate on user / flag acceptance.

    Raises SystemExit(1) if the license is not accepted.
    """
    print(
        "WARNING: jinaai/jina-reranker-v2-base-multilingual is licensed CC-BY-NC-4.0\n"
        "(non-commercial use only). Commercial use of multilingual profiles 2 and 3\n"
        "requires an alternative reranker. You will be required to confirm license\n"
        "acceptance before this model is downloaded."
    )

    if accept_jina_license:
        return

    if non_interactive:
        print(
            "Non-interactive mode: Jina license automatically declined. "
            "Use an English profile for commercial installs."
        )
        raise SystemExit(1)

    response = input("Type 'accept' to confirm license acceptance and continue, or anything else to abort: ")
    if response.strip().lower() == "accept":
        return
    print("License not accepted. Aborting.")
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# fasttext license gate (Task 4.1)
# ---------------------------------------------------------------------------


def _prompt_fasttext_license(non_interactive: bool, accept_fasttext_license: bool = False) -> None:
    """Print the fasttext CC-BY-SA 3.0 warning and gate on user / flag acceptance.

    Raises SystemExit(1) if the license is not accepted.
    Pattern mirrors _prompt_jina_license exactly.
    """
    print(
        "WARNING: lid.176.ftz (fasttext language identification model) is licensed CC-BY-SA 3.0.\n"
        "This model was created by Facebook Research and redistributed under CC-BY-SA 3.0.\n"
        "You must comply with its terms for any use."
    )

    if accept_fasttext_license:
        return

    if non_interactive:
        print("Non-interactive mode: fasttext license automatically declined.")
        raise SystemExit(1)

    response = input("Type 'accept' to confirm license acceptance and continue, or anything else to abort: ")
    if response.strip().lower() == "accept":
        return
    print("License not accepted. Aborting.")
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# fasttext model download (Task 4.2)
# ---------------------------------------------------------------------------

FASTTEXT_MODEL_URL = "https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz"


def _download_fasttext_model(models_dir: Path) -> None:
    """Download the fasttext language identification model to *models_dir*.

    - Creates *models_dir* (mode 0o700) if absent.
    - No-op if ``lid.176.ftz`` already exists in *models_dir*.
    - Uses ``urllib.request.urlopen`` with an explicit 120-second socket timeout
      instead of ``urlretrieve`` (which has no timeout).
    - Raises ``InstallError`` on network failure or if the downloaded file is empty/corrupt.
    """
    target = models_dir / "lid.176.ftz"

    if target.exists():
        logger.debug("fasttext model already present at %s — skipping download", target)
        return

    models_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    print("[4b/5] Downloading fasttext language model...")

    try:
        with urllib.request.urlopen(FASTTEXT_MODEL_URL, timeout=120) as response:
            with target.open("wb") as out_file:
                shutil.copyfileobj(response, out_file)
    except urllib.error.URLError as exc:
        target.unlink(missing_ok=True)
        raise InstallError(
            f"Failed to download fasttext lid.176.ftz model: {exc}. "
            "Check your network connection and re-run install."
        ) from exc
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise InstallError(
            f"Failed to write fasttext lid.176.ftz model to disk: {exc}. "
            "Check available disk space and permissions."
        ) from exc

    # Validate download
    if target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise InstallError(
            "fasttext model download appears corrupt (empty file); re-run install."
        )
