"""Owner deletion removes the local derived index, not the provider resource."""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
import pytest
from conftest import database_dsn
from test_source_detail_runtime import _chunk, _engine, _session
from test_source_reindex_runtime import _KEY, _setup
from uas_connector_sdk import NormalizedDocument, Provider

from test_indexing_runtime import CONNECTION_ID, USER_ID, WORKSPACE_ID
from universal_ai_search.conversations.repository import (
    ConversationKeyConflict,
    ConversationMissing,
    ConversationScope,
)
from universal_ai_search.indexing.pipeline import IndexingError
from universal_ai_search.indexing.repository import EnqueueStatus, IndexRepository
from universal_ai_search.search.source_deletion import (
    SourceDeletionStore,
    SourceDeletionUnavailable,
)
from universal_ai_search.search.source_reindex import SourceReindexStore


def _citation(
    connection: psycopg.Connection[tuple[object, ...]], source: UUID, version: UUID
) -> tuple[UUID, UUID]:
    conversation, user_turn, answer, claim, citation = (uuid4() for _ in range(5))
    connection.execute(
        """INSERT INTO app.conversations (id,workspace_id,user_id,title,status)
           VALUES (%s,%s,%s,'Test conversation','active')""",
        (conversation, WORKSPACE_ID, USER_ID),
    )
    for message, role, body in (
        (user_turn, "user", "Ask about this document."),
        (answer, "assistant", "This answer contains source-derived material."),
    ):
        connection.execute(
            """INSERT INTO app.messages
                   (id,workspace_id,conversation_id,role,status,content)
               VALUES (%s,%s,%s,%s,'complete',%s)""",
            (message, WORKSPACE_ID, conversation, role, body),
        )
    connection.execute(
        """INSERT INTO app.message_claims
              (id,workspace_id,message_id,claim_index,text,material)
           VALUES (%s,%s,%s,0,'Source-derived material',true)""",
        (claim, WORKSPACE_ID, answer),
    )
    connection.execute(
        """INSERT INTO app.citations
              (id,workspace_id,message_id,claim_id,source_id,
               document_version_id,chunk_id,citation_index,excerpt)
           VALUES (%s,%s,%s,%s,%s,%s,%s,0,'Private excerpt')""",
        (
            citation,
            WORKSPACE_ID,
            answer,
            claim,
            source,
            version,
            _chunk(connection, version),
        ),
    )
    connection.commit()
    return user_turn, answer


@pytest.mark.asyncio
async def test_deletion_scrubs_inline_copy_and_prevents_sync_resurrection(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    scope, source, version, _ = _setup(connection)
    user_turn, answer = _citation(connection, source, version)
    engine = _engine()
    store = SourceDeletionStore(
        engine, idempotency_secret=b"s" * 32, audit_secret=b"a" * 32
    )
    repo = IndexRepository(database_dsn())
    reindex = SourceReindexStore(engine, idempotency_secret=b"s" * 32)
    try:
        op = await reindex.request(scope=scope, source_id=source, idempotency_key=_KEY)
        claimed = repo.claim("test-source-delete")
        assert claimed is not None and claimed.job_id == op.id
        result = await store.delete(
            scope=scope, source_id=source, idempotency_key=_KEY, request_id=uuid4()
        )
        assert result == await store.read(scope=scope, operation_id=result.id)
        assert result == await store.delete(
            scope=scope, source_id=source, idempotency_key=_KEY, request_id=uuid4()
        )
        with pytest.raises(ConversationKeyConflict):
            await store.delete(
                scope=scope, source_id=uuid4(), idempotency_key=_KEY, request_id=uuid4()
            )
        with pytest.raises(ConversationMissing):
            await store.delete(
                scope=scope,
                source_id=source,
                idempotency_key=_KEY + "2",
                request_id=uuid4(),
            )
        with pytest.raises(IndexingError):
            repo.load_pending(claimed)
        assert (
            await reindex.read(scope=scope, operation_id=op.id)
        ).error_code == "SOURCE_DELETED"
        row = connection.execute(
            """SELECT title, external_id, state, metadata, canonical_url,
                      current_document_version_id
               FROM app.sources WHERE id = %s""",
            (source,),
        ).fetchone()
        assert row == (
            "Deleted source",
            f"suppressed:{source}",
            "deleted",
            {},
            None,
            None,
        )
        assert (
            connection.execute(
                "SELECT id FROM app.document_versions WHERE source_id = %s", (source,)
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT id FROM app.chunks WHERE document_version_id = %s", (version,)
            ).fetchone()
            is None
        )
        assert connection.execute(
            "SELECT id FROM app.messages WHERE id = ANY(%s)", ([user_turn, answer],)
        ).fetchall() == [(user_turn,)]
        assert connection.execute(
            "SELECT indexed_source_count, extracted_bytes FROM app.workspace_usage "
            "WHERE workspace_id = %s",
            (WORKSPACE_ID,),
        ).fetchone() == (0, 0)
        resync = repo.enqueue(
            WORKSPACE_ID,
            CONNECTION_ID,
            NormalizedDocument(
                external_id="search-evidence",
                provider=Provider.GITHUB,
                source_type="file",
                title="Changed upstream",
                content="A subsequent snapshot reports changed upstream content.",
                mime_type="text/markdown",
            ),
        )
        assert resync.status is EnqueueStatus.SUPPRESSED
        assert resync.source_id == source and resync.document_version_id is None
        assert repo.claim("test-source-delete") is None
        repo.mark_provider_sync_seen(
            WORKSPACE_ID, CONNECTION_ID, ("search-evidence",), uuid4()
        )
        assert connection.execute(
            "SELECT state, metadata FROM app.sources WHERE id = %s", (source,)
        ).fetchone() == ("deleted", {})
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_source_delete_cannot_claim_unpurged_object_success(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    scope, source, version, _ = _setup(connection)
    connection.execute(
        "UPDATE app.document_versions SET object_storage_key = 'private/test' "
        "WHERE id = %s",
        (version,),
    )
    connection.commit()
    engine = _engine()
    store = SourceDeletionStore(
        engine, idempotency_secret=b"s" * 32, audit_secret=b"a" * 32
    )
    try:
        with pytest.raises(SourceDeletionUnavailable):
            await store.delete(
                scope=scope, source_id=source, idempotency_key=_KEY, request_id=uuid4()
            )
        assert connection.execute(
            "SELECT state, current_document_version_id FROM app.sources WHERE id = %s",
            (source,),
        ).fetchone() == ("active", version)
        assert (
            connection.execute(
                "SELECT id FROM app.deletion_requests WHERE target_id = %s", (source,)
            ).fetchone()
            is None
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_source_deletion_checks_owner_and_current_repository_access(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    scope, source, _, grant = _setup(connection)
    engine = _engine()
    store = SourceDeletionStore(
        engine, idempotency_secret=b"s" * 32, audit_secret=b"a" * 32
    )
    peer = uuid4()
    connection.execute(
        """INSERT INTO app.users (id, email, full_name, status, email_verified_at)
           VALUES (%s, 'nonowner@example.test', 'Peer', 'active', now())""",
        (peer,),
    )
    connection.execute(
        """INSERT INTO app.workspace_members (workspace_id, user_id, role, status)
           VALUES (%s, %s, 'member', 'active')""",
        (WORKSPACE_ID, peer),
    )
    peer_scope = ConversationScope(WORKSPACE_ID, peer, _session(connection, peer), 1, 1)
    try:
        with pytest.raises(ConversationMissing):
            await store.delete(
                scope=peer_scope,
                source_id=source,
                idempotency_key=_KEY,
                request_id=uuid4(),
            )
        connection.execute(
            "UPDATE app.source_collections SET selected = false WHERE id = %s", (grant,)
        )
        connection.commit()
        with pytest.raises(ConversationMissing):
            await store.delete(
                scope=scope, source_id=source, idempotency_key=_KEY, request_id=uuid4()
            )
        connection.execute(
            "UPDATE app.source_collections SET selected = true WHERE id = %s", (grant,)
        )
        connection.commit()
        receipt = await store.delete(
            scope=scope, source_id=source, idempotency_key=_KEY, request_id=uuid4()
        )
        with pytest.raises(ConversationMissing):
            await store.read(scope=peer_scope, operation_id=receipt.id)
    finally:
        await engine.dispose()
