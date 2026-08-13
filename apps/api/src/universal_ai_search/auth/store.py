"""Database-backed authentication persistence with explicit RLS context."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


@dataclass(frozen=True)
class LoginIdentity:
    user_id: UUID
    password_hash: str | None
    full_name: str
    status: str
    email_verified: bool
    authorization_version: int


@dataclass(frozen=True)
class VerifiedIdentity:
    user_id: UUID
    email: str
    full_name: str
    workspace_id: UUID
    authorization_version: int


@dataclass(frozen=True)
class SessionIdentity:
    session_id: UUID
    user_id: UUID
    authorization_version: int


@dataclass(frozen=True)
class Membership:
    workspace_id: UUID
    name: str
    role: str
    status: str
    authorization_version: int


@dataclass(frozen=True)
class UserAccount:
    user_id: UUID
    email: str
    full_name: str
    email_verified: bool
    authorization_version: int


class RotationState(StrEnum):
    ROTATED = "rotated"
    INVALID = "invalid"
    REPLAYED = "replayed"


@dataclass(frozen=True)
class RotationResult:
    state: RotationState
    identity: SessionIdentity | None = None


class AuthStore(Protocol):
    async def register(
        self,
        *,
        user_id: UUID,
        email: str,
        password_hash: str,
        full_name: str,
        terms_version: str,
        locale: str,
        token_id: UUID,
        token_hash: bytes,
        token_expiry: datetime,
    ) -> bool: ...

    async def verify_email(
        self, *, token_hash: bytes, workspace_id: UUID
    ) -> VerifiedIdentity | None: ...

    async def login_identity(self, email: str) -> LoginIdentity | None: ...

    async def update_password_hash(self, user_id: UUID, password_hash: str) -> None: ...

    async def create_session(
        self,
        *,
        session_id: UUID,
        user_id: UUID,
        family_id: UUID,
        refresh_hash: bytes,
        csrf_hash: bytes,
        authorization_version: int,
        expires_at: datetime,
        device_metadata: dict[str, str],
    ) -> None: ...

    async def rotate_session(
        self,
        *,
        refresh_hash: bytes,
        presented_csrf_hash: bytes | None,
        successor_id: UUID,
        successor_refresh_hash: bytes,
        successor_csrf_hash: bytes,
        expires_at: datetime,
    ) -> RotationResult: ...

    async def validate_session(
        self, *, user_id: UUID, session_id: UUID, authorization_version: int
    ) -> SessionIdentity | None: ...

    async def revoke_session(self, *, user_id: UUID, session_id: UUID) -> None: ...

    async def validate_csrf(
        self, *, user_id: UUID, session_id: UUID, csrf_hash: bytes
    ) -> bool: ...

    async def account(self, user_id: UUID) -> UserAccount | None: ...

    async def memberships(self, user_id: UUID) -> list[Membership]: ...

    async def membership(
        self, *, user_id: UUID, workspace_id: UUID
    ) -> Membership | None: ...


async def _set_user_context(connection: AsyncConnection, user_id: UUID) -> None:
    await connection.execute(
        text("SELECT set_config('app.user_id', :user_id, true)"),
        {"user_id": str(user_id)},
    )


class SQLAlchemyAuthStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def register(self, **values: object) -> bool:
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            """SELECT * FROM app.auth_register_user(
                            :user_id, :email, :password_hash, :full_name,
                            :terms_version, :locale,
                        :token_id, :token_hash, :token_expiry)"""
                        ),
                        values,
                    )
                )
                .mappings()
                .one()
            )
        return bool(row["created"])

    async def verify_email(
        self, *, token_hash: bytes, workspace_id: UUID
    ) -> VerifiedIdentity | None:
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM app.auth_verify_email("
                            ":token_hash, :workspace_id)"
                        ),
                        {"token_hash": token_hash, "workspace_id": workspace_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return VerifiedIdentity(**row) if row else None

    async def login_identity(self, email: str) -> LoginIdentity | None:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text("SELECT * FROM app.auth_login_lookup(:email)"),
                        {"email": email},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return LoginIdentity(**row) if row else None

    async def update_password_hash(self, user_id: UUID, password_hash: str) -> None:
        async with self._engine.begin() as connection:
            await _set_user_context(connection, user_id)
            await connection.execute(
                text(
                    "UPDATE app.users SET password_hash = :password_hash, "
                    "updated_at = clock_timestamp() WHERE id = :user_id"
                ),
                {"password_hash": password_hash, "user_id": user_id},
            )

    async def create_session(
        self,
        *,
        session_id: UUID,
        user_id: UUID,
        family_id: UUID,
        refresh_hash: bytes,
        csrf_hash: bytes,
        authorization_version: int,
        expires_at: datetime,
        device_metadata: dict[str, str],
    ) -> None:
        async with self._engine.begin() as connection:
            await _set_user_context(connection, user_id)
            await connection.execute(
                text(
                    """INSERT INTO app.sessions (
                    id, user_id, refresh_token_hash, family_id, issued_at,
                    expires_at, csrf_token_hash, authorization_version,
                    device_metadata, last_seen_at
                    ) VALUES (
                    :session_id, :user_id, :refresh_hash, :family_id,
                    clock_timestamp(), :expires_at, :csrf_hash,
                    :authorization_version, CAST(:device_metadata AS JSONB),
                    clock_timestamp())"""
                ),
                {
                    "session_id": session_id,
                    "user_id": user_id,
                    "refresh_hash": refresh_hash,
                    "family_id": family_id,
                    "expires_at": expires_at,
                    "csrf_hash": csrf_hash,
                    "authorization_version": authorization_version,
                    "device_metadata": json.dumps(device_metadata),
                },
            )

    async def rotate_session(
        self,
        *,
        refresh_hash: bytes,
        presented_csrf_hash: bytes | None,
        successor_id: UUID,
        successor_refresh_hash: bytes,
        successor_csrf_hash: bytes,
        expires_at: datetime,
    ) -> RotationResult:
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        text("SELECT * FROM app.auth_refresh_lookup(:refresh_hash)"),
                        {"refresh_hash": refresh_hash},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return RotationResult(RotationState.INVALID)
            user_id = row["user_id"]
            await _set_user_context(connection, user_id)
            now = datetime.now(UTC)
            invalid = (
                row["revoked_at"] is not None
                or row["expires_at"] <= now
                or row["user_status"] != "active"
                or row["authorization_version"] != row["current_user_version"]
                or (
                    presented_csrf_hash is not None
                    and row["csrf_token_hash"] != presented_csrf_hash
                )
            )
            if row["rotated_at"] is not None:
                await connection.execute(
                    text(
                        "UPDATE app.sessions SET revoked_at = "
                        "COALESCE(revoked_at, clock_timestamp()) "
                        "WHERE user_id = :user_id AND family_id = :family_id"
                    ),
                    {"user_id": user_id, "family_id": row["family_id"]},
                )
                return RotationResult(RotationState.REPLAYED)
            if invalid:
                return RotationResult(RotationState.INVALID)
            await connection.execute(
                text(
                    """INSERT INTO app.sessions (
                    id, user_id, refresh_token_hash, family_id, issued_at,
                    expires_at, csrf_token_hash, authorization_version,
                    device_metadata, last_seen_at
                    ) SELECT :successor_id, user_id, :successor_refresh_hash,
                    family_id, clock_timestamp(), :expires_at,
                    :successor_csrf_hash, authorization_version,
                    device_metadata, clock_timestamp()
                    FROM app.sessions WHERE id = :session_id"""
                ),
                {
                    "successor_id": successor_id,
                    "successor_refresh_hash": successor_refresh_hash,
                    "expires_at": min(expires_at, row["expires_at"]),
                    "successor_csrf_hash": successor_csrf_hash,
                    "session_id": row["session_id"],
                },
            )
            await connection.execute(
                text(
                    "UPDATE app.sessions SET rotated_at = clock_timestamp(), "
                    "replaced_by_session_id = :successor_id WHERE id = :session_id"
                ),
                {"successor_id": successor_id, "session_id": row["session_id"]},
            )
            return RotationResult(
                RotationState.ROTATED,
                SessionIdentity(successor_id, user_id, row["current_user_version"]),
            )

    async def validate_session(
        self, *, user_id: UUID, session_id: UUID, authorization_version: int
    ) -> SessionIdentity | None:
        async with self._engine.begin() as connection:
            await _set_user_context(connection, user_id)
            row = (
                (
                    await connection.execute(
                        text(
                            """SELECT sessions.id AS session_id, sessions.user_id,
                        users.lock_version AS authorization_version
                        FROM app.sessions JOIN app.users ON users.id = sessions.user_id
                        WHERE sessions.id = :session_id AND sessions.user_id = :user_id
                          AND sessions.revoked_at IS NULL
                          AND sessions.expires_at > clock_timestamp()
                          AND sessions.authorization_version = :authorization_version
                          AND users.lock_version = :authorization_version
                          AND users.status = 'active'"""
                        ),
                        {
                            "session_id": session_id,
                            "user_id": user_id,
                            "authorization_version": authorization_version,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
        return SessionIdentity(**row) if row else None

    async def revoke_session(self, *, user_id: UUID, session_id: UUID) -> None:
        async with self._engine.begin() as connection:
            await _set_user_context(connection, user_id)
            await connection.execute(
                text(
                    "UPDATE app.sessions SET revoked_at = "
                    "COALESCE(revoked_at, clock_timestamp()) "
                    "WHERE id = :session_id AND user_id = :user_id"
                ),
                {"session_id": session_id, "user_id": user_id},
            )

    async def validate_csrf(
        self, *, user_id: UUID, session_id: UUID, csrf_hash: bytes
    ) -> bool:
        async with self._engine.begin() as connection:
            await _set_user_context(connection, user_id)
            row = (
                await connection.execute(
                    text(
                        """SELECT 1 FROM app.sessions
                        WHERE id = :session_id AND user_id = :user_id
                          AND csrf_token_hash = :csrf_hash
                          AND revoked_at IS NULL AND expires_at > clock_timestamp()"""
                    ),
                    {
                        "session_id": session_id,
                        "user_id": user_id,
                        "csrf_hash": csrf_hash,
                    },
                )
            ).one_or_none()
        return row is not None

    async def account(self, user_id: UUID) -> UserAccount | None:
        async with self._engine.begin() as connection:
            await _set_user_context(connection, user_id)
            row = (
                (
                    await connection.execute(
                        text(
                            """SELECT id AS user_id, email::TEXT, full_name,
                        email_verified_at IS NOT NULL AS email_verified,
                        lock_version AS authorization_version
                        FROM app.users WHERE id = :user_id AND status = 'active'"""
                        ),
                        {"user_id": user_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return UserAccount(**row) if row else None

    async def memberships(self, user_id: UUID) -> list[Membership]:
        async with self._engine.begin() as connection:
            await _set_user_context(connection, user_id)
            rows = (
                (
                    await connection.execute(
                        text("SELECT * FROM app.auth_memberships(:user_id)"),
                        {"user_id": user_id},
                    )
                )
                .mappings()
                .all()
            )
        return [Membership(**row) for row in rows]

    async def membership(
        self, *, user_id: UUID, workspace_id: UUID
    ) -> Membership | None:
        async with self._engine.begin() as connection:
            await _set_user_context(connection, user_id)
            await connection.execute(
                text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": str(workspace_id)},
            )
            row = (
                (
                    await connection.execute(
                        text(
                            """SELECT members.workspace_id, workspaces.name,
                        members.role,
                        members.status, workspaces.authorization_version
                        FROM app.workspace_members AS members
                        JOIN app.workspaces ON workspaces.id = members.workspace_id
                        WHERE members.workspace_id = :workspace_id
                          AND members.user_id = :user_id
                          AND members.status = 'active'
                          AND workspaces.status = 'active'"""
                        ),
                        {"workspace_id": workspace_id, "user_id": user_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return Membership(**row) if row else None
