"""Language detection module — wraps fasttext lid.176.ftz with async support."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

# Module-level constants
FASTTEXT_MODEL_FILENAME = "lid.176.ftz"
FASTTEXT_MODELS_DIR = Path.home() / ".archon-search" / "models"

# Try to import fasttext at module load time so tests can mock it.
# If the package is not installed, `fasttext` will be None and
# LanguageDetector.__init__ will raise RuntimeError on construction.
try:
    import fasttext as fasttext  # type: ignore[import-untyped]  # noqa: PLC0414
except ImportError:
    fasttext = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# ISO 639-3 → ISO 639-1 normalization map
# fasttext outputs mostly ISO 639-1 codes already; this map covers the
# exceptions where fasttext emits a 3-letter code but a 2-letter ISO 639-1
# code exists.
# ---------------------------------------------------------------------------
_FASTTEXT_ISO_MAP: dict[str, str] = {
    "afr": "af",
    "aka": "ak",
    "amh": "am",
    "ara": "ar",
    "aze": "az",
    "bel": "be",
    "ben": "bn",
    "bos": "bs",
    "bul": "bg",
    "cat": "ca",
    "ces": "cs",
    "cym": "cy",
    "dan": "da",
    "deu": "de",
    "ell": "el",
    "eng": "en",
    "epo": "eo",
    "est": "et",
    "eus": "eu",
    "fas": "fa",
    "fin": "fi",
    "fra": "fr",
    "gle": "ga",
    "glg": "gl",
    "guj": "gu",
    "hau": "ha",
    "heb": "he",
    "hin": "hi",
    "hrv": "hr",
    "hun": "hu",
    "hye": "hy",
    "ind": "id",
    "isl": "is",
    "ita": "it",
    "jpn": "ja",
    "kan": "kn",
    "kat": "ka",
    "kaz": "kk",
    "khm": "km",
    "kir": "ky",
    "kor": "ko",
    "lao": "lo",
    "lat": "la",
    "lav": "lv",
    "lit": "lt",
    "lug": "lg",
    "mal": "ml",
    "mar": "mr",
    "mkd": "mk",
    "mlg": "mg",
    "mlt": "mt",
    "mon": "mn",
    "mri": "mi",
    "msa": "ms",
    "mya": "my",
    "nep": "ne",
    "nld": "nl",
    "nor": "no",
    "nya": "ny",
    "pan": "pa",
    "pol": "pl",
    "por": "pt",
    "ron": "ro",
    "rus": "ru",
    "sin": "si",
    "slk": "sk",
    "slv": "sl",
    "sna": "sn",
    "som": "so",
    "sot": "st",
    "spa": "es",
    "srd": "sc",
    "srp": "sr",
    "swa": "sw",
    "swe": "sv",
    "tam": "ta",
    "tel": "te",
    "tgk": "tg",
    "tha": "th",
    "tur": "tr",
    "ukr": "uk",
    "urd": "ur",
    "uzb": "uz",
    "vie": "vi",
    "xho": "xh",
    "yor": "yo",
    "zho": "zh",
    "zul": "zu",
}


def _normalize_lang_code(code: str) -> str:
    """Normalize a fasttext language code to ISO 639-1 (2-letter) if possible.

    Looks up ``code`` in ``_FASTTEXT_ISO_MAP``; returns the 2-letter ISO 639-1
    code if found, otherwise returns ``code`` unchanged (ISO 639-3 passthrough).
    """
    return _FASTTEXT_ISO_MAP.get(code, code)


class LanguageDetector:
    """Async language detector backed by the fasttext ``lid.176.ftz`` model.

    Parameters
    ----------
    model_path:
        Absolute path to the ``lid.176.ftz`` model file.

    Raises
    ------
    RuntimeError
        If ``fasttext-wheel`` is not installed.
    FileNotFoundError
        If ``model_path`` does not exist.
    """

    def __init__(self, model_path: Path) -> None:
        if fasttext is None:
            raise RuntimeError(
                "fasttext-wheel not installed; "
                "run: pip install archon-search[multilingual]"
            )
        if not model_path.exists():
            raise FileNotFoundError(
                f"fasttext model not found at {model_path}; "
                "run: archon-search install --multilingual"
            )
        self._model: Any = fasttext.load_model(str(model_path))

    async def detect(self, text: str, *, confidence_threshold: float) -> str:
        """Detect the language of *text* and return an ISO code or ``"unknown"``.

        Parameters
        ----------
        text:
            Input text.  Truncated to 2000 characters; newlines replaced with
            spaces (fasttext ``predict`` requires single-line input).
        confidence_threshold:
            Minimum top-1 probability required to return a non-``"unknown"``
            result.

        Returns
        -------
        str
            Normalized ISO 639-1 (2-letter) code, ISO 639-3 code (for languages
            without a 2-letter code), or ``"unknown"`` when confidence is below
            *confidence_threshold* or *text* is empty / whitespace-only.
        """
        if not text or not text.strip():
            return "unknown"

        cleaned = text[:2000].replace("\n", " ")
        labels, probabilities = await asyncio.to_thread(
            self._model.predict, cleaned, 1
        )
        if not labels:
            return "unknown"
        label: str = labels[0]
        confidence: float = probabilities[0]

        if confidence < confidence_threshold:
            return "unknown"

        # Strip the "__label__" prefix fasttext prepends to every code.
        raw_code = label.removeprefix("__label__")
        return _normalize_lang_code(raw_code)
