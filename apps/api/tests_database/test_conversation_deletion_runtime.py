from __future__ import annotations

import secrets
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import psycopg
import pytest
from conftest import database_dsn
from sqlalchemy.ext.asyncio import create_async_engine
from test_conversation_repository import seed_user

from universal_ai_search.conversations.deletion import ConversationDeletionStore
from universal_ai_search.conversations.repository import (
    ConversationDenied,
    ConversationKeyConflict,
    ConversationMissing,
    ConversationRepository,
    ConversationScope,
)


def seed_citation(
    connection: psycopg.Connection[tuple[object, ...]],
    scope: ConversationScope,
    conversation_id: UUID,
) -> tuple[UUID, UUID, UUID, UUID, UUID]:
    workspace = scope.workspace_id
    connector, source, version, chunk, search, message, claim, citation = (
        uuid4() for _ in range(8)
    )
    connection.execute(
        """INSERT INTO app.connections (id,workspace_id,owner_user_id,provider,
                                          display_label,status)
           VALUES (%s,%s,%s,'local_files','Local test','active')""",
        (connector, workspace, scope.user_id),
    )
    connection.execute(
        """INSERT INTO app.sources (id,workspace_id,connection_id,provider,external_id,
                                      source_type,title,content_hash,permissions_hash,state)
           VALUES (%s,%s,%s,'local_files','local-test','file',
                   'Test source',%s,%s,'active')""",
        (source, workspace, connector, b"c", b"p"),
    )
    connection.execute(
        """INSERT INTO app.document_versions (id,workspace_id,source_id,version_key,
                      state,normalized_text,language,parser_version,chunker_version,
                      content_hash,permissions_hash,token_count,extracted_bytes)
           VALUES (%s,%s,%s,%s,'ready','Source preview','en','p','c',%s,%s,4,14)""",
        (version, workspace, source, b"v", b"c", b"p"),
    )
    connection.execute(
        """INSERT INTO app.chunks (id,workspace_id,document_version_id,chunk_index,
                                     chunk_hash,content,token_count)
           VALUES (%s,%s,%s,0,%s,'Source preview',4)""",
        (chunk, workspace, version, b"chunk"),
    )
    connection.execute(
        """INSERT INTO app.search_requests
              (id,workspace_id,user_id,conversation_id,mode,query_text,normalized_plan,
               planner_version,ranker_version,index_generation,authorization_version,
               status,purge_after)
           VALUES (%s,%s,%s,%s,'answer','Private question','{}','p','r',1,1,
                   'completed',now() + INTERVAL '1 day')""",
        (search, workspace, scope.user_id, conversation_id),
    )
    connection.execute(
        """INSERT INTO app.messages (id,workspace_id,conversation_id,search_request_id,
                                       role,status,content,completed_at)
           VALUES (%s,%s,%s,%s,'assistant','complete','Private answer',now())""",
        (message, workspace, conversation_id, search),
    )
    connection.execute(
        """INSERT INTO app.message_claims (id,workspace_id,message_id,claim_index,
                                             text,material)
           VALUES (%s,%s,%s,0,'Private claim',true)""",
        (claim, workspace, message),
    )
    connection.execute(
        """INSERT INTO app.citations (id,workspace_id,message_id,claim_id,source_id,
                            document_version_id,chunk_id,citation_index,excerpt)
           VALUES (%s,%s,%s,%s,%s,%s,%s,0,'Private excerpt')""",
        (citation, workspace, message, claim, source, version, chunk),
    )
    connection.commit()
    return source, search, message, claim, citation


async def test_deletion_is_private_atomic_and_cascades_only_its_content(
    connection: psycopg.Connection[tuple[object, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = uuid4()
    connection.execute(
        """INSERT INTO app.workspaces (id,name,plan,status)
           VALUES (%s,'Thread Workspace','free','active')""",
        (workspace,),
    )
    owner, other = seed_user(connection, workspace), seed_user(connection, workspace)
    connection.commit()
    engine = create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )
    repo = ConversationRepository(engine)
    store = ConversationDeletionStore(
        engine, idempotency_secret=b"d" * 32, audit_secret=b"a" * 32
    )
    create_key, create_hash = secrets.token_bytes(32), secrets.token_bytes(32)
    try:
        target = await repo.create(
            scope=owner,
            title="Private title",
            key_hash=create_key,
            request_hash=create_hash,
        )
        unrelated = await repo.create(
            scope=owner, title="Keep this", key_hash=b"x" * 32, request_hash=b"y" * 32
        )
        source, search, message, claim, citation = seed_citation(
            connection, owner, target.id
        )
        arguments = {
            "conversation_id": target.id,
            "idempotency_key": "private-delete-key",
            "request_id": uuid4(),
        }
        with pytest.raises(ConversationMissing):
            await store.delete(scope=other, **arguments)
        with monkeypatch.context() as injected:
            injected.setattr(store, "_remember", AsyncMock(side_effect=RuntimeError))
            with pytest.raises(RuntimeError):
                await store.delete(scope=owner, **arguments)
        assert (
            await repo.read(scope=owner, conversation_id=target.id, limit=1, after=None)
        )[0].title == target.title
        assert connection.execute(
            "SELECT excerpt FROM app.citations WHERE id = %s", (citation,)
        ).fetchone() == ("Private excerpt",)
        connection.commit()

        receipt = await store.delete(scope=owner, **arguments)
        assert await store.delete(scope=owner, **arguments) == receipt
        assert await store.read(scope=owner, receipt_id=receipt.id) == receipt
        with pytest.raises(ConversationMissing):
            await store.read(scope=other, receipt_id=receipt.id)
        with pytest.raises(ConversationKeyConflict):
            await store.delete(
                scope=owner, **{**arguments, "conversation_id": unrelated.id}
            )
        with pytest.raises(ConversationMissing):
            await repo.read(scope=owner, conversation_id=target.id, limit=1, after=None)
        with pytest.raises(ConversationMissing):
            await repo.create(
                scope=owner,
                title=target.title,
                key_hash=create_key,
                request_hash=create_hash,
            )
        assert [
            row.id for row in await repo.list_owned(scope=owner, limit=5, after=None)
        ] == [unrelated.id]
        assert connection.execute(
            "SELECT title, status FROM app.conversations WHERE id = %s", (target.id,)
        ).fetchone() == ("[deleted]", "deleted")
        assert (
            connection.execute(
                "SELECT id FROM app.messages WHERE id = %s", (message,)
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT id FROM app.message_claims WHERE id = %s", (claim,)
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT id FROM app.citations WHERE id = %s", (citation,)
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT id FROM app.search_requests WHERE id = %s", (search,)
            ).fetchone()
            is None
        )
        assert connection.execute(
            "SELECT id FROM app.sources WHERE id = %s", (source,)
        ).fetchone() == (source,)
        key = connection.execute(
            "SELECT idempotency_key FROM app.deletion_requests WHERE id = %s",
            (receipt.id,),
        ).fetchone()
        assert key and key[0] != "private-delete-key"
        assert connection.execute(
            "SELECT count(*) FROM app.audit_events WHERE workspace_id = %s "
            "AND action = 'conversation.delete'",
            (workspace,),
        ).fetchone() == (1,)
        connection.execute(
            "UPDATE app.sessions SET revoked_at = now() WHERE id = %s",
            (owner.session_id,),
        )
        connection.commit()
        with pytest.raises(ConversationDenied):
            await store.delete(scope=owner, **arguments)
    finally:
        await engine.dispose()
