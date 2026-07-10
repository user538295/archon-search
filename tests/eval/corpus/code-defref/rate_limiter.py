"""Rate limiter — distractor module mentioning "token" (a bucket token,
unrelated to auth bearer tokens) but never calling validate_token. Tests
that a co-occurrence match on the word "token" does not falsely answer
"who calls validate_token" — only a real calls edge should.
"""


def consume_token(bucket):
    """Consume one rate-limit token from the bucket, unrelated to auth."""
    if bucket["tokens"] <= 0:
        return False
    bucket["tokens"] -= 1
    return True
