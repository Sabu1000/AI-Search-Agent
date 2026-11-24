"""Atomic public-stream attempt reservations; never store unverified evidence."""

from __future__ import annotations

import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from universal_ai_search.api.idempotency import validate_idempotency_key
from universal_ai_search.search.planner import SearchPlan

from .extracts import (
    EXTRACTIVE_MODEL,
    EvidencePointer,
    LiveExtract,
    active_delivery_session,
    bound_delivery_sql,
    current_persisted_extracts,
    current_ranked_extracts,
    persist_only_pointers,
)
from .repository import (
    ConversationBusy,
    ConversationDenied,
    ConversationKeyConflict,
    ConversationMissing,
    ConversationRepository,
    ConversationScope,
)
from .user_turn import UserTurnInputError, UserTurnStore, _prepare

STREAM_ROUTE = "/v1/conversations/{conversation_id}/messages:stream"
_RESOURCE = "conversation_assistant_attempt"

type FinalState = Literal["insufficient_evidence", "interrupted", "failed"]
type CompletedState = Literal["complete", "insufficient_evidence"]


class StreamAlreadyProcessed(Exception):
    pass


class StreamRetryUnavailable(Exception):
    pass


@dataclass(frozen=True)
class StreamAttempt:
    request_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID
    query: str = field(repr=False)
    replay: bool = False


@dataclass(frozen=True)
class StoredStreamDelivery:
    status: CompletedState
    extracts: tuple[LiveExtract, ...] = field(repr=False)


class StreamAttemptStore:
    def __init__(self, engine: AsyncEngine, *, idempotency_secret: bytes) -> None:
        if len(idempotency_secret) < 32:
            raise ValueError("idempotency secret must be at least 32 bytes")
        self._engine = engine
        self._secret = idempotency_secret

    async def reserve(
        self,
        *,
        scope: ConversationScope,
        conversation_id: UUID,
        client_message_id: UUID,
        retry_user_message_id: UUID | None,
        content: str | None,
        options: dict[str, object],
        idempotency_key: str,
    ) -> StreamAttempt:
        if client_message_id.int == 0 or (
            retry_user_message_id is not None and retry_user_message_id.int == 0
        ):
            raise UserTurnInputError
        key = validate_idempotency_key(idempotency_key)
        if retry_user_message_id is None:
            if content is None:
                raise UserTurnInputError
            user_message_id, _, _, _ = _prepare(
                secret=self._secret,
                scope=scope,
                conversation_id=conversation_id,
                client_message_id=client_message_id,
                content=content,
                idempotency_key=key,
            )
        else:
            if content is not None:
                raise UserTurnInputError
            user_message_id = retry_user_message_id
        assistant_message_id = uuid5(
            conversation_id, f"uas-assistant-stream-v1:{client_message_id}"
        )
        key_hash = hmac.digest(self._secret, b"stream-key:" + key.encode(), "sha256")
        # HMAC private user content and filters, never persist them in replay rows.
        request_document = json.dumps(
            {
                "conversation": str(conversation_id),
                "assistant": str(assistant_message_id),
                "user": str(user_message_id),
                "content": content,
                "options": options,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        request_hash = hmac.digest(
            self._secret, b"stream-body:" + request_document, "sha256"
        )
        request_id = uuid4()
        async with self._engine.begin() as connection:
            context = await ConversationRepository._authorize(connection, scope)
            values = {
                **context,
                "conversation": conversation_id,
                "message": user_message_id,
                "assistant": assistant_message_id,
                "request_id": request_id,
                "reservation": uuid4(),
                "key_hash": key_hash,
                "request_hash": request_hash,
                "path": STREAM_ROUTE,
                "resource_type": _RESOURCE,
            }
            await UserTurnStore._lock_owned_conversation(connection, values)
            # A row created here is only committed together with the user turn,
            # assistant attempt and search request below. Unrelated key conflicts
            # cannot leave behind new user turns or attempts.
            replay = await self._reserve_key(connection, values, request_hash)
            if replay is not None:
                if replay != assistant_message_id:
                    raise ConversationKeyConflict
                return await self._replay_attempt(
                    connection, values, user_message_id, assistant_message_id
                )
            previous_id = await connection.scalar(
                text(
                    """SELECT id FROM app.messages WHERE workspace_id = :workspace
                        AND conversation_id = :conversation AND id = :assistant"""
                ),
                values,
            )
            if previous_id is not None:
                raise StreamAlreadyProcessed

            user_message = await UserTurnStore._read_user_turn(connection, values)
            if retry_user_message_id is not None:
                if user_message is None or user_message.content is None:
                    raise ConversationMissing
                await self._authorize_retry(connection, values)
                query = user_message.content
            else:
                if content is None:
                    raise UserTurnInputError
                query = content
                if user_message is None:
                    await UserTurnStore._insert_user_turn(connection, values, content)
                elif user_message.content != content:
                    raise ConversationKeyConflict

            await connection.execute(
                text(
                    """INSERT INTO app.search_requests
                         (id, workspace_id, user_id, conversation_id, mode, query_text,
                          normalized_plan, planner_version, ranker_version,
                          index_generation, authorization_version, status, purge_after)
                       SELECT :request_id, w.id, :actor, :conversation, 'answer',
                              :query,
                              jsonb_build_object('stream_user_message_id',
                                                 CAST(:message AS text)),
                              'conversation-stream-v1', 'weighted-rrf-v1',
                              w.search_index_generation, w.authorization_version,
                              'running', clock_timestamp() + interval '30 days'
                       FROM app.workspaces w WHERE w.id = :workspace"""
                ),
                {**values, "query": query, "message": str(user_message_id)},
            )
            await connection.execute(
                text(
                    """INSERT INTO app.messages
                         (id, workspace_id, conversation_id, search_request_id,
                          role, status, content, created_at)
                       VALUES (:assistant, :workspace, :conversation, :request_id,
                               'assistant', 'pending', '', clock_timestamp())"""
                ),
                values,
            )
            return StreamAttempt(
                request_id, user_message_id, assistant_message_id, query
            )

    @staticmethod
    async def _reserve_key(
        connection: AsyncConnection, values: dict[str, object], request_hash: bytes
    ) -> UUID | None:
        reserved = await connection.scalar(
            text(
                """INSERT INTO app.api_idempotency_keys AS entry
                     (id, workspace_id, user_id, key_hash, method, route_template,
                      request_hash, status, resource_type, resource_id, expires_at)
                   VALUES (:reservation, :workspace, :actor, :key_hash, 'POST', :path,
                           :request_hash, 'completed', :resource_type, :assistant,
                           clock_timestamp() + INTERVAL '24 hours')
                   ON CONFLICT ON CONSTRAINT uq_api_idempotency_keys_reservation
                   DO UPDATE SET request_hash = EXCLUDED.request_hash,
                                 status = 'completed', response_status = NULL,
                                 response_body = NULL, resource_type = :resource_type,
                                 resource_id = :assistant,
                                 created_at = clock_timestamp(),
                                 updated_at = clock_timestamp(),
                                 expires_at = EXCLUDED.expires_at
                   WHERE entry.expires_at <= clock_timestamp()
                   RETURNING true"""
            ),
            values,
        )
        if reserved is True:
            return None
        existing = await connection.execute(
            text(
                """SELECT request_hash, status, resource_type, resource_id
                   FROM app.api_idempotency_keys
                   WHERE workspace_id = :workspace AND user_id = :actor
                     AND method = 'POST' AND route_template = :path
                     AND key_hash = :key_hash"""
            ),
            values,
        )
        prior = existing.mappings().one_or_none()
        if prior is None or not hmac.compare_digest(
            prior["request_hash"], request_hash
        ):
            raise ConversationKeyConflict
        if prior["status"] == "processing":
            raise ConversationBusy
        identifier = prior["resource_id"]
        if (
            prior["status"] != "completed"
            or prior["resource_type"] != _RESOURCE
            or not isinstance(identifier, UUID)
        ):
            raise ConversationKeyConflict
        return identifier

    @staticmethod
    async def _replay_attempt(
        connection: AsyncConnection,
        values: dict[str, object],
        user_message_id: UUID,
        assistant_id: UUID,
    ) -> StreamAttempt:
        row = (
            (
                await connection.execute(
                    text(
                        """SELECT msg.search_request_id, msg.status, msg.model
                       FROM app.messages msg JOIN app.search_requests req
                         ON req.workspace_id = msg.workspace_id
                        AND req.id = msg.search_request_id
                       JOIN app.messages owner_turn
                         ON owner_turn.workspace_id = msg.workspace_id
                        AND owner_turn.conversation_id = msg.conversation_id
                        AND owner_turn.id = :message
                       WHERE msg.workspace_id = :workspace
                         AND msg.conversation_id = :conversation AND msg.id = :assistant
                         AND msg.role = 'assistant' AND req.user_id = :actor
                         AND req.conversation_id = :conversation
                         AND owner_turn.role = 'user' AND owner_turn.status = 'complete'
                         AND req.normalized_plan->>'stream_user_message_id'
                             = :owner_turn
                       FOR SHARE OF msg, req, owner_turn"""
                    ),
                    {**values, "owner_turn": str(user_message_id)},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ConversationMissing
        if row["status"] == "pending":
            raise ConversationBusy
        if row["status"] == "insufficient_evidence" or (
            row["status"] == "complete" and row["model"] == EXTRACTIVE_MODEL
        ):
            return StreamAttempt(
                row["search_request_id"], user_message_id, assistant_id, "", True
            )
        raise StreamAlreadyProcessed

    @staticmethod
    async def _authorize_retry(
        connection: AsyncConnection, values: dict[str, object]
    ) -> None:
        latest = await connection.execute(
            text(
                """SELECT msg.id, req.id AS request_id, msg.status,
                          msg.created_at < clock_timestamp() - interval '60 seconds'
                              AS lease_expired
                   FROM app.messages msg JOIN app.search_requests req
                     ON req.workspace_id = msg.workspace_id
                    AND req.id = msg.search_request_id
                   WHERE msg.workspace_id = :workspace
                     AND msg.conversation_id = :conversation AND msg.role = 'assistant'
                     AND req.conversation_id = :conversation AND req.user_id = :actor
                     AND req.normalized_plan->>'stream_user_message_id' = :user_message
                   ORDER BY msg.created_at DESC, msg.id DESC LIMIT 1
                   FOR UPDATE OF msg, req"""
            ),
            {**values, "user_message": str(values["message"])},
        )
        prior = latest.mappings().one_or_none()
        if prior and prior["status"] in {"interrupted", "failed"}:
            return
        if prior and prior["status"] == "pending" and prior["lease_expired"]:
            # Recover loss of a process that could not perform its disconnect
            # cleanup. An expired token cannot be turned into a late completion.
            await StreamAttemptStore._finalize(
                connection,
                {
                    **values,
                    "assistant": prior["id"],
                    "request_id": prior["request_id"],
                },
                status="interrupted",
                latency_ms=60_000,
            )
            return
        raise StreamRetryUnavailable

    async def finish_with_pointers(
        self,
        *,
        scope: ConversationScope,
        conversation_id: UUID,
        attempt: StreamAttempt,
        candidates: tuple[EvidencePointer, ...],
        plan: SearchPlan,
        latency_ms: int,
    ) -> None:
        """Commit verified evidence pointers, never the hydrated content."""
        if attempt.replay:
            raise StreamAlreadyProcessed
        async with self._engine.begin() as connection:
            await bound_delivery_sql(connection)
            context = await ConversationRepository._authorize(connection, scope)
            values = {
                **context,
                "conversation": conversation_id,
                "assistant": attempt.assistant_message_id,
                "request_id": attempt.request_id,
            }
            await UserTurnStore._lock_owned_conversation(connection, values)
            current = (
                await current_ranked_extracts(connection, values, candidates, plan)
                if candidates
                else ()
            )
            if current:
                await persist_only_pointers(
                    connection, values, current, attempt.assistant_message_id
                )
            await self._finalize(
                connection,
                values,
                status="complete" if current else "insufficient_evidence",
                latency_ms=latency_ms,
                evidence_count=len(current),
            )

    @asynccontextmanager
    async def delivery(
        self, *, scope: ConversationScope, conversation_id: UUID, attempt: StreamAttempt
    ) -> AsyncIterator[StoredStreamDelivery]:
        """Lock the active authorization proof through one protected ASGI send."""
        async with self._engine.begin() as connection:
            await bound_delivery_sql(connection)
            context = await ConversationRepository._authorize(connection, scope)
            await ConversationRepository._existing(connection, context, conversation_id)
            values = {
                **context,
                "conversation": conversation_id,
                "assistant": attempt.assistant_message_id,
                "request_id": attempt.request_id,
                "message": attempt.user_message_id,
                "owner_turn": str(attempt.user_message_id),
            }
            if not await active_delivery_session(connection, values):
                raise ConversationDenied
            row = (
                (
                    await connection.execute(
                        text(
                            """SELECT msg.status, msg.model,
                                  req.status AS request_status
                           FROM app.messages msg JOIN app.search_requests req
                             ON req.workspace_id = msg.workspace_id
                            AND req.id = msg.search_request_id
                           JOIN app.messages turn
                             ON turn.workspace_id = msg.workspace_id
                            AND turn.conversation_id = msg.conversation_id
                            AND turn.id = :message
                           WHERE msg.workspace_id = :workspace
                             AND msg.conversation_id = :conversation
                             AND msg.id = :assistant AND msg.role = 'assistant'
                             AND req.id = :request_id AND req.user_id = :actor
                             AND req.conversation_id = :conversation
                             AND req.normalized_plan->>'stream_user_message_id'
                                 = :owner_turn
                             AND turn.role = 'user' AND turn.status = 'complete'
                           FOR SHARE OF msg, req, turn NOWAIT"""
                        ),
                        values,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ConversationMissing
            if (
                row["status"] == "insufficient_evidence"
                and row["request_status"] == "insufficient_evidence"
            ):
                yield StoredStreamDelivery("insufficient_evidence", ())
                return
            if not (
                row["status"] == "complete"
                and row["request_status"] == "completed"
                and row["model"] == EXTRACTIVE_MODEL
            ):
                raise StreamAlreadyProcessed
            # Rehydrate from the base index after commit, not from the retrieval
            # snapshot or pointer-write transaction. Partial revocation blanks
            # the whole saved response; retained pointers are not disclosed.
            items = await current_persisted_extracts(
                connection, values, attempt.assistant_message_id
            )
            yield StoredStreamDelivery(
                "complete" if items else "insufficient_evidence", items
            )

    async def finish(
        self,
        *,
        scope: ConversationScope,
        conversation_id: UUID,
        attempt: StreamAttempt,
        status: FinalState,
        latency_ms: int,
    ) -> None:
        async with self._engine.begin() as connection:
            context = await ConversationRepository._authorize(connection, scope)
            values = {
                **context,
                "conversation": conversation_id,
                "assistant": attempt.assistant_message_id,
                "request_id": attempt.request_id,
            }
            await UserTurnStore._lock_owned_conversation(connection, values)
            await self._finalize(
                connection, values, status=status, latency_ms=latency_ms
            )

    @staticmethod
    async def _finalize(
        connection: AsyncConnection,
        values: dict[str, object],
        *,
        status: FinalState | Literal["complete"],
        latency_ms: int,
        evidence_count: int = 0,
    ) -> None:
        request_status = (
            "cancelled"
            if status == "interrupted"
            else "completed" if status == "complete" else status
        )
        params = {
            **values,
            "message_status": status,
            "request_status": request_status,
            "latency": max(latency_ms, 0),
            "count": evidence_count,
            "model": EXTRACTIVE_MODEL if status == "complete" else None,
            "must_be_fresh": status == "complete",
            "reason": (
                "no_authorized_results" if status == "insufficient_evidence" else None
            ),
        }
        saved = await connection.scalar(
            text(
                """UPDATE app.search_requests
                      SET status = :request_status, insufficient_reason = :reason,
                          completed_at = clock_timestamp(), latency_ms = :latency,
                          result_count = :count
                    WHERE id = :request_id AND conversation_id = :conversation
                      AND workspace_id = :workspace AND user_id = :actor
                      AND status = 'running'
                    RETURNING true"""
            ),
            params,
        )
        if saved is not True:
            raise StreamAlreadyProcessed
        saved = await connection.scalar(
            text(
                """UPDATE app.messages SET status = :message_status, model = :model,
                           latency_ms = :latency, completed_at = clock_timestamp()
                   WHERE id = :assistant AND workspace_id = :workspace
                     AND conversation_id = :conversation AND role = 'assistant'
                     AND search_request_id = :request_id AND status = 'pending'
                     AND (NOT :must_be_fresh OR created_at >
                           clock_timestamp() - INTERVAL '45 seconds')
                   RETURNING true"""
            ),
            params,
        )
        if saved is not True:
            raise StreamAlreadyProcessed
        await connection.execute(
            text(
                """UPDATE app.conversations
                   SET updated_at = greatest(updated_at, clock_timestamp())
                   WHERE workspace_id = :workspace AND user_id = :actor
                     AND id = :conversation"""
            ),
            params,
        )
