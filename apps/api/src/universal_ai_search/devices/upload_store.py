"""PostgreSQL adapter for reservations; never generates or persists URLs."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from universal_ai_search.api.models import CanonicalUUID, ResponseModel, UtcDateTime

from .heartbeat import HeartbeatAuthority
from .manifest_store import SQLAlchemyDeviceManifestStore
from .manifests import MAX_FILE_BYTES, ManifestUnavailable
from .uploads import (
    UPLOAD_GRANT_TTL,
    UploadGrant,
    UploadMimeType,
    UploadQuotaExceeded,
    UploadRateLimited,
    UploadRejected,
    UploadReservationExpired,
    UploadReserve,
    UploadUnavailable,
)


class _GrantRow(ResponseModel):
    status: Literal["authorized"]
    upload_id: CanonicalUUID
    workspace_id: CanonicalUUID
    object_key: Annotated[
        str,
        Field(
            pattern=r"^device-inbox/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            repr=False,
        ),
    ]
    issued_at: UtcDateTime
    expires_at: UtcDateTime
    size_bytes: Annotated[int, Field(strict=True, ge=0, le=MAX_FILE_BYTES)]
    mime_type: UploadMimeType
    content_sha256: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$", repr=False)]


class SQLAlchemyDeviceUploadStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._authority = SQLAlchemyDeviceManifestStore(engine)

    async def lookup(
        self, device_id: UUID, credential_hash: bytes
    ) -> HeartbeatAuthority | None:
        # Manifest lookup is already strictly active, unlike heartbeat lookup.
        try:
            return await self._authority.lookup(device_id, credential_hash)
        except ManifestUnavailable:
            raise UploadUnavailable from None

    async def reserve(self, data: UploadReserve) -> UploadGrant:
        request = data.request
        try:
            async with self._engine.begin() as connection:
                result = await connection.scalar(
                    text(
                        "SELECT app.reserve_device_upload("
                        ":device, :credential, :public_key, :signed_at, :nonce_hash, "
                        ":folder, :manifest, :upload, :external, :size, :digest, :mime)"
                    ),
                    {
                        "device": data.device_id,
                        "credential": data.credential_hash,
                        "public_key": data.public_key,
                        "signed_at": data.signed_at,
                        "nonce_hash": data.nonce_hash,
                        "folder": request.folder_id,
                        "manifest": request.manifest_id,
                        "upload": request.upload_id,
                        "external": request.external_id,
                        "size": request.size_bytes,
                        "digest": request.content_sha256,
                        "mime": request.mime_type,
                    },
                )
                if not isinstance(result, dict):
                    raise UploadUnavailable
                status = result.get("status")
                if status == "authorized":
                    grant = _GrantRow.model_validate(result)
                    if (
                        grant.upload_id != request.upload_id
                        or grant.workspace_id.int == 0
                        or grant.issued_at + UPLOAD_GRANT_TTL != grant.expires_at
                        or grant.size_bytes != request.size_bytes
                        or grant.mime_type != request.mime_type
                        or grant.content_sha256 != request.content_sha256
                    ):
                        raise UploadUnavailable
                    return UploadGrant(
                        grant.upload_id,
                        grant.workspace_id,
                        grant.object_key,
                        grant.issued_at,
                        grant.expires_at,
                        grant.size_bytes,
                        grant.mime_type,
                        grant.content_sha256,
                    )
                if status not in {"rejected", "expired", "rate_limited", "quota"}:
                    raise UploadUnavailable
        except (SQLAlchemyError, ValidationError):
            raise UploadUnavailable from None

        # Authenticated expired/quota attempts still consume their signed nonce.
        if status == "expired":
            raise UploadReservationExpired
        if status == "rate_limited":
            raise UploadRateLimited
        if status == "quota":
            raise UploadQuotaExceeded
        raise UploadRejected
