"""Token service — defines validate_token, called by auth_gateway.py.

No back-reference to auth_gateway here; the only link between the two
files is the directed calls edge auth_gateway -> validate_token.
"""


def validate_token(token):
    """Validate a bearer token and return the decoded claims."""
    if not token:
        raise ValueError("missing token")
    return {"sub": token.split(".")[0]}
