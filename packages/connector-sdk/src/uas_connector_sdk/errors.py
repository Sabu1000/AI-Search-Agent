"""Public, sanitized connector error taxonomy."""

from __future__ import annotations


class ConnectorError(Exception):
    """Base error safe for orchestration code to classify."""

    code = "connector_error"
    retryable = False

    def __init__(self, message: str = "Connector operation failed") -> None:
        super().__init__(message)
        self.public_message = message


class AuthenticationError(ConnectorError):
    code = "authentication_failed"


class PermissionDeniedError(ConnectorError):
    code = "permission_denied"


class RateLimitError(ConnectorError):
    code = "rate_limited"
    retryable = True

    def __init__(self, retry_after_seconds: float | None = None) -> None:
        super().__init__("Provider rate limit reached")
        self.retry_after_seconds = retry_after_seconds


class ProviderUnavailableError(ConnectorError):
    code = "provider_unavailable"
    retryable = True


class MalformedItemError(ConnectorError):
    code = "malformed_item"


class CursorInvalidError(ConnectorError):
    code = "cursor_invalid"


class ContractViolationError(ConnectorError):
    code = "contract_violation"
