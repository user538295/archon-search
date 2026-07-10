"""Notification dispatch queue worker — inherits BaseService and calls
validate_token as its only connection to the auth vocabulary; deliberately
weak lexical overlap with auth-domain query terms so it only surfaces via
the directed calls/inherits graph, not lexical or vector similarity.
"""

from base_service import BaseService
from token_service import validate_token


class NotificationService(BaseService):
    """Delivers queued push notifications to mobile devices."""

    def startup(self):
        self.ready = True

    def shutdown(self):
        self.ready = False

    def send(self, request, message):
        validate_token(request.get("header"))
        return {"delivered": message}
