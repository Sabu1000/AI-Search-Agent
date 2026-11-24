"""Durable, repository-scoped GitHub sync and authoritative reconciliation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row
from uas_connector_sdk import NormalizedDocument

from universal_ai_search.indexing.repository import IndexRepository, _sync_dsn

from .repository import ClaimedSyncJob, GoogleSyncRepository

_MAX_DOCUMENTS = 1000
_MAX_TEXT_BYTES = 16 * 1024 * 1024


class GitHubSyncClaimError(Exception):
    """Local job or durable authority became unavailable."""


@dataclass(frozen=True)
class GitHubSyncTarget:
    collection_id: UUID
    installation_id: int
    repository_id: int
    authorization_generation: int = 1


class GitHubSyncRepository:
    def __init__(self, database_url: str) -> None:
        self._dsn = _sync_dsn(database_url)

    def claim(self, worker_id: str) -> ClaimedSyncJob | None:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            IndexRepository._set_worker_context(connection)
            row = connection.execute(
                "SELECT * FROM app.claim_github_sync_job(%s::TEXT, 180)",
                (worker_id,),
            ).fetchone()
        return ClaimedSyncJob(**row, worker_id=worker_id) if row else None

    @staticmethod
    def _target(
        connection: psycopg.Connection[dict[str, object]], claim: ClaimedSyncJob
    ) -> GitHubSyncTarget:
        row = connection.execute(
            """SELECT collection.id, collection.provider_external_id AS repository_id,
                      account.github_installation_id AS installation_id,
                      collection.github_authorization_generation AS generation
            FROM app.jobs AS job
            JOIN app.connections AS account ON account.workspace_id = job.workspace_id
                AND account.id = job.connection_id
            JOIN app.source_collections AS collection
                ON collection.workspace_id = account.workspace_id
                AND collection.connection_id = account.id
                AND collection.id::TEXT = job.payload ->> 'collection_id'
            WHERE job.id = %s AND job.workspace_id = %s AND job.connection_id = %s
                AND job.status = 'leased' AND job.lease_owner = %s
                AND job.attempt_count = %s AND job.lease_expires_at > clock_timestamp()
                AND account.provider = 'github' AND account.github_installation_id > 0
                AND account.status IN ('pending', 'active')
                AND collection.kind = 'repository' AND collection.selected
                AND collection.status = 'active'
            FOR UPDATE OF job, account, collection""",
            (
                claim.job_id,
                claim.workspace_id,
                claim.connection_id,
                claim.worker_id,
                claim.attempt_number,
            ),
        ).fetchone()
        if row is None:
            raise GitHubSyncClaimError
        return GitHubSyncTarget(
            collection_id=cast(UUID, row["id"]),
            installation_id=cast(int, row["installation_id"]),
            repository_id=int(cast(str, row["repository_id"])),
            authorization_generation=cast(int, row["generation"]),
        )

    def load(self, claim: ClaimedSyncJob) -> GitHubSyncTarget:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            IndexRepository._set_worker_context(connection, claim.workspace_id)
            return self._target(connection, claim)

    def commit(
        self,
        claim: ClaimedSyncJob,
        target: GitHubSyncTarget,
        documents: tuple[NormalizedDocument, ...],
    ) -> None:
        external_ids = {doc.external_id for doc in documents}
        if (
            len(documents) > _MAX_DOCUMENTS
            or len(external_ids) != len(documents)
            or sum(len(doc.content.encode()) for doc in documents) > _MAX_TEXT_BYTES
            or any(
                doc.provider.value != "github"
                or str(doc.provider_metadata.get("repository_id"))
                != str(target.repository_id)
                for doc in documents
            )
        ):
            raise GitHubSyncClaimError
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            IndexRepository._set_worker_context(connection, claim.workspace_id)
            if self._target(connection, claim) != target:
                raise GitHubSyncClaimError
            # The first successful scan activates the connection. Repository
            # visibility is independently fenced by verified_at and membership.
            connection.execute(
                """UPDATE app.connections SET status = 'active',
                     last_successful_sync_at = clock_timestamp(),
                     last_error_code = NULL, updated_at = clock_timestamp(),
                     lock_version = lock_version + 1 WHERE id = %s""",
                (claim.connection_id,),
            )
            for doc in documents:
                enqueued = IndexRepository.enqueue_in_transaction(
                    connection, claim.workspace_id, claim.connection_id, doc
                )
                connection.execute(
                    """INSERT INTO app.source_collection_memberships
                       (workspace_id, connection_id, source_id, collection_id,
                        relationship) VALUES (%s, %s, %s, %s, 'direct')
                       ON CONFLICT DO NOTHING""",
                    (
                        claim.workspace_id,
                        claim.connection_id,
                        enqueued.source_id,
                        target.collection_id,
                    ),
                )
            self._purge_absent(connection, claim, target, external_ids)
            connection.execute(
                """UPDATE app.source_collections
                   SET github_verified_at = clock_timestamp(),
                       updated_at = clock_timestamp() WHERE id = %s""",
                (target.collection_id,),
            )
            self._schedule_next(connection, claim, target)
            GoogleSyncRepository._finish_current(connection, claim)

    def fail(self, claim: ClaimedSyncJob, *, definitely_removed: bool = False) -> None:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            IndexRepository._set_worker_context(connection, claim.workspace_id)
            try:
                target = self._target(connection, claim)
            except GitHubSyncClaimError:
                return
            # All failures close the repository's visibility gate. Only an
            # independently authoritative removal may purge retained content.
            connection.execute(
                """UPDATE app.source_collections SET github_verified_at = NULL,
                      status = CASE WHEN %s THEN 'removed' ELSE status END,
                      selected = CASE WHEN %s THEN false ELSE selected END,
                      updated_at = clock_timestamp() WHERE id = %s""",
                (definitely_removed, definitely_removed, target.collection_id),
            )
            if definitely_removed:
                self._purge_absent(connection, claim, target, set())
            else:
                self._schedule_next(connection, claim, target)
            connection.execute(
                """UPDATE app.job_attempts SET status = 'permanent_failure',
                      error_code = 'GITHUB_SYNC_NOT_VERIFIED',
                      finished_at = clock_timestamp()
                   WHERE job_id = %s AND attempt_number = %s AND status = 'running'""",
                (claim.job_id, claim.attempt_number),
            )
            connection.execute(
                """UPDATE app.jobs SET status = 'failed',
                      error_code = 'GITHUB_SYNC_NOT_VERIFIED',
                      lease_owner = NULL, lease_expires_at = NULL,
                      completed_at = clock_timestamp(), updated_at = clock_timestamp()
                   WHERE id = %s""",
                (claim.job_id,),
            )

    @staticmethod
    def _purge_absent(
        connection: psycopg.Connection[dict[str, object]],
        claim: ClaimedSyncJob,
        target: GitHubSyncTarget,
        external_ids: set[str],
    ) -> None:
        rows = connection.execute(
            """SELECT s.id, s.state, s.lock_version, s.current_document_version_id,
                      v.extracted_bytes
            FROM app.sources s
            JOIN app.source_collection_memberships m
              ON m.workspace_id = s.workspace_id AND m.connection_id = s.connection_id
             AND m.source_id = s.id
            LEFT JOIN app.document_versions v ON v.id = s.current_document_version_id
            WHERE s.workspace_id = %s AND s.connection_id = %s AND s.provider = 'github'
              AND m.collection_id = %s AND s.state = 'active'
              AND NOT (s.external_id = ANY(%s::TEXT[]))
            FOR UPDATE OF s""",
            (
                claim.workspace_id,
                claim.connection_id,
                target.collection_id,
                sorted(external_ids),
            ),
        ).fetchall()
        IndexRepository._purge_sources(connection, claim.workspace_id, rows)

    @staticmethod
    def _schedule_next(
        connection: psycopg.Connection[dict[str, object]],
        claim: ClaimedSyncJob,
        target: GitHubSyncTarget,
    ) -> None:
        connection.execute(
            """INSERT INTO app.jobs
               (id, workspace_id, connection_id, job_type, queue,
                idempotency_key, status, available_at, payload)
               VALUES (%s, %s, %s, 'sync', 'sync', %s, 'pending',
                   clock_timestamp() + INTERVAL '5 minutes', %s::JSONB)
               ON CONFLICT (workspace_id, job_type, idempotency_key) DO NOTHING""",
            (
                uuid5(claim.job_id, "github-reconcile"),
                claim.workspace_id,
                claim.connection_id,
                f"github-reconcile:{claim.job_id}",
                json.dumps(
                    {
                        "mode": "github_repository",
                        "collection_id": str(target.collection_id),
                    }
                ),
            ),
        )
