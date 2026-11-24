"""Durable manual sync and narrowing of already verified repository grants."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from universal_ai_search.api.idempotency import ReplayResponse, build_fingerprint

from .github import READ_PERMISSIONS
from .google import GMAIL_READONLY_SCOPE
from .query import (
    _ALLOWED_SCOPES,
    _BASE,
    _DETAIL_COLLECTIONS_LIMIT,
    ConnectionResponse,
    ConnectionSnapshot,
    _snapshot,
)
from .store import _set_context

SYNC_ROUTE = "/v1/connections/{connection_id}/sync"
SELECTION_ROUTE = "/v1/connections/{connection_id}/selections"
_MAX_SELECTIONS = 500
_GITHUB_SCOPES = frozenset(
    f"{permission}:{level}" for permission, level in READ_PERMISSIONS.items()
)


class ConnectionMutationError(Exception):
    pass


class ConnectionMutationDenied(ConnectionMutationError):
    pass


class ConnectionMutationMissing(ConnectionMutationError):
    pass


class ConnectionMutationBusy(ConnectionMutationError):
    pass


class ConnectionMutationKeyConflict(ConnectionMutationError):
    pass


class ConnectionMutationNotSupported(ConnectionMutationError):
    pass


class ConnectionMutationStale(ConnectionMutationError):
    pass


class ConnectionSelectionInvalid(ConnectionMutationError):
    pass


@dataclass(frozen=True)
class ConnectionMutationScope:
    workspace_id: UUID
    user_id: UUID
    session_id: UUID
    workspace_version: int
    user_version: int


@dataclass(frozen=True)
class SyncReceipt:
    id: UUID
    created_at: datetime
    status: Literal["pending", "running", "retry_wait"]
    started_at: datetime | None


class ConnectionMutationStore(Protocol):
    async def sync(
        self,
        *,
        scope: ConnectionMutationScope,
        connection_id: UUID,
        idempotency_key: str,
        request_id: UUID,
    ) -> SyncReceipt: ...

    async def narrow(
        self,
        *,
        scope: ConnectionMutationScope,
        connection_id: UUID,
        collection_ids: tuple[UUID, ...],
        if_match: str,
        idempotency_key: str,
        request_id: UUID,
    ) -> ConnectionSnapshot: ...


def mutation_etag(connection_id: UUID, lock_version: int) -> str:
    # Same strong ETag encoding as the existing connection read endpoint.
    return f'"conn-{connection_id.hex}-{lock_version:x}"'


class SQLAlchemyConnectionMutationStore:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        idempotency_secret: bytes,
        audit_secret: bytes,
        google_sync_enabled: bool,
        github_sync_enabled: bool,
    ) -> None:
        if min(len(idempotency_secret), len(audit_secret)) < 32:
            raise ValueError("connection mutation secrets must be at least 32 bytes")
        self._engine = engine
        self._idempotency_secret = idempotency_secret
        self._audit_secret = audit_secret
        self._google_sync_enabled = google_sync_enabled
        self._github_sync_enabled = github_sync_enabled

    async def sync(
        self,
        *,
        scope: ConnectionMutationScope,
        connection_id: UUID,
        idempotency_key: str,
        request_id: UUID,
    ) -> SyncReceipt:
        async with self._engine.begin() as conn:
            params = await self._begin(
                conn,
                scope,
                connection_id,
                "POST",
                SYNC_ROUTE,
                idempotency_key,
                {"connection_id": str(connection_id)},
            )
            current = await self._get_connection(conn, params)
            actual_grants = await self._scopes(conn, params)
            collection_id: UUID | None = None
            if (
                self._google_sync_enabled
                and current["provider"] == "google"
                and current["status"] == "active"
                and current["has_credentials"]
                and actual_grants == {GMAIL_READONLY_SCOPE}
            ):
                family = "gmail"
            elif self._github_sync_enabled and self._is_github(current, actual_grants):
                selected = await self._selected(conn, params)
                if len(selected) != 1:
                    raise ConnectionMutationNotSupported
                family, collection_id = "github", selected[0]
            else:
                raise ConnectionMutationNotSupported
            prior = await self._prior(conn, params, kind="connection_sync_job")
            if prior is not None:
                job = await conn.scalar(
                    text(
                        """SELECT id FROM app.jobs
                           WHERE id = :job AND workspace_id = :workspace
                             AND connection_id = :target
                             AND job_type = 'sync' AND queue = 'sync'
                             AND (CAST(:collection AS UUID) IS NULL OR
                                  payload ->> 'collection_id' = :collection_text)"""
                    ),
                    {
                        **params,
                        "job": prior["resource_id"],
                        "collection": collection_id,
                        "collection_text": str(collection_id),
                    },
                )
                if job is None:
                    raise ConnectionMutationMissing
                cached = prior["response_body"]
                return SyncReceipt(
                    id=job,
                    created_at=datetime.fromisoformat(cached["created_at"]),
                    status=cached["status"],
                    started_at=(
                        datetime.fromisoformat(cached["started_at"])
                        if cached["started_at"] is not None
                        else None
                    ),
                )
            receipt = await self._queue(conn, params, family, collection_id)
            await self._remember(
                conn,
                params,
                kind="connection_sync_job",
                resource_id=receipt.id,
                response_status=202,
                body={
                    "created_at": receipt.created_at.isoformat(),
                    "status": receipt.status,
                    "started_at": (
                        receipt.started_at.isoformat() if receipt.started_at else None
                    ),
                },
            )
            await self._audit(
                conn, params, scope, "connection.sync_requested", request_id
            )
            return receipt

    async def narrow(
        self,
        *,
        scope: ConnectionMutationScope,
        connection_id: UUID,
        collection_ids: tuple[UUID, ...],
        if_match: str,
        idempotency_key: str,
        request_id: UUID,
    ) -> ConnectionSnapshot:
        keep = frozenset(collection_ids)
        if not 1 <= len(keep) <= _MAX_SELECTIONS or len(keep) != len(collection_ids):
            raise ConnectionSelectionInvalid
        async with self._engine.begin() as conn:
            params = await self._begin(
                conn,
                scope,
                connection_id,
                "PUT",
                SELECTION_ROUTE,
                idempotency_key,
                {
                    "connection_id": str(connection_id),
                    "collection_ids": sorted(map(str, keep)),
                },
                if_match=if_match,
            )
            current = await self._get_connection(conn, params)
            if not self._github_sync_enabled or not self._is_github(
                current, await self._scopes(conn, params)
            ):
                raise ConnectionMutationNotSupported
            selected = frozenset(await self._selected(conn, params))
            prior = await self._prior(conn, params, kind="connection_selections")
            if prior is not None:
                if selected != keep or prior["resource_id"] != connection_id:
                    raise ConnectionMutationStale
                cached = prior["response_body"]
                return ConnectionSnapshot(
                    body=ConnectionResponse.model_validate(cached["body"]),
                    lock_version=cached["version"],
                )
            expected = mutation_etag(connection_id, current["lock_version"])
            if if_match != expected:
                raise ConnectionMutationStale
            if not keep <= selected:
                raise ConnectionSelectionInvalid

            removed = sorted(map(str, selected - keep))
            if removed:
                await conn.execute(
                    text(
                        """UPDATE app.source_collections SET selected = false,
                             github_verified_at = NULL,
                             github_authorization_generation =
                                github_authorization_generation + 1,
                             updated_at = clock_timestamp()
                           WHERE workspace_id = :workspace AND connection_id = :target
                             AND id = ANY(CAST(:removed AS UUID[]))"""
                    ),
                    {**params, "removed": removed},
                )
                # Workers validate the selection and workspace versions again
                # before opening or publishing a snapshot. Omitted repositories
                # become inaccessible without guessing provider deletion.
                await conn.execute(
                    text(
                        """UPDATE app.workspaces SET
                             authorization_version = authorization_version + 1,
                             search_index_generation = search_index_generation + 1,
                             updated_at = clock_timestamp()
                           WHERE id = :workspace"""
                    ),
                    params,
                )
                await conn.execute(
                    text(
                        """UPDATE app.connections SET lock_version = lock_version + 1,
                               updated_at = clock_timestamp()
                           WHERE workspace_id = :workspace AND id = :target"""
                    ),
                    params,
                )
                await self._cancel_removed(conn, params, removed)
                for collection_id in sorted(keep):
                    await self._queue(conn, params, "github", collection_id)
                await self._audit(
                    conn, params, scope, "connection.selections_narrowed", request_id
                )
            result = await conn.execute(
                text(_BASE + " AND c.id = :target"),
                {
                    **params,
                    "workspace_id": scope.workspace_id,
                    "allowed_scopes": _ALLOWED_SCOPES,
                    "collection_limit": _DETAIL_COLLECTIONS_LIMIT + 1,
                },
            )
            snapshot = _snapshot(result.mappings().one(), _DETAIL_COLLECTIONS_LIMIT)
            await self._remember(
                conn,
                params,
                kind="connection_selections",
                resource_id=connection_id,
                response_status=200,
                body={
                    "body": snapshot.body.model_dump(mode="json"),
                    "version": snapshot.lock_version,
                },
            )
            return snapshot

    async def _begin(
        self,
        conn: AsyncConnection,
        scope: ConnectionMutationScope,
        target: UUID,
        method: str,
        route: str,
        key: str,
        body: object,
        *,
        if_match: str | None = None,
    ) -> dict[str, object]:
        fingerprint = build_fingerprint(
            key=key,
            hash_secret=self._idempotency_secret,
            workspace_id=scope.workspace_id,
            principal_id=scope.user_id,
            method=method,
            route_template=route,
            validated_body=body,
            relevant_headers={"If-Match": if_match} if if_match is not None else None,
        )
        key_hash = bytes.fromhex(fingerprint.key_hash)
        lock = int.from_bytes(
            hashlib.sha256(
                b"connection-mutation"
                + scope.workspace_id.bytes
                + scope.user_id.bytes
                + key_hash
            ).digest()[:8],
            "big",
            signed=True,
        )
        if (
            await conn.scalar(
                text("SELECT pg_try_advisory_xact_lock(CAST(:key AS BIGINT))"),
                {"key": lock},
            )
            is not True
        ):
            raise ConnectionMutationBusy

        await _set_context(conn, workspace_id=scope.workspace_id, user_id=scope.user_id)
        params: dict[str, object] = {
            "workspace": scope.workspace_id,
            "actor": scope.user_id,
            "session": scope.session_id,
            "workspace_version": scope.workspace_version,
            "user_version": scope.user_version,
            "method": method,
            "route": route,
            "target": target,
            "key_hash": key_hash,
            "request_hash": bytes.fromhex(fingerprint.request_hash),
        }
        # Same workspace-first order as provider snapshot and webhook paths.
        locked = await conn.scalar(
            text(
                """SELECT true FROM app.workspaces AS w
                   WHERE w.id = :workspace AND w.status = 'active'
                     AND w.authorization_version = :workspace_version
                   FOR UPDATE"""
            ),
            params,
        )
        if locked is not True:
            raise ConnectionMutationDenied
        manager = await conn.scalar(
            text(
                """SELECT true FROM app.workspace_members AS m
                   JOIN app.users AS u ON u.id = m.user_id
                   JOIN app.sessions AS s ON s.user_id = u.id
                   WHERE m.workspace_id = :workspace AND m.user_id = :actor
                     AND m.role IN ('owner', 'admin') AND m.status = 'active'
                     AND u.status = 'active' AND u.email_verified_at IS NOT NULL
                     AND u.lock_version = :user_version
                     AND s.id = :session AND s.authorization_version = :user_version
                     AND s.revoked_at IS NULL AND s.rotated_at IS NULL
                     AND s.expires_at > clock_timestamp()
                   FOR SHARE OF m, u, s"""
            ),
            params,
        )
        if manager is not True:
            raise ConnectionMutationDenied
        return params

    @staticmethod
    async def _get_connection(
        conn: AsyncConnection, params: dict[str, object]
    ) -> RowMapping:
        result = await conn.execute(
            text(
                """SELECT provider, status, lock_version,
                          github_app_id, github_installation_id,
                          (credential_ciphertext IS NOT NULL
                           AND encrypted_data_key IS NOT NULL
                           AND key_version IS NOT NULL) AS has_credentials
                   FROM app.connections WHERE workspace_id = :workspace
                     AND id = :target AND status NOT IN ('deleting', 'deleted')
                   FOR UPDATE"""
            ),
            params,
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise ConnectionMutationMissing
        return row

    @staticmethod
    async def _scopes(
        conn: AsyncConnection, params: dict[str, object]
    ) -> frozenset[str]:
        result = await conn.scalars(
            text(
                """SELECT scope FROM app.connection_scopes
                   WHERE workspace_id = :workspace AND connection_id = :target
                   ORDER BY scope LIMIT 10"""
            ),
            params,
        )
        return frozenset(result)

    @staticmethod
    def _is_github(current: RowMapping, actual_grants: frozenset[str]) -> bool:
        return (
            current["provider"] == "github"
            and current["status"] in {"active", "pending"}
            and current["github_app_id"] is not None
            and current["github_installation_id"] is not None
            and actual_grants == _GITHUB_SCOPES
        )

    @staticmethod
    async def _selected(
        conn: AsyncConnection, params: dict[str, object]
    ) -> tuple[UUID, ...]:
        result = await conn.execute(
            text(
                """SELECT id, kind, provider_external_id FROM app.source_collections
                   WHERE workspace_id = :workspace AND connection_id = :target
                     AND selected AND status = 'active'
                   ORDER BY id LIMIT 501 FOR UPDATE"""
            ),
            params,
        )
        rows = result.mappings().all()
        if len(rows) > _MAX_SELECTIONS or any(
            row["kind"] != "repository"
            or re.fullmatch(r"[1-9][0-9]{0,15}", row["provider_external_id"]) is None
            or int(row["provider_external_id"]) > 2**53 - 1
            for row in rows
        ):
            raise ConnectionMutationNotSupported
        return tuple(row["id"] for row in rows)

    @staticmethod
    async def _prior(
        conn: AsyncConnection, params: dict[str, object], *, kind: str
    ) -> RowMapping | None:
        result = await conn.execute(
            text(
                """SELECT method, route_template, request_hash, status,
                          resource_type, resource_id, response_body
                   FROM app.api_idempotency_keys
                   WHERE workspace_id = :workspace AND user_id = :actor
                     AND route_template IN (:sync_route, :selection_route)
                     AND key_hash = :key_hash AND expires_at > clock_timestamp()
                   ORDER BY created_at LIMIT 1"""
            ),
            {**params, "sync_route": SYNC_ROUTE, "selection_route": SELECTION_ROUTE},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        if (
            row["method"] != params["method"]
            or row["route_template"] != params["route"]
            or not hmac.compare_digest(
                row["request_hash"], cast(bytes, params["request_hash"])
            )
        ):
            raise ConnectionMutationKeyConflict
        if row["status"] == "processing":
            raise ConnectionMutationBusy
        if row["status"] != "completed" or row["resource_type"] != kind:
            raise ConnectionMutationKeyConflict
        return row

    @staticmethod
    async def _remember(
        conn: AsyncConnection,
        params: dict[str, object],
        *,
        kind: str,
        resource_id: UUID,
        response_status: int,
        body: dict[str, object],
    ) -> None:
        replay = ReplayResponse(status=response_status, body=body)
        serialized = json.dumps(replay.body, ensure_ascii=False)
        if len(serialized.encode()) > 63_000:
            raise ConnectionMutationNotSupported
        written = await conn.scalar(
            text(
                """INSERT INTO app.api_idempotency_keys AS entry
                   (id, workspace_id, user_id, key_hash, method, route_template,
                    request_hash, status, response_status, response_body,
                    resource_type, resource_id, expires_at)
                   VALUES (:reservation, :workspace, :actor, :key_hash, :method, :route,
                           :request_hash, 'completed', :status, CAST(:body AS JSONB),
                           :kind, :resource, clock_timestamp() + INTERVAL '24 hours')
                   ON CONFLICT (workspace_id, user_id, method, route_template, key_hash)
                   DO UPDATE SET request_hash = EXCLUDED.request_hash,
                       status = 'completed', response_status = EXCLUDED.response_status,
                       response_body = EXCLUDED.response_body,
                       resource_type = EXCLUDED.resource_type,
                       resource_id = EXCLUDED.resource_id,
                       created_at = clock_timestamp(),
                       updated_at = clock_timestamp(), expires_at = EXCLUDED.expires_at
                   WHERE entry.expires_at <= clock_timestamp()
                   RETURNING true"""
            ),
            {
                **params,
                "reservation": uuid4(),
                "status": response_status,
                "body": serialized,
                "kind": kind,
                "resource": resource_id,
            },
        )
        if written is not True:
            raise ConnectionMutationBusy

    @staticmethod
    async def _queue(
        conn: AsyncConnection,
        params: dict[str, object],
        family: str,
        collection_id: UUID | None,
    ) -> SyncReceipt:
        # Exact worker-owned discriminators. Neither a root, remote repository
        # ID, arbitrary mode, nor arbitrary job payload comes from the caller.
        if family == "github" and collection_id is not None:
            condition = (
                "j.payload ->> 'mode' = 'github_repository' "
                "AND j.payload ->> 'collection_id' = :collection"
            )
            payload: dict[str, object] = {
                "mode": "github_repository",
                "collection_id": str(collection_id),
            }
        else:
            condition = (
                "j.payload ->> 'mode' IN ('incremental', 'full') "
                "AND j.payload -> 'source_families' = '[\"gmail\"]'::jsonb"
            )
            payload = {"mode": "full", "source_families": ["gmail"]}
        result = await conn.execute(
            text(
                """SELECT j.id, j.status, j.created_at,
                       (SELECT min(a.started_at) FROM app.job_attempts AS a
                        WHERE a.workspace_id = j.workspace_id AND a.job_id = j.id)
                       AS started_at
                   FROM app.jobs AS j
                   WHERE j.workspace_id = :workspace AND j.connection_id = :target
                     AND j.job_type = 'sync' AND j.queue = 'sync'
                     AND j.status IN ('pending', 'retry_wait', 'leased')
                     AND (j.attempt_count < j.max_attempts
                          OR j.lease_expires_at > clock_timestamp())
                     AND """
                + condition
                + """ ORDER BY CASE j.status WHEN 'leased' THEN 0 ELSE 1 END,
                               j.available_at, j.id LIMIT 1 FOR UPDATE OF j"""
            ),
            {**params, "collection": str(collection_id)},
        )
        row = result.mappings().one_or_none()
        if row is not None:
            # Do not bypass a provider-mandated backoff or reset a worker lease.
            if row["status"] == "pending":
                await conn.execute(
                    text(
                        """UPDATE app.jobs SET available_at = LEAST(
                               available_at, clock_timestamp()),
                               updated_at = clock_timestamp()
                           WHERE id = :job AND workspace_id = :workspace"""
                    ),
                    {**params, "job": row["id"]},
                )
            status: Literal["pending", "running", "retry_wait"] = (
                "running"
                if row["status"] == "leased"
                else "retry_wait" if row["status"] == "retry_wait" else "pending"
            )
            return SyncReceipt(row["id"], row["created_at"], status, row["started_at"])
        job_id = uuid4()
        values = {
            **params,
            "job": job_id,
            "job_key": f"manual-sync:{job_id}",
            "job_payload": json.dumps(payload),
        }
        created = await conn.scalar(
            text(
                """INSERT INTO app.jobs
                   (id, workspace_id, connection_id, job_type, queue,
                    idempotency_key, status, payload)
                   VALUES (:job, :workspace, :target, 'sync', 'sync', :job_key,
                           'pending', CAST(:job_payload AS JSONB))
                   RETURNING created_at"""
            ),
            values,
        )
        await conn.execute(
            text(
                """INSERT INTO app.outbox_events
                   (id, workspace_id, aggregate_type, aggregate_id, event_type, payload)
                   VALUES (:event, :workspace, 'connection', :target,
                           'connection.sync.requested',
                           CAST(:event_payload AS JSONB))"""
            ),
            {
                **values,
                "event": uuid4(),
                "event_payload": json.dumps(
                    {"job_id": str(job_id), "source_family": family}
                ),
            },
        )
        return SyncReceipt(job_id, cast(datetime, created), "pending", None)

    @staticmethod
    async def _cancel_removed(
        conn: AsyncConnection, params: dict[str, object], removed: list[str]
    ) -> None:
        await conn.execute(
            text(
                """WITH stopped AS (
                       UPDATE app.jobs AS j SET status = 'failed',
                           error_code = 'COLLECTION_DESELECTED',
                           lease_owner = NULL, lease_expires_at = NULL,
                           updated_at = clock_timestamp(),
                           completed_at = clock_timestamp()
                       WHERE j.workspace_id = :workspace AND j.connection_id = :target
                         AND j.queue = 'sync' AND j.job_type = 'sync'
                         AND j.status IN ('pending', 'retry_wait', 'leased')
                         AND j.payload ->> 'mode' = 'github_repository'
                         AND j.payload ->> 'collection_id' =
                             ANY(CAST(:removed AS TEXT[]))
                       RETURNING j.id, j.attempt_count
                   )
                   UPDATE app.job_attempts AS a SET status = 'permanent_failure',
                       error_code = 'COLLECTION_DESELECTED',
                       finished_at = clock_timestamp()
                   FROM stopped
                   WHERE a.workspace_id = :workspace AND a.job_id = stopped.id
                     AND a.attempt_number = stopped.attempt_count
                     AND a.status = 'running'"""
            ),
            {**params, "removed": removed},
        )

    async def _audit(
        self,
        conn: AsyncConnection,
        params: dict[str, object],
        scope: ConnectionMutationScope,
        action: str,
        request_id: UUID,
    ) -> None:
        target = cast(UUID, params["target"])
        await conn.execute(
            text(
                """INSERT INTO app.audit_events
                   (id, workspace_id, workspace_ref_hash, actor_user_id,
                    action, target_type, outcome, target_ref_hash, request_id)
                   VALUES (:event, :workspace, :whash, :actor, :action, 'connection',
                           'succeeded', :thash, :request)"""
            ),
            {
                **params,
                "event": uuid4(),
                "request": request_id,
                "action": action,
                "whash": hmac.digest(
                    self._audit_secret,
                    b"workspace:" + scope.workspace_id.bytes,
                    "sha256",
                ),
                "thash": hmac.digest(
                    self._audit_secret, b"connection:" + target.bytes, "sha256"
                ),
            },
        )
