"""Owner-only standalone search history with transactional deletion/replay."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from universal_ai_search.api.idempotency import build_fingerprint
from universal_ai_search.conversations.repository import (
    ConversationRepository,
    ConversationScope,
)

HISTORY_PATH = "/v1/search/history"
CLEAR_CONFIRMATION = "clear-search-history"
SUGGESTION_WINDOW = 200
SUGGESTION_TEXT_LIMIT = 200
MAX_SUGGESTIONS = 10

type HistoryOrder = tuple[datetime, UUID]
type SearchMode = Literal["results", "answer"]
type SearchState = Literal[
    "running", "completed", "insufficient_evidence", "failed", "cancelled"
]


@dataclass(frozen=True)
class HistoryEntry:
    id: UUID
    query: str
    mode: SearchMode
    status: SearchState
    result_count: int | None
    created_at: datetime
    completed_at: datetime | None


class HistoryKeyConflict(Exception):
    pass


class HistoryClearPending(Exception):
    pass


class SearchHistoryStore:
    def __init__(
        self, engine: AsyncEngine, *, idempotency_secret: bytes, audit_secret: bytes
    ) -> None:
        if min(len(idempotency_secret), len(audit_secret)) < 32:
            raise ValueError("search history secrets must be at least 32 bytes")
        self._engine = engine
        self._idempotency_secret = idempotency_secret
        self._audit_secret = audit_secret

    async def list_owned(
        self, *, scope: ConversationScope, limit: int, after: HistoryOrder | None
    ) -> list[HistoryEntry]:
        if not 1 <= limit <= 101:
            raise ValueError("history page limit is outside its bound")
        async with self._engine.begin() as connection:
            # Both private surfaces use the same locked session and membership
            # checks as well as the same transaction-local RLS settings.
            values = await ConversationRepository._authorize(connection, scope)
            result = await connection.execute(
                text(
                    """SELECT id, query_text, mode, status, result_count,
                              created_at, completed_at
                       FROM app.search_requests
                       WHERE workspace_id = :workspace AND user_id = :actor
                         AND conversation_id IS NULL AND purge_after > created_at
                         AND purge_after > statement_timestamp()
                         AND (CAST(:after_time AS TIMESTAMPTZ) IS NULL
                              OR (created_at, id) < (:after_time, :after_id))
                       ORDER BY created_at DESC, id DESC
                       LIMIT :limit"""
                ),
                {
                    **values,
                    "limit": limit,
                    "after_time": after[0] if after else None,
                    "after_id": after[1] if after else None,
                },
            )
            return [
                HistoryEntry(
                    row["id"],
                    row["query_text"],
                    row["mode"],
                    row["status"],
                    row["result_count"],
                    row["created_at"],
                    row["completed_at"],
                )
                for row in result.mappings()
            ]

    async def suggest_owned(
        self, *, scope: ConversationScope, prefix: str, limit: int
    ) -> list[str]:
        """Suggest only the current user's directly entered search terms."""
        if not 1 <= limit <= MAX_SUGGESTIONS or not 1 <= len(prefix) <= 128:
            raise ValueError("suggestion limit or prefix size is outside its bound")
        async with self._engine.begin() as connection:
            values = await ConversationRepository._authorize(connection, scope)
            result = await connection.execute(
                text(
                    """WITH recent AS MATERIALIZED (
                         SELECT id, query_text, created_at
                         FROM app.search_requests
                         WHERE workspace_id = :workspace AND user_id = :actor
                           AND conversation_id IS NULL AND purge_after > created_at
                           AND purge_after > statement_timestamp()
                           AND status IN ('completed', 'insufficient_evidence')
                         ORDER BY created_at DESC, id DESC
                         LIMIT :window
                       ), unique_queries AS (
                         SELECT DISTINCT ON (lower(query_text))
                                id, query_text, created_at
                         FROM recent
                         WHERE char_length(query_text) <= :max_chars
                           AND starts_with(lower(query_text), lower(:prefix))
                         ORDER BY lower(query_text), created_at DESC, id DESC
                       )
                       SELECT query_text FROM unique_queries
                       ORDER BY created_at DESC, id DESC LIMIT :limit"""
                ),
                {
                    **values,
                    "window": SUGGESTION_WINDOW,
                    "max_chars": SUGGESTION_TEXT_LIMIT,
                    "limit": limit,
                    "prefix": prefix,
                },
            )
            return list(result.scalars())

    async def clear_owned(
        self, *, scope: ConversationScope, idempotency_key: str, request_id: UUID
    ) -> None:
        fingerprint = build_fingerprint(
            key=idempotency_key,
            hash_secret=self._idempotency_secret,
            workspace_id=scope.workspace_id,
            principal_id=scope.user_id,
            method="DELETE",
            route_template=HISTORY_PATH,
            validated_body=None,
            relevant_headers={"X-Confirm-Action": CLEAR_CONFIRMATION},
        )
        request_hash = bytes.fromhex(fingerprint.request_hash)
        async with self._engine.begin() as connection:
            values = await ConversationRepository._authorize(connection, scope)
            params = {
                **values,
                "route": HISTORY_PATH,
                "id": uuid4(),
                "key_hash": bytes.fromhex(fingerprint.key_hash),
                "request_hash": request_hash,
            }
            # Reserve and complete in the same transaction as the hard delete.
            # A committed replay skips the delete, protecting later searches.
            reserved = await connection.scalar(
                text(
                    """INSERT INTO app.api_idempotency_keys AS entry
                         (id, workspace_id, user_id, key_hash, method, route_template,
                          request_hash, status, response_status, expires_at)
                       VALUES (:id, :workspace, :actor, :key_hash, 'DELETE', :route,
                               :request_hash, 'completed', 204,
                               clock_timestamp() + INTERVAL '24 hours')
                       ON CONFLICT ON CONSTRAINT uq_api_idempotency_keys_reservation
                       DO UPDATE SET request_hash = EXCLUDED.request_hash,
                                     status = 'completed', response_status = 204,
                                     response_body = NULL, resource_type = NULL,
                                     resource_id = NULL, created_at = clock_timestamp(),
                                     updated_at = clock_timestamp(),
                                     expires_at = EXCLUDED.expires_at
                       WHERE entry.expires_at <= clock_timestamp()
                       RETURNING true"""
                ),
                params,
            )
            if reserved is not True:
                replay = await connection.execute(
                    text(
                        """SELECT request_hash, status, response_status, response_body,
                                  resource_id
                           FROM app.api_idempotency_keys
                           WHERE workspace_id = :workspace AND user_id = :actor
                             AND method = 'DELETE' AND route_template = :route
                             AND key_hash = :key_hash"""
                    ),
                    params,
                )
                row = replay.mappings().one_or_none()
                if row is None or not hmac.compare_digest(
                    row["request_hash"], request_hash
                ):
                    raise HistoryKeyConflict
                if row["status"] == "processing":
                    raise HistoryClearPending
                if (
                    row["status"] != "completed"
                    or row["response_status"] != 204
                    or row["response_body"] is not None
                    or row["resource_id"] is not None
                ):
                    raise HistoryKeyConflict
                return

            # Conversation-associated requests have their own retention and
            # deletion contract. Expired standalone rows are removed too.
            await connection.execute(
                text(
                    """DELETE FROM app.search_requests
                       WHERE workspace_id = :workspace AND user_id = :actor
                         AND conversation_id IS NULL"""
                ),
                params,
            )
            await connection.execute(
                text(
                    """INSERT INTO app.audit_events
                         (id, workspace_id, workspace_ref_hash, actor_user_id, action,
                          target_type, outcome, request_id)
                       VALUES (:event_id, :workspace, :workspace_hash, :actor,
                               'search_history.clear', 'search_history', 'succeeded',
                               :request_id)"""
                ),
                {
                    **values,
                    "event_id": uuid4(),
                    "workspace_hash": hmac.digest(
                        self._audit_secret,
                        b"workspace:" + scope.workspace_id.bytes,
                        "sha256",
                    ),
                    "request_id": request_id,
                },
            )
