import pytest
from archon_search.profiles import (
    JINA_RERANKER_MODEL,
    MULTILINGUAL_PROFILES,
    ENGLISH_PROFILES,
    VALID_PROFILE_NAMES,
    get_profile,
)


def test_get_profile_english_minimal():
    profile = get_profile("minimal", multilingual=False)
    assert profile.embedder == "BAAI/bge-small-en-v1.5"
    assert profile.reranker == "Xenova/ms-marco-MiniLM-L-6-v2"
    assert profile.chunk_size == 512


def test_get_profile_multilingual_max():
    profile = get_profile("max", multilingual=True)
    assert profile.embedder == "intfloat/multilingual-e5-large"
    assert profile.reranker == JINA_RERANKER_MODEL


def test_get_profile_minimal_multilingual_has_no_reranker():
    profile = get_profile("minimal", multilingual=True)
    assert profile.reranker is None


def test_get_profile_invalid_name_raises_valueerror():
    with pytest.raises(ValueError):
        get_profile("ultra", multilingual=False)


def test_all_english_profiles_have_reranker():
    for profile in ENGLISH_PROFILES.values():
        assert profile.reranker is not None


def test_multilingual_balanced_and_max_use_jina_reranker():
    assert MULTILINGUAL_PROFILES["balanced"].reranker == JINA_RERANKER_MODEL
    assert MULTILINGUAL_PROFILES["max"].reranker == JINA_RERANKER_MODEL


def test_valid_profile_names_matches_dict_keys():
    assert VALID_PROFILE_NAMES == set(ENGLISH_PROFILES.keys()) == set(MULTILINGUAL_PROFILES.keys())


@pytest.mark.parametrize("name", ["minimal", "balanced", "max"])
@pytest.mark.parametrize("multilingual", [False, True])
def test_get_profile_all_combinations_no_error(name, multilingual):
    profile = get_profile(name, multilingual=multilingual)
    assert profile.name == name
