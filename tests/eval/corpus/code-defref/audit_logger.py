"""Audit logger — also imports and calls validate_token, so validate_token
has TWO callers (auth_gateway and audit_logger). A co-occurrence graph
would associate validate_token with both files equally; a directed
'calls' edge distinguishes the caller relationship precisely.
"""

from token_service import validate_token


def log_privileged_action(request, action_name):
    """Log a privileged action after re-validating the request's token."""
    claims = validate_token(request.get("authorization"))
    return {"actor": claims["sub"], "action": action_name}
