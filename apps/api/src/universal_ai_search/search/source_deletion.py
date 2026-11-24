"""Atomic owner removal of an inline source and its local index artifacts."""

from __future__ import annotations

import hashlib
import hmac
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from universal_ai_search.api.idempotency import validate_idempotency_key
from universal_ai_search.conversations.deletion import (
    ConversationDeletion,
    ConversationDeletionStore,
    _receipt,
)
from universal_ai_search.conversations.repository import (
    ConversationBusy,
    ConversationKeyConflict,
    ConversationMissing,
    ConversationRepository,
    ConversationScope,
)

from .repository import SearchRepository
from .source_reindex import _CURRENT

_ROUTE = "/v1/sources/{source_id}"
_RESOURCE = "source_delete"
DELETE_SOURCE_CONFIRMATION = "delete-source"


class SourceDeletionUnavailable(Exception):
    """A derived source cannot be fully purged with the current contract."""


class SourceDeletionStore:
    def __init__(
        self, engine: AsyncEngine, *, idempotency_secret: bytes, audit_secret: bytes
    ) -> None:
        if min(len(idempotency_secret), len(audit_secret)) < 32:
            raise ValueError("source deletion secrets must be at least 32 bytes")
        self._engine = engine
        self._idempotency_secret = idempotency_secret
        self._audit_secret = audit_secret

    async def delete(
        self,
        *,
        scope: ConversationScope,
        source_id: UUID,
        idempotency_key: str,
        request_id: UUID,
    ) -> ConversationDeletion:
        key = validate_idempotency_key(idempotency_key)
        key_hash = hmac.digest(
            self._idempotency_secret, b"delete-source-key:" + key.encode(), "sha256"
        )
        request_hash = hmac.digest(
            self._idempotency_secret,
            b"delete-source-target:" + source_id.bytes,
            "sha256",
        )
        lock = int.from_bytes(
            hashlib.sha256(
                b"delete-source-lock:"
                + scope.workspace_id.bytes
                + scope.user_id.bytes
                + key_hash
            ).digest()[:8],
            "big",
            signed=True,
        )
        try:
            async with self._engine.begin() as connection:
                # Keep an inline purge bounded, and fail closed on any error.
                await connection.execute(text("SET LOCAL statement_timeout = '5s'"))
                await SearchRepository._context(
                    connection, scope.workspace_id, scope.user_id
                )
                workspace = await connection.scalar(
                    text(
                        """SELECT id FROM app.workspaces
                           WHERE id = :workspace AND status = 'active'
                           FOR NO KEY UPDATE NOWAIT"""
                    ),
                    {"workspace": scope.workspace_id},
                )
                if workspace is None:
                    raise ConversationMissing
                context = await ConversationRepository._authorize(connection, scope)
                if (
                    await connection.scalar(
                        text("SELECT pg_try_advisory_xact_lock(CAST(:key AS BIGINT))"),
                        {"key": lock},
                    )
                    is not True
                ):
                    raise ConversationBusy
                params = {
                    **context,
                    "workspace_id": scope.workspace_id,
                    "target": source_id,
                    "route": _ROUTE,
                    "type": _RESOURCE,
                    "key_hash": key_hash,
                    "request_hash": request_hash,
                }
                prior = await self._prior(connection, params)
                if prior is not None:
                    return await self._get(connection, params, prior)
                return await self._purge(
                    connection, params, request_id, scope, source_id
                )
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) in {"55P03", "57014", "40P01"}:
                raise ConversationBusy from None
            raise

    async def _purge(
        self,
        connection: AsyncConnection,
        params: dict[str, object],
        request_id: UUID,
        scope: ConversationScope,
        source_id: UUID,
    ) -> ConversationDeletion:
        target = await connection.scalar(
            text(
                _CURRENT + " FOR UPDATE OF source NOWAIT FOR SHARE OF connection NOWAIT"
            ),
            params,
        )
        if target is None:
            raise ConversationMissing
        # Require an actual storage deletion contract before changing visibility
        # or issuing a successful receipt for a source with any object version.
        object_backed = await connection.scalar(
            text(
                """SELECT EXISTS (SELECT 1 FROM app.document_versions
                   WHERE workspace_id = :workspace AND source_id = :target
                     AND object_storage_key IS NOT NULL)"""
            ),
            params,
        )
        if object_backed is not False:
            raise SourceDeletionUnavailable

        old_bytes = await connection.scalar(
            text(
                """SELECT version.extracted_bytes FROM app.sources AS source
                   JOIN app.document_versions AS version
                     ON version.workspace_id = source.workspace_id
                    AND version.id = source.current_document_version_id
                   WHERE source.workspace_id = :workspace AND source.id = :target"""
            ),
            params,
        )
        if not isinstance(old_bytes, int):
            raise ConversationMissing

        # Remove saved assistant answers containing an excerpt of this source
        # before FK cascades erase citation lineage. User messages are kept.
        await connection.execute(
            text(
                """DELETE FROM app.messages AS message
                   WHERE message.workspace_id = :workspace
                     AND message.role = 'assistant'
                     AND EXISTS (
                        SELECT 1 FROM app.citations AS citation
                        WHERE citation.workspace_id = message.workspace_id
                          AND citation.message_id = message.id
                          AND citation.source_id = :target
                     )"""
            ),
            params,
        )
        await connection.execute(
            text(
                """UPDATE app.sources SET state = 'deleted', title = 'Deleted source',
                       external_id = 'suppressed:' || id::text,
                       mime_type = NULL, file_extension = NULL, canonical_url = NULL,
                       author_display = NULL, source_timestamp = NULL,
                       source_timestamp_kind = NULL, current_document_version_id = NULL,
                       content_hash = decode(repeat('00', 32), 'hex'),
                       permissions_hash = decode(repeat('00', 32), 'hex'),
                       metadata = '{}'::jsonb, provider_sync_marker = NULL,
                       deleted_at = clock_timestamp(), updated_at = clock_timestamp(),
                       lock_version = lock_version + 1
                   WHERE id = :target AND workspace_id = :workspace"""
            ),
            params,
        )
        for sql in (
            "DELETE FROM app.source_people "
            "WHERE workspace_id = :workspace AND source_id = :target",
            "DELETE FROM app.source_collection_memberships "
            "WHERE workspace_id = :workspace AND source_id = :target",
            "DELETE FROM app.document_versions "
            "WHERE workspace_id = :workspace AND source_id = :target",
        ):
            await connection.execute(text(sql), params)

        await connection.execute(
            text(
                """WITH cancelled AS (
                       UPDATE app.jobs
                       SET status = 'failed', error_code = 'SOURCE_DELETED',
                           lease_owner = NULL, lease_expires_at = NULL,
                           completed_at = clock_timestamp(),
                           updated_at = clock_timestamp()
                       WHERE workspace_id = :workspace AND source_id = :target
                         AND job_type IN ('index','embed')
                         AND status IN ('pending','leased','retry_wait')
                       RETURNING id, attempt_count
                   )
                   UPDATE app.job_attempts AS attempt
                   SET status = 'permanent_failure', error_code = 'SOURCE_DELETED',
                       finished_at = clock_timestamp()
                   FROM cancelled
                   WHERE attempt.workspace_id = :workspace
                     AND attempt.job_id = cancelled.id
                     AND attempt.attempt_number = cancelled.attempt_count
                     AND attempt.status = 'running'"""
            ),
            params,
        )
        await connection.execute(
            text(
                """UPDATE app.workspace_usage
                   SET indexed_source_count = GREATEST(indexed_source_count - 1, 0),
                       extracted_bytes = GREATEST(extracted_bytes - :extracted, 0),
                       updated_at = clock_timestamp(), lock_version = lock_version + 1
                   WHERE workspace_id = :workspace"""
            ),
            {**params, "extracted": old_bytes},
        )
        await connection.execute(
            text(
                """UPDATE app.workspaces
                   SET search_index_generation = search_index_generation + 1,
                       updated_at = clock_timestamp(), lock_version = lock_version + 1
                   WHERE id = :workspace"""
            ),
            params,
        )
        receipt_id = uuid4()
        result = await connection.execute(
            text(
                """INSERT INTO app.deletion_requests (
                      id, workspace_id, requested_by_user_id, target_type,
                      target_id, status, idempotency_key, deadline_at,
                      started_at, completed_at
                   ) VALUES (
                      :receipt, :workspace, :actor, 'source', :target, 'completed',
                      :receipt_key, transaction_timestamp() + INTERVAL '24 hours',
                      transaction_timestamp(), clock_timestamp()
                    ) RETURNING id, requested_at, started_at, completed_at,
                               deadline_at"""
            ),
            {
                **params,
                "receipt": receipt_id,
                "receipt_key": cast(bytes, params["key_hash"]).hex(),
            },
        )
        await ConversationDeletionStore._remember(connection, params, receipt_id)
        await connection.execute(
            text(
                """INSERT INTO app.audit_events (
                      id, workspace_id, workspace_ref_hash, actor_user_id,
                      action, target_type, outcome, target_ref_hash, request_id
                   ) VALUES (:event, :workspace, :workspace_hash, :actor,
                             'source.delete', 'source', 'succeeded',
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
                    self._audit_secret, b"source:" + source_id.bytes, "sha256"
                ),
                "request_id": request_id,
            },
        )
        await connection.execute(
            text(
                """INSERT INTO app.outbox_events (
                      id, workspace_id, aggregate_type, aggregate_id,
                      event_type, payload
                   ) VALUES (
                       :event, :workspace, 'source', :target, 'index.source_deleted',
                       jsonb_build_object('source_id', CAST(:target AS uuid))
                   )"""
            ),
            {**params, "event": uuid4()},
        )
        return _receipt(result.mappings().one())

    @staticmethod
    async def _prior(
        connection: AsyncConnection, params: dict[str, object]
    ) -> UUID | None:
        result = await connection.execute(
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
        row = result.mappings().one_or_none()
        if row is None:
            return None
        if not hmac.compare_digest(row["request_hash"], params["request_hash"]):
            raise ConversationKeyConflict
        if row["status"] == "processing":
            raise ConversationBusy
        receipt = row["resource_id"]
        if (
            row["status"] != "completed"
            or row["response_status"] != 202
            or row["resource_type"] != _RESOURCE
            or not isinstance(receipt, UUID)
        ):
            raise ConversationKeyConflict
        return receipt

    async def read(
        self, *, scope: ConversationScope, operation_id: UUID
    ) -> ConversationDeletion:
        async with self._engine.begin() as connection:
            context = await ConversationRepository._authorize(connection, scope)
            return await self._get(connection, context, operation_id)

    @staticmethod
    async def _get(
        connection: AsyncConnection, context: dict[str, object], operation_id: UUID
    ) -> ConversationDeletion:
        row = (
            (
                await connection.execute(
                    text(
                        """SELECT id, requested_at, started_at, completed_at,
                               deadline_at
                       FROM app.deletion_requests
                       WHERE workspace_id = :workspace AND id = :id
                         AND target_type = 'source' AND status = 'completed'
                         AND requested_by_user_id = :actor"""
                    ),
                    {**context, "id": operation_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ConversationMissing
        return _receipt(row)
