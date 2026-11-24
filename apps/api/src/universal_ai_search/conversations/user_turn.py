"""Persist one authorized user turn; never writes or reads assistant text.

This adapter is deliberately internal. Completing a user-turn transaction is
not completion of an assistant attempt or of the future public SSE request.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from universal_ai_search.api.idempotency import validate_idempotency_key

from .repository import (
    ConversationBusy,
    ConversationKeyConflict,
    ConversationMissing,
    ConversationRepository,
    ConversationScope,
    Message,
    _message,
)

USER_TURN_NAMESPACE = "/internal/conversations/{conversation_id}/user-turn"
RESOURCE_TYPE = "conversation_user_turn"
MAX_USER_CONTENT_LENGTH = 10_000


class UserTurnInputError(Exception):
    """Invalid user message content or client message identifier."""


@dataclass(frozen=True)
class SavedUserTurn:
    message: Message = field(repr=False)
    created: bool


def _prepare(
    *,
    secret: bytes,
    scope: ConversationScope,
    conversation_id: UUID,
    client_message_id: UUID,
    content: str,
    idempotency_key: str,
) -> tuple[UUID, bytes, bytes, int]:
    # Canonical UUID inputs are decoded at the HTTP boundary. Reject a nil
    # client identifier to avoid accidentally treating every turn as one.
    if (
        not isinstance(client_message_id, UUID)
        or client_message_id.int == 0
        or not isinstance(content, str)
        or len(content) > MAX_USER_CONTENT_LENGTH
        or not content.strip()
        or "\x00" in content
    ):
        raise UserTurnInputError
    try:
        content_bytes = content.encode("utf-8")
    except UnicodeEncodeError:
        raise UserTurnInputError from None

    key = validate_idempotency_key(idempotency_key)
    message_id = uuid5(conversation_id, f"uas-user-turn-v1:{client_message_id}")
    key_hash = hmac.digest(
        secret, b"conversation-user-turn-key:" + key.encode(), "sha256"
    )
    # The message ID already binds the conversation and client ID. HMAC the
    # body so this table is not a dictionary oracle for short private prompts.
    request_hash = hmac.digest(
        secret,
        b"conversation-user-turn-body:" + message_id.bytes + content_bytes,
        "sha256",
    )
    lock_digest = hashlib.sha256(
        USER_TURN_NAMESPACE.encode()
        + scope.workspace_id.bytes
        + scope.user_id.bytes
        + key_hash
    ).digest()
    return (
        message_id,
        key_hash,
        request_hash,
        int.from_bytes(lock_digest[:8], "big", signed=True),
    )


class UserTurnStore:
    def __init__(self, engine: AsyncEngine, *, idempotency_secret: bytes) -> None:
        if len(idempotency_secret) < 32:
            raise ValueError("idempotency secret must be at least 32 bytes")
        self._engine = engine
        self._secret = idempotency_secret

    async def append(
        self,
        *,
        scope: ConversationScope,
        conversation_id: UUID,
        client_message_id: UUID,
        content: str,
        idempotency_key: str,
    ) -> SavedUserTurn:
        message_id, key_hash, request_hash, lock_id = _prepare(
            secret=self._secret,
            scope=scope,
            conversation_id=conversation_id,
            client_message_id=client_message_id,
            content=content,
            idempotency_key=idempotency_key,
        )
        async with self._engine.begin() as connection:
            context = await ConversationRepository._authorize(connection, scope)
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_xact_lock(CAST(:id AS BIGINT))"),
                {"id": lock_id},
            )
            if acquired is not True:
                raise ConversationBusy
            params = {
                **context,
                "conversation": conversation_id,
                "message": message_id,
                "route": USER_TURN_NAMESPACE,
                "resource_type": RESOURCE_TYPE,
                "key_hash": key_hash,
                "request_hash": request_hash,
            }
            # Serializes distinct request keys for the same logical turn and
            # keeps delete/revoke from racing message creation or its replay.
            await self._lock_owned_conversation(connection, params)
            replay = await self._check_replay(
                connection, params, message_id, request_hash
            )
            prior_message = await self._read_user_turn(connection, params)
            if prior_message is not None:
                if prior_message.role != "user" or prior_message.status != "complete":
                    raise ConversationKeyConflict
                if prior_message.content != content:
                    raise ConversationKeyConflict
                if not replay:
                    await self._remember(connection, params)
                return SavedUserTurn(prior_message, created=False)
            if replay:
                # A saved key must not recreate a formerly deleted message.
                raise ConversationMissing

            message = await self._insert_user_turn(connection, params, content)
            await self._remember(connection, params)
            return SavedUserTurn(message, created=True)

    @staticmethod
    async def _insert_user_turn(
        connection: AsyncConnection, params: dict[str, object], content: str
    ) -> Message:
        created = await connection.execute(
            text(
                """INSERT INTO app.messages
                   (id, workspace_id, conversation_id, role, status, content,
                    created_at, completed_at)
                   VALUES (:message, :workspace, :conversation, 'user', 'complete',
                           :content, statement_timestamp(), statement_timestamp())
                   RETURNING id, role, status, content, created_at, completed_at"""
            ),
            {**params, "content": content},
        )
        message = _message(created.mappings().one())
        await connection.execute(
            text(
                """UPDATE app.conversations
                   SET updated_at = greatest(updated_at, clock_timestamp())
                   WHERE workspace_id = :workspace AND user_id = :actor
                     AND id = :conversation"""
            ),
            params,
        )
        return message

    @staticmethod
    async def _lock_owned_conversation(
        connection: AsyncConnection, params: dict[str, object]
    ) -> None:
        owner = await connection.scalar(
            text(
                """SELECT true FROM app.conversations
                   WHERE workspace_id = :workspace AND user_id = :actor
                     AND id = :conversation AND status = 'active' AND deleted_at IS NULL
                   FOR UPDATE"""
            ),
            params,
        )
        if owner is not True:
            raise ConversationMissing

    @staticmethod
    async def _check_replay(
        connection: AsyncConnection,
        params: dict[str, object],
        message_id: UUID,
        request_hash: bytes,
    ) -> bool:
        result = await connection.execute(
            text(
                """SELECT request_hash, status, resource_type, resource_id
                   FROM app.api_idempotency_keys
                   WHERE workspace_id = :workspace AND user_id = :actor
                     AND method = 'POST' AND route_template = :route
                     AND key_hash = :key_hash AND expires_at > clock_timestamp()"""
            ),
            params,
        )
        row = result.mappings().one_or_none()
        if row is None:
            return False
        if not hmac.compare_digest(row["request_hash"], request_hash):
            raise ConversationKeyConflict
        if row["status"] == "processing":
            raise ConversationBusy
        if (
            row["status"] != "completed"
            or row["resource_type"] != RESOURCE_TYPE
            or row["resource_id"] != message_id
        ):
            raise ConversationKeyConflict
        return True

    @staticmethod
    async def _read_user_turn(
        connection: AsyncConnection, params: dict[str, object]
    ) -> Message | None:
        result = await connection.execute(
            text(
                """SELECT id, role, status,
                          CASE WHEN role = 'user' AND status = 'complete' THEN content
                               ELSE NULL END AS content,
                          created_at, completed_at
                   FROM app.messages
                   WHERE workspace_id = :workspace AND conversation_id = :conversation
                     AND id = :message
                   FOR SHARE"""
            ),
            params,
        )
        row = result.mappings().one_or_none()
        return _message(row) if row else None

    @staticmethod
    async def _remember(connection: AsyncConnection, params: dict[str, object]) -> None:
        wrote = await connection.scalar(
            text(
                """INSERT INTO app.api_idempotency_keys AS entry
                     (id, workspace_id, user_id, key_hash, method, route_template,
                      request_hash, status, resource_type, resource_id, expires_at)
                   VALUES (:reservation, :workspace, :actor, :key_hash, 'POST', :route,
                           :request_hash, 'completed', :resource_type, :message,
                           clock_timestamp() + INTERVAL '24 hours')
                   ON CONFLICT (workspace_id, user_id, method, route_template, key_hash)
                   DO UPDATE SET
                       request_hash = EXCLUDED.request_hash, status = 'completed',
                       response_body = NULL, response_status = NULL,
                       resource_type = EXCLUDED.resource_type,
                       resource_id = EXCLUDED.resource_id,
                       created_at = clock_timestamp(), updated_at = clock_timestamp(),
                       expires_at = EXCLUDED.expires_at
                   WHERE entry.expires_at <= clock_timestamp()
                   RETURNING true"""
            ),
            {**params, "reservation": uuid4()},
        )
        if wrote is not True:
            raise ConversationBusy
