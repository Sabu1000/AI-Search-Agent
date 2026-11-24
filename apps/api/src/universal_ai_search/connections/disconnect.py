"""Atomic local GitHub cutoff, with completion only for untouched grants."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from universal_ai_search.api.idempotency import validate_idempotency_key
from universal_ai_search.auth.reauth import (
    ReauthenticationFailed,
    ReauthenticationService,
)
from universal_ai_search.auth.reauth_store import ReauthScope

DELETE_ROUTE = "/v1/connections/{connection_id}"
CONFIRMATION = "disconnect"
RESOURCE = "connection_deletion"
MAX_JOBS = 1000


class DisconnectMissing(Exception):
    """No available resource or current manager authority."""


class DisconnectUnsupported(Exception):
    """Cannot safely complete this local disconnect."""


class DisconnectConflict(Exception):
    """The idempotency key was bound to a different authenticated request."""


class DisconnectBusy(Exception):
    """Work is already in progress."""


@dataclass(frozen=True)
class DisconnectReceipt:
    id: UUID
    status: Literal["pending", "completed"]
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    deadline_at: datetime


def _receipt(row: RowMapping) -> DisconnectReceipt:
    return DisconnectReceipt(
        row["id"],
        row["status"],
        row["requested_at"],
        row["started_at"],
        row["completed_at"],
        row["deadline_at"],
    )


class ConnectionDisconnectStore:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        reauth_service: ReauthenticationService,
        idempotency_secret: bytes,
        audit_secret: bytes,
    ) -> None:
        if min(len(idempotency_secret), len(audit_secret)) < 32:
            raise ValueError("disconnect secrets must be at least 32 bytes")
        self._engine = engine
        self._reauth = reauth_service
        self._idempotency_secret = idempotency_secret
        self._audit_secret = audit_secret

    async def delete(
        self,
        *,
        scope: ReauthScope,
        connection_id: UUID,
        reauth_token: str,
        idempotency_key: str,
        request_id: UUID,
        allow_pending: bool = False,
    ) -> DisconnectReceipt:
        """Default: only a never-fetched GitHub grant. No remote installation changes.

        allow_pending is an internal boundary for future worker integration only.
        The HTTP route must not enable it before there is a real cleanup worker.
        """
        key = validate_idempotency_key(idempotency_key)
        if len(reauth_token) != 43 or not reauth_token.isascii():
            raise ReauthenticationFailed
        key_hash = hmac.digest(
            self._idempotency_secret,
            b"connection-disconnect-key:" + key.encode(),
            "sha256",
        )
        request_hash = hmac.digest(
            self._idempotency_secret,
            b"connection-disconnect-request:"
            + connection_id.bytes
            + scope.session_id.bytes
            + reauth_token.encode(),
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
        async with self._engine.begin() as conn:
            await conn.execute(text("SET LOCAL lock_timeout = '2s'"))
            await conn.execute(text("SET LOCAL statement_timeout = '10s'"))
            params = {
                **await self._authorize(conn, scope, mutate=True),
                "target": connection_id,
                "key_hash": key_hash,
                "request_hash": request_hash,
                "route": DELETE_ROUTE,
                "type": RESOURCE,
                "lock": lock_id,
            }
            if (
                await conn.scalar(
                    text("SELECT pg_try_advisory_xact_lock(CAST(:lock AS BIGINT))"),
                    params,
                )
                is not True
            ):
                raise DisconnectBusy
            previous = await self._prior(conn, params, request_hash)
            if previous is not None:
                if previous.status == "pending" and not allow_pending:
                    raise DisconnectUnsupported
                return previous

            await self._reauth.consume(
                conn, scope=scope, purpose="disconnect", token=reauth_token
            )
            current = (
                (
                    await conn.execute(
                        text(
                            """SELECT provider,
                             github_installation_id IS NOT NULL AS installed,
                             github_app_id IS NOT NULL AS app_verified, status,
                             last_successful_sync_at IS NULL AS never_synced,
                             credential_ciphertext IS NULL AS no_credential
                           FROM app.connections WHERE workspace_id = :workspace
                             AND id = :target AND status NOT IN ('deleting','deleted')
                           FOR UPDATE"""
                        ),
                        params,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if current is None:
                raise DisconnectMissing
            if (
                current["provider"] != "github"
                or not current["installed"]
                or not current["app_verified"]
                or not current["no_credential"]
            ):
                raise DisconnectUnsupported

            # This order matches GitHub scan publication and signed webhook
            # routing. NOWAIT avoids inversion with index promotions that
            # already hold a job while updating the workspace's generation.
            jobs = (
                (
                    await conn.execute(
                        text(
                            """SELECT attempt_count, status, job_type,
                             payload ->> 'mode' AS mode FROM app.jobs
                           WHERE workspace_id = :workspace AND connection_id = :target
                           ORDER BY id LIMIT :job_limit FOR UPDATE NOWAIT"""
                        ),
                        {**params, "job_limit": MAX_JOBS + 1},
                    )
                )
                .mappings()
                .all()
            )
            if len(jobs) > MAX_JOBS:
                raise DisconnectUnsupported
            untouched = (
                current["status"] == "pending"
                and current["never_synced"]
                and bool(jobs)
                and all(
                    job["attempt_count"] == 0
                    and job["status"] == "pending"
                    and job["job_type"] == "sync"
                    and job["mode"] == "github_repository"
                    for job in jobs
                )
                and await conn.scalar(
                    text(
                        """SELECT NOT (
                             EXISTS (SELECT 1 FROM app.sources
                                     WHERE workspace_id = :workspace AND
                                       connection_id = :target)
                             OR EXISTS (SELECT 1 FROM app.devices
                                     WHERE workspace_id = :workspace AND
                                       connection_id = :target)
                             OR EXISTS (SELECT 1 FROM app.source_collections
                                     WHERE workspace_id = :workspace AND
                                       connection_id = :target
                                       AND github_verified_at IS NOT NULL)
                             OR EXISTS (SELECT 1 FROM app.connection_cursors
                                     WHERE workspace_id = :workspace AND
                                       connection_id = :target)
                           )"""
                    ),
                    params,
                )
                is True
            )
            if not untouched and not allow_pending:
                raise DisconnectUnsupported
            await self._cutoff(conn, params, complete=untouched)
            return await self._record(
                conn, params, scope, connection_id, request_id, untouched
            )

    @staticmethod
    async def _authorize(
        conn: AsyncConnection, scope: ReauthScope, *, mutate: bool
    ) -> dict[str, object]:
        await conn.execute(
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
            "user_version": scope.user_version,
            "workspace_version": scope.workspace_version,
        }

        # Acquire in the same account -> workspace -> session order used when
        # minting a proof; take the stronger workspace lock before consuming.
        identity = await conn.scalar(
            text(
                """SELECT true FROM app.users WHERE id = :actor
                     AND status = 'active' AND email_verified_at IS NOT NULL
                     AND lock_version = :user_version FOR SHARE"""
            ),
            values,
        )
        lock = "FOR SHARE OF m FOR UPDATE OF w" if mutate else "FOR SHARE OF m,w"
        membership = await conn.scalar(
            text(
                f"""SELECT true FROM app.workspace_members m JOIN app.workspaces w
                         ON w.id = m.workspace_id
                    WHERE m.user_id = :actor AND m.workspace_id = :workspace
                      AND m.status = 'active' AND m.role IN ('owner','admin')
                      AND w.status = 'active'
                      AND w.authorization_version = :workspace_version {lock}"""
            ),
            values,
        )
        active_session = await conn.scalar(
            text(
                """SELECT true FROM app.sessions WHERE id = :session
                     AND user_id = :actor AND authorization_version = :user_version
                     AND revoked_at IS NULL AND rotated_at IS NULL
                     AND expires_at > clock_timestamp() FOR SHARE"""
            ),
            values,
        )
        if not all(value is True for value in (identity, membership, active_session)):
            raise DisconnectMissing
        return values

    @staticmethod
    async def _cutoff(
        conn: AsyncConnection, params: dict[str, object], *, complete: bool
    ) -> None:
        await conn.execute(
            text(
                """UPDATE app.connections
                   SET status = :status, display_label = 'Disconnected GitHub',
                       credential_ciphertext = NULL, encrypted_data_key = NULL,
                       key_version = NULL, last_error_code = NULL,
                       external_account_id_hash = NULL,
                       github_app_id = NULL,
                       github_installation_id = CASE WHEN :complete THEN NULL
                                                     ELSE github_installation_id END,
                       deleted_at = CASE WHEN :complete THEN clock_timestamp()
                                         ELSE NULL END,
                       updated_at = clock_timestamp(), lock_version = lock_version + 1
                   WHERE workspace_id = :workspace AND id = :target"""
            ),
            {
                **params,
                "complete": complete,
                "status": "deleted" if complete else "deleting",
            },
        )
        for statement in (
            (
                "DELETE FROM app.connection_cursors "
                "WHERE workspace_id = :workspace AND connection_id = :target"
            ),
            (
                "DELETE FROM app.connection_scopes "
                "WHERE workspace_id = :workspace AND connection_id = :target"
            ),
        ):
            await conn.execute(text(statement), params)
        if complete:
            # No sources or documents exist, so only connection-local grant
            # records and never-started sync jobs exist. Leave a scrubbed tombstone.
            for statement in (
                (
                    "DELETE FROM app.jobs "
                    "WHERE workspace_id = :workspace AND connection_id = :target"
                ),
                (
                    "DELETE FROM app.source_collections "
                    "WHERE workspace_id = :workspace AND connection_id = :target"
                ),
                (
                    "DELETE FROM app.provider_events "
                    "WHERE workspace_id = :workspace AND connection_id = :target"
                ),
                """DELETE FROM app.outbox_events WHERE workspace_id = :workspace
                       AND aggregate_type = 'connection' AND aggregate_id = :target""",
            ):
                await conn.execute(text(statement), params)
        else:
            await conn.execute(
                text(
                    """UPDATE app.source_collections
                         SET selected = false, status = 'deleting',
                         github_verified_at = NULL,
                         github_authorization_generation =
                           github_authorization_generation + 1,
                         updated_at = clock_timestamp()
                       WHERE workspace_id = :workspace AND connection_id = :target"""
                ),
                params,
            )
            await conn.execute(
                text(
                    """UPDATE app.job_attempts SET status = 'permanent_failure',
                         error_code = 'CONNECTION_DISCONNECTED', finished_at =
                           clock_timestamp()
                       WHERE workspace_id = :workspace AND status = 'running'
                         AND job_id IN (SELECT id FROM app.jobs WHERE workspace_id
                           = :workspace
                                           AND connection_id = :target AND status
                                             = 'leased')"""
                ),
                params,
            )
            await conn.execute(
                text(
                    """UPDATE app.jobs SET status = 'failed',
                         error_code = 'CONNECTION_DISCONNECTED',
                         lease_owner = NULL, lease_expires_at = NULL,
                         completed_at = clock_timestamp(), updated_at =
                           clock_timestamp()
                       WHERE workspace_id = :workspace AND connection_id = :target
                         AND status IN ('pending','retry_wait','leased')"""
                ),
                params,
            )
        await conn.execute(
            text(
                """UPDATE app.workspaces
                     SET authorization_version = authorization_version + 1,
                     search_index_generation = search_index_generation + 1,
                     lock_version = lock_version + 1, updated_at = clock_timestamp()
                   WHERE id = :workspace"""
            ),
            params,
        )

    async def _record(
        self,
        conn: AsyncConnection,
        params: dict[str, object],
        scope: ReauthScope,
        connection_id: UUID,
        request_id: UUID,
        complete: bool,
    ) -> DisconnectReceipt:
        receipt_id = uuid4()
        values = {
            **params,
            "receipt": receipt_id,
            "complete": complete,
            "receipt_key": hmac.digest(
                self._idempotency_secret,
                b"disconnect-receipt:"
                + scope.user_id.bytes
                + cast(bytes, params["key_hash"]),
                "sha256",
            ).hex(),
            "status": "completed" if complete else "pending",
        }
        result = await conn.execute(
            text(
                """WITH time_at AS (SELECT transaction_timestamp() AS at)
                   INSERT INTO app.deletion_requests
(id,workspace_id,requested_by_user_id,target_type,target_id,status,
idempotency_key,requested_at,deadline_at,started_at,completed_at)
                   SELECT :receipt,:workspace,:actor,'connection',:target,:status,
                          :receipt_key,at,at + INTERVAL '24 hours',
                          CASE WHEN :complete THEN at ELSE NULL END,
                          CASE WHEN :complete THEN clock_timestamp() ELSE NULL END
                   FROM time_at RETURNING
                     id,status,requested_at,started_at,completed_at,
                       deadline_at"""
            ),
            values,
        )
        if (
            await conn.scalar(
                text(
                    """INSERT INTO app.api_idempotency_keys AS stored
(id,workspace_id,user_id,key_hash,method,route_template,request_hash,status,
                      response_status,resource_type,resource_id,expires_at)
                   VALUES
                     (:entry,:workspace,:actor,:key_hash,'DELETE',:route,:request_hash,
                           'completed',202,:type,:receipt,clock_timestamp() +
                             INTERVAL '24 hours')
                   ON CONFLICT (workspace_id,user_id,method,route_template,key_hash)
                   DO UPDATE SET request_hash = EXCLUDED.request_hash, status =
                     'completed',
                        response_status = 202, response_body = NULL, resource_type
                          = :type,
                        resource_id = :receipt, created_at = clock_timestamp(),
                        updated_at = clock_timestamp(), expires_at =
                          EXCLUDED.expires_at
                   WHERE stored.expires_at <= clock_timestamp()
                   RETURNING true"""
                ),
                {**values, "entry": uuid4()},
            )
            is not True
        ):
            raise DisconnectBusy
        await conn.execute(
            text(
                """INSERT INTO app.audit_events
(id,workspace_id,workspace_ref_hash,actor_user_id,action,target_type,
                      outcome,target_ref_hash,request_id)
                   VALUES
                     (:audit,:workspace,:workspace_hash,:actor,'connection.disconnect',
                           'connection','succeeded',:target_hash,:request_id)"""
            ),
            {
                **values,
                "audit": uuid4(),
                "request_id": request_id,
                "workspace_hash": hmac.digest(
                    self._audit_secret,
                    b"workspace:" + scope.workspace_id.bytes,
                    "sha256",
                ),
                "target_hash": hmac.digest(
                    self._audit_secret, b"connection:" + connection_id.bytes, "sha256"
                ),
            },
        )
        return _receipt(result.mappings().one())

    async def _prior(
        self, conn: AsyncConnection, params: dict[str, object], request_hash: bytes
    ) -> DisconnectReceipt | None:
        prior = (
            (
                await conn.execute(
                    text(
                        """SELECT request_hash,status,response_status,
                           resource_type,resource_id
                       FROM app.api_idempotency_keys
                       WHERE workspace_id = :workspace AND user_id = :actor AND
                         method = 'DELETE'
                         AND route_template = :route AND key_hash = :key_hash
                         AND expires_at > clock_timestamp()"""
                    ),
                    params,
                )
            )
            .mappings()
            .one_or_none()
        )
        if prior is None:
            return None
        if not hmac.compare_digest(prior["request_hash"], request_hash):
            raise DisconnectConflict
        if prior["status"] == "processing":
            raise DisconnectBusy
        if (
            prior["status"] != "completed"
            or prior["response_status"] != 202
            or prior["resource_type"] != RESOURCE
            or not isinstance(prior["resource_id"], UUID)
        ):
            raise DisconnectConflict
        result = await self._read(conn, {**params, "receipt": prior["resource_id"]})
        if result is None:
            raise DisconnectConflict
        return result

    async def read(
        self, *, scope: ReauthScope, receipt_id: UUID
    ) -> DisconnectReceipt | None:
        async with self._engine.begin() as conn:
            values = await self._authorize(conn, scope, mutate=False)
            return await self._read(
                conn, {**values, "receipt": receipt_id, "target": None}
            )

    @staticmethod
    async def _read(
        conn: AsyncConnection, params: dict[str, object]
    ) -> DisconnectReceipt | None:
        result = await conn.execute(
            text(
                """SELECT d.id,d.status,d.requested_at,d.started_at,
                       d.completed_at,d.deadline_at
                   FROM app.deletion_requests d JOIN app.connections c
                     ON c.workspace_id = d.workspace_id AND c.id = d.target_id
                   WHERE d.workspace_id = :workspace AND d.id = :receipt
                     AND d.target_type = 'connection' AND d.requested_by_user_id =
                       :actor
                     AND (CAST(:target AS UUID) IS NULL OR d.target_id = :target)
                     AND ((d.status = 'completed' AND c.status = 'deleted' AND
                       c.deleted_at IS NOT NULL)
                         OR (d.status = 'pending' AND c.status = 'deleting'))"""
            ),
            params,
        )
        row = result.mappings().one_or_none()
        return _receipt(row) if row is not None else None
