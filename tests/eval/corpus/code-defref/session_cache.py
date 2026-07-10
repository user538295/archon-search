"""Session cache — second distractor mentioning "token" (a cache eviction
token) with no call to validate_token. Strengthens the co-occurrence trap:
two distractor files use the word "token" without ever calling
validate_token, so a co-occurrence-only graph has two false positives to
choose from, while the directed calls graph has none.
"""


def evict_by_token(cache, eviction_token):
    """Evict a cache entry identified by an opaque authorization eviction
    token — validates the caller's session bearer token format before
    eviction, without ever calling validate_token."""
    return cache.pop(eviction_token, None)
