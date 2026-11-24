"""Atomic conversation privacy cutoff and content-free deletion receipts."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from universal_ai_search.api.idempotency import validate_idempotency_key

from .repository import (
    ConversationBusy,
    ConversationKeyConflict,
    ConversationMissing,
    ConversationRepository,
    ConversationScope,
)

DELETE_ROUTE = "/v1/conversations/{conversation_id}"
DELETE_CONFIRMATION = "delete-conversation"
RESOURCE = "conversation_deletion"


@dataclass(frozen=True)
class ConversationDeletion:
    id: UUID
    created_at: datetime
    started_at: datetime
    completed_at: datetime
    deadline_at: datetime


def _receipt(row: RowMapping) -> ConversationDeletion:
    return ConversationDeletion(
        row["id"],
        row["requested_at"],
        row["started_at"],
        row["completed_at"],
        row["deadline_at"],
    )


class ConversationDeletionStore:
    def __init__(
        self, engine: AsyncEngine, *, idempotency_secret: bytes, audit_secret: bytes
    ) -> None:
        if min(len(idempotency_secret), len(audit_secret)) < 32:
            raise ValueError("deletion secrets must be at least 32 bytes")
        self._engine = engine
        self._idempotency_secret = idempotency_secret
        self._audit_secret = audit_secret

    async def delete(
        self,
        *,
        scope: ConversationScope,
        conversation_id: UUID,
        idempotency_key: str,
        request_id: UUID,
    ) -> ConversationDeletion:
        key = validate_idempotency_key(idempotency_key)
        key_hash = hmac.digest(
            self._idempotency_secret,
            b"conversation-delete-key:" + key.encode(),
            "sha256",
        )
        request_hash = hmac.digest(
            self._idempotency_secret,
            b"conversation-delete-target:" + conversation_id.bytes,
            "sha256",
        )
        lock_id = int.from_bytes(
            hashlib.sha256(
                DELETE_ROUTE.encode()
                + scope.workspace_id.bytes
                + scope.user_id.bytes
                + key_hash
            ).digest()[:8],
            "big",
            signed=True,
        )
        async with self._engine.begin() as connection:
            context = await ConversationRepository._authorize(connection, scope)
            if (
                await connection.scalar(
                    text("SELECT pg_try_advisory_xact_lock(CAST(:key AS BIGINT))"),
                    {"key": lock_id},
                )
                is not True
            ):
                raise ConversationBusy
            params = {
                **context,
                "target": conversation_id,
                "key_hash": key_hash,
                "route": DELETE_ROUTE,
                "type": RESOURCE,
                "request_hash": request_hash,
            }

            prior = await self._prior_receipt(connection, params, request_hash)
            if prior is not None:
                return await self._get(
                    connection, context, prior, target=conversation_id
                )

            # A real update atomically obtains the exclusive row lock shared by
            # append/read paths. No private title remains in the tombstone.
            created = await connection.scalar(
                text(
                    """UPDATE app.conversations
                       SET status = 'deleted', title = '[deleted]',
                           updated_at = clock_timestamp(),
                           deleted_at = clock_timestamp()
                       WHERE id = :target AND workspace_id = :workspace
                         AND user_id = :actor AND status = 'active'
                         AND deleted_at IS NULL
                       RETURNING id"""
                ),
                params,
            )
            if created is None:
                raise ConversationMissing

            # FK cascades remove message claims and citations; indexed source
            # content and unrelated search history are not affected.
            await connection.execute(
                text(
                    """DELETE FROM app.messages
                       WHERE workspace_id = :workspace AND conversation_id = :target"""
                ),
                params,
            )
            await connection.execute(
                text(
                    """DELETE FROM app.search_requests
                       WHERE workspace_id = :workspace AND user_id = :actor
                         AND conversation_id = :target"""
                ),
                params,
            )
            receipt_id = uuid4()
            receipt = await connection.execute(
                text(
                    """INSERT INTO app.deletion_requests
                         (id, workspace_id, requested_by_user_id, target_type,
                          target_id, status, idempotency_key, deadline_at,
                          started_at, completed_at)
                       VALUES (:receipt, :workspace, :actor, 'conversation', :target,
                               'completed', :receipt_key,
                               transaction_timestamp() + INTERVAL '24 hours',
                               transaction_timestamp(), clock_timestamp())
                       RETURNING id, requested_at, started_at, completed_at,
                           deadline_at"""
                ),
                {**params, "receipt": receipt_id, "receipt_key": key_hash.hex()},
            )
            await self._remember(connection, params, receipt_id)
            await connection.execute(
                text(
                    """INSERT INTO app.audit_events
                       (id, workspace_id, workspace_ref_hash, actor_user_id,
                        action, target_type, outcome, target_ref_hash, request_id)
                       VALUES (:event, :workspace, :workspace_hash, :actor,
                               'conversation.delete', 'conversation', 'succeeded',
                               :target_hash, :request_id)"""
                ),
                {
                    **params,
                    "event": uuid4(),
                    "workspace_hash": hmac.digest(
                        self._audit_secret,
                        b"workspace:" + scope.workspace_id.bytes,
                        "sha256",
                    ),
                    "target_hash": hmac.digest(
                        self._audit_secret,
                        b"conversation:" + conversation_id.bytes,
                        "sha256",
                    ),
                    "request_id": request_id,
                },
            )
            return _receipt(receipt.mappings().one())

    @staticmethod
    async def _prior_receipt(
        connection: AsyncConnection, params: dict[str, object], request_hash: bytes
    ) -> UUID | None:
        previous = await connection.execute(
            text(
                """SELECT request_hash, status, response_status,
                          resource_type, resource_id
                   FROM app.api_idempotency_keys
                   WHERE workspace_id = :workspace AND user_id = :actor
                     AND method = 'DELETE' AND route_template = :route
                     AND key_hash = :key_hash AND expires_at > clock_timestamp()"""
            ),
            params,
        )
        row = previous.mappings().one_or_none()
        if row is None:
            return None
        if not hmac.compare_digest(row["request_hash"], request_hash):
            raise ConversationKeyConflict
        if row["status"] == "processing":
            raise ConversationBusy
        receipt_id = row["resource_id"]
        if (
            row["status"] != "completed"
            or row["response_status"] != 202
            or row["resource_type"] != RESOURCE
            or not isinstance(receipt_id, UUID)
        ):
            raise ConversationKeyConflict
        return receipt_id

    @staticmethod
    async def _remember(
        connection: AsyncConnection, params: dict[str, object], receipt_id: UUID
    ) -> None:
        written = await connection.scalar(
            text(
                """INSERT INTO app.api_idempotency_keys AS entry
                     (id, workspace_id, user_id, key_hash, method, route_template,
                      request_hash, status, response_status, resource_type,
                      resource_id, expires_at)
                   VALUES (:reservation, :workspace, :actor, :key_hash, 'DELETE',
                           :route, :request_hash, 'completed', 202, :type, :receipt,
                           clock_timestamp() + INTERVAL '24 hours')
                   ON CONFLICT (workspace_id, user_id, method, route_template, key_hash)
                   DO UPDATE SET request_hash = EXCLUDED.request_hash,
                                 status = 'completed', response_status = 202,
                                 response_body = NULL, resource_type = :type,
                                 resource_id = :receipt, created_at = clock_timestamp(),
                                 updated_at = clock_timestamp(),
                                 expires_at = EXCLUDED.expires_at
                   WHERE entry.expires_at <= clock_timestamp()
                   RETURNING true"""
            ),
            {**params, "reservation": uuid4(), "receipt": receipt_id},
        )
        if written is not True:
            raise ConversationBusy

    async def read(
        self, *, scope: ConversationScope, receipt_id: UUID
    ) -> ConversationDeletion:
        async with self._engine.begin() as connection:
            context = await ConversationRepository._authorize(connection, scope)
            return await self._get(connection, context, receipt_id)

    @staticmethod
    async def _get(
        connection: AsyncConnection,
        context: dict[str, object],
        receipt_id: UUID,
        *,
        target: UUID | None = None,
    ) -> ConversationDeletion:
        result = await connection.execute(
            text(
                """SELECT d.id, d.requested_at, d.started_at,
                          d.completed_at, d.deadline_at
                   FROM app.deletion_requests d
                   JOIN app.conversations c ON c.workspace_id = d.workspace_id
                                            AND c.id = d.target_id
                   WHERE d.id = :receipt AND d.workspace_id = :workspace
                     AND d.target_type = 'conversation' AND d.status = 'completed'
                     AND d.requested_by_user_id = :actor AND c.user_id = :actor
                     AND c.status = 'deleted' AND c.deleted_at IS NOT NULL
                     AND (CAST(:target AS UUID) IS NULL OR d.target_id = :target)"""
            ),
            {**context, "receipt": receipt_id, "target": target},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise ConversationMissing
        return _receipt(row)
