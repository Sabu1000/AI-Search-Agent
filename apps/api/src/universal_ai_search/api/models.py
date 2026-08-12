"""Strict public boundary and serialization models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    PlainSerializer,
)


def _require_timezone(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def _serialize_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


CanonicalUUID = Annotated[
    UUID,
    PlainSerializer(lambda value: str(value), return_type=str, when_used="json"),
]
UtcDateTime = Annotated[
    datetime,
    AfterValidator(_require_timezone),
    PlainSerializer(_serialize_utc, return_type=str, when_used="json"),
]


class APIModel(BaseModel):
    """Base model for typed API boundaries."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        validate_assignment=True,
    )


class RequestModel(APIModel):
    """A request body that rejects unknown fields."""


class ResponseModel(APIModel):
    """A typed response body with deterministic JSON serialization."""
