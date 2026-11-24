from __future__ import annotations

import asyncio
from uuid import uuid4

import psycopg
import pytest
from conftest import database_dsn
from sqlalchemy.ext.asyncio import create_async_engine
from test_conversation_repository import seed_user

from universal_ai_search.conversations.attempts import (
    STREAM_ROUTE,
    StreamAlreadyProcessed,
    StreamAttemptStore,
    StreamRetryUnavailable,
)
from universal_ai_search.conversations.repository import (
    ConversationBusy,
    ConversationKeyConflict,
    ConversationMissing,
    ConversationRepository,
)


async def test_reservation_is_atomic_private_and_retries_once(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    workspace = uuid4()
    connection.execute(
        """INSERT INTO app.workspaces (id,name,plan,status)
           VALUES (%s,'Stream Workspace','free','active')""",
        (workspace,),
    )
    owner, other = seed_user(connection, workspace), seed_user(connection, workspace)
    connection.commit()
    engine = create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )
    try:
        conversation = await ConversationRepository(engine).create(
            scope=owner,
            title="Stream topic",
            key_hash=b"k" * 32,
            request_hash=b"r" * 32,
        )
        store = StreamAttemptStore(engine, idempotency_secret=b"s" * 32)
        original = {
            "scope": owner,
            "conversation_id": conversation.id,
            "client_message_id": uuid4(),
            "retry_user_message_id": None,
            "content": "User asks privately",
            "options": {"providers": ["github"]},
            "idempotency_key": "search-stream-start",
        }
        first, duplicate = await asyncio.gather(
            store.reserve(**original), store.reserve(**original), return_exceptions=True
        )
        if isinstance(first, ConversationBusy):
            first, duplicate = duplicate, first
        assert not isinstance(first, BaseException)
        assert isinstance(duplicate, ConversationBusy)
        with pytest.raises(ConversationMissing):
            await store.reserve(**{**original, "scope": other})
        with pytest.raises(ConversationKeyConflict):
            await store.reserve(**{**original, "client_message_id": uuid4()})
        with pytest.raises(ConversationKeyConflict):
            await store.reserve(**{**original, "options": {"providers": ["gmail"]}})
        with pytest.raises(StreamAlreadyProcessed):
            await store.reserve(**{**original, "idempotency_key": "search-same-turn"})
        retry = {
            **original,
            "client_message_id": uuid4(),
            "retry_user_message_id": first.user_message_id,
            "content": None,
            "idempotency_key": "search-stream-retry",
        }
        with pytest.raises(StreamRetryUnavailable):
            await store.reserve(**retry)
        await store.finish(
            scope=owner,
            conversation_id=conversation.id,
            attempt=first,
            status="interrupted",
            latency_ms=10,
        )
        second = await store.reserve(**retry)
        assert second.user_message_id == first.user_message_id
        assert second.query == first.query
        assert second.assistant_message_id != first.assistant_message_id
        await store.finish(
            scope=owner,
            conversation_id=conversation.id,
            attempt=second,
            status="insufficient_evidence",
            latency_ms=10,
        )
        with pytest.raises(StreamRetryUnavailable):
            await store.reserve(
                **{
                    **retry,
                    "client_message_id": uuid4(),
                    "idempotency_key": "third-after-no-result",
                }
            )
        assert (
            connection.execute(
                """SELECT role, status, content FROM app.messages
               WHERE conversation_id = %s
               ORDER BY created_at,id""",
                (conversation.id,),
            ).fetchall()
            == [
                ("user", "complete", "User asks privately"),
                ("assistant", "interrupted", ""),
                ("assistant", "insufficient_evidence", ""),
            ]
        )
        assert (
            connection.execute(
                """SELECT status, insufficient_reason FROM app.search_requests
               WHERE conversation_id = %s ORDER BY created_at, id""",
                (conversation.id,),
            ).fetchall()
            == [
                ("cancelled", None),
                ("insufficient_evidence", "no_authorized_results"),
            ]
        )
        assert (
            connection.execute(
                """SELECT count(*) FROM app.api_idempotency_keys
               WHERE workspace_id = %s AND route_template = %s
                 AND response_body IS NULL""",
                (workspace, STREAM_ROUTE),
            ).fetchone()
            == (2,)
        )
    finally:
        await engine.dispose()
