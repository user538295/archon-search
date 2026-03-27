"""Tests for archon/rag/sync.py — path_to_collection_name and RagCollectionSync."""
from archon.rag.sync import path_to_collection_name


class TestPathToCollectionName:
    def test_path_to_collection_name_basic(self):
        """~/.archon/history/sessions → 'sessions'"""
        assert path_to_collection_name("~/.archon/history/sessions") == "sessions"

    def test_path_to_collection_name_sanitizes_special_chars(self):
        """Spaces and dots are replaced with underscores, collapsed, and stripped."""
        result = path_to_collection_name("/some/path/my docs.v2")
        assert result == "my_docs_v2"

    def test_path_to_collection_name_empty_component_fallback(self):
        """Root path (or a path whose last component is empty after sanitization) returns 'collection'."""
        # A path whose basename consists entirely of non-alphanumeric chars
        result = path_to_collection_name("/some/path/---")
        assert result == "collection"

    def test_path_to_collection_name_workspace(self):
        """~/.archon/workspace → 'workspace'"""
        assert path_to_collection_name("~/.archon/workspace") == "workspace"

    def test_path_to_collection_name_lowercase(self):
        """Result is always lowercased."""
        result = path_to_collection_name("/some/path/MyDocs")
        assert result == "mydocs"

    def test_path_to_collection_name_collapses_multiple_underscores(self):
        """Multiple consecutive non-alphanumeric chars collapse to a single underscore."""
        result = path_to_collection_name("/some/path/my--docs")
        assert result == "my_docs"

    def test_path_to_collection_name_root_path_fallback(self):
        """Root path '/' has empty basename — should return 'collection'."""
        assert path_to_collection_name("/") == "collection"

    def test_path_to_collection_name_numeric_basename(self):
        """Digits survive sanitization."""
        assert path_to_collection_name("/data/12345") == "12345"

    def test_path_to_collection_name_trailing_slash(self):
        """Trailing slash is stripped by pathlib; result same as without slash."""
        assert path_to_collection_name("/some/sessions/") == "sessions"
