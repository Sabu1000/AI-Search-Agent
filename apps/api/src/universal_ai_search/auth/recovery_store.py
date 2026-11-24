"""Purpose-bound SQL gateway; raw reset values never cross this boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class RecoveryTarget:
    user_id: UUID
    email: str = field(repr=False)
    authorization_version: int


class RecoveryStore(Protocol):
    async def issue(
        self, *, email: str, token_id: UUID, token_hash: bytes, expires_at: datetime
    ) -> str | None: ...

    async def lookup(self, *, token_hash: bytes) -> RecoveryTarget | None: ...

    async def complete(
        self,
        *,
        token_hash: bytes,
        user_id: UUID,
        expected_authorization_version: int,
        expected_email: str,
        password_hash: str,
        audit_id: UUID,
        audit_subject_hash: bytes,
        request_id: UUID,
    ) -> str | None: ...


class SQLAlchemyRecoveryStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def issue(
        self, *, email: str, token_id: UUID, token_hash: bytes, expires_at: datetime
    ) -> str | None:
        async with self._engine.begin() as connection:
            return cast(
                str | None,
                await connection.scalar(
                    text(
                        "SELECT app.auth_issue_password_reset("
                        ":email,:token_id,:token_hash,:expires_at)"
                    ),
                    {
                        "email": email,
                        "token_id": token_id,
                        "token_hash": token_hash,
                        "expires_at": expires_at,
                    },
                ),
            )

    async def lookup(self, *, token_hash: bytes) -> RecoveryTarget | None:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                text("SELECT * FROM app.auth_lookup_password_reset(:token_hash)"),
                {"token_hash": token_hash},
            )
            row = result.mappings().one_or_none()
        return RecoveryTarget(**row) if row else None

    async def complete(
        self,
        *,
        token_hash: bytes,
        user_id: UUID,
        expected_authorization_version: int,
        expected_email: str,
        password_hash: str,
        audit_id: UUID,
        audit_subject_hash: bytes,
        request_id: UUID,
    ) -> str | None:
        async with self._engine.begin() as connection:
            return cast(
                str | None,
                await connection.scalar(
                    text(
                        "SELECT app.auth_complete_password_reset("
                        ":token_hash,:user_id,:version,:email,:password_hash,"
                        ":audit_id,:audit_subject_hash,:request_id)"
                    ),
                    {
                        "token_hash": token_hash,
                        "user_id": user_id,
                        "version": expected_authorization_version,
                        "email": expected_email,
                        "password_hash": password_hash,
                        "audit_id": audit_id,
                        "audit_subject_hash": audit_subject_hash,
                        "request_id": request_id,
                    },
                ),
            )
