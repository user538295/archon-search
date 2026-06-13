"""Tests for LanguageDetector (Task 3.1)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Test: __init__ raises on missing fasttext package
# ---------------------------------------------------------------------------

def test_detect_model_not_installed(tmp_path: Path) -> None:
    """LanguageDetector.__init__ raises RuntimeError when fasttext not installed."""
    model_path = tmp_path / "lid.176.ftz"
    model_path.touch()

    with patch.dict(sys.modules, {"fasttext": None}):
        # Remove cached module reference to force fresh import attempt
        import importlib
        import archon_search.language_detector as ld_module

        with patch.object(ld_module, "fasttext", None):
            from archon_search.language_detector import LanguageDetector as LD

            with pytest.raises(RuntimeError, match="fasttext-wheel not installed"):
                LD(model_path)


# ---------------------------------------------------------------------------
# Test: __init__ raises on missing model file
# ---------------------------------------------------------------------------

def test_detect_model_file_missing(tmp_path: Path) -> None:
    """LanguageDetector.__init__ raises FileNotFoundError for missing model path."""
    from archon_search.language_detector import LanguageDetector

    missing_path = tmp_path / "nonexistent.ftz"

    with patch("archon_search.language_detector.fasttext") as mock_ft:
        with pytest.raises(FileNotFoundError):
            LanguageDetector(missing_path)


# ---------------------------------------------------------------------------
# Test: detect English text
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_english_text(tmp_path: Path) -> None:
    """detect() returns 'en' for high-confidence English prediction."""
    from archon_search.language_detector import LanguageDetector

    model_path = tmp_path / "lid.176.ftz"
    model_path.touch()

    mock_model = MagicMock()
    mock_model.predict.return_value = (["__label__en"], [0.99])

    with patch("archon_search.language_detector.fasttext") as mock_ft:
        mock_ft.load_model.return_value = mock_model
        detector = LanguageDetector(model_path)

    with patch("asyncio.to_thread", new=AsyncMock(return_value=(["__label__en"], [0.99]))):
        result = await detector.detect("Hello world", confidence_threshold=0.7)

    assert result == "en"


# ---------------------------------------------------------------------------
# Test: detect French text
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_french_text(tmp_path: Path) -> None:
    """detect() returns 'fr' for high-confidence French prediction."""
    from archon_search.language_detector import LanguageDetector

    model_path = tmp_path / "lid.176.ftz"
    model_path.touch()

    mock_model = MagicMock()

    with patch("archon_search.language_detector.fasttext") as mock_ft:
        mock_ft.load_model.return_value = mock_model
        detector = LanguageDetector(model_path)

    with patch("asyncio.to_thread", new=AsyncMock(return_value=(["__label__fr"], [0.95]))):
        result = await detector.detect("Bonjour le monde", confidence_threshold=0.7)

    assert result == "fr"


# ---------------------------------------------------------------------------
# Test: below threshold returns "unknown"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_below_threshold(tmp_path: Path) -> None:
    """detect() returns 'unknown' when confidence is below threshold."""
    from archon_search.language_detector import LanguageDetector

    model_path = tmp_path / "lid.176.ftz"
    model_path.touch()

    mock_model = MagicMock()

    with patch("archon_search.language_detector.fasttext") as mock_ft:
        mock_ft.load_model.return_value = mock_model
        detector = LanguageDetector(model_path)

    with patch("asyncio.to_thread", new=AsyncMock(return_value=(["__label__de"], [0.4]))):
        result = await detector.detect("some text", confidence_threshold=0.7)

    assert result == "unknown"


# ---------------------------------------------------------------------------
# Test: empty/whitespace text returns "unknown" without calling fasttext
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_empty_text(tmp_path: Path) -> None:
    """detect() returns 'unknown' for empty string without calling fasttext."""
    from archon_search.language_detector import LanguageDetector

    model_path = tmp_path / "lid.176.ftz"
    model_path.touch()

    mock_model = MagicMock()

    with patch("archon_search.language_detector.fasttext") as mock_ft:
        mock_ft.load_model.return_value = mock_model
        detector = LanguageDetector(model_path)

    with patch("asyncio.to_thread", new=AsyncMock()) as mock_thread:
        result_empty = await detector.detect("", confidence_threshold=0.7)
        result_whitespace = await detector.detect("   ", confidence_threshold=0.7)

    assert result_empty == "unknown"
    assert result_whitespace == "unknown"
    mock_thread.assert_not_called()


# ---------------------------------------------------------------------------
# Test: _normalize_lang_code passthrough (already 2-letter)
# ---------------------------------------------------------------------------

def test_normalize_lang_code_passthrough() -> None:
    """_normalize_lang_code returns input unchanged if already 2-letter."""
    from archon_search.language_detector import _normalize_lang_code

    assert _normalize_lang_code("fr") == "fr"
    assert _normalize_lang_code("en") == "en"
    assert _normalize_lang_code("de") == "de"


# ---------------------------------------------------------------------------
# Test: _normalize_lang_code 3-letter to 2-letter
# ---------------------------------------------------------------------------

def test_normalize_lang_code_3_to_2() -> None:
    """_normalize_lang_code maps 'fra' → 'fr' (3-letter to ISO 639-1)."""
    from archon_search.language_detector import _normalize_lang_code

    assert _normalize_lang_code("fra") == "fr"
    assert _normalize_lang_code("deu") == "de"


# ---------------------------------------------------------------------------
# Test: _normalize_lang_code unknown 3-letter passthrough
# ---------------------------------------------------------------------------

def test_normalize_lang_code_unknown_3letter() -> None:
    """_normalize_lang_code returns raw code for unmapped 3-letter codes."""
    from archon_search.language_detector import _normalize_lang_code

    assert _normalize_lang_code("xxx") == "xxx"


# ---------------------------------------------------------------------------
# Test: detect runs in thread (asyncio.to_thread called)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_runs_in_thread(tmp_path: Path) -> None:
    """detect() calls asyncio.to_thread to avoid blocking the event loop."""
    from archon_search.language_detector import LanguageDetector

    model_path = tmp_path / "lid.176.ftz"
    model_path.touch()

    mock_model = MagicMock()

    with patch("archon_search.language_detector.fasttext") as mock_ft:
        mock_ft.load_model.return_value = mock_model
        detector = LanguageDetector(model_path)

    with patch("asyncio.to_thread", new=AsyncMock(return_value=(["__label__en"], [0.99]))) as mock_thread:
        await detector.detect("Hello world", confidence_threshold=0.7)

    mock_thread.assert_called_once()


# ---------------------------------------------------------------------------
# Test: detect strips newlines before predict
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_strips_newlines(tmp_path: Path) -> None:
    """detect() replaces newlines with spaces before passing to fasttext."""
    from archon_search.language_detector import LanguageDetector

    model_path = tmp_path / "lid.176.ftz"
    model_path.touch()

    mock_model = MagicMock()

    with patch("archon_search.language_detector.fasttext") as mock_ft:
        mock_ft.load_model.return_value = mock_model
        detector = LanguageDetector(model_path)

    captured_args: list = []

    async def capture_thread(fn, text, k):
        captured_args.append(text)
        return (["__label__fr"], [0.95])

    with patch("asyncio.to_thread", side_effect=capture_thread):
        await detector.detect("bonjour\nmonde", confidence_threshold=0.7)

    assert "\n" not in captured_args[0]
    assert "bonjour monde" in captured_args[0]


# ---------------------------------------------------------------------------
# Test: detect truncates long text to 2000 chars
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_truncates_long_text(tmp_path: Path) -> None:
    """detect() truncates input to at most 2000 chars before calling fasttext."""
    from archon_search.language_detector import LanguageDetector

    model_path = tmp_path / "lid.176.ftz"
    model_path.touch()

    mock_model = MagicMock()

    with patch("archon_search.language_detector.fasttext") as mock_ft:
        mock_ft.load_model.return_value = mock_model
        detector = LanguageDetector(model_path)

    long_text = "a" * 5000
    captured_args: list = []

    async def capture_thread(fn, text, k):
        captured_args.append(text)
        return (["__label__en"], [0.99])

    with patch("asyncio.to_thread", side_effect=capture_thread):
        await detector.detect(long_text, confidence_threshold=0.7)

    assert len(captured_args[0]) <= 2000


# ---------------------------------------------------------------------------
# Test: detect handles empty predictions from fasttext (defensive guard)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_empty_predictions_returns_unknown(tmp_path: Path) -> None:
    """detect() returns 'unknown' if fasttext predict returns empty labels list."""
    from archon_search.language_detector import LanguageDetector

    model_path = tmp_path / "lid.176.ftz"
    model_path.touch()

    mock_model = MagicMock()

    with patch("archon_search.language_detector.fasttext") as mock_ft:
        mock_ft.load_model.return_value = mock_model
        detector = LanguageDetector(model_path)

    # fasttext returns ([], ()) when no predictions meet threshold
    with patch("asyncio.to_thread", new=AsyncMock(return_value=([], []))):
        result = await detector.detect("some text", confidence_threshold=0.7)

    assert result == "unknown"


# ---------------------------------------------------------------------------
# Test: "mak" (Makasar) is NOT in the map (must not be mis-mapped to "mk")
# ---------------------------------------------------------------------------

def test_normalize_lang_code_mak_is_not_macedonian() -> None:
    """mak (Makasar) must not be mapped to mk (Macedonian)."""
    from archon_search.language_detector import _normalize_lang_code, _FASTTEXT_ISO_MAP

    assert "mak" not in _FASTTEXT_ISO_MAP, (
        "'mak' is Makasar (Indonesia), not Macedonian; must not map to 'mk'"
    )
    # mkd (Macedonian) should still map correctly
    assert _normalize_lang_code("mkd") == "mk"


# ---------------------------------------------------------------------------
# Test: module constants are defined
# ---------------------------------------------------------------------------

@pytest.mark.archon_unset_data_dir
def test_module_constants() -> None:
    """FASTTEXT_MODEL_FILENAME is defined and get_fasttext_models_dir() resolves to the default."""
    from archon_search.language_detector import (
        FASTTEXT_MODEL_FILENAME,
        get_fasttext_models_dir,
    )

    assert FASTTEXT_MODEL_FILENAME == "lid.176.ftz"
    assert get_fasttext_models_dir() == Path.home() / ".archon-search" / "models"
