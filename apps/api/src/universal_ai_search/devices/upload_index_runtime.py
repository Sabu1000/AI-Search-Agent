"""Worker bridge from immutable staging receipt to one real index operation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row
from uas_connector_sdk import NormalizedDocument

from universal_ai_search.indexing.repository import (
    EnqueueStatus,
    IndexRepository,
    _sync_dsn,
)

from .upload_index import (
    S3PinnedGetClient,
    S3PinnedNativeTextReader,
    UploadIndexError,
    UploadIndexTarget,
)


@dataclass(frozen=True)
class UploadIndexClaim:
    operation_id: UUID
    workspace_id: UUID
    attempt: int
    worker_id: str


class NativeUploadIndexRepository:
    def __init__(self, database_url: str) -> None:
        self._dsn = _sync_dsn(database_url)

    def claim(self, worker_id: str) -> UploadIndexClaim | None:
        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            self._context(conn)
            row = conn.execute(
                "SELECT * FROM app.claim_verified_device_upload(%s::text)", (worker_id,)
            ).fetchone()
        return UploadIndexClaim(**row, worker_id=worker_id) if row else None

    @staticmethod
    def _context(
        conn: psycopg.Connection[dict[str, object]], workspace_id: UUID | None = None
    ) -> None:
        IndexRepository._set_worker_context(conn, workspace_id)
        conn.execute("SET LOCAL lock_timeout = '500ms'")
        conn.execute("SET LOCAL statement_timeout = '4s'")

    @staticmethod
    def _target(
        conn: psycopg.Connection[dict[str, object]],
        claim: UploadIndexClaim,
        *,
        publishing: bool,
    ) -> UploadIndexTarget:
        row = conn.execute(
            """SELECT * FROM app.load_verified_device_upload(
               %s::uuid,%s::uuid,%s::text,%s::integer,%s::boolean)""",
            (
                claim.operation_id,
                claim.workspace_id,
                claim.worker_id,
                claim.attempt,
                publishing,
            ),
        ).fetchone()
        if row is None:
            raise UploadIndexError("UPLOAD_INDEX_DENIED")
        return UploadIndexTarget(
            upload_id=cast(UUID, row["upload_id"]),
            device_id=cast(UUID, row["device_id"]),
            folder_id=cast(UUID, row["folder_id"]),
            connection_id=cast(UUID, row["connection_id"]),
            owner_id=cast(UUID, row["owner_id"]),
            external_id=cast(str, row["external_id"]),
            object_key=cast(str, row["object_key"]),
            immutable_version=cast(str, row["immutable_version"]),
            size_bytes=cast(int, row["size_bytes"]),
            content_sha256=cast(str, row["content_sha256"]),
            mime_type=cast(str, row["mime_type"]),
            manifest_sequence=cast(int, row["manifest_sequence"]),
        )

    def load(self, claim: UploadIndexClaim) -> UploadIndexTarget:
        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            self._context(conn, claim.workspace_id)
            return self._target(conn, claim, publishing=False)

    def publish(
        self,
        claim: UploadIndexClaim,
        target: UploadIndexTarget,
        item: NormalizedDocument,
    ) -> None:
        if (
            item.external_id != target.source_external_id
            or item.mime_type != target.mime_type
            or item.canonical_url is not None
            or item.access_metadata.public
            or item.access_metadata.user_ids != (str(target.owner_id),)
            or item.access_metadata.group_ids
            or item.access_metadata.domain_ids
            or item.provider_metadata
            != {
                "local_upload_id": str(target.upload_id),
                "local_device_id": str(target.device_id),
                "local_folder_id": str(target.folder_id),
                "local_manifest_sequence": target.manifest_sequence,
            }
        ):
            raise UploadIndexError("UPLOAD_INDEX_INVALID")
        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            self._context(conn, claim.workspace_id)
            if self._target(conn, claim, publishing=True) != target:
                raise UploadIndexError("UPLOAD_INDEX_DENIED")
            source_id = uuid5(target.connection_id, target.source_external_id)
            prior = conn.execute(
                """SELECT metadata->>'local_manifest_sequence' AS sequence
                   FROM app.sources WHERE id=%s AND workspace_id=%s
                   FOR UPDATE NOWAIT""",
                (source_id, claim.workspace_id),
            ).fetchone()
            if prior and (
                prior["sequence"] is None
                or int(str(prior["sequence"])) >= target.manifest_sequence
            ):
                # Lease replay rolls back all DB effects together. Only an
                # already finished or newer immutable upload can be present.
                raise UploadIndexError("UPLOAD_INDEX_SUPERSEDED")
            result = IndexRepository.enqueue_in_transaction(
                conn,
                claim.workspace_id,
                target.connection_id,
                item,
                version_scope=target.upload_id.bytes,
            )
            if result.status == EnqueueStatus.SUPPRESSED:
                raise UploadIndexError("UPLOAD_INDEX_SOURCE_REMOVED")
            if result.job_id is None or result.document_version_id is None:
                raise UploadIndexError("UPLOAD_INDEX_UNAVAILABLE", retryable=True)
            # The original immutable stage may still exist. Keep its opaque
            # handle attached so DB-only privacy paths do not report a purge.
            written = conn.execute(
                """UPDATE app.document_versions SET object_storage_key=%s
                   WHERE id=%s AND workspace_id=%s AND source_id=%s
                     AND state='pending' AND object_storage_key IS NULL
                   RETURNING true""",
                (
                    target.object_key,
                    result.document_version_id,
                    claim.workspace_id,
                    result.source_id,
                ),
            ).fetchone()
            if written is None or not self._finish(
                conn, claim, job=result.job_id, failure=None, retryable=False
            ):
                raise UploadIndexError("UPLOAD_INDEX_UNAVAILABLE", retryable=True)

    @staticmethod
    def _finish(
        conn: psycopg.Connection[dict[str, object]],
        claim: UploadIndexClaim,
        *,
        job: UUID | None,
        failure: str | None,
        retryable: bool,
    ) -> bool:
        row = conn.execute(
            """SELECT app.finish_verified_device_upload(
               %s::uuid,%s::uuid,%s::text,%s::integer,%s::uuid,%s::text,%s::boolean)
               AS accepted""",
            (
                claim.operation_id,
                claim.workspace_id,
                claim.worker_id,
                claim.attempt,
                job,
                failure,
                retryable,
            ),
        ).fetchone()
        return row is not None and row["accepted"] is True

    def fail(self, claim: UploadIndexClaim, error: UploadIndexError) -> None:
        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            self._context(conn, claim.workspace_id)
            self._finish(
                conn, claim, job=None, failure=error.code, retryable=error.retryable
            )


class UploadIndexReader(Protocol):
    def read(self, target: UploadIndexTarget) -> NormalizedDocument: ...


class NativeUploadIndexRuntime:
    def __init__(
        self, repository: NativeUploadIndexRepository, reader: UploadIndexReader
    ) -> None:
        self._repository = repository
        self._reader = reader

    def run_once(self, worker_id: str) -> bool:
        claim = self._repository.claim(worker_id)
        if claim is None:
            return False
        try:
            target = self._repository.load(claim)
            item = self._reader.read(target)
            self._repository.publish(claim, target, item)
        except UploadIndexError as error:
            self._repository.fail(claim, error)
        except psycopg.Error:
            self._repository.fail(
                claim, UploadIndexError("UPLOAD_INDEX_UNAVAILABLE", retryable=True)
            )
        return True


def build_native_upload_index_runtime(
    database_url: str, client: S3PinnedGetClient, bucket: str
) -> NativeUploadIndexRuntime:
    return NativeUploadIndexRuntime(
        NativeUploadIndexRepository(database_url),
        S3PinnedNativeTextReader(client, bucket=bucket),
    )
