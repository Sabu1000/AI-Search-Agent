"""Atomic owner-only cutoff and inline cleanup for bounded DB-only local devices."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from universal_ai_search.api.idempotency import validate_idempotency_key

from .query import DeviceQueryDenied, SQLAlchemyDeviceQueryReader
from .registration import RegistrationScope

ROUTE = "/v1/devices/{device_id}"
CONFIRMATION = "revoke-device"
RESOURCE = "device_deletion"

_MAX_SOURCES = 250
_MAX_ROWS = 5_000


class DeviceRevocationRejected(Exception):
    """Missing resource or revoked caller; do not distinguish externally."""


class DeviceRevocationConflict(Exception):
    """Idempotency key already bound to a different request."""


class DeviceRevocationBusy(Exception):
    """Concurrent revocation to retry without assuming completion."""


class DeviceCleanupUnsupported(Exception):
    """The connection needs an external or larger cleanup worker."""


class DeviceRevocationUnavailable(Exception):
    """Revocation dependency failed with no committed operation."""


@dataclass(frozen=True, slots=True)
class DeviceRevocationReceipt:
    id: UUID
    created_at: datetime
    started_at: datetime
    completed_at: datetime
    deadline_at: datetime


def _receipt(row: RowMapping) -> DeviceRevocationReceipt:
    return DeviceRevocationReceipt(
        row["id"],
        row["requested_at"],
        row["started_at"],
        row["completed_at"],
        row["deadline_at"],
    )


class DeviceRevocationStore:
    def __init__(
        self, engine: AsyncEngine, *, idempotency_secret: bytes, audit_secret: bytes
    ) -> None:
        if min(len(idempotency_secret), len(audit_secret)) < 32:
            raise ValueError("device revocation secrets must be at least 32 bytes")
        self._engine = engine
        self._key = idempotency_secret
        self._audit = audit_secret

    async def delete(
        self,
        *,
        scope: RegistrationScope,
        device_id: UUID,
        idempotency_key: str,
        request_id: UUID,
    ) -> DeviceRevocationReceipt:
        key = validate_idempotency_key(idempotency_key)
        key_hash = hmac.digest(
            self._key, b"device-delete-key:" + key.encode(), "sha256"
        )
        request_hash = hmac.digest(
            self._key, b"device-delete-target:" + device_id.bytes, "sha256"
        )
        # Workspace lock prevents simultaneous lock upgrades on the same
        # generation row. Database timeouts also bound conflicts with workers.
        lock = int.from_bytes(
            hashlib.sha256(b"device-delete:" + scope.workspace_id.bytes).digest()[:8],
            "big",
            signed=True,
        )
        try:
            async with self._engine.begin() as conn:
                await conn.execute(text("SET LOCAL lock_timeout = '2s'"))
                await conn.execute(text("SET LOCAL statement_timeout = '5s'"))
                if (
                    await conn.scalar(
                        text("SELECT pg_try_advisory_xact_lock(CAST(:key AS BIGINT))"),
                        {"key": lock},
                    )
                    is not True
                ):
                    raise DeviceRevocationBusy
                params = await SQLAlchemyDeviceQueryReader._authorize(conn, scope)
                params.update(
                    target=device_id,
                    key_hash=key_hash,
                    request_hash=request_hash,
                    route=ROUTE,
                    resource=RESOURCE,
                )
                previous = await self._replay(conn, params, request_hash)
                if previous is not None:
                    return await self._read(conn, params, previous, target=device_id)

                connection_id = await self._lock_connection(conn, params)
                params["connection"] = connection_id
                counts = await self._check_bounded(conn, params)
                await self._cutoff(conn, params)
                await self._purge(conn, params, counts)
                return await self._finish(conn, params, scope, request_id, device_id)
        except DeviceQueryDenied:
            raise DeviceRevocationRejected from None
        except SQLAlchemyError:
            raise DeviceRevocationUnavailable from None

    @staticmethod
    async def _lock_connection(
        conn: AsyncConnection, params: dict[str, object]
    ) -> UUID:
        # The registering user owns a dedicated connection. A legacy shared
        # connection or another provider is not a safe revocation target.
        result = await conn.execute(
            text(
                """SELECT c.id, c.provider, c.owner_user_id
                   FROM app.devices d
                   JOIN app.connections c
                     ON c.workspace_id=d.workspace_id AND c.id=d.connection_id
                   WHERE d.workspace_id=:workspace AND d.user_id=:actor
                     AND d.id=:target AND d.status IN ('pending', 'active')
                   FOR UPDATE OF d, c"""
            ),
            params,
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise DeviceRevocationRejected
        if row["provider"] != "local_files" or row["owner_user_id"] != params["actor"]:
            raise DeviceCleanupUnsupported
        shared = await conn.scalar(
            text(
                """SELECT EXISTS (SELECT 1 FROM app.devices
                   WHERE workspace_id=:workspace AND connection_id=:connection
                     AND id<>:target)"""
            ),
            {**params, "connection": row["id"]},
        )
        if shared is not False:
            raise DeviceCleanupUnsupported
        connection_id = row["id"]
        if not isinstance(connection_id, UUID):
            raise DeviceCleanupUnsupported
        return connection_id

    @staticmethod
    async def _check_bounded(
        conn: AsyncConnection, params: dict[str, object]
    ) -> tuple[int, int]:
        # All baseline index writes first lock this device's connection.
        # Lock the selected source rows so in-flight ready promotion cannot
        # change the usage delta between inspection and purge.
        rows = (
            (
                await conn.execute(
                    text(
                        """SELECT id, provider, current_document_version_id
                           FROM app.sources WHERE workspace_id=:workspace
                             AND connection_id=:connection
                           LIMIT :row_limit FOR UPDATE"""
                    ),
                    {**params, "row_limit": _MAX_SOURCES + 1},
                )
            )
            .mappings()
            .all()
        )
        if len(rows) > _MAX_SOURCES or any(
            row["provider"] != "local_files" for row in rows
        ):
            raise DeviceCleanupUnsupported
        params["sources"] = [row["id"] for row in rows]
        current = [
            row["current_document_version_id"]
            for row in rows
            if row["current_document_version_id"] is not None
        ]
        params["versions"] = current

        # Unknown jobs could own bytes outside PostgreSQL: do not erase their
        # handles or report completion. All existing index jobs only point at
        # inline document rows and the embedding profile.
        checks = await conn.execute(
            text(
                """SELECT
                   EXISTS (
                     SELECT 1 FROM app.document_versions v
                     WHERE v.workspace_id=:workspace
                       AND v.source_id=ANY(CAST(:sources AS UUID[]))
                       AND v.object_storage_key IS NOT NULL
                   ) OR app.device_has_staged_upload(:target) AS external_content,
                   EXISTS (
                     SELECT 1 FROM app.jobs j WHERE j.workspace_id=:workspace
                       AND (j.connection_id=:connection
                            OR j.source_id=ANY(CAST(:sources AS UUID[])))
                       AND (j.job_type<>'index'
                            OR j.connection_id IS DISTINCT FROM :connection
                            OR j.source_id IS NULL
                            OR NOT (j.source_id=ANY(CAST(:sources AS UUID[])))
                            OR j.payload - 'document_version_id'
                                         - 'embedding_profile_id'
                                         - 'embedding_model' <> '{}'::jsonb)
                   ) AS unknown_jobs,
                   (SELECT count(*) FROM (
                     SELECT 1 FROM app.chunks chunk
                     JOIN app.document_versions v
                       ON v.workspace_id=chunk.workspace_id
                      AND v.id=chunk.document_version_id
                     WHERE v.workspace_id=:workspace
                       AND v.source_id=ANY(CAST(:sources AS UUID[]))
                     LIMIT :row_limit
                   ) bounded) AS chunks,
                   (SELECT count(*) FROM (
                     SELECT 1 FROM app.document_versions v
                     WHERE v.workspace_id=:workspace
                       AND v.source_id=ANY(CAST(:sources AS UUID[]))
                     LIMIT :row_limit
                   ) bounded) AS versions,
                   (SELECT count(*) FROM (
                     SELECT 1 FROM app.jobs j
                     WHERE j.workspace_id=:workspace
                       AND j.connection_id=:connection LIMIT :row_limit
                   ) bounded) AS jobs,
                   (SELECT count(*) FROM (
                     SELECT 1 FROM app.citations c WHERE c.workspace_id=:workspace
                       AND c.source_id=ANY(CAST(:sources AS UUID[]))
                     LIMIT :row_limit
                   ) bounded) AS citations,
                   (SELECT COALESCE(SUM(v.extracted_bytes), 0)
                     FROM app.document_versions v WHERE v.workspace_id=:workspace
                       AND v.id=ANY(CAST(:versions AS UUID[]))) AS extracted_bytes"""
            ),
            {**params, "row_limit": _MAX_ROWS + 1},
        )
        result = checks.mappings().one()
        if (
            result["external_content"]
            or result["unknown_jobs"]
            or any(
                result[field] > _MAX_ROWS
                for field in ("chunks", "versions", "jobs", "citations")
            )
        ):
            raise DeviceCleanupUnsupported
        # These fixed predicates also bound cascading rows that can exist
        # independently of the number of selected sources.
        scoped_tables = {
            "source_people": "source_id=ANY(CAST(:sources AS UUID[]))",
            "source_collection_memberships": "source_id=ANY(CAST(:sources AS UUID[]))",
            "job_attempts": "job_id IN (SELECT id FROM app.jobs "
            "WHERE workspace_id=:workspace AND connection_id=:connection)",
            "source_collections": "connection_id=:connection",
            "connection_scopes": "connection_id=:connection",
            "connection_cursors": "connection_id=:connection",
            "provider_events": "connection_id=:connection",
            "device_folders": "device_id=:target",
        }
        for table, predicate in scoped_tables.items():
            count = await conn.scalar(
                text(
                    f"SELECT count(*) FROM (SELECT 1 FROM app.{table} "
                    f"WHERE workspace_id=:workspace AND {predicate} "
                    "LIMIT :row_limit) bounded"
                ),
                {**params, "row_limit": _MAX_ROWS + 1},
            )
            if not isinstance(count, int) or count > _MAX_ROWS:
                raise DeviceCleanupUnsupported
        return len(current), int(result["extracted_bytes"])

    @staticmethod
    async def _cutoff(conn: AsyncConnection, params: dict[str, object]) -> None:
        await conn.execute(
            text(
                """UPDATE app.devices SET status='revoked', revoked_at=now(),
                         updated_at=now(), lock_version=lock_version+1,
                         name='Revoked device', app_version='unknown',
                         last_seen_at=NULL,
                         public_key=:replacement_key, credential_hash=:replacement_hash
                   WHERE workspace_id=:workspace AND user_id=:actor AND id=:target"""
            ),
            {
                **params,
                "replacement_key": secrets.token_bytes(32),
                "replacement_hash": secrets.token_bytes(32),
            },
        )
        await conn.execute(
            text(
                """UPDATE app.connections SET status='deleted', deleted_at=now(),
                         updated_at=now(), lock_version=lock_version+1,
                         credential_ciphertext=NULL, encrypted_data_key=NULL,
                         key_version=NULL, last_successful_sync_at=NULL,
                         last_error_code=NULL, external_account_id_hash=NULL,
                         display_label='Local files'
                   WHERE workspace_id=:workspace AND id=:connection"""
            ),
            params,
        )

    @staticmethod
    async def _purge(
        conn: AsyncConnection, params: dict[str, object], counts: tuple[int, int]
    ) -> None:
        await conn.execute(
            text(
                """DELETE FROM app.messages m
                   WHERE m.workspace_id=:workspace AND m.role='assistant'
                     AND EXISTS (
                       SELECT 1 FROM app.citations c WHERE c.workspace_id=m.workspace_id
                         AND c.message_id=m.id
                         AND c.source_id=ANY(CAST(:sources AS UUID[]))
                     )"""
            ),
            params,
        )
        # Cascades cover source people, versions, chunks, embeddings, citations,
        # source collection memberships, and per-job attempts.
        await conn.execute(
            text(
                """DELETE FROM app.outbox_events e WHERE e.workspace_id=:workspace
                   AND ((e.aggregate_type='source'
                         AND e.aggregate_id=ANY(CAST(:sources AS UUID[])))
                     OR (e.aggregate_type='connection' AND e.aggregate_id=:connection)
                     OR (e.aggregate_type='device' AND e.aggregate_id=:target)
                     OR (e.aggregate_type='job' AND EXISTS (
                          SELECT 1 FROM app.jobs j WHERE j.workspace_id=e.workspace_id
                            AND j.id=e.aggregate_id
                            AND j.connection_id=:connection)))"""
            ),
            params,
        )
        for table in (
            "jobs",
            "sources",
            "source_collections",
            "connection_scopes",
            "connection_cursors",
            "provider_events",
        ):
            await conn.execute(
                text(
                    f"DELETE FROM app.{table} WHERE workspace_id=:workspace "
                    "AND connection_id=:connection"
                ),
                params,
            )
        await conn.execute(
            text(
                "DELETE FROM app.device_folders WHERE workspace_id=:workspace "
                "AND device_id=:target"
            ),
            params,
        )
        await conn.execute(
            text(
                """UPDATE app.workspace_usage SET
                     indexed_source_count=GREATEST(indexed_source_count-:indexed,0),
                     extracted_bytes=GREATEST(extracted_bytes-:bytes,0),
                     updated_at=now(), lock_version=lock_version+1
                   WHERE workspace_id=:workspace"""
            ),
            {**params, "indexed": counts[0], "bytes": counts[1]},
        )
        await conn.execute(
            text(
                """UPDATE app.workspaces SET
                     search_index_generation=search_index_generation+1,
                     updated_at=now(), lock_version=lock_version+1
                   WHERE id=:workspace"""
            ),
            params,
        )

    async def _finish(
        self,
        conn: AsyncConnection,
        params: dict[str, object],
        scope: RegistrationScope,
        request_id: UUID,
        target: UUID,
    ) -> DeviceRevocationReceipt:
        receipt_id = uuid4()
        key_hash = params["key_hash"]
        assert isinstance(key_hash, bytes)
        event = await conn.execute(
            text(
                """INSERT INTO app.deletion_requests
                     (id, workspace_id, requested_by_user_id, target_type,
                      target_id, status, idempotency_key, deadline_at,
                      started_at, completed_at)
                   VALUES (:receipt, :workspace, :actor, 'device',
                           :target, 'completed', :receipt_key,
                           transaction_timestamp() + INTERVAL '24 hours',
                           transaction_timestamp(), clock_timestamp())
                   RETURNING id, requested_at, started_at, completed_at, deadline_at"""
            ),
            {**params, "receipt": receipt_id, "receipt_key": key_hash.hex()},
        )
        written = await conn.scalar(
            text(
                """INSERT INTO app.api_idempotency_keys AS entry
                     (id, workspace_id, user_id, key_hash, method, route_template,
                      request_hash, status, response_status, resource_type,
                      resource_id, expires_at)
                   VALUES (:reservation, :workspace, :actor, :key_hash, 'DELETE',
                           :route, :request_hash, 'completed', 202, :resource, :receipt,
                           clock_timestamp() + INTERVAL '24 hours')
                   ON CONFLICT (workspace_id,user_id,method,route_template,key_hash)
                   DO UPDATE SET request_hash=EXCLUDED.request_hash,
                     status='completed', response_status=202, response_body=NULL,
                     resource_type=:resource, resource_id=:receipt,
                     created_at=clock_timestamp(), updated_at=clock_timestamp(),
                     expires_at=EXCLUDED.expires_at
                   WHERE entry.expires_at<=clock_timestamp()
                   RETURNING true"""
            ),
            {**params, "reservation": uuid4(), "receipt": receipt_id},
        )
        if written is not True:
            raise DeviceRevocationBusy
        await conn.execute(
            text(
                """INSERT INTO app.audit_events
                     (id, workspace_id, workspace_ref_hash, actor_user_id, action,
                      target_type,outcome,target_ref_hash,request_id)
                   VALUES (:event,:workspace,:workspace_hash,:actor,'device.revoked',
                           'device','succeeded',:target_hash,:request_id)"""
            ),
            {
                **params,
                "event": uuid4(),
                "workspace_hash": hmac.digest(
                    self._audit, b"workspace:" + scope.workspace_id.bytes, "sha256"
                ),
                "target_hash": hmac.digest(
                    self._audit, b"device:" + target.bytes, "sha256"
                ),
                "request_id": request_id,
            },
        )
        return _receipt(event.mappings().one())

    @staticmethod
    async def _replay(
        conn: AsyncConnection, params: dict[str, object], request_hash: bytes
    ) -> UUID | None:
        result = await conn.execute(
            text(
                """SELECT request_hash, status, response_status,
                          resource_type, resource_id
                   FROM app.api_idempotency_keys
                   WHERE workspace_id=:workspace AND user_id=:actor
                     AND method='DELETE' AND route_template=:route
                     AND key_hash=:key_hash
                     AND expires_at>clock_timestamp()"""
            ),
            params,
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        if not hmac.compare_digest(row["request_hash"], request_hash):
            raise DeviceRevocationConflict
        if row["status"] == "processing":
            raise DeviceRevocationBusy
        receipt_id = row["resource_id"]
        if (
            row["status"] != "completed"
            or row["response_status"] != 202
            or row["resource_type"] != RESOURCE
            or not isinstance(receipt_id, UUID)
        ):
            raise DeviceRevocationConflict
        return receipt_id

    async def read(
        self, *, scope: RegistrationScope, receipt_id: UUID
    ) -> DeviceRevocationReceipt:
        try:
            async with self._engine.begin() as conn:
                params = await SQLAlchemyDeviceQueryReader._authorize(conn, scope)
                return await self._read(conn, params, receipt_id)
        except DeviceQueryDenied:
            raise DeviceRevocationRejected from None
        except SQLAlchemyError:
            raise DeviceRevocationUnavailable from None

    @staticmethod
    async def _read(
        conn: AsyncConnection,
        params: dict[str, object],
        receipt_id: UUID,
        *,
        target: UUID | None = None,
    ) -> DeviceRevocationReceipt:
        result = await conn.execute(
            text(
                """SELECT r.id, r.requested_at, r.started_at,
                          r.completed_at, r.deadline_at
                   FROM app.deletion_requests r
                   JOIN app.devices d ON d.workspace_id=r.workspace_id
                                     AND d.id=r.target_id
                   WHERE r.workspace_id=:workspace AND r.id=:receipt
                     AND r.target_type='device' AND r.status='completed'
                     AND r.requested_by_user_id=:actor AND d.user_id=:actor
                     AND d.status='revoked' AND d.revoked_at IS NOT NULL
                     AND (CAST(:read_target AS UUID) IS NULL
                          OR r.target_id=:read_target)"""
            ),
            {**params, "receipt": receipt_id, "read_target": target},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise DeviceRevocationRejected
        return _receipt(row)
