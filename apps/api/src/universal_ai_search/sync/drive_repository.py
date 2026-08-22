"""PostgreSQL persistence for bounded Google Drive folder-sync jobs."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row

from universal_ai_search.connections.crypto import EncryptedEnvelope
from universal_ai_search.indexing.repository import _sync_dsn

from .repository import ClaimedSyncJob, GoogleSyncInput


@dataclass(frozen=True)
class ScheduledDriveJob:
    job_id: UUID
    idempotency_key: str
    encrypted_progress: EncryptedEnvelope


class DriveSyncRepository:
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
                "SELECT * FROM app.claim_drive_sync_job(%s::TEXT, %s::INTEGER)",
                (worker_id, lease_seconds),
            ).fetchone()
            if row is None:
                return None
            return ClaimedSyncJob(**row, worker_id=worker_id)

    def load(self, claim: ClaimedSyncJob) -> GoogleSyncInput:
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            self._context(connection, claim.workspace_id)
            row = connection.execute(
                """SELECT provider_connection.credential_ciphertext AS ciphertext,
                    provider_connection.encrypted_data_key,
                    provider_connection.key_version, job.payload,
                    ARRAY(SELECT scope.scope FROM app.connection_scopes AS scope
                          WHERE scope.connection_id = provider_connection.id
                          ORDER BY scope.scope) AS scopes
                FROM app.jobs AS job
                JOIN app.connections AS provider_connection
                  ON provider_connection.id = job.connection_id
                 AND provider_connection.workspace_id = job.workspace_id
                WHERE job.id = %s AND job.status = 'leased'
                  AND job.lease_owner = %s
                  AND job.lease_expires_at > clock_timestamp()
                  AND provider_connection.status = 'active'
                  AND provider_connection.provider = 'google'""",
                (claim.job_id, claim.worker_id),
            ).fetchone()
            if row is None or not all(
                row[key] is not None
                for key in ("ciphertext", "encrypted_data_key", "key_version")
            ):
                raise RuntimeError("claimed Drive sync input is unavailable")
            payload = row["payload"]
            if not isinstance(payload, dict):
                raise RuntimeError("claimed Drive sync payload is invalid")
            return GoogleSyncInput(
                credentials=EncryptedEnvelope(
                    ciphertext=bytes(row["ciphertext"]),
                    encrypted_data_key=bytes(row["encrypted_data_key"]),
                    key_version=int(row["key_version"]),
                ),
                payload=payload,
                scopes=frozenset(str(scope) for scope in row["scopes"]),
                history_id=None,
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
                raise RuntimeError("Drive credentials could not be updated")

    def finish_page(
        self,
        claim: ClaimedSyncJob,
        *,
        sync_run_id: UUID,
        scheduled_jobs: tuple[ScheduledDriveJob, ...],
    ) -> None:
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            self._context(connection, claim.workspace_id)
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (str(sync_run_id),),
            )
            self._lock_claim(connection, claim)
            for job in scheduled_jobs:
                progress = {
                    "ciphertext": base64.b64encode(
                        job.encrypted_progress.ciphertext
                    ).decode(),
                    "encrypted_data_key": base64.b64encode(
                        job.encrypted_progress.encrypted_data_key
                    ).decode(),
                    "key_version": job.encrypted_progress.key_version,
                }
                connection.execute(
                    """INSERT INTO app.jobs (
                        id, workspace_id, connection_id, job_type, queue,
                        idempotency_key, status, payload
                    ) VALUES (%s, %s, %s, 'sync', 'sync', %s, 'pending', %s::JSONB)
                    ON CONFLICT (workspace_id, job_type, idempotency_key) DO NOTHING""",
                    (
                        job.job_id,
                        claim.workspace_id,
                        claim.connection_id,
                        job.idempotency_key,
                        json.dumps(
                            {
                                "drive_progress": progress,
                                "drive_sync_run_id": str(sync_run_id),
                                "mode": "full",
                                "source_families": ["google_drive"],
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
            self._finish_current(connection, claim)
            summary = connection.execute(
                """SELECT
                    count(*) FILTER (WHERE status IN
                        ('pending', 'retry_wait', 'leased')) AS active_count,
                    count(*) FILTER (WHERE status IN
                        ('failed', 'dead_letter', 'cancelled')) AS failure_count
                FROM app.jobs
                WHERE workspace_id = %s AND connection_id = %s
                  AND payload ->> 'drive_sync_run_id' = %s""",
                (claim.workspace_id, claim.connection_id, str(sync_run_id)),
            ).fetchone()
            if (
                summary is None
                or int(summary["active_count"]) != 0
                or int(summary["failure_count"]) != 0
            ):
                return
            reconciliation_job_id = uuid5(sync_run_id, "drive-full-reconciliation")
            connection.execute(
                """INSERT INTO app.jobs (
                    id, workspace_id, connection_id, job_type, queue,
                    idempotency_key, status, payload
                ) VALUES (%s, %s, %s, 'sync', 'sync', %s, 'pending', %s::JSONB)
                ON CONFLICT (workspace_id, job_type, idempotency_key) DO NOTHING""",
                (
                    reconciliation_job_id,
                    claim.workspace_id,
                    claim.connection_id,
                    f"drive-reconcile:{sync_run_id}",
                    json.dumps(
                        {
                            "drive_reconcile": True,
                            "drive_sync_run_id": str(sync_run_id),
                            "mode": "full",
                            "source_families": ["google_drive"],
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )

    def complete_reconciliation(
        self, claim: ClaimedSyncJob, *, sync_run_id: UUID
    ) -> None:
        """Complete a full scan only after authoritative deletion cleanup."""

        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            self._context(connection, claim.workspace_id)
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (str(sync_run_id),),
            )
            self._lock_claim(connection, claim)
            updated = connection.execute(
                """UPDATE app.connections SET
                    last_successful_sync_at = clock_timestamp(),
                    last_error_code = NULL, updated_at = clock_timestamp(),
                    lock_version = lock_version + 1
                WHERE id = %s AND workspace_id = %s AND status = 'active'""",
                (claim.connection_id, claim.workspace_id),
            ).rowcount
            if updated != 1:
                return
            connection.execute(
                """INSERT INTO app.outbox_events (
                    id, workspace_id, aggregate_type, aggregate_id,
                    event_type, payload
                ) VALUES (%s, %s, 'connection', %s,
                    'connection.drive.full_sync_completed', %s::JSONB)
                ON CONFLICT (id) DO NOTHING""",
                (
                    uuid5(sync_run_id, "drive-full-sync-completed"),
                    claim.workspace_id,
                    claim.connection_id,
                    json.dumps({"sync_run_id": str(sync_run_id)}),
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
        retry_delay_seconds: float | None = None,
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
                        THEN clock_timestamp() + make_interval(secs => %s)
                        ELSE available_at END,
                    lease_owner = NULL, lease_expires_at = NULL,
                    completed_at = CASE WHEN %s THEN NULL ELSE clock_timestamp() END,
                    updated_at = clock_timestamp() WHERE id = %s""",
                (
                    status,
                    error_code,
                    can_retry,
                    retry_delay_seconds or 0.0,
                    can_retry,
                    claim.job_id,
                ),
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
            raise RuntimeError("Drive sync lost its lease")

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
