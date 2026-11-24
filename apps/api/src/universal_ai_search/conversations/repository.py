"""Transactional, owner-only conversation storage under PostgreSQL RLS."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

ROUTE = "/v1/conversations"
type SortKey = tuple[datetime, UUID]
type MessageStatus = Literal[
    "pending", "complete", "insufficient_evidence", "interrupted", "failed"
]


@dataclass(frozen=True)
class ConversationScope:
    workspace_id: UUID
    user_id: UUID
    session_id: UUID
    workspace_version: int
    user_version: int


@dataclass(frozen=True)
class Conversation:
    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Message:
    id: UUID
    role: Literal["user", "assistant"]
    status: MessageStatus
    content: str | None
    created_at: datetime
    completed_at: datetime | None


class ConversationDenied(Exception):
    pass


class ConversationMissing(Exception):
    pass


class ConversationBusy(Exception):
    pass


class ConversationKeyConflict(Exception):
    pass


def _conversation(row: RowMapping) -> Conversation:
    return Conversation(row["id"], row["title"], row["created_at"], row["updated_at"])


def _message(row: RowMapping) -> Message:
    return Message(
        row["id"],
        row["role"],
        row["status"],
        row["content"],
        row["created_at"],
        row["completed_at"],
    )


class ConversationRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @staticmethod
    async def _authorize(
        connection: AsyncConnection, scope: ConversationScope
    ) -> dict[str, object]:
        await connection.execute(
            text(
                """SELECT set_config('app.workspace_id', :workspace, true),
                          set_config('app.user_id', :actor, true)"""
            ),
            {"workspace": str(scope.workspace_id), "actor": str(scope.user_id)},
        )
        values: dict[str, object] = {
            "workspace": scope.workspace_id,
            "actor": scope.user_id,
            "session": scope.session_id,
            "workspace_version": scope.workspace_version,
            "user_version": scope.user_version,
        }
        allowed = await connection.scalar(
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
            values,
        )
        if allowed is not True:
            raise ConversationDenied
        return values

    @staticmethod
    async def _existing(
        connection: AsyncConnection,
        values: dict[str, object],
        conversation_id: UUID,
    ) -> Conversation:
        result = await connection.execute(
            text(
                """SELECT id, title, created_at, updated_at FROM app.conversations
                   WHERE id = :id AND workspace_id = :workspace AND user_id = :actor
                     AND status = 'active' AND deleted_at IS NULL
                   FOR SHARE"""
            ),
            {**values, "id": conversation_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise ConversationMissing
        return _conversation(row)

    async def create(
        self,
        *,
        scope: ConversationScope,
        title: str,
        key_hash: bytes,
        request_hash: bytes,
    ) -> Conversation:
        """Create and remember only the new resource ID in the same transaction."""
        lock_id = int.from_bytes(
            hashlib.sha256(
                ROUTE.encode()
                + scope.workspace_id.bytes
                + scope.user_id.bytes
                + key_hash
            ).digest()[:8],
            byteorder="big",
            signed=True,
        )
        async with self._engine.begin() as connection:
            values = await self._authorize(connection, scope)
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_xact_lock(CAST(:key AS BIGINT))"),
                {"key": lock_id},
            )
            if acquired is not True:
                raise ConversationBusy

            replay_parameters = {
                **values,
                "method": "POST",
                "route": ROUTE,
                "key_hash": key_hash,
                "request_hash": request_hash,
            }
            prior = await connection.execute(
                text(
                    """SELECT request_hash, status, response_status,
                              resource_type, resource_id
                       FROM app.api_idempotency_keys
                       WHERE workspace_id = :workspace AND user_id = :actor
                         AND method = :method AND route_template = :route
                         AND key_hash = :key_hash AND expires_at > clock_timestamp()"""
                ),
                replay_parameters,
            )
            previous = prior.mappings().one_or_none()
            if previous is not None:
                if not hmac.compare_digest(previous["request_hash"], request_hash):
                    raise ConversationKeyConflict
                if previous["status"] == "processing":
                    raise ConversationBusy
                if (
                    previous["status"] != "completed"
                    or previous["resource_type"] != "conversation"
                    or previous["response_status"] != 201
                    or previous["resource_id"] is None
                ):
                    raise ConversationKeyConflict
                return await self._existing(connection, values, previous["resource_id"])

            new_id = uuid4()
            inserted = await connection.execute(
                text(
                    """INSERT INTO app.conversations (id, workspace_id, user_id,
                                                       title, status)
                       VALUES (:id, :workspace, :actor, :title, 'active')
                       RETURNING id, title, created_at, updated_at"""
                ),
                {**values, "id": new_id, "title": title},
            )
            row = inserted.mappings().one()
            await connection.execute(
                text(
                    """INSERT INTO app.api_idempotency_keys
                         (id, workspace_id, user_id, key_hash, method, route_template,
                          request_hash, status, response_status, resource_type,
                          resource_id, expires_at)
                       VALUES (:reservation, :workspace, :actor, :key_hash, :method,
                               :route, :request_hash, 'completed', 201, 'conversation',
                               :resource, clock_timestamp() + INTERVAL '24 hours')
                       ON CONFLICT (workspace_id, user_id, method, route_template,
                                    key_hash) DO UPDATE
                         SET request_hash = EXCLUDED.request_hash, status = 'completed',
                             response_status = 201, response_body = NULL,
                             resource_type = 'conversation', resource_id = :resource,
                             created_at = clock_timestamp(),
                             updated_at = clock_timestamp(),
                             expires_at = EXCLUDED.expires_at"""
                ),
                {**replay_parameters, "reservation": uuid4(), "resource": new_id},
            )
            return _conversation(row)

    async def list_owned(
        self, *, scope: ConversationScope, limit: int, after: SortKey | None
    ) -> list[Conversation]:
        async with self._engine.begin() as connection:
            values = await self._authorize(connection, scope)
            result = await connection.execute(
                text(
                    """SELECT id, title, created_at, updated_at FROM app.conversations
                       WHERE workspace_id = :workspace AND user_id = :actor
                         AND status = 'active' AND deleted_at IS NULL
                         AND (CAST(:after_at AS TIMESTAMPTZ) IS NULL
                              OR (created_at, id) < (:after_at, :after_id))
                       ORDER BY created_at DESC, id DESC LIMIT :limit"""
                ),
                {**values, **_page_parameters(after, limit)},
            )
            return [_conversation(row) for row in result.mappings().all()]

    async def read(
        self,
        *,
        scope: ConversationScope,
        conversation_id: UUID,
        limit: int,
        after: SortKey | None,
    ) -> tuple[Conversation, list[Message]]:
        async with self._engine.begin() as connection:
            values = await self._authorize(connection, scope)
            conversation = await self._existing(connection, values, conversation_id)
            # Until citation reauthorization exists, never hydrate assistant
            # text or call it complete. Do not transfer it out of PostgreSQL.
            result = await connection.execute(
                text(
                    """SELECT id, role,
                         CASE WHEN role = 'assistant' AND status = 'complete'
                              THEN 'insufficient_evidence' ELSE status END AS status,
                         CASE WHEN role = 'user' AND status = 'complete' THEN content
                              ELSE NULL END AS content,
                         created_at, completed_at
                       FROM app.messages
                       WHERE workspace_id = :workspace AND conversation_id = :id
                         AND (CAST(:after_at AS TIMESTAMPTZ) IS NULL
                              OR (created_at, id) > (:after_at, :after_id))
                       ORDER BY created_at ASC, id ASC LIMIT :limit"""
                ),
                {**values, "id": conversation_id, **_page_parameters(after, limit)},
            )
            return conversation, [_message(row) for row in result.mappings().all()]


def _page_parameters(after: SortKey | None, limit: int) -> dict[str, object]:
    return {
        "after_at": after[0] if after is not None else None,
        "after_id": after[1] if after is not None else None,
        "limit": limit,
    }
