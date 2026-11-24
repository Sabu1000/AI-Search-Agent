"""Private account-owned device metadata, with no device transport secrets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .registration import DevicePlatform, RegistrationScope

type DeviceStatus = Literal["pending", "active", "revoked", "deleting"]
type DeviceSortKey = tuple[datetime, UUID]

_VERSION = re.compile(r"[0-9A-Za-z.+-]{1,50}")
_OPAQUE_VALUE = re.compile(r"[A-Za-z0-9_-]{40,200}")
_LABEL_PUNCTUATION = frozenset(" .-_'’()")
_DEVICE_PAGE_LIMIT = 101


@dataclass(frozen=True, slots=True)
class DeviceSummary:
    id: UUID
    name: str
    platform: DevicePlatform
    app_version: str | None
    status: DeviceStatus
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None


class DeviceQueryUnavailable(Exception):
    """Repository dependency failed without exposing storage details."""


class DeviceQueryDenied(Exception):
    """The authenticated session and tenant scope are no longer valid."""


class DeviceQueryReader(Protocol):
    async def list_devices(
        self, *, scope: RegistrationScope, before: DeviceSortKey | None, limit: int
    ) -> list[DeviceSummary]: ...

    async def get(
        self, *, scope: RegistrationScope, device_id: UUID
    ) -> DeviceSummary | None: ...


_PRIVATE_SELECT = """
SELECT d.id, d.name, d.platform, d.app_version, d.status,
       d.created_at, d.updated_at, d.last_seen_at, d.revoked_at
FROM app.devices AS d
WHERE d.workspace_id = :workspace AND d.user_id = :actor
"""


def _summary(row: RowMapping) -> DeviceSummary:
    label = row["name"].strip()
    # Registration accepts a human label, not a filesystem path. Legacy or
    # malformed rows containing path separators, opaque keys or unusual
    # control punctuation are rendered as a platform label instead.
    if (
        not label
        or len(label) > 150
        or _OPAQUE_VALUE.fullmatch(label)
        or not all(ch.isalnum() or ch in _LABEL_PUNCTUATION for ch in label)
    ):
        label = "Mac device" if row["platform"] == "macos" else "Windows device"
    version = row["app_version"]
    return DeviceSummary(
        id=row["id"],
        name=label,
        platform=row["platform"],
        app_version=(
            version
            if _VERSION.fullmatch(version) and not _OPAQUE_VALUE.fullmatch(version)
            else None
        ),
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_seen_at=row["last_seen_at"],
        revoked_at=row["revoked_at"],
    )


class SQLAlchemyDeviceQueryReader:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @staticmethod
    async def _authorize(
        conn: AsyncConnection, scope: RegistrationScope
    ) -> dict[str, object]:
        await conn.execute(
            text(
                """SELECT set_config('app.workspace_id', :workspace, true),
                          set_config('app.user_id', :actor, true)"""
            ),
            {"workspace": str(scope.workspace_id), "actor": str(scope.user_id)},
        )
        params: dict[str, object] = {
            "workspace": scope.workspace_id,
            "actor": scope.user_id,
            "session": scope.session_id,
            "workspace_version": scope.workspace_authorization_version,
            "user_version": scope.user_authorization_version,
        }
        valid = await conn.scalar(
            text(
                """SELECT true FROM app.workspace_members m
                JOIN app.workspaces w ON w.id = m.workspace_id
                JOIN app.users u ON u.id = m.user_id
                JOIN app.sessions s ON s.user_id = u.id
                WHERE m.workspace_id = :workspace AND m.user_id = :actor
                  AND m.status = 'active' AND w.status = 'active'
                  AND w.authorization_version = :workspace_version
                  AND u.lock_version = :user_version AND u.status = 'active'
                  AND u.email_verified_at IS NOT NULL
                  AND s.id = :session AND s.authorization_version = :user_version
                  AND s.revoked_at IS NULL AND s.rotated_at IS NULL
                  AND s.expires_at > clock_timestamp()
                FOR SHARE OF m, w, u, s"""
            ),
            params,
        )
        if valid is not True:
            raise DeviceQueryDenied
        return params

    async def list_devices(
        self, *, scope: RegistrationScope, before: DeviceSortKey | None, limit: int
    ) -> list[DeviceSummary]:
        if not 1 <= limit <= _DEVICE_PAGE_LIMIT:
            raise ValueError("invalid device page bound")
        sql = _PRIVATE_SELECT
        try:
            async with self._engine.begin() as conn:
                params = await self._authorize(conn, scope)
                params["limit"] = limit
                if before is not None:
                    sql += (
                        " AND (d.created_at, d.id) < (:before_created_at, :before_id)"
                    )
                    params["before_created_at"], params["before_id"] = before
                sql += " ORDER BY d.created_at DESC, d.id DESC LIMIT :limit"
                rows = (await conn.execute(text(sql), params)).mappings().all()
        except SQLAlchemyError:
            raise DeviceQueryUnavailable from None
        return [_summary(row) for row in rows]

    async def get(
        self, *, scope: RegistrationScope, device_id: UUID
    ) -> DeviceSummary | None:
        try:
            async with self._engine.begin() as conn:
                params = await self._authorize(conn, scope)
                params["device"] = device_id
                result = await conn.execute(
                    text(_PRIVATE_SELECT + " AND d.id = :device"), params
                )
                row = result.mappings().one_or_none()
        except SQLAlchemyError:
            raise DeviceQueryUnavailable from None
        return _summary(row) if row is not None else None
