"""Owner-scoped acceptance and private receipts for local reindex operations."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from universal_ai_search.api.idempotency import validate_idempotency_key
from universal_ai_search.conversations.repository import (
    ConversationDenied,
    ConversationRepository,
    ConversationScope,
)
from universal_ai_search.indexing.pipeline import (
    CHUNKER_VERSION,
    EMBEDDING_MODEL,
    PARSER_VERSION,
    document_version_id,
    version_key,
)

from .repository import _ELIGIBLE

_ROUTE = "/v1/sources/{source_id}/reindex"
_RESOURCE = "source_reindex"
_CURRENT = f"""
    SELECT source.connection_id, version.id, source.content_hash,
           source.permissions_hash,
           version.object_storage_key IS NOT NULL AS object_backed
    FROM app.sources AS source
    JOIN app.connections AS connection ON connection.workspace_id = source.workspace_id
      AND connection.id = source.connection_id
    JOIN app.document_versions AS version ON version.workspace_id = source.workspace_id
      AND version.source_id = source.id
      AND version.id = source.current_document_version_id
    WHERE {_ELIGIBLE} AND source.id = :target AND connection.owner_user_id = :actor
      AND source.content_hash = version.content_hash
"""
_RECEIPT = """
    SELECT job.id, job.status, job.error_code, job.created_at, job.completed_at,
           job.created_at + INTERVAL '24 hours' AS deadline_at,
           (SELECT min(attempt.started_at) FROM app.job_attempts AS attempt
             WHERE attempt.workspace_id = job.workspace_id
               AND attempt.job_id = job.id) AS started_at
    FROM app.jobs AS job
    WHERE job.workspace_id = :workspace AND job.id = :operation
      AND job.job_type = 'index' AND job.queue = 'index'
      AND job.payload ->> 'source_reindex_actor' = CAST(CAST(:actor AS uuid) AS text)
"""


class SourceReindexMissing(Exception):
    pass


class SourceReindexUnavailable(Exception):
    pass


class SourceReindexBusy(Exception):
    pass


class SourceReindexConflict(Exception):
    pass


@dataclass(frozen=True)
class SourceReindexReceipt:
    id: UUID
    status: str
    error_code: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    deadline_at: datetime


def _receipt(row: RowMapping) -> SourceReindexReceipt:
    return SourceReindexReceipt(
        row["id"],
        row["status"],
        row["error_code"],
        row["created_at"],
        row["started_at"],
        row["completed_at"],
        row["deadline_at"],
    )


class SourceReindexStore:
    def __init__(self, engine: AsyncEngine, *, idempotency_secret: bytes) -> None:
        if len(idempotency_secret) < 32:
            raise ValueError("source reindex secret must be at least 32 bytes")
        self._engine = engine
        self._secret = idempotency_secret

    async def request(
        self, *, scope: ConversationScope, source_id: UUID, idempotency_key: str
    ) -> SourceReindexReceipt:
        key = validate_idempotency_key(idempotency_key)
        key_hash = hmac.digest(
            self._secret, b"source-reindex-key:" + key.encode(), "sha256"
        )
        target_hash = hmac.digest(
            self._secret, b"source-reindex-target:" + source_id.bytes, "sha256"
        )
        async with self._engine.begin() as connection:
            try:
                context = await ConversationRepository._authorize(connection, scope)
            except ConversationDenied:
                raise SourceReindexMissing from None
            params = {
                **context,
                "workspace_id": scope.workspace_id,
                "target": source_id,
                "route": _ROUTE,
                "resource": _RESOURCE,
                "key_hash": key_hash,
                "target_hash": target_hash,
            }
            await self._lock(
                connection,
                b"key",
                scope.workspace_id.bytes + scope.user_id.bytes + key_hash,
            )
            prior = await self._prior(connection, params, target_hash)
            if prior is not None:
                return await self._get(connection, params, prior)

            await self._lock(
                connection, b"source", scope.workspace_id.bytes + source_id.bytes
            )
            target = (
                (await connection.execute(text(_CURRENT), params))
                .mappings()
                .one_or_none()
            )
            if target is None:
                raise SourceReindexMissing
            if target["object_backed"]:
                # No data/object fetch contract in the normalized-text rebuild.
                raise SourceReindexUnavailable
            profile = await connection.scalar(
                text(
                    """SELECT id FROM app.embedding_profiles
                       WHERE status = 'active' AND provider = 'local'
                         AND model = :model"""
                ),
                {"model": EMBEDDING_MODEL},
            )
            if not isinstance(profile, int):
                raise SourceReindexUnavailable
            expected = target["id"]
            canonical = version_key(
                source_id=source_id,
                content_hash=target["content_hash"],
                permissions_hash=target["permissions_hash"],
                profile_id=profile,
            )
            operation = await connection.scalar(
                text(
                    """SELECT id FROM app.jobs
                       WHERE workspace_id = :workspace AND source_id = :target
                         AND job_type = 'index' AND queue = 'index'
                         AND status IN ('pending', 'leased', 'retry_wait')
                         AND payload ->> 'source_reindex_actor'
                           = CAST(CAST(:actor AS uuid) AS text)
                         AND payload ->> 'workspace_authorization_version'
                           = CAST(CAST(:workspace_version AS integer) AS text)
                         AND payload ->> 'user_authorization_version'
                           = CAST(CAST(:user_version AS integer) AS text)
                         AND payload ->> 'expected_current_document_version_id'
                           = CAST(CAST(:expected AS uuid) AS text)
                         AND payload ->> 'canonical_version_key' = :canonical
                         AND created_at > clock_timestamp() - INTERVAL '24 hours'
                       ORDER BY created_at DESC LIMIT 1"""
                ),
                {**params, "expected": expected, "canonical": canonical.hex()},
            )
            if operation is None:
                operation = uuid4()
                version = hashlib.sha256(
                    b"manual-reindex:" + canonical + operation.bytes
                ).digest()
                new_id = document_version_id(source_id, version)
                copied = await connection.scalar(
                    text(
                        """INSERT INTO app.document_versions (
                               id, workspace_id, source_id, version_key, state,
                               language, parser_version, chunker_version,
                               content_hash, permissions_hash,
                               token_count, extracted_bytes
                           )
                           SELECT :new_id, workspace_id, source_id, :key, 'pending',
                                  language, :parser, :chunker,
                                  content_hash, permissions_hash, 0, 0
                           FROM app.document_versions
                           WHERE workspace_id = :workspace AND source_id = :target
                             AND id = :expected
                             AND state = 'ready' AND object_storage_key IS NULL
                           RETURNING id"""
                    ),
                    {
                        **params,
                        "new_id": new_id,
                        "key": version,
                        "parser": PARSER_VERSION,
                        "chunker": CHUNKER_VERSION,
                        "expected": expected,
                    },
                )
                if copied != new_id:
                    raise SourceReindexMissing
                await connection.execute(
                    text(
                        """INSERT INTO app.jobs (
                               id, workspace_id, connection_id, source_id,
                               job_type, queue,
                               idempotency_key, status, priority, payload
                           ) VALUES (
                               :operation, :workspace, :connection, :target,
                               'index', 'index', :receipt_key, 'pending', -10,
                               CAST(:payload AS jsonb)
                           )"""
                    ),
                    {
                        **params,
                        "operation": operation,
                        "connection": target["connection_id"],
                        "receipt_key": f"source-reindex:{operation}",
                        "payload": json.dumps(
                            {
                                "source_reindex_actor": str(scope.user_id),
                                "workspace_authorization_version": (
                                    scope.workspace_version
                                ),
                                "user_authorization_version": scope.user_version,
                                "expected_current_document_version_id": str(expected),
                                "canonical_version_key": canonical.hex(),
                                "document_version_id": str(new_id),
                                "embedding_profile_id": profile,
                                "embedding_model": EMBEDDING_MODEL,
                            }
                        ),
                    },
                )
            await self._remember(connection, params, operation)
            return await self._get(connection, params, operation)

    async def read(
        self, *, scope: ConversationScope, operation_id: UUID
    ) -> SourceReindexReceipt:
        async with self._engine.begin() as connection:
            try:
                context = await ConversationRepository._authorize(connection, scope)
            except ConversationDenied:
                raise SourceReindexMissing from None
            return await self._get(connection, context, operation_id)

    @staticmethod
    async def _lock(
        connection: AsyncConnection, category: bytes, identity: bytes
    ) -> None:
        lock_id = int.from_bytes(
            hashlib.sha256(b"source-reindex-lock:" + category + identity).digest()[:8],
            "big",
            signed=True,
        )
        if (
            await connection.scalar(
                text("SELECT pg_try_advisory_xact_lock(CAST(:lock AS bigint))"),
                {"lock": lock_id},
            )
            is not True
        ):
            raise SourceReindexBusy

    @staticmethod
    async def _prior(
        connection: AsyncConnection, params: dict[str, object], target_hash: bytes
    ) -> UUID | None:
        row = (
            (
                await connection.execute(
                    text(
                        """SELECT request_hash, resource_id, resource_type,
                                  response_status, status
                       FROM app.api_idempotency_keys
                       WHERE workspace_id = :workspace AND user_id = :actor
                         AND method = 'POST' AND route_template = :route
                         AND key_hash = :key_hash AND expires_at > clock_timestamp()"""
                    ),
                    params,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        if not hmac.compare_digest(row["request_hash"], target_hash):
            raise SourceReindexConflict
        if row["status"] == "processing":
            raise SourceReindexBusy
        operation = row["resource_id"]
        if (
            row["resource_type"] != _RESOURCE
            or row["response_status"] != 202
            or row["status"] != "completed"
            or not isinstance(operation, UUID)
        ):
            raise SourceReindexConflict
        return operation

    @staticmethod
    async def _remember(
        connection: AsyncConnection, params: dict[str, object], operation: UUID
    ) -> None:
        if (
            await connection.scalar(
                text(
                    """INSERT INTO app.api_idempotency_keys AS entry (
                       id, workspace_id, user_id, key_hash, method, route_template,
                       request_hash, status, response_status, resource_type,
                       resource_id, expires_at
                   ) VALUES (
                       :id, :workspace, :actor, :key_hash, 'POST', :route,
                       :target_hash, 'completed', 202, :resource, :operation,
                       clock_timestamp() + INTERVAL '24 hours'
                   )
                   ON CONFLICT (workspace_id, user_id, method, route_template, key_hash)
                   DO UPDATE SET request_hash = EXCLUDED.request_hash,
                                 response_body = NULL,
                                 status = 'completed', response_status = 202,
                                 resource_type = EXCLUDED.resource_type,
                                 resource_id = EXCLUDED.resource_id,
                                 updated_at = clock_timestamp(),
                                 created_at = clock_timestamp(),
                                 expires_at = EXCLUDED.expires_at
                   WHERE entry.expires_at <= clock_timestamp()
                   RETURNING true"""
                ),
                {**params, "id": uuid4(), "operation": operation},
            )
            is not True
        ):
            raise SourceReindexBusy

    @staticmethod
    async def _get(
        connection: AsyncConnection, params: dict[str, object], operation_id: UUID
    ) -> SourceReindexReceipt:
        row = (
            (
                await connection.execute(
                    text(_RECEIPT), {**params, "operation": operation_id}
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise SourceReindexMissing
        return _receipt(row)
