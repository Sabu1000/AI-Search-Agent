"""Content-free owner operation status for the verified native index queue."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from universal_ai_search.api.models import CanonicalUUID, ResponseModel, UtcDateTime
from universal_ai_search.auth.reauth_store import ReauthScope

from .uploads import UploadUnavailable


class NativeUploadIndexOperation(ResponseModel):
    id: CanonicalUUID
    status: Literal["pending", "running", "retry_wait", "completed", "failed"]
    created_at: UtcDateTime
    started_at: UtcDateTime | None
    completed_at: UtcDateTime | None
    error_code: (
        Literal[
            "UPLOAD_INDEX_UNSUPPORTED",
            "UPLOAD_INDEX_DENIED",
            "UPLOAD_INDEX_INVALID",
            "UPLOAD_INDEX_UNAVAILABLE",
            "UPLOAD_INDEX_SUPERSEDED",
            "UPLOAD_INDEX_SOURCE_REMOVED",
            "UPLOAD_INDEX_MAX_ATTEMPTS",
            "UPLOAD_INDEX_FAILED",
        ]
        | None
    )


async def read_native_upload_index_operation(
    engine: AsyncEngine, *, scope: ReauthScope, operation_id: UUID
) -> NativeUploadIndexOperation | None:
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL statement_timeout = '4s'"))
            await connection.execute(
                text(
                    """SELECT set_config('app.user_id', :actor, true),
                              set_config('app.workspace_id', :workspace, true)"""
                ),
                {"actor": str(scope.user_id), "workspace": str(scope.workspace_id)},
            )
            row = await connection.scalar(
                text(
                    """SELECT app.read_device_upload_index_operation(
                       :operation,:workspace,:actor,:session,:user_version,:workspace_version)"""
                ),
                {
                    "operation": operation_id,
                    "workspace": scope.workspace_id,
                    "actor": scope.user_id,
                    "session": scope.session_id,
                    "user_version": scope.user_version,
                    "workspace_version": scope.workspace_version,
                },
            )
        return (
            NativeUploadIndexOperation.model_validate(row) if row is not None else None
        )
    except (SQLAlchemyError, ValidationError):
        raise UploadUnavailable from None
