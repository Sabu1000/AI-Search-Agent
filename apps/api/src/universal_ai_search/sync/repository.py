"""PostgreSQL persistence for bounded provider sync jobs."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row

from universal_ai_search.connections.crypto import EncryptedEnvelope
from universal_ai_search.indexing.repository import _sync_dsn


@dataclass(frozen=True)
class ClaimedSyncJob:
    job_id: UUID
    workspace_id: UUID
    connection_id: UUID
    attempt_number: int
    worker_id: str


@dataclass(frozen=True)
class GoogleSyncInput:
    credentials: EncryptedEnvelope
    payload: dict[str, object]
    scopes: frozenset[str]


class GoogleSyncRepository:
    def __init__(self, database_url: str) -> None:
        self._database_url = _sync_dsn(database_url)

    @staticmethod
    def _context(
        connection: psycopg.Connection[dict[str, object]],
        workspace_id: UUID | None = None,
    ) -> None:
        connection.execute("SET LOCAL ROLE app_worker")
        if workspace_id is not None:
            connection.execute(
                "SELECT set_config('app.workspace_id', %s, true)",
                (str(workspace_id),),
            )

    def claim(self, worker_id: str, lease_seconds: int = 120) -> ClaimedSyncJob | None:
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            self._context(connection)
            row = connection.execute(
                "SELECT * FROM app.claim_gmail_sync_job(%s::TEXT, %s::INTEGER)",
                (worker_id, lease_seconds),
            ).fetchone()
            if row is None:
                return None
            return ClaimedSyncJob(**row, worker_id=worker_id)

    def load(self, claim: ClaimedSyncJob) -> GoogleSyncInput:
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            self._context(connection, claim.workspace_id)
            row = connection.execute(
                """SELECT connection.credential_ciphertext AS ciphertext,
                    connection.encrypted_data_key, connection.key_version,
                    job.payload,
                    ARRAY(SELECT scope.scope FROM app.connection_scopes AS scope
                          WHERE scope.connection_id = connection.id
                          ORDER BY scope.scope) AS scopes
                FROM app.jobs AS job
                JOIN app.connections AS connection
                  ON connection.id = job.connection_id
                 AND connection.workspace_id = job.workspace_id
                WHERE job.id = %s AND job.status = 'leased'
                  AND job.lease_owner = %s AND job.lease_expires_at > clock_timestamp()
                  AND connection.status = 'active'
                  AND connection.provider = 'google'""",
                (claim.job_id, claim.worker_id),
            ).fetchone()
            if row is None or not all(
                row[key] is not None
                for key in ("ciphertext", "encrypted_data_key", "key_version")
            ):
                raise RuntimeError("claimed Gmail sync input is unavailable")
            payload = row["payload"]
            if not isinstance(payload, dict):
                raise RuntimeError("claimed Gmail sync payload is invalid")
            return GoogleSyncInput(
                credentials=EncryptedEnvelope(
                    ciphertext=bytes(row["ciphertext"]),
                    encrypted_data_key=bytes(row["encrypted_data_key"]),
                    key_version=int(row["key_version"]),
                ),
                payload=payload,
                scopes=frozenset(str(scope) for scope in row["scopes"]),
            )

    def save_credentials(
        self, claim: ClaimedSyncJob, credentials: EncryptedEnvelope
    ) -> None:
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            self._context(connection, claim.workspace_id)
            updated = connection.execute(
                """UPDATE app.connections SET credential_ciphertext = %s,
                    encrypted_data_key = %s, key_version = %s,
                    updated_at = clock_timestamp(), lock_version = lock_version + 1
                WHERE id = %s AND workspace_id = %s AND status = 'active'""",
                (
                    credentials.ciphertext,
                    credentials.encrypted_data_key,
                    credentials.key_version,
                    claim.connection_id,
                    claim.workspace_id,
                ),
            ).rowcount
            if updated != 1:
                raise RuntimeError("Gmail credentials could not be updated")

    def advance(
        self,
        claim: ClaimedSyncJob,
        *,
        next_job_id: UUID,
        token_fingerprint: str,
        encrypted_progress: EncryptedEnvelope,
    ) -> None:
        payload = json.dumps(
            {
                "gmail_progress": {
                    "ciphertext": base64.b64encode(
                        encrypted_progress.ciphertext
                    ).decode(),
                    "encrypted_data_key": base64.b64encode(
                        encrypted_progress.encrypted_data_key
                    ).decode(),
                    "key_version": encrypted_progress.key_version,
                },
                "mode": "full",
                "source_families": ["gmail"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            self._context(connection, claim.workspace_id)
            self._lock_claim(connection, claim)
            connection.execute(
                """INSERT INTO app.jobs (
                    id, workspace_id, connection_id, job_type, queue,
                    idempotency_key, status, payload
                ) VALUES (%s, %s, %s, 'sync', 'sync', %s, 'pending', %s::JSONB)
                ON CONFLICT (workspace_id, job_type, idempotency_key) DO NOTHING""",
                (
                    next_job_id,
                    claim.workspace_id,
                    claim.connection_id,
                    f"gmail-full-page:{claim.connection_id}:{token_fingerprint}",
                    payload,
                ),
            )
            self._finish_current(connection, claim)

    def complete(self, claim: ClaimedSyncJob, *, history_id: str) -> None:
        cursor = json.dumps({"history_id": history_id})
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            self._context(connection, claim.workspace_id)
            self._lock_claim(connection, claim)
            connection.execute(
                """INSERT INTO app.connection_cursors (
                    id, workspace_id, connection_id, stream, cursor, committed_at
                ) VALUES (%s, %s, %s, 'gmail', %s::JSONB, clock_timestamp())
                ON CONFLICT (connection_id, stream) DO UPDATE SET
                    cursor = EXCLUDED.cursor,
                    cursor_version = app.connection_cursors.cursor_version + 1,
                    committed_at = EXCLUDED.committed_at,
                    updated_at = clock_timestamp()""",
                (
                    uuid5(claim.connection_id, "gmail-cursor"),
                    claim.workspace_id,
                    claim.connection_id,
                    cursor,
                ),
            )
            connection.execute(
                """UPDATE app.connections SET
                    last_successful_sync_at = clock_timestamp(), last_error_code = NULL,
                    updated_at = clock_timestamp(), lock_version = lock_version + 1
                WHERE id = %s""",
                (claim.connection_id,),
            )
            connection.execute(
                """INSERT INTO app.outbox_events (
                    id, workspace_id, aggregate_type, aggregate_id, event_type, payload
                ) VALUES (%s, %s, 'connection', %s,
                    'connection.gmail.full_sync_completed', %s::JSONB)
                ON CONFLICT (id) DO NOTHING""",
                (
                    uuid5(claim.job_id, "gmail-sync-completed"),
                    claim.workspace_id,
                    claim.connection_id,
                    json.dumps({"job_id": str(claim.job_id)}),
                ),
            )
            self._finish_current(connection, claim)

    def fail(
        self,
        claim: ClaimedSyncJob,
        *,
        error_code: str,
        retryable: bool,
        reauthorization_required: bool = False,
    ) -> None:
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            self._context(connection, claim.workspace_id)
            job = connection.execute(
                "SELECT attempt_count, max_attempts FROM app.jobs WHERE id = %s "
                "AND status = 'leased' AND lease_owner = %s FOR UPDATE",
                (claim.job_id, claim.worker_id),
            ).fetchone()
            if job is None:
                return
            can_retry = retryable and int(job["attempt_count"]) < int(
                job["max_attempts"]
            )
            status = (
                "retry_wait" if can_retry else "dead_letter" if retryable else "failed"
            )
            attempt_status = "retryable_failure" if can_retry else "permanent_failure"
            connection.execute(
                """UPDATE app.job_attempts SET status = %s, error_code = %s,
                    finished_at = clock_timestamp()
                WHERE job_id = %s AND attempt_number = %s AND status = 'running'""",
                (attempt_status, error_code, claim.job_id, claim.attempt_number),
            )
            connection.execute(
                """UPDATE app.jobs SET status = %s, error_code = %s,
                    available_at = CASE WHEN %s
                        THEN clock_timestamp() + INTERVAL '30 seconds'
                        ELSE available_at END,
                    lease_owner = NULL, lease_expires_at = NULL,
                    completed_at = CASE WHEN %s THEN NULL ELSE clock_timestamp() END,
                    updated_at = clock_timestamp() WHERE id = %s""",
                (status, error_code, can_retry, can_retry, claim.job_id),
            )
            connection.execute(
                """UPDATE app.connections SET status = CASE WHEN %s
                        THEN 'reauthorization_required' ELSE status END,
                    last_error_code = %s, updated_at = clock_timestamp(),
                    lock_version = lock_version + 1 WHERE id = %s""",
                (reauthorization_required, error_code, claim.connection_id),
            )

    @staticmethod
    def _lock_claim(
        connection: psycopg.Connection[dict[str, object]], claim: ClaimedSyncJob
    ) -> None:
        row = connection.execute(
            "SELECT id FROM app.jobs WHERE id = %s AND status = 'leased' "
            "AND lease_owner = %s AND lease_expires_at > clock_timestamp() FOR UPDATE",
            (claim.job_id, claim.worker_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("Gmail sync lost its lease")

    @staticmethod
    def _finish_current(
        connection: psycopg.Connection[dict[str, object]], claim: ClaimedSyncJob
    ) -> None:
        connection.execute(
            """UPDATE app.job_attempts SET status = 'succeeded',
                finished_at = clock_timestamp()
            WHERE job_id = %s AND attempt_number = %s AND status = 'running'""",
            (claim.job_id, claim.attempt_number),
        )
        connection.execute(
            """UPDATE app.jobs SET status = 'completed', lease_owner = NULL,
                lease_expires_at = NULL, completed_at = clock_timestamp(),
                updated_at = clock_timestamp() WHERE id = %s""",
            (claim.job_id,),
        )
