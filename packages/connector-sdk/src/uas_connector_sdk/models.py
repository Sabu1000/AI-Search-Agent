"""Provider-neutral connector data contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    field_validator,
    model_validator,
)

type JsonObject = dict[str, JsonValue]


class Provider(StrEnum):
    GMAIL = "gmail"
    GOOGLE_DRIVE = "google_drive"
    GITHUB = "github"
    LOCAL_FILES = "local_files"


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value


def canonical_json(value: JsonObject) -> str:
    """Serialize JSON deterministically for hashes and idempotency keys."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def stable_hash(value: JsonObject | str) -> str:
    serialized = value if isinstance(value, str) else canonical_json(value)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class Credentials(StrictModel):
    """Ephemeral credential material; persistence belongs to the secret store."""

    access_token: SecretStr
    refresh_token: SecretStr | None = None
    expires_at: datetime | None = None
    scopes: tuple[str, ...] = ()

    _validate_expires_at = field_validator("expires_at")(_require_utc)


class SyncContext(StrictModel):
    workspace_id: UUID
    connection_id: UUID
    sync_job_id: UUID
    collection_ids: tuple[UUID, ...] = ()
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    _validate_requested_at = field_validator("requested_at")(_require_utc)


class RawItem(StrictModel):
    external_id: str = Field(min_length=1, max_length=512)
    payload: JsonObject


class AccessMetadata(StrictModel):
    public: bool = False
    user_ids: tuple[str, ...] = ()
    group_ids: tuple[str, ...] = ()
    domain_ids: tuple[str, ...] = ()


class NormalizedDocument(StrictModel):
    external_id: str = Field(min_length=1, max_length=512)
    provider: Provider
    source_type: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=2_000)
    content: str = Field(max_length=5_000_000)
    canonical_url: str | None = Field(default=None, max_length=4_096)
    mime_type: str = Field(min_length=1, max_length=255)
    authors: tuple[str, ...] = ()
    created_at: datetime | None = None
    modified_at: datetime | None = None
    access_metadata: AccessMetadata = Field(default_factory=AccessMetadata)
    provider_metadata: JsonObject = Field(default_factory=dict)

    _validate_created_at = field_validator("created_at")(_require_utc)
    _validate_modified_at = field_validator("modified_at")(_require_utc)

    @field_validator("canonical_url")
    @classmethod
    def validate_canonical_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("canonical_url must be an HTTPS URL without credentials")
        return value

    @model_validator(mode="after")
    def validate_dates_and_metadata(self) -> NormalizedDocument:
        if self.created_at and self.modified_at and self.modified_at < self.created_at:
            raise ValueError("modified_at cannot precede created_at")
        if len(canonical_json(self.provider_metadata).encode()) > 65_536:
            raise ValueError("provider_metadata exceeds 64 KiB")
        return self

    @property
    def content_hash(self) -> str:
        return stable_hash(self.content)

    @property
    def permissions_hash(self) -> str:
        return stable_hash(self.access_metadata.model_dump(mode="json"))


class ChangeBase(StrictModel):
    provider: Provider
    change_id: str = Field(min_length=1, max_length=512)


class UpsertSource(ChangeBase):
    type: Literal["UPSERT"] = "UPSERT"
    document: NormalizedDocument

    @model_validator(mode="after")
    def provider_matches_document(self) -> UpsertSource:
        if self.provider != self.document.provider:
            raise ValueError("change provider must match document provider")
        return self


class DeleteSource(ChangeBase):
    type: Literal["DELETE"] = "DELETE"
    external_id: str = Field(min_length=1, max_length=512)


class PermissionChanged(ChangeBase):
    type: Literal["PERMISSION_CHANGED"] = "PERMISSION_CHANGED"
    external_id: str = Field(min_length=1, max_length=512)
    access_metadata: AccessMetadata


class CursorAdvanced(ChangeBase):
    type: Literal["CURSOR_ADVANCED"] = "CURSOR_ADVANCED"
    cursor: JsonObject

    @field_validator("cursor")
    @classmethod
    def validate_cursor(cls, value: JsonObject) -> JsonObject:
        if not value:
            raise ValueError("cursor must not be empty")
        if len(canonical_json(value).encode()) > 16_384:
            raise ValueError("cursor exceeds 16 KiB")
        return value


type Change = Annotated[
    UpsertSource | DeleteSource | PermissionChanged | CursorAdvanced,
    Field(discriminator="type"),
]


class HealthResult(StrictModel):
    status: HealthStatus
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    latency_ms: int = Field(ge=0)
    detail: str | None = Field(default=None, max_length=500)

    _validate_checked_at = field_validator("checked_at")(_require_utc)


def make_change_id(provider: Provider, external_id: str, version: str) -> str:
    return stable_hash({"provider": provider.value, "external_id": external_id, "version": version})
