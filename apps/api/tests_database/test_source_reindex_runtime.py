"""Exercise the real API-owner transaction and real PostgreSQL worker."""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
import pytest
from conftest import database_dsn
from test_source_detail_runtime import _PRIVATE, _engine, _grant, _index, _session
from uas_connector_sdk import NormalizedDocument, Provider

from test_indexing_runtime import CONNECTION_ID, USER_ID, WORKSPACE_ID, seed_workspace
from universal_ai_search.conversations.repository import ConversationScope
from universal_ai_search.indexing.pipeline import IndexingError, IndexingPipeline
from universal_ai_search.indexing.repository import EnqueueStatus, IndexRepository
from universal_ai_search.indexing.runtime import IndexingRuntime
from universal_ai_search.search.source_reindex import (
    SourceReindexConflict,
    SourceReindexMissing,
    SourceReindexStore,
    SourceReindexUnavailable,
)

_KEY = "manual-reindex-request-000001"


def _setup(
    connection: psycopg.Connection[tuple[object, ...]],
) -> tuple[ConversationScope, UUID, UUID, UUID]:
    seed_workspace(connection)
    connection.execute(
        "UPDATE app.users SET email_verified_at = now() WHERE id = %s", (USER_ID,)
    )
    session = _session(connection, USER_ID)
    source, version = _index(account=CONNECTION_ID)
    grant = _grant(connection, source)
    return (
        ConversationScope(WORKSPACE_ID, USER_ID, session, 1, 1),
        source,
        version,
        grant,
    )


def _current(connection: psycopg.Connection[tuple[object, ...]], source: UUID) -> UUID:
    row = connection.execute(
        "SELECT current_document_version_id FROM app.sources WHERE id = %s", (source,)
    ).fetchone()
    assert row is not None and isinstance(row[0], UUID)
    return row[0]


@pytest.mark.asyncio
async def test_reindex_idempotently_promotes_a_new_generation(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    scope, source, original, _ = _setup(connection)
    engine = _engine()
    store = SourceReindexStore(engine, idempotency_secret=b"r" * 32)
    repo = IndexRepository(database_dsn())
    try:
        pending = await store.request(
            scope=scope, source_id=source, idempotency_key=_KEY
        )
        assert pending.status == "pending"
        assert pending.started_at is None
        assert _current(connection, source) == original
        assert (
            await store.request(scope=scope, source_id=source, idempotency_key=_KEY)
            == pending
        )
        assert (
            await store.request(
                scope=scope, source_id=source, idempotency_key=_KEY + "b"
            )
        ).id == pending.id
        with pytest.raises(SourceReindexConflict):
            await store.request(scope=scope, source_id=uuid4(), idempotency_key=_KEY)
        assert IndexingRuntime(repo, IndexingPipeline()).run_once("real-reindex-worker")

        completed = await store.read(scope=scope, operation_id=pending.id)
        assert completed.id == pending.id and completed.status == "completed"
        assert completed.started_at is not None and completed.completed_at is not None
        assert completed.error_code is None
        replay = await store.request(
            scope=scope, source_id=source, idempotency_key=_KEY
        )
        assert replay == completed
        current = _current(connection, source)
        assert current != original
        assert connection.execute(
            "SELECT state FROM app.document_versions WHERE id = %s", (original,)
        ).fetchone() == ("superseded",)
        assert connection.execute(
            "SELECT count(*) FROM app.chunks WHERE document_version_id = %s", (current,)
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT indexed_source_count FROM app.workspace_usage "
            "WHERE workspace_id = %s",
            (WORKSPACE_ID,),
        ).fetchone() == (1,)
        document = NormalizedDocument(
            external_id="search-evidence",
            provider=Provider.GITHUB,
            source_type="file",
            title="file.md",
            content=_PRIVATE,
            mime_type="text/markdown",
        )
        unchanged = repo.enqueue(WORKSPACE_ID, CONNECTION_ID, document)
        assert unchanged.status is EnqueueStatus.UNCHANGED
        assert unchanged.document_version_id == current
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reindex_blocks_publication_when_grant_revoked_after_load(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    scope, source, original, grant = _setup(connection)
    engine = _engine()
    store = SourceReindexStore(engine, idempotency_secret=b"r" * 32)
    repo = IndexRepository(database_dsn())
    try:
        operation = await store.request(
            scope=scope, source_id=source, idempotency_key=_KEY
        )
        claim = repo.claim("revocation-reindex")
        assert claim is not None and claim.job_id == operation.id
        prepared = IndexingPipeline().prepare(repo.load_pending(claim))
        connection.execute(
            "UPDATE app.source_collections SET selected = false WHERE id = %s", (grant,)
        )
        connection.commit()
        with pytest.raises(IndexingError) as error:
            repo.promote(claim, prepared)
        assert error.value.code == "AUTHORIZATION_CHANGED"
        repo.fail(claim, error.value.code, retryable=False)
        failed = await store.read(scope=scope, operation_id=operation.id)
        assert (failed.status, failed.error_code) == ("failed", "AUTHORIZATION_CHANGED")
        assert _current(connection, source) == original
        assert connection.execute(
            "SELECT count(*) FROM app.chunks WHERE document_version_id = %s",
            (claim.document_version_id,),
        ).fetchone() == (0,)
        with pytest.raises(SourceReindexMissing):
            await store.request(
                scope=scope, source_id=source, idempotency_key=_KEY + "2"
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reindex_refuses_object_copy_and_foreign_requester(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    scope, source, original, _ = _setup(connection)
    engine = _engine()
    store = SourceReindexStore(engine, idempotency_secret=b"r" * 32)
    peer = uuid4()
    connection.execute(
        """INSERT INTO app.users (id, email, full_name, status, email_verified_at)
           VALUES (%s, 'peer@example.test', 'Peer', 'active', now())""",
        (peer,),
    )
    connection.execute(
        """INSERT INTO app.workspace_members (workspace_id, user_id, role, status)
           VALUES (%s, %s, 'member', 'active')""",
        (WORKSPACE_ID, peer),
    )
    peer_scope = ConversationScope(WORKSPACE_ID, peer, _session(connection, peer), 1, 1)
    try:
        with pytest.raises(SourceReindexMissing):
            await store.request(
                scope=peer_scope, source_id=source, idempotency_key=_KEY
            )
        operation = await store.request(
            scope=scope, source_id=source, idempotency_key=_KEY
        )
        with pytest.raises(SourceReindexMissing):
            await store.read(scope=peer_scope, operation_id=operation.id)
        connection.execute(
            "UPDATE app.document_versions SET object_storage_key = 'object/key' "
            "WHERE id = %s",
            (original,),
        )
        connection.commit()
        with pytest.raises(SourceReindexUnavailable):
            await store.request(
                scope=scope, source_id=source, idempotency_key=_KEY + "2"
            )
        repo = IndexRepository(database_dsn())
        assert IndexingRuntime(repo, IndexingPipeline()).run_once("object-reindex")
        assert (
            await store.read(scope=scope, operation_id=operation.id)
        ).status == "failed"
        assert _current(connection, source) == original
    finally:
        await engine.dispose()
