"""Bounded, same-session export that never stores portable archive data."""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from universal_ai_search.api.idempotency import validate_idempotency_key

from .reauth import ReauthenticationService
from .reauth_store import ReauthScope


class ExportMissing(Exception):
    """Export is expired, used, unavailable, unauthorized or nonexistent."""


class ExportUnsupported(Exception):
    """The account or snapshot falls outside supported portability bounds."""


class ExportBusy(Exception):
    """The account already has a current export or its export budget is used."""


@dataclass(frozen=True)
class ExportOperation:
    id: UUID
    created_at: datetime
    completed_at: datetime
    download_expires_at: datetime
    downloadable: bool = False


@dataclass(frozen=True)
class ExportFile:
    content: bytes = field(repr=False)


def _operation(row: RowMapping) -> ExportOperation:
    return ExportOperation(
        row["id"],
        row["created_at"],
        row["completed_at"],
        row["download_expires_at"],
        row.get("downloadable", False),
    )


_SCOPE = """workspace_id = :workspace AND user_id = :actor
            AND session_id = :session AND user_version = :user_version
            AND workspace_version = :workspace_version"""
_SNAPSHOT = """
WITH conversations AS MATERIALIZED (
    SELECT id,title,created_at,updated_at FROM app.conversations
    WHERE workspace_id = :workspace AND user_id = :actor
      AND status = 'active' AND deleted_at IS NULL
    ORDER BY id LIMIT 501 FOR SHARE
), messages AS MATERIALIZED (
    SELECT m.id,m.conversation_id,m.content,m.created_at
    FROM app.messages m JOIN conversations c ON c.id = m.conversation_id
    WHERE m.workspace_id = :workspace AND m.role = 'user' AND m.status = 'complete'
    ORDER BY m.id LIMIT 501 FOR SHARE OF m
), history AS MATERIALIZED (
    SELECT h.id,h.conversation_id,h.query_text,h.mode,h.created_at
    FROM app.search_requests h
    WHERE h.workspace_id = :workspace AND h.user_id = :actor
      AND h.purge_after > statement_timestamp() AND h.purge_after > h.created_at
      AND (h.conversation_id IS NULL OR EXISTS (
          SELECT FROM conversations c WHERE c.id = h.conversation_id))
    ORDER BY h.id LIMIT 501 FOR SHARE
), bounds AS (
    SELECT (SELECT count(*) FROM conversations)
         + (SELECT count(*) FROM messages)
         + (SELECT count(*) FROM history) AS rows,
           (SELECT coalesce(sum(octet_length(title)), 0) FROM conversations)
         + (SELECT coalesce(sum(octet_length(content)), 0) FROM messages)
         + (SELECT coalesce(sum(octet_length(query_text)), 0) FROM history) AS bytes
)
SELECT CASE WHEN bounds.rows <= 500 AND bounds.bytes <= 262144
                     AND octet_length(u.email::TEXT) <= 1024 THEN
    jsonb_build_object(
        'format', 'universal-ai-search/local-account/v1',
        'profile', jsonb_build_object('email',u.email::TEXT,'full_name',u.full_name,
                                      'created_at',u.created_at),
        'workspace', jsonb_build_object('id',w.id,'name',w.name,
                                        'created_at',w.created_at),
        'conversations', COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.id)
                                   FROM conversations c), '[]'::JSONB),
        'user_messages', COALESCE((SELECT jsonb_agg(to_jsonb(m) ORDER BY m.id)
                                   FROM messages m), '[]'::JSONB),
        'search_history', COALESCE((SELECT jsonb_agg(to_jsonb(h) ORDER BY h.id)
                                    FROM history h), '[]'::JSONB)
    )::TEXT
END
FROM app.users u, app.workspaces w, bounds
WHERE u.id = :actor AND w.id = :workspace
"""


class LocalAccountExportStore:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        reauth: ReauthenticationService,
        export_secret: bytes,
    ) -> None:
        if len(export_secret) < 32:
            raise ValueError("account export key must be at least 32 bytes")
        self._engine = engine
        self._reauth = reauth
        self._secret = export_secret

    def _digest(self, domain: bytes, content: bytes) -> bytes:
        return hmac.digest(
            self._secret, b"local-account-export:" + domain + b":" + content, "sha256"
        )

    async def _authorize(
        self, connection: AsyncConnection, scope: ReauthScope
    ) -> dict[str, Any]:
        params = {
            "actor": scope.user_id,
            "workspace": scope.workspace_id,
            "session": scope.session_id,
            "user_version": scope.user_version,
            "workspace_version": scope.workspace_version,
        }
        await connection.execute(
            text(
                """SELECT set_config('app.user_id', :user_setting, true),
                          set_config('app.workspace_id', :workspace_setting, true),
                          set_config('statement_timeout', '5000', true),
                          set_config('lock_timeout', '2000', true),
                          set_config('TimeZone', 'UTC', true)"""
            ),
            {
                "user_setting": str(scope.user_id),
                "workspace_setting": str(scope.workspace_id),
            },
        )
        verdict = await connection.scalar(
            text(
                """SELECT app.authorize_local_account_export
                    (:actor,:workspace,:session,:user_version,:workspace_version)"""
            ),
            params,
        )
        if verdict == "unsupported":
            raise ExportUnsupported
        if verdict != "authorized":
            raise ExportMissing
        return params

    async def _snapshot(
        self, connection: AsyncConnection, params: dict[str, Any]
    ) -> bytes:
        archive = await connection.scalar(text(_SNAPSHOT), params)
        if not isinstance(archive, str):
            raise ExportUnsupported
        encoded = archive.encode()
        if len(encoded) > 2 * 1024 * 1024:
            raise ExportUnsupported
        return encoded

    async def _audit(
        self,
        connection: AsyncConnection,
        scope: ReauthScope,
        request_id: UUID,
        *,
        download: bool,
    ) -> None:
        await connection.execute(
            text(
                """INSERT INTO app.audit_events
                     (id,workspace_id,workspace_ref_hash,actor_user_id,action,
                      target_type,outcome,target_ref_hash,request_id)
                   VALUES (:id,:workspace,:workspace_hash,:actor,:action,'user',
                           'succeeded',:target_hash,:request_id)"""
            ),
            {
                "id": uuid4(),
                "workspace": scope.workspace_id,
                "workspace_hash": self._digest(
                    b"audit-workspace", scope.workspace_id.bytes
                ),
                "actor": scope.user_id,
                "target_hash": self._digest(b"audit-user", scope.user_id.bytes),
                "request_id": request_id,
                "action": "account.export_download" if download else "account.export",
            },
        )

    async def start(
        self, *, scope: ReauthScope, proof: str, idempotency_key: str, request_id: UUID
    ) -> ExportOperation:
        key = self._digest(
            b"idempotency", validate_idempotency_key(idempotency_key).encode()
        )
        lock_id = int.from_bytes(
            self._digest(
                b"request-lock", scope.workspace_id.bytes + scope.user_id.bytes + key
            )[:8],
            "big",
            signed=True,
        )
        async with self._engine.begin() as connection:
            locked = await connection.scalar(
                text("SELECT pg_try_advisory_xact_lock(CAST(:id AS BIGINT))"),
                {"id": lock_id},
            )
            if locked is not True:
                raise ExportBusy
            params = {**await self._authorize(connection, scope), "key": key}
            await connection.execute(
                text(
                    """DELETE FROM app.local_account_exports
                       WHERE workspace_id = :workspace AND user_id = :actor
                         AND created_at <= clock_timestamp() - interval '24 hours'"""
                ),
                params,
            )
            replay = (
                (
                    await connection.execute(
                        text(
                            """SELECT id,created_at,completed_at,download_expires_at,
                                 session_id,user_version,workspace_version
                           FROM app.local_account_exports
                           WHERE workspace_id = :workspace AND user_id = :actor
                             AND request_key_hash = :key"""
                        ),
                        params,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if replay is not None:
                if (
                    replay["session_id"],
                    replay["user_version"],
                    replay["workspace_version"],
                ) != (scope.session_id, scope.user_version, scope.workspace_version):
                    raise ExportMissing
                return _operation(replay)
            await self._reauth.consume(
                connection, scope=scope, purpose="export", token=proof
            )
            if await connection.scalar(
                text(
                    """SELECT count(*) >= 12 OR
                              COALESCE(bool_or(consumed_at IS NULL
                                      AND download_expires_at > clock_timestamp()),
                                       false)
                       FROM app.local_account_exports
                       WHERE workspace_id = :workspace AND user_id = :actor"""
                ),
                params,
            ):
                raise ExportBusy
            snapshot = await self._snapshot(connection, params)
            operation_id = uuid4()
            result = await connection.execute(
                text(
                    """WITH completed AS (SELECT clock_timestamp() AS at)
                       INSERT INTO app.local_account_exports
                           (id,workspace_id,user_id,session_id,user_version,workspace_version,
                            request_key_hash,snapshot_hash,created_at,
                            completed_at,download_expires_at)
                       SELECT :id,:workspace,:actor,:session,
                              :user_version,:workspace_version,
                              :key,:snapshot_hash,transaction_timestamp(),completed.at,
                              completed.at + interval '5 minutes' FROM completed
                       RETURNING id,created_at,completed_at,download_expires_at"""
                ),
                {
                    **params,
                    "id": operation_id,
                    "snapshot_hash": self._digest(
                        b"snapshot", operation_id.bytes + snapshot
                    ),
                },
            )
            record = _operation(result.mappings().one())
            await self._audit(connection, scope, request_id, download=False)
        return record

    async def read(self, *, scope: ReauthScope, operation_id: UUID) -> ExportOperation:
        try:
            async with self._engine.begin() as connection:
                params = {
                    **await self._authorize(connection, scope),
                    "id": operation_id,
                }
                result = await connection.execute(
                    text(
                        f"""SELECT id,created_at,completed_at,download_expires_at,
                                   consumed_at IS NULL AND
                                   download_expires_at > clock_timestamp()
                                      AS downloadable
                            FROM app.local_account_exports WHERE id = :id AND {_SCOPE}
                              AND created_at >
                                  clock_timestamp() - interval '24 hours'"""
                    ),
                    params,
                )
                row = result.mappings().one_or_none()
                if row is None:
                    raise ExportMissing
                return _operation(row)
        except ExportUnsupported:
            raise ExportMissing from None

    async def download(
        self,
        *,
        scope: ReauthScope,
        operation_id: UUID,
        request_id: UUID,
    ) -> ExportFile:
        try:
            async with self._engine.begin() as connection:
                params = {
                    **await self._authorize(connection, scope),
                    "id": operation_id,
                }
                digest = await connection.scalar(
                    text(
                        f"""SELECT snapshot_hash FROM app.local_account_exports
                            WHERE id = :id AND {_SCOPE} AND consumed_at IS NULL
                              AND download_expires_at > clock_timestamp() FOR UPDATE"""
                    ),
                    params,
                )
                if digest is None:
                    raise ExportMissing
                snapshot = await self._snapshot(connection, params)
                if not hmac.compare_digest(
                    self._digest(b"snapshot", operation_id.bytes + snapshot), digest
                ):
                    raise ExportMissing
                consumed = await connection.scalar(
                    text(
                        f"""UPDATE app.local_account_exports
                            SET consumed_at = clock_timestamp()
                            WHERE id = :id AND {_SCOPE} AND consumed_at IS NULL
                              AND download_expires_at > clock_timestamp()
                            RETURNING true"""
                    ),
                    params,
                )
                if consumed is not True:
                    raise ExportMissing
                await self._audit(connection, scope, request_id, download=True)
            return ExportFile(snapshot)
        except ExportUnsupported:
            raise ExportMissing from None
