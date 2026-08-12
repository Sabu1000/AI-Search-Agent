"""Durable-write idempotency contracts and coordination."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from universal_ai_search.api.models import APIModel
from universal_ai_search.api.problems import ProblemError

IDEMPOTENCY_LIFETIME = timedelta(hours=24)
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{8,128}$")
_SAFE_REPLAY_HEADERS = frozenset({"content-type", "etag", "location", "retry-after"})
_FORBIDDEN_REPLAY_FIELDS = frozenset(
    {
        "access_token",
        "authorization_url",
        "deletion_receipt",
        "download_url",
        "excerpt",
        "oauth_state",
        "provider_credentials",
        "refresh_token",
        "signed_url",
        "source_content",
        "upload_url",
    }
)


class IdempotencyFingerprint(APIModel):
    workspace_id: UUID
    principal_id: UUID
    method: str = Field(pattern=r"^(POST|PUT|PATCH|DELETE)$")
    route_template: str = Field(min_length=1, max_length=255)
    key_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReplayResponse(APIModel):
    status: int = Field(ge=100, le=599)
    body: dict[str, object]
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for name, header in value.items():
            lowered_name = name.lower()
            if lowered_name in normalized:
                raise ValueError("replay response contains a duplicate header")
            normalized[lowered_name] = header
        if not normalized.keys() <= _SAFE_REPLAY_HEADERS:
            raise ValueError("replay response contains an unsafe header")
        return normalized

    @field_validator("body")
    @classmethod
    def validate_body(cls, value: dict[str, object]) -> dict[str, object]:
        _reject_sensitive_fields(value)
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
        if len(encoded) > 65_536:
            raise ValueError("replay response exceeds 64 KiB")
        return value


class ReservationState(StrEnum):
    RESERVED = "reserved"
    REPLAY = "replay"
    IN_PROGRESS = "in_progress"
    CONFLICT = "conflict"


class ReservationResult(APIModel):
    state: ReservationState
    reservation_id: UUID | None = None
    replay: ReplayResponse | None = None

    @model_validator(mode="after")
    def validate_state_payload(self) -> Self:
        if self.state is ReservationState.RESERVED and (
            self.reservation_id is None or self.replay is not None
        ):
            raise ValueError("reserved result requires only a reservation ID")
        if self.state is ReservationState.REPLAY and (
            self.replay is None or self.reservation_id is not None
        ):
            raise ValueError("replay result requires only a response")
        if self.state not in {ReservationState.RESERVED, ReservationState.REPLAY} and (
            self.reservation_id is not None or self.replay is not None
        ):
            raise ValueError("non-success result cannot contain a payload")
        return self


class IdempotencyStore(Protocol):
    async def reserve(
        self,
        *,
        fingerprint: IdempotencyFingerprint,
        expires_at: datetime,
    ) -> ReservationResult: ...

    async def complete(
        self, *, reservation_id: UUID, response: ReplayResponse
    ) -> None: ...


class IdempotencyCoordinator:
    def __init__(self, store: IdempotencyStore) -> None:
        self._store = store

    async def begin(
        self,
        *,
        fingerprint: IdempotencyFingerprint,
        now: datetime | None = None,
    ) -> ReservationResult:
        current = _utc_now(now)
        result = await self._store.reserve(
            fingerprint=fingerprint,
            expires_at=current + IDEMPOTENCY_LIFETIME,
        )
        if result.state is ReservationState.CONFLICT:
            raise ProblemError(
                status=409,
                code="IDEMPOTENCY_KEY_REUSED",
                title="Idempotency key was already used",
                detail="Use a new idempotency key for this request.",
            )
        if result.state is ReservationState.IN_PROGRESS:
            raise ProblemError(
                status=409,
                code="IDEMPOTENCY_IN_PROGRESS",
                title="Request is already in progress",
                detail="Retry this request after the current attempt completes.",
                retryable=True,
                headers={"Retry-After": "2"},
            )
        return result

    async def complete(self, *, reservation_id: UUID, response: ReplayResponse) -> None:
        await self._store.complete(reservation_id=reservation_id, response=response)


def validate_idempotency_key(value: str | None) -> str:
    if value is None:
        raise ProblemError(
            status=400,
            code="IDEMPOTENCY_KEY_REQUIRED",
            title="Idempotency key is required",
            detail="Supply an Idempotency-Key header and try again.",
        )
    if _KEY_PATTERN.fullmatch(value) is None:
        raise ProblemError(
            status=400,
            code="IDEMPOTENCY_KEY_INVALID",
            title="Idempotency key is invalid",
            detail="Use 8–128 URL-safe ASCII characters.",
        )
    return value


def build_fingerprint(
    *,
    key: str,
    hash_secret: bytes,
    workspace_id: UUID,
    principal_id: UUID,
    method: str,
    route_template: str,
    validated_body: object,
    relevant_headers: dict[str, str] | None = None,
) -> IdempotencyFingerprint:
    validated_key = validate_idempotency_key(key)
    if len(hash_secret) < 32:
        raise ValueError("idempotency hash secret must be at least 32 bytes")
    normalized_method = method.upper()
    normalized_headers: dict[str, str] = {}
    for name, value in (relevant_headers or {}).items():
        lowered_name = name.lower()
        if lowered_name in normalized_headers:
            raise ValueError("relevant headers contain a case-insensitive duplicate")
        normalized_headers[lowered_name] = value
    request_document = {
        "body": validated_body,
        "headers": normalized_headers,
    }
    try:
        encoded_request = json.dumps(
            request_document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError) as error:
        raise ValueError("idempotent request must be JSON serializable") from error
    return IdempotencyFingerprint(
        workspace_id=workspace_id,
        principal_id=principal_id,
        method=normalized_method,
        route_template=route_template,
        key_hash=hmac.new(
            hash_secret, validated_key.encode(), hashlib.sha256
        ).hexdigest(),
        request_hash=hashlib.sha256(encoded_request).hexdigest(),
    )


def _reject_sensitive_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_REPLAY_FIELDS:
                raise ValueError("replay response contains sensitive data")
            _reject_sensitive_fields(child)
    elif isinstance(value, list):
        for child in value:
            _reject_sensitive_fields(child)


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("current time must include a timezone")
    return current.astimezone(UTC)
