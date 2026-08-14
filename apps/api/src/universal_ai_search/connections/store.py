"""Tenant-scoped persistence for OAuth transactions and Google connections."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .crypto import EncryptedEnvelope


@dataclass(frozen=True)
class OAuthTransaction:
    id: UUID
    workspace_id: UUID
    user_id: UUID
    encrypted_payload: EncryptedEnvelope
    redirect_path: str


class GoogleConnectionStore(Protocol):
    async def create_transaction(
        self,
        *,
        transaction_id: UUID,
        workspace_id: UUID,
        user_id: UUID,
        state_hash: bytes,
        nonce_hash: bytes,
        encrypted_payload: EncryptedEnvelope,
        redirect_path: str,
        expires_at: datetime,
    ) -> None: ...

    async def consume_transaction(
        self, *, workspace_id: UUID, user_id: UUID, state_hash: bytes
    ) -> OAuthTransaction | None: ...

    async def connection_id_for_account(
        self,
        *,
        workspace_id: UUID,
        user_id: UUID,
        external_account_hash: bytes,
        proposed_id: UUID,
    ) -> UUID: ...

    async def save_connection(
        self,
        *,
        connection_id: UUID,
        workspace_id: UUID,
        user_id: UUID,
        external_account_hash: bytes,
        display_label: str,
        credentials: EncryptedEnvelope,
        scopes: frozenset[str],
        source_families: tuple[str, ...],
    ) -> UUID: ...


async def _set_context(
    connection: AsyncConnection, *, workspace_id: UUID, user_id: UUID
) -> None:
    await connection.execute(
        text("SELECT set_config('app.user_id', :user_id, true)"),
        {"user_id": str(user_id)},
    )
    await connection.execute(
        text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
        {"workspace_id": str(workspace_id)},
    )


class SQLAlchemyGoogleConnectionStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_transaction(self, **values: object) -> None:
        envelope = values.pop("encrypted_payload")
        assert isinstance(envelope, EncryptedEnvelope)
        async with self._engine.begin() as connection:
            await _set_context(
                connection,
                workspace_id=values["workspace_id"],  # type: ignore[arg-type]
                user_id=values["user_id"],  # type: ignore[arg-type]
            )
            await connection.execute(
                text(
                    """INSERT INTO app.oauth_transactions (
                    id, workspace_id, user_id, provider, state_hash, nonce_hash,
                    pkce_verifier_ciphertext, encrypted_data_key, key_version,
                    redirect_path, expires_at
                    ) VALUES (
                    :transaction_id, :workspace_id, :user_id, 'google',
                    :state_hash, :nonce_hash, :ciphertext, :encrypted_data_key,
                    :key_version, :redirect_path, :expires_at)"""
                ),
                {
                    **values,
                    "ciphertext": envelope.ciphertext,
                    "encrypted_data_key": envelope.encrypted_data_key,
                    "key_version": envelope.key_version,
                },
            )

    async def consume_transaction(
        self, *, workspace_id: UUID, user_id: UUID, state_hash: bytes
    ) -> OAuthTransaction | None:
        async with self._engine.begin() as connection:
            await _set_context(connection, workspace_id=workspace_id, user_id=user_id)
            row = (
                (
                    await connection.execute(
                        text(
                            """UPDATE app.oauth_transactions SET
                            consumed_at = clock_timestamp()
                            WHERE workspace_id = :workspace_id
                              AND user_id = :user_id AND provider = 'google'
                              AND state_hash = :state_hash
                              AND consumed_at IS NULL
                              AND expires_at > clock_timestamp()
                            RETURNING id, workspace_id, user_id,
                              pkce_verifier_ciphertext AS ciphertext,
                              encrypted_data_key, key_version, redirect_path"""
                        ),
                        {
                            "workspace_id": workspace_id,
                            "user_id": user_id,
                            "state_hash": state_hash,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return OAuthTransaction(
            id=row["id"],
            workspace_id=row["workspace_id"],
            user_id=row["user_id"],
            encrypted_payload=EncryptedEnvelope(
                ciphertext=row["ciphertext"],
                encrypted_data_key=row["encrypted_data_key"],
                key_version=row["key_version"],
            ),
            redirect_path=row["redirect_path"],
        )

    async def connection_id_for_account(
        self,
        *,
        workspace_id: UUID,
        user_id: UUID,
        external_account_hash: bytes,
        proposed_id: UUID,
    ) -> UUID:
        async with self._engine.begin() as connection:
            await _set_context(connection, workspace_id=workspace_id, user_id=user_id)
            value = await connection.scalar(
                text(
                    """SELECT id FROM app.connections
                    WHERE workspace_id = :workspace_id AND provider = 'google'
                      AND external_account_id_hash = :account_hash
                      AND status <> 'deleted'"""
                ),
                {
                    "workspace_id": workspace_id,
                    "account_hash": external_account_hash,
                },
            )
        return value if isinstance(value, UUID) else proposed_id

    async def save_connection(
        self,
        *,
        connection_id: UUID,
        workspace_id: UUID,
        user_id: UUID,
        external_account_hash: bytes,
        display_label: str,
        credentials: EncryptedEnvelope,
        scopes: frozenset[str],
        source_families: tuple[str, ...],
    ) -> UUID:
        async with self._engine.begin() as connection:
            await _set_context(connection, workspace_id=workspace_id, user_id=user_id)
            existing = (
                (
                    await connection.execute(
                        text(
                            """SELECT id FROM app.connections
                            WHERE workspace_id = :workspace_id
                              AND provider = 'google'
                              AND external_account_id_hash = :account_hash
                              AND status <> 'deleted' FOR UPDATE"""
                        ),
                        {
                            "workspace_id": workspace_id,
                            "account_hash": external_account_hash,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
            effective_id = existing["id"] if existing else connection_id
            if effective_id != connection_id:
                raise ValueError("connection identity changed during authorization")
            if existing:
                await connection.execute(
                    text(
                        """UPDATE app.connections SET display_label = :display_label,
                        status = 'active', credential_ciphertext = :ciphertext,
                        encrypted_data_key = :encrypted_data_key,
                        key_version = :key_version, last_error_code = NULL,
                        updated_at = clock_timestamp(), lock_version = lock_version + 1
                        WHERE id = :connection_id"""
                    ),
                    self._connection_values(effective_id, display_label, credentials),
                )
                await connection.execute(
                    text("DELETE FROM app.connection_scopes WHERE connection_id = :id"),
                    {"id": effective_id},
                )
            else:
                await connection.execute(
                    text(
                        """INSERT INTO app.connections (
                        id, workspace_id, owner_user_id, provider,
                        external_account_id_hash, display_label, status,
                        credential_ciphertext, encrypted_data_key, key_version
                        ) VALUES (
                        :connection_id, :workspace_id, :user_id, 'google',
                        :account_hash, :display_label, 'active', :ciphertext,
                        :encrypted_data_key, :key_version)"""
                    ),
                    {
                        **self._connection_values(
                            effective_id, display_label, credentials
                        ),
                        "workspace_id": workspace_id,
                        "user_id": user_id,
                        "account_hash": external_account_hash,
                    },
                )
            for scope in sorted(scopes):
                await connection.execute(
                    text(
                        """INSERT INTO app.connection_scopes (
                        workspace_id, connection_id, scope
                        ) VALUES (:workspace_id, :connection_id, :scope)"""
                    ),
                    {
                        "workspace_id": workspace_id,
                        "connection_id": effective_id,
                        "scope": scope,
                    },
                )
            await self._queue_initial_sync(
                connection,
                workspace_id=workspace_id,
                connection_id=effective_id,
                source_families=source_families,
            )
        return effective_id

    @staticmethod
    def _connection_values(
        connection_id: UUID, display_label: str, credentials: EncryptedEnvelope
    ) -> dict[str, object]:
        return {
            "connection_id": connection_id,
            "display_label": display_label,
            "ciphertext": credentials.ciphertext,
            "encrypted_data_key": credentials.encrypted_data_key,
            "key_version": credentials.key_version,
        }

    @staticmethod
    async def _queue_initial_sync(
        connection: AsyncConnection,
        *,
        workspace_id: UUID,
        connection_id: UUID,
        source_families: tuple[str, ...],
    ) -> None:
        job_id = uuid5(connection_id, "google-initial-sync")
        payload = json.dumps({"source_families": list(source_families), "mode": "full"})
        await connection.execute(
            text("DELETE FROM app.job_attempts WHERE job_id = :job_id"),
            {"job_id": job_id},
        )
        await connection.execute(
            text(
                """INSERT INTO app.jobs (
                id, workspace_id, connection_id, job_type, queue,
                idempotency_key, status, payload
                ) VALUES (
                :job_id, :workspace_id, :connection_id, 'sync', 'sync',
                :idempotency_key, 'pending', CAST(:payload AS JSONB))
                ON CONFLICT (workspace_id, job_type, idempotency_key)
                DO UPDATE SET status = 'pending', available_at = clock_timestamp(),
                  payload = EXCLUDED.payload, updated_at = clock_timestamp(),
                  attempt_count = 0, lease_owner = NULL, lease_expires_at = NULL,
                  completed_at = NULL, error_code = NULL"""
            ),
            {
                "job_id": job_id,
                "workspace_id": workspace_id,
                "connection_id": connection_id,
                "idempotency_key": f"google-initial:{connection_id}",
                "payload": payload,
            },
        )
        await connection.execute(
            text(
                """INSERT INTO app.outbox_events (
                id, workspace_id, aggregate_type, aggregate_id, event_type, payload
                ) VALUES (
                :event_id, :workspace_id, 'connection', :connection_id,
                'connection.sync.requested', CAST(:payload AS JSONB))"""
            ),
            {
                "event_id": uuid4(),
                "workspace_id": workspace_id,
                "connection_id": connection_id,
                "payload": json.dumps({"job_id": str(job_id)}),
            },
        )
