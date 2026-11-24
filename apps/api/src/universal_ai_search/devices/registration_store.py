"""RLS-scoped challenge state and atomic device issuance in PostgreSQL."""

from __future__ import annotations

import hmac
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from universal_ai_search.api.context import current_request_id

from .registration import (
    DevicePlatform,
    DeviceRegistrationRateLimited,
    DeviceRegistrationRejected,
    DeviceRegistrationUnavailable,
    RegistrationScope,
)

_LIVE = """
    id = :challenge_id AND workspace_id = :workspace_id AND user_id = :user_id
    AND session_id = :session_id
    AND user_authorization_version = :user_version
    AND workspace_authorization_version = :workspace_version
    AND consumed_at IS NULL
"""


def _parameters(scope: RegistrationScope, challenge_id: UUID) -> dict[str, UUID | int]:
    return {
        "workspace_id": scope.workspace_id,
        "user_id": scope.user_id,
        "session_id": scope.session_id,
        "user_version": scope.user_authorization_version,
        "workspace_version": scope.workspace_authorization_version,
        "challenge_id": challenge_id,
    }


class SQLAlchemyDeviceRegistrationStore:
    def __init__(self, engine: AsyncEngine, *, audit_key: bytes) -> None:
        if len(audit_key) < 32:
            raise ValueError("device audit key must be at least 32 bytes")
        self._engine = engine
        self._audit_key = audit_key

    @staticmethod
    async def _check_scope(
        connection: AsyncConnection,
        scope: RegistrationScope,
        params: dict[str, UUID | int],
    ) -> None:
        await connection.execute(
            text(
                "SELECT set_config('app.user_id', :actor, true), "
                "set_config('app.workspace_id', :workspace, true)"
            ),
            {"actor": str(scope.user_id), "workspace": str(scope.workspace_id)},
        )
        active = await connection.scalar(
            text(
                """SELECT true FROM app.workspace_members AS m
                JOIN app.workspaces AS w ON w.id = m.workspace_id
                JOIN app.users AS u ON u.id = m.user_id
                JOIN app.sessions AS s ON s.user_id = u.id
                WHERE m.workspace_id = :workspace_id AND m.user_id = :user_id
                  AND m.status = 'active' AND w.status = 'active'
                  AND w.authorization_version = :workspace_version
                  AND u.status = 'active' AND u.email_verified_at IS NOT NULL
                  AND u.lock_version = :user_version
                  AND s.id = :session_id AND s.authorization_version = :user_version
                  AND s.rotated_at IS NULL AND s.revoked_at IS NULL
                  AND s.expires_at > clock_timestamp()
                FOR SHARE OF m, w, u, s"""
            ),
            params,
        )
        if active is not True:
            raise DeviceRegistrationRejected

    async def issue(self, scope: RegistrationScope, challenge_id: UUID) -> datetime:
        params = _parameters(scope, challenge_id)
        try:
            async with self._engine.begin() as connection:
                await self._check_scope(connection, scope, params)
                expiry = await connection.scalar(
                    text(
                        """WITH issued AS (SELECT clock_timestamp() AS ts)
                        INSERT INTO app.device_registration_challenges (
                            id, workspace_id, user_id, session_id,
                            user_authorization_version, workspace_authorization_version,
                            issued_at, expires_at
                        )
                        SELECT :challenge_id, :workspace_id, :user_id, :session_id,
                               :user_version, :workspace_version, ts,
                               ts + INTERVAL '60 seconds'
                        FROM issued
                        ON CONFLICT (workspace_id, session_id) DO UPDATE SET
                            id = EXCLUDED.id, user_id = EXCLUDED.user_id,
                            user_authorization_version =
                                EXCLUDED.user_authorization_version,
                            workspace_authorization_version =
                                EXCLUDED.workspace_authorization_version,
                            issued_at = EXCLUDED.issued_at,
                            expires_at = EXCLUDED.expires_at, consumed_at = NULL
                        WHERE device_registration_challenges.issued_at
                              <= clock_timestamp() - INTERVAL '10 seconds'
                        RETURNING expires_at"""
                    ),
                    params,
                )
                if expiry is None:
                    raise DeviceRegistrationRateLimited
                return cast(datetime, expiry)
        except SQLAlchemyError:
            raise DeviceRegistrationUnavailable from None

    async def is_live(self, scope: RegistrationScope, challenge_id: UUID) -> bool:
        params = _parameters(scope, challenge_id)
        try:
            async with self._engine.begin() as connection:
                await self._check_scope(connection, scope, params)
                return (
                    await connection.scalar(
                        text(
                            "SELECT true FROM app.device_registration_challenges "
                            "WHERE " + _LIVE + " AND expires_at > clock_timestamp()"
                        ),
                        params,
                    )
                    is True
                )
        except SQLAlchemyError:
            raise DeviceRegistrationUnavailable from None

    async def complete(
        self,
        *,
        scope: RegistrationScope,
        challenge_id: UUID,
        device_id: UUID,
        connection_id: UUID,
        public_key: bytes,
        credential_hash: bytes,
        platform: DevicePlatform,
        app_version: str,
        display_name: str,
    ) -> None:
        params = _parameters(scope, challenge_id)
        registration = {
            **params,
            "device_id": device_id,
            "connection_id": connection_id,
            "public_key": public_key,
            "credential_hash": credential_hash,
            "platform": platform,
            "app_version": app_version,
            "name": display_name,
        }
        try:
            async with self._engine.begin() as connection:
                await self._check_scope(connection, scope, params)
                locked = await connection.scalar(
                    text(
                        "SELECT id FROM app.device_registration_challenges "
                        "WHERE " + _LIVE + " FOR UPDATE"
                    ),
                    params,
                )
                if locked is None:
                    raise DeviceRegistrationRejected
                consumed = await connection.scalar(
                    text(
                        """WITH claim_time AS (SELECT clock_timestamp() AS ts)
                        UPDATE app.device_registration_challenges AS challenge
                        SET consumed_at = claim_time.ts FROM claim_time
                        WHERE challenge.id = :challenge_id
                          AND challenge.expires_at > claim_time.ts
                        RETURNING challenge.id"""
                    ),
                    params,
                )
                if consumed is None:
                    raise DeviceRegistrationRejected
                await connection.execute(
                    text(
                        """INSERT INTO app.connections (
                            id, workspace_id, owner_user_id, provider,
                            display_label, status
                        ) VALUES (
                            :connection_id, :workspace_id, :user_id,
                            'local_files', 'Local files', 'pending'
                        )"""
                    ),
                    registration,
                )
                await connection.execute(
                    text(
                        """INSERT INTO app.devices (
                            id, workspace_id, user_id, connection_id, name,
                            platform, app_version, public_key, credential_hash,
                            status
                        ) VALUES (
                            :device_id, :workspace_id, :user_id, :connection_id, :name,
                            :platform, :app_version, :public_key, :credential_hash,
                            'active'
                        )"""
                    ),
                    registration,
                )
                await connection.execute(
                    text(
                        """INSERT INTO app.audit_events (
                            id, workspace_id, workspace_ref_hash, actor_user_id,
                            action, target_type, outcome, target_ref_hash, request_id
                        ) VALUES (
                            :audit_id, :workspace_id, :scope_hash, :user_id,
                            'device.registered', 'device', 'succeeded', :target_hash,
                            :request_id
                        )"""
                    ),
                    {
                        **params,
                        "audit_id": uuid4(),
                        "scope_hash": hmac.digest(
                            self._audit_key,
                            b"workspace:" + scope.workspace_id.bytes,
                            "sha256",
                        ),
                        "target_hash": hmac.digest(
                            self._audit_key, b"device:" + device_id.bytes, "sha256"
                        ),
                        "request_id": current_request_id(),
                    },
                )
        except IntegrityError as error:
            if getattr(error.orig, "sqlstate", None) == "23505":
                raise DeviceRegistrationRejected from None
            raise DeviceRegistrationUnavailable from None
        except SQLAlchemyError:
            raise DeviceRegistrationUnavailable from None
