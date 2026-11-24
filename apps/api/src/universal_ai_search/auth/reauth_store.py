"""PostgreSQL storage for session-, purpose-, and workspace-bound proofs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

type ReauthPurpose = Literal["disconnect", "export", "delete_account"]


@dataclass(frozen=True)
class ReauthScope:
    user_id: UUID
    session_id: UUID
    workspace_id: UUID
    user_version: int
    workspace_version: int


class ReauthenticationStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @staticmethod
    async def _authorize(
        connection: AsyncConnection,
        scope: ReauthScope,
        purpose: ReauthPurpose,
        *,
        lock: bool,
        return_password_hash: bool,
    ) -> tuple[dict[str, object], str | None] | None:
        # Parameters come only from the already-resolved authenticated actor,
        # never from the proof, request body, or an arbitrary provider URL.
        await connection.execute(
            text(
                """SELECT set_config('app.user_id', :actor, true),
                          set_config('app.workspace_id', :workspace, true)"""
            ),
            {"actor": str(scope.user_id), "workspace": str(scope.workspace_id)},
        )
        params: dict[str, object] = {
            "actor": scope.user_id,
            "workspace": scope.workspace_id,
            "session": scope.session_id,
            "user_version": scope.user_version,
            "workspace_version": scope.workspace_version,
            "purpose": purpose,
            "read_password_hash": return_password_hash,
        }

        # Choose fixed SQL clauses and an account → workspace → session lock
        # order so password reset cannot race issuance or token consumption.
        lock_user = "FOR SHARE OF u" if lock else ""
        identity = await connection.execute(
            text(
                f"""SELECT CASE WHEN :read_password_hash THEN u.password_hash
                                ELSE NULL END AS password_hash
                    FROM app.users u
                    WHERE u.id = :actor AND u.lock_version = :user_version
                      AND u.status = 'active' AND u.email_verified_at IS NOT NULL
                      AND u.password_hash IS NOT NULL
                    {lock_user}"""
            ),
            params,
        )
        row = identity.mappings().one_or_none()
        if row is None:
            return None
        lock_membership = "FOR SHARE OF m, w" if lock else ""
        membership = await connection.scalar(
            text(
                f"""SELECT true FROM app.workspace_members m
                    JOIN app.workspaces w ON w.id = m.workspace_id
                    WHERE m.workspace_id = :workspace AND m.user_id = :actor
                      AND m.status = 'active' AND w.status = 'active'
                      AND w.authorization_version = :workspace_version
                      AND (
                         (:purpose = 'disconnect' AND m.role IN ('owner','admin'))
                         OR :purpose = 'export'
                         OR (:purpose = 'delete_account' AND m.role = 'owner')
                      )
                    {lock_membership}"""
            ),
            params,
        )
        if membership is not True:
            return None
        lock_session = "FOR SHARE OF s" if lock else ""
        session = await connection.scalar(
            text(
                f"""SELECT true FROM app.sessions s
                    WHERE s.id = :session AND s.user_id = :actor
                      AND s.authorization_version = :user_version
                      AND s.revoked_at IS NULL AND s.rotated_at IS NULL
                      AND s.expires_at > clock_timestamp()
                    {lock_session}"""
            ),
            params,
        )
        if session is not True:
            return None
        return params, row["password_hash"]

    async def password_hash(
        self, *, scope: ReauthScope, purpose: ReauthPurpose
    ) -> str | None:
        async with self._engine.begin() as connection:
            result = await self._authorize(
                connection, scope, purpose, lock=False, return_password_hash=True
            )
        return result[1] if result else None

    async def issue(
        self,
        *,
        scope: ReauthScope,
        purpose: ReauthPurpose,
        expected_hash: str,
        token_hash: bytes,
        request_id: UUID,
        audit_subject: bytes,
        audit_workspace: bytes,
    ) -> datetime | None:
        async with self._engine.begin() as connection:
            result = await self._authorize(
                connection, scope, purpose, lock=True, return_password_hash=True
            )
            if result is None:
                return None
            params, password_hash = result
            if password_hash != expected_hash:
                return None
            inserted = await connection.execute(
                text(
                    """WITH issued AS (SELECT clock_timestamp() AS at)
                       INSERT INTO app.one_time_tokens
                        (id,user_id,purpose,token_hash,created_at,expires_at,
                         reauth_session_id,reauth_workspace_id,reauth_purpose,
                         reauth_user_version,reauth_workspace_version)
                       SELECT :id,:actor,'reauthenticate',:digest,issued.at,
                          issued.at + INTERVAL '5 minutes',:session,:workspace,
                          :purpose,:user_version,:workspace_version FROM issued
                       RETURNING expires_at"""
                ),
                {**params, "id": uuid4(), "digest": token_hash},
            )
            row = inserted.mappings().one()
            await connection.execute(
                text(
                    """INSERT INTO app.audit_events
                         (id,workspace_id,workspace_ref_hash,actor_user_id,action,
                          target_type,outcome,target_ref_hash,request_id)
                       VALUES (:event,:workspace,:workspace_hash,:actor,
                               'auth.reauthenticate','user','succeeded',
                               :subject_hash,:request_id)"""
                ),
                {
                    **params,
                    "event": uuid4(),
                    "workspace_hash": audit_workspace,
                    "subject_hash": audit_subject,
                    "request_id": request_id,
                },
            )
            expiry: datetime = row["expires_at"]
        return expiry

    async def consume(
        self,
        connection: AsyncConnection,
        *,
        scope: ReauthScope,
        purpose: ReauthPurpose,
        token_hash: bytes,
    ) -> bool:
        """Consume within the caller's destructive action transaction."""
        if not connection.in_transaction():
            raise RuntimeError("reauthentication consumption requires a transaction")
        result = await self._authorize(
            connection, scope, purpose, lock=True, return_password_hash=False
        )
        if result is None:
            return False
        params, _ = result
        consumed = await connection.scalar(
            text(
                """UPDATE app.one_time_tokens
                   SET consumed_at = clock_timestamp()
                   WHERE user_id = :actor AND purpose = 'reauthenticate'
                     AND token_hash = :digest AND consumed_at IS NULL
                     AND expires_at > clock_timestamp()
                     AND reauth_session_id = :session
                     AND reauth_workspace_id = :workspace
                     AND reauth_purpose = :purpose
                     AND reauth_user_version = :user_version
                     AND reauth_workspace_version = :workspace_version
                   RETURNING true"""
            ),
            {**params, "digest": token_hash},
        )
        return consumed is True
