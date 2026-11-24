"""Transactional prepare/complete and current-session read for native staging."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from universal_ai_search.api.models import CanonicalUUID, ResponseModel
from universal_ai_search.auth.reauth_store import ReauthScope

from .heartbeat import HeartbeatAuthority
from .manifest_store import SQLAlchemyDeviceManifestStore
from .manifests import MAX_FILE_BYTES, ManifestUnavailable
from .upload_completion import (
    ChangeCandidate,
    ChangeClaim,
    ChangeConflict,
    ChangeExpired,
    ChangeRateLimited,
    VerifiedChange,
)
from .uploads import UploadMimeType, UploadRejected, UploadUnavailable


class _CandidateRow(ResponseModel):
    status: Literal["check"]
    upload_id: CanonicalUUID
    object_key: str
    size_bytes: Annotated[int, Field(strict=True, ge=0, le=MAX_FILE_BYTES)]
    content_sha256: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]
    mime_type: UploadMimeType


class SQLAlchemyUploadCompletionStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._authority = SQLAlchemyDeviceManifestStore(engine)

    async def lookup(
        self, device_id: UUID, credential_hash: bytes
    ) -> HeartbeatAuthority | None:
        try:
            return await self._authority.lookup(device_id, credential_hash)
        except ManifestUnavailable:
            raise UploadUnavailable from None

    async def _call(
        self,
        claim: ChangeClaim,
        candidate: ChangeCandidate | None = None,
        version: str | None = None,
    ) -> ChangeCandidate | VerifiedChange:
        try:
            async with self._engine.begin() as connection:
                await connection.execute(text("SET LOCAL statement_timeout = '4s'"))
                result = await connection.scalar(
                    text(
                        """SELECT app.device_upload_change(
                           :device,:credential,:key,:signed,:nonce,:batch,:upload,
                           :version,:size,:digest,:mime)"""
                    ),
                    {
                        "device": claim.device_id,
                        "credential": claim.credential_hash,
                        "key": claim.public_key,
                        "signed": claim.signed_at,
                        "nonce": claim.nonce_hash,
                        "batch": claim.batch_id,
                        "upload": claim.upload_id,
                        "version": version,
                        "size": candidate.size_bytes if candidate else None,
                        "digest": candidate.content_sha256 if candidate else None,
                        "mime": candidate.mime_type if candidate else None,
                    },
                )
                if not isinstance(result, dict):
                    raise UploadUnavailable
                status = result.get("status")
                if status == "verified":
                    return VerifiedChange.model_validate(result)
                if status == "check" and candidate is None:
                    row = _CandidateRow.model_validate(result)
                    return ChangeCandidate(
                        row.upload_id,
                        row.object_key,
                        row.size_bytes,
                        row.content_sha256,
                        row.mime_type,
                    )
        except (SQLAlchemyError, ValidationError):
            raise UploadUnavailable from None
        if status == "rejected":
            raise UploadRejected
        if status in {"conflict", "busy"}:
            raise ChangeConflict
        if status == "expired":
            raise ChangeExpired
        if status == "rate_limited":
            raise ChangeRateLimited
        raise UploadUnavailable

    async def prepare(self, claim: ChangeClaim) -> ChangeCandidate | VerifiedChange:
        return await self._call(claim)

    async def complete(
        self, claim: ChangeClaim, candidate: ChangeCandidate, version: str
    ) -> VerifiedChange:
        result = await self._call(claim, candidate, version)
        if not isinstance(result, VerifiedChange):
            raise UploadUnavailable
        return result

    async def read(
        self, *, scope: ReauthScope, operation_id: UUID
    ) -> VerifiedChange | None:
        try:
            async with self._engine.begin() as connection:
                await connection.execute(
                    text(
                        """SELECT set_config('app.user_id', :actor, true),
                                  set_config('app.workspace_id', :workspace, true)"""
                    ),
                    {"actor": str(scope.user_id), "workspace": str(scope.workspace_id)},
                )
                result = await connection.scalar(
                    text(
                        """SELECT app.read_device_upload_change(
                           :operation,:workspace,:actor,:session,:user_version,:workspace_version)"""
                    ),
                    {
                        "operation": operation_id,
                        "actor": scope.user_id,
                        "workspace": scope.workspace_id,
                        "session": scope.session_id,
                        "user_version": scope.user_version,
                        "workspace_version": scope.workspace_version,
                    },
                )
                return (
                    VerifiedChange.model_validate(result)
                    if result is not None
                    else None
                )
        except (SQLAlchemyError, ValidationError):
            raise UploadUnavailable from None
