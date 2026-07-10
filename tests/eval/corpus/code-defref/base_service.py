"""Base service — defines a base class inherited by NotificationService.

Tests the 'inherits' edge: "what breaks if I change BaseService" should
resolve to NotificationService via a directed inherits edge, not via
prose co-occurrence.
"""


class BaseService:
    """Common lifecycle hooks shared by all services."""

    def startup(self):
        raise NotImplementedError

    def shutdown(self):
        raise NotImplementedError
