"""Public connector SDK API."""

from .contract import validate_change_stream
from .errors import (
    AuthenticationError,
    ConnectorError,
    ContractViolationError,
    CursorInvalidError,
    MalformedItemError,
    PermissionDeniedError,
    ProviderUnavailableError,
    RateLimitError,
)
from .models import (
    AccessMetadata,
    Change,
    Credentials,
    CursorAdvanced,
    DeleteSource,
    HealthResult,
    HealthStatus,
    JsonObject,
    NormalizedDocument,
    PermissionChanged,
    Provider,
    RawItem,
    SyncContext,
    UpsertSource,
    canonical_json,
    make_change_id,
    stable_hash,
)
from .protocol import Connector
from .registry import ConnectorFactory, ConnectorRegistry
from .retry import RetryPolicy, with_retry

__all__ = [
    "AccessMetadata",
    "AuthenticationError",
    "Change",
    "Connector",
    "ConnectorError",
    "ConnectorFactory",
    "ConnectorRegistry",
    "ContractViolationError",
    "Credentials",
    "CursorAdvanced",
    "CursorInvalidError",
    "DeleteSource",
    "HealthResult",
    "HealthStatus",
    "JsonObject",
    "MalformedItemError",
    "NormalizedDocument",
    "PermissionChanged",
    "PermissionDeniedError",
    "Provider",
    "ProviderUnavailableError",
    "RateLimitError",
    "RawItem",
    "RetryPolicy",
    "SyncContext",
    "UpsertSource",
    "canonical_json",
    "make_change_id",
    "stable_hash",
    "validate_change_stream",
    "with_retry",
]
