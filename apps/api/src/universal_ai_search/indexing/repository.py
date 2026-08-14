"""PostgreSQL queue and index persistence under the worker RLS role."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row
from uas_connector_sdk import NormalizedDocument

from .pipeline import (
    CHUNKER_VERSION,
    EMBEDDING_MODEL,
    PARSER_VERSION,
    PendingDocument,
    PreparedDocument,
    document_version_id,
    version_key,
)


class EnqueueStatus(StrEnum):
    QUEUED = "queued"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class EnqueueResult:
    status: EnqueueStatus
    source_id: UUID
    document_version_id: UUID
    job_id: UUID | None


@dataclass(frozen=True)
class ClaimedJob:
    job_id: UUID
    workspace_id: UUID
    source_id: UUID
    document_version_id: UUID
    attempt_number: int
    worker_id: str


def _sync_dsn(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgresql+psycopg://", "postgresql://"
    )


def _source_type(value: str) -> str:
    aliases = {"repository_file": "file", "document": "file"}
    result = aliases.get(value, value)
    allowed = {
        "email",
        "attachment",
        "file",
        "issue",
        "pull_request",
        "review",
        "commit",
        "code",
    }
    if result not in allowed:
        raise ValueError("unsupported source type")
    return result


class IndexRepository:
    """Own indexing transactions; callers never supply a claim workspace."""

    def __init__(self, database_url: str) -> None:
        self._database_url = _sync_dsn(database_url)

    @staticmethod
    def _set_worker_context(
        connection: psycopg.Connection[dict[str, object]],
        workspace_id: UUID | None = None,
    ) -> None:
        connection.execute("SET LOCAL ROLE app_worker")
        if workspace_id is not None:
            connection.execute(
                "SELECT set_config('app.workspace_id', %s, true)",
                (str(workspace_id),),
            )

    def enqueue(
        self, workspace_id: UUID, connection_id: UUID, document: NormalizedDocument
    ) -> EnqueueResult:
        content_hash = bytes.fromhex(document.content_hash)
        permissions_hash = bytes.fromhex(document.permissions_hash)
        source_id = uuid5(connection_id, document.external_id)
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            self._set_worker_context(connection, workspace_id)
            account = connection.execute(
                "SELECT provider, status FROM app.connections "
                "WHERE workspace_id = %s AND id = %s FOR UPDATE",
                (workspace_id, connection_id),
            ).fetchone()
            provider = document.provider.value
            if account is None or account["status"] != "active":
                raise ValueError("connection is not active")
            if account["provider"] != provider:
                raise ValueError("connector provider does not match connection")

            profile = connection.execute(
                "SELECT id FROM app.embedding_profiles WHERE status = 'active' "
                "AND provider = 'local' AND model = %s",
                (EMBEDDING_MODEL,),
            ).fetchone()
            if profile is None:
                raise RuntimeError("no active embedding profile")
            profile_id = int(profile["id"])
            key = version_key(
                source_id=source_id,
                content_hash=content_hash,
                permissions_hash=permissions_hash,
                profile_id=profile_id,
            )
            version_id = document_version_id(source_id, key)

            current = connection.execute(
                "SELECT version.id FROM app.sources AS source "
                "JOIN app.document_versions AS version "
                "ON version.id = source.current_document_version_id "
                "WHERE source.id = %s AND version.version_key = %s "
                "AND version.state = 'ready'",
                (source_id, key),
            ).fetchone()
            if current is not None:
                return EnqueueResult(
                    EnqueueStatus.UNCHANGED, source_id, current["id"], None
                )

            timestamp = document.modified_at or document.created_at
            timestamp_kind = (
                "modified" if document.modified_at else "created" if timestamp else None
            )
            extension = PurePosixPath(document.external_id).suffix.lstrip(".") or None
            authors = ", ".join(document.authors)[:500] or None
            connection.execute(
                """INSERT INTO app.sources (
                    id, workspace_id, connection_id, provider, external_id,
                    source_type, title, mime_type, file_extension, canonical_url,
                    author_display, source_timestamp, source_timestamp_kind,
                    content_hash, permissions_hash, state, metadata
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, 'active', %s::JSONB
                ) ON CONFLICT (connection_id, external_id) DO UPDATE SET
                    title = EXCLUDED.title, mime_type = EXCLUDED.mime_type,
                    file_extension = EXCLUDED.file_extension,
                    canonical_url = EXCLUDED.canonical_url,
                    author_display = EXCLUDED.author_display,
                    source_timestamp = EXCLUDED.source_timestamp,
                    source_timestamp_kind = EXCLUDED.source_timestamp_kind,
                    content_hash = EXCLUDED.content_hash,
                    permissions_hash = EXCLUDED.permissions_hash,
                    state = 'active', metadata = EXCLUDED.metadata,
                    updated_at = clock_timestamp(),
                    lock_version = app.sources.lock_version + 1
                """,
                (
                    source_id,
                    workspace_id,
                    connection_id,
                    provider,
                    document.external_id,
                    _source_type(document.source_type),
                    document.title,
                    document.mime_type,
                    extension,
                    document.canonical_url,
                    authors,
                    timestamp,
                    timestamp_kind,
                    content_hash,
                    permissions_hash,
                    json.dumps(document.provider_metadata),
                ),
            )
            connection.execute(
                """INSERT INTO app.document_versions (
                    id, workspace_id, source_id, version_key, state,
                    normalized_text, language, parser_version, chunker_version,
                    content_hash, permissions_hash, token_count, extracted_bytes
                ) VALUES (
                    %s, %s, %s, %s, 'pending', %s, 'und', %s, %s,
                    %s, %s, 0, 0
                ) ON CONFLICT (source_id, version_key) DO NOTHING""",
                (
                    version_id,
                    workspace_id,
                    source_id,
                    key,
                    document.content,
                    PARSER_VERSION,
                    CHUNKER_VERSION,
                    content_hash,
                    permissions_hash,
                ),
            )
            job_id = uuid5(version_id, "index-job")
            payload = json.dumps(
                {
                    "document_version_id": str(version_id),
                    "embedding_profile_id": profile_id,
                    "embedding_model": EMBEDDING_MODEL,
                }
            )
            connection.execute(
                """INSERT INTO app.jobs (
                    id, workspace_id, connection_id, source_id, job_type, queue,
                    idempotency_key, status, payload
                ) VALUES (
                    %s, %s, %s, %s, 'index', 'index', %s, 'pending', %s::JSONB
                ) ON CONFLICT (workspace_id, job_type, idempotency_key) DO NOTHING""",
                (
                    job_id,
                    workspace_id,
                    connection_id,
                    source_id,
                    f"index:{key.hex()}",
                    payload,
                ),
            )
            return EnqueueResult(EnqueueStatus.QUEUED, source_id, version_id, job_id)

    def claim(self, worker_id: str, lease_seconds: int = 120) -> ClaimedJob | None:
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            self._set_worker_context(connection)
            row = connection.execute(
                "SELECT * FROM app.claim_index_job(%s::TEXT, %s::INTEGER)",
                (worker_id, lease_seconds),
            ).fetchone()
            if row is None:
                return None
            return ClaimedJob(**row, worker_id=worker_id)

    def load_pending(self, claim: ClaimedJob) -> PendingDocument:
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            self._set_worker_context(connection, claim.workspace_id)
            row = connection.execute(
                """SELECT version.workspace_id, version.source_id,
                    version.id AS document_version_id, source.title,
                    version.normalized_text AS content, source.mime_type,
                    version.content_hash, version.permissions_hash,
                    (job.payload ->> 'embedding_profile_id')::INTEGER
                        AS embedding_profile_id
                FROM app.jobs AS job
                JOIN app.document_versions AS version
                  ON version.workspace_id = job.workspace_id
                 AND version.id = (job.payload ->> 'document_version_id')::UUID
                JOIN app.sources AS source ON source.id = version.source_id
                WHERE job.id = %s AND job.status = 'leased'
                  AND job.lease_owner = %s AND version.state = 'pending'""",
                (claim.job_id, claim.worker_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("claimed indexing input is unavailable")
            return PendingDocument(**row)

    def promote(self, claim: ClaimedJob, prepared: PreparedDocument) -> None:
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            self._set_worker_context(connection, claim.workspace_id)
            locked = connection.execute(
                """SELECT source.current_document_version_id, old.extracted_bytes
                FROM app.jobs AS job
                JOIN app.document_versions AS version
                  ON version.id = %s AND version.workspace_id = job.workspace_id
                JOIN app.sources AS source
                  ON source.id = version.source_id
                 AND source.content_hash = version.content_hash
                 AND source.permissions_hash = version.permissions_hash
                LEFT JOIN app.document_versions AS old
                  ON old.id = source.current_document_version_id
                WHERE job.id = %s AND job.status = 'leased'
                  AND job.lease_owner = %s
                  AND job.lease_expires_at > clock_timestamp()
                  AND version.state = 'pending'
                FOR UPDATE OF job, version, source""",
                (claim.document_version_id, claim.job_id, claim.worker_id),
            ).fetchone()
            if locked is None:
                raise RuntimeError("index promotion lost its lease or became stale")

            profile_id = self._profile_id(connection, claim.document_version_id)
            for chunk in prepared.chunks:
                connection.execute(
                    """INSERT INTO app.chunks (
                        id, workspace_id, document_version_id, chunk_index,
                        chunk_hash, heading_path, content, search_config,
                        token_count, start_offset, end_offset, metadata
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s::REGCONFIG,
                        %s, %s, %s, %s::JSONB
                    )""",
                    (
                        chunk.id,
                        claim.workspace_id,
                        claim.document_version_id,
                        chunk.index,
                        chunk.content_hash,
                        list(chunk.heading_path),
                        chunk.content,
                        chunk.search_config,
                        chunk.token_count,
                        chunk.start_offset,
                        chunk.end_offset,
                        json.dumps({"simhash64": f"{chunk.simhash:016x}"}),
                    ),
                )
                vector = (
                    "[" + ",".join(f"{value:.9g}" for value in chunk.embedding) + "]"
                )
                connection.execute(
                    """INSERT INTO app.chunk_embeddings (
                        workspace_id, chunk_id, embedding_profile_id, embedding
                    ) VALUES (%s, %s, %s, %s::VECTOR)""",
                    (
                        claim.workspace_id,
                        chunk.id,
                        profile_id,
                        vector,
                    ),
                )

            connection.execute(
                """UPDATE app.document_versions SET
                    state = 'ready', normalized_text = %s, language = %s,
                    token_count = %s, extracted_bytes = %s,
                    failure_code = NULL, ready_at = clock_timestamp()
                WHERE id = %s""",
                (
                    prepared.normalized_text,
                    prepared.language,
                    prepared.token_count,
                    prepared.extracted_bytes,
                    claim.document_version_id,
                ),
            )
            old_id = locked["current_document_version_id"]
            connection.execute(
                "UPDATE app.sources SET current_document_version_id = %s, "
                "updated_at = clock_timestamp(), lock_version = lock_version + 1 "
                "WHERE id = %s",
                (claim.document_version_id, claim.source_id),
            )
            if old_id is not None and old_id != claim.document_version_id:
                connection.execute(
                    "UPDATE app.document_versions SET state = 'superseded', "
                    "superseded_at = clock_timestamp() WHERE id = %s "
                    "AND state = 'ready'",
                    (old_id,),
                )
            old_bytes = int(locked["extracted_bytes"] or 0)
            source_delta = 1 if old_id is None else 0
            connection.execute(
                """UPDATE app.workspace_usage SET
                    indexed_source_count = indexed_source_count + %s,
                    extracted_bytes = extracted_bytes + %s,
                    updated_at = clock_timestamp(), lock_version = lock_version + 1
                WHERE workspace_id = %s""",
                (
                    source_delta,
                    prepared.extracted_bytes - old_bytes,
                    claim.workspace_id,
                ),
            )
            connection.execute(
                "UPDATE app.workspaces SET search_index_generation = "
                "search_index_generation + 1, updated_at = clock_timestamp(), "
                "lock_version = lock_version + 1 WHERE id = %s",
                (claim.workspace_id,),
            )
            connection.execute(
                """INSERT INTO app.outbox_events (
                    id, workspace_id, aggregate_type, aggregate_id, event_type, payload
                ) VALUES (%s, %s, 'source', %s, 'index.version_ready', %s::JSONB)""",
                (
                    uuid5(claim.job_id, "ready-event"),
                    claim.workspace_id,
                    claim.source_id,
                    json.dumps(
                        {
                            "source_id": str(claim.source_id),
                            "document_version_id": str(claim.document_version_id),
                        }
                    ),
                ),
            )
            self._finish_attempt(connection, claim, "succeeded", None)
            connection.execute(
                """UPDATE app.jobs SET status = 'completed', lease_owner = NULL,
                    lease_expires_at = NULL, completed_at = clock_timestamp(),
                    updated_at = clock_timestamp() WHERE id = %s""",
                (claim.job_id,),
            )

    @staticmethod
    def _profile_id(
        connection: psycopg.Connection[dict[str, object]], version_id: UUID
    ) -> int:
        row = connection.execute(
            "SELECT (payload ->> 'embedding_profile_id')::INTEGER AS id "
            "FROM app.jobs WHERE payload ->> 'document_version_id' = %s",
            (str(version_id),),
        ).fetchone()
        if row is None:
            raise RuntimeError("embedding profile is unavailable")
        profile_id = row["id"]
        if not isinstance(profile_id, int):
            raise RuntimeError("embedding profile is invalid")
        return profile_id

    @staticmethod
    def _finish_attempt(
        connection: psycopg.Connection[dict[str, object]],
        claim: ClaimedJob,
        status: str,
        error_code: str | None,
    ) -> None:
        connection.execute(
            """UPDATE app.job_attempts SET status = %s, error_code = %s,
                finished_at = clock_timestamp()
            WHERE job_id = %s AND attempt_number = %s AND status = 'running'""",
            (status, error_code, claim.job_id, claim.attempt_number),
        )

    def fail(self, claim: ClaimedJob, error_code: str, retryable: bool) -> None:
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            self._set_worker_context(connection, claim.workspace_id)
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
            self._finish_attempt(connection, claim, attempt_status, error_code)
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
            if not can_retry:
                connection.execute(
                    "UPDATE app.document_versions SET state = 'failed', "
                    "failure_code = %s "
                    "WHERE id = %s AND state = 'pending'",
                    (error_code, claim.document_version_id),
                )
