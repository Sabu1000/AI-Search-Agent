"""Authenticated keyset cursor and common page contracts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import Field, ValidationError

from universal_ai_search.api.models import APIModel, ResponseModel
from universal_ai_search.api.problems import ProblemError

CURSOR_LIFETIME = timedelta(hours=24)
MAX_CURSOR_LENGTH = 4096

JsonScalar = str | int | float | bool | None


class CursorPayload(APIModel):
    version: int = 1
    endpoint: str = Field(min_length=1, max_length=255)
    workspace_id: UUID
    principal_id: UUID
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    sort_key: list[JsonScalar] = Field(min_length=1, max_length=10)
    expires_at: int = Field(gt=0)


class CursorContext(APIModel):
    endpoint: str = Field(min_length=1, max_length=255)
    workspace_id: UUID
    principal_id: UUID
    filter_sort_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class PaginationQuery(APIModel):
    limit: int = Field(default=25, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=MAX_CURSOR_LENGTH)


class PageMetadata(ResponseModel):
    next_cursor: str | None = None
    has_more: bool


class Page[PageItem: ResponseModel](ResponseModel):
    items: list[PageItem]
    page: PageMetadata


def normalized_context_hash(*, filters: object, sort: list[str]) -> str:
    """Hash normalized filters and sorting without leaking them into cursors."""

    try:
        encoded = json.dumps(
            {"filters": filters, "sort": sort},
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError) as error:
        raise ValueError("cursor context must be JSON serializable") from error
    return hashlib.sha256(encoded).hexdigest()


class CursorSigner:
    """Issue and verify opaque, HMAC-authenticated pagination cursors."""

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("cursor signing secret must be at least 32 bytes")
        self._secret = secret

    def issue(
        self,
        *,
        context: CursorContext,
        sort_key: list[JsonScalar],
        now: datetime | None = None,
        lifetime: timedelta = CURSOR_LIFETIME,
    ) -> str:
        issued_at = _utc_now(now)
        if lifetime <= timedelta(0) or lifetime > CURSOR_LIFETIME:
            raise ValueError("cursor lifetime must be within 24 hours")
        if any(
            isinstance(value, float) and not math.isfinite(value) for value in sort_key
        ):
            raise ValueError("cursor sort values must be finite")
        payload = CursorPayload(
            endpoint=context.endpoint,
            workspace_id=context.workspace_id,
            principal_id=context.principal_id,
            context_hash=context.filter_sort_hash,
            sort_key=sort_key,
            expires_at=int((issued_at + lifetime).timestamp()),
        )
        body = payload.model_dump_json().encode()
        signature = hmac.digest(self._secret, body, "sha256")
        return f"{_encode(body)}.{_encode(signature)}"

    def verify(
        self,
        cursor: str,
        *,
        expected: CursorContext,
        now: datetime | None = None,
    ) -> CursorPayload:
        if len(cursor) > MAX_CURSOR_LENGTH:
            raise _invalid_cursor()
        try:
            body_part, signature_part = cursor.split(".", maxsplit=1)
            body = _decode(body_part)
            signature = _decode(signature_part)
        except (ValueError, UnicodeError) as error:
            raise _invalid_cursor() from error
        expected_signature = hmac.digest(self._secret, body, "sha256")
        if not hmac.compare_digest(signature, expected_signature):
            raise _invalid_cursor()
        try:
            payload = CursorPayload.model_validate_json(body)
        except ValidationError as error:
            raise _invalid_cursor() from error
        if payload.expires_at <= int(_utc_now(now).timestamp()):
            raise _invalid_cursor()
        actual_context = (
            payload.endpoint,
            payload.workspace_id,
            payload.principal_id,
            payload.context_hash,
        )
        expected_context = (
            expected.endpoint,
            expected.workspace_id,
            expected.principal_id,
            expected.filter_sort_hash,
        )
        if actual_context != expected_context:
            raise ProblemError(
                status=400,
                code="CURSOR_CONTEXT_MISMATCH",
                title="Cursor does not match this request",
                detail="Start pagination again after changing its context.",
            )
        return payload


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("current time must include a timezone")
    return current.astimezone(UTC)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not value or any(character not in _BASE64URL for character in value):
        raise ValueError("invalid base64url")
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _invalid_cursor() -> ProblemError:
    return ProblemError(
        status=400,
        code="CURSOR_INVALID",
        title="Cursor is invalid",
        detail="Start pagination again with no cursor.",
    )


_BASE64URL = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
