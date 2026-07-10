"""Auth gateway — entrypoint that calls into token_service.validate_token.

This module and token_service.py never mention each other's vocabulary
outside the actual call: a co-occurrence-only graph (bag-of-entities per
document, no call direction) cannot tell which of several similarly-named
modules actually invokes validate_token — only a directed 'calls' edge
extracted from the AST can answer "who calls validate_token".
"""

from token_service import validate_token


def handle_incoming_request(request):
    """Handle an incoming HTTP request by validating its bearer token."""
    token = request.get("authorization")
    return validate_token(token)
