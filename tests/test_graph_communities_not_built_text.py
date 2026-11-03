"""BE-3: the GraphCommunitiesNotBuiltError wire text is a class-constant template (S76)."""

import pytest

from archon_search.pipeline import GraphCommunitiesNotBuiltError

_PREFIX = "No community representatives found for collection "
_SUFFIX = ". Run community detection first."


def test_graph_communities_not_built_text_is_byte_identical() -> None:
    exc = GraphCommunitiesNotBuiltError("docs")

    assert str(exc) == "No community representatives found for collection 'docs'. Run community detection first."


@pytest.mark.parametrize(
    ("name", "quoted"),
    [
        ("it's", '"it\'s"'),
        ('say "hi"', "'say \"hi\"'"),
        ("back\\slash", "'back\\\\slash'"),
    ],
)
def test_text_keeps_the_repr_quoting_of_the_collection_name(name: str, quoted: str) -> None:
    assert str(GraphCommunitiesNotBuiltError(name)) == f"{_PREFIX}{quoted}{_SUFFIX}"


def test_template_rendered_from_the_typed_collection_equals_the_message() -> None:
    exc = GraphCommunitiesNotBuiltError("it's")

    assert exc.collection == "it's"
    assert GraphCommunitiesNotBuiltError.MESSAGE_TEMPLATE.format(collection=exc.collection) == str(exc)
