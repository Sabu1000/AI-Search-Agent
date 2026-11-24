from __future__ import annotations

import asyncio
import secrets
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from conftest import database_dsn
from sqlalchemy.ext.asyncio import create_async_engine
from test_conversation_repository import seed_user

from universal_ai_search.conversations.repository import (
    ConversationDenied,
    ConversationKeyConflict,
    ConversationMissing,
    ConversationRepository,
)
from universal_ai_search.conversations.user_turn import (
    RESOURCE_TYPE,
    UserTurnStore,
    _prepare,
)


async def test_user_turn_is_durable_private_and_single_write(
    connection: psycopg.Connection[tuple[object, ...]],
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
    secret = b"t" * 32
    turns = UserTurnStore(engine, idempotency_secret=secret)
    try:
        conversation = await repo.create(
            scope=owner,
            title="Owner's workspace thread",
            key_hash=secrets.token_bytes(32),
            request_hash=secrets.token_bytes(32),
        )
        another = await repo.create(
            scope=owner,
            title="Another thread",
            key_hash=secrets.token_bytes(32),
            request_hash=secrets.token_bytes(32),
        )
        request = {
            "conversation_id": conversation.id,
            "client_message_id": uuid4(),
            "content": "  owner question with formatting\n",
        }
        created, simultaneous = await asyncio.gather(
            turns.append(
                scope=owner, **request, idempotency_key="fixture-user-turn-01"
            ),
            turns.append(
                scope=owner, **request, idempotency_key="fixture-user-turn-02"
            ),
        )
        assert created.created != simultaneous.created
        assert created.message.id == simultaneous.message.id
        assert created.message.content == request["content"]
        assert created.message.completed_at == created.message.created_at
        assert request["content"] not in repr(created)
        replay = await turns.append(
            scope=owner, **request, idempotency_key="fixture-user-turn-01"
        )
        assert replay.created is False
        assert replay.message.id == created.message.id

        with pytest.raises(ConversationKeyConflict):
            await turns.append(
                scope=owner,
                **{**request, "content": "changed"},
                idempotency_key="fixture-user-turn-01",
            )
        with pytest.raises(ConversationKeyConflict):
            await turns.append(
                scope=owner,
                **{**request, "content": "changed"},
                idempotency_key="different-user-turn-key",
            )
        with pytest.raises(ConversationKeyConflict):
            await turns.append(
                scope=owner,
                **{**request, "conversation_id": another.id},
                idempotency_key="fixture-user-turn-01",
            )
        with pytest.raises(ConversationMissing):
            await turns.append(
                scope=other, **request, idempotency_key="fixture-user-turn-01"
            )

        changed, messages = await repo.read(
            scope=owner, conversation_id=conversation.id, limit=5, after=None
        )
        assert changed.updated_at >= created.message.created_at
        assert [message.id for message in messages] == [created.message.id]
        assert (
            connection.execute(
                """SELECT count(*), count(response_body), count(response_status)
               FROM app.api_idempotency_keys WHERE resource_type = %s
                 AND resource_id = %s""",
                (RESOURCE_TYPE, created.message.id),
            ).fetchone()
            == (2, 0, 0)
        )

        # A row with matching deterministic ID of any other role is not
        # returned or overwritten by the internal user-turn service.
        seeded_request = {**request, "client_message_id": uuid4()}
        seeded_id, _, _, _ = _prepare(
            secret=secret,
            scope=owner,
            **seeded_request,
            idempotency_key="fixture-user-turn-03",
        )
        connection.execute(
            """INSERT INTO app.messages
                 (id,workspace_id,conversation_id,role,status,content)
               VALUES (%s,%s,%s,'assistant','complete','hidden assistant content')""",
            (seeded_id, workspace, conversation.id),
        )
        connection.commit()
        with pytest.raises(ConversationKeyConflict):
            await turns.append(
                scope=owner, **seeded_request, idempotency_key="fixture-user-turn-03"
            )
        _, messages = await repo.read(
            scope=owner, conversation_id=conversation.id, limit=5, after=None
        )
        assert all(
            item.content is None and item.status == "insufficient_evidence"
            for item in messages
            if item.role == "assistant"
        )

        connection.execute(
            "DELETE FROM app.messages WHERE id = %s", (created.message.id,)
        )
        connection.commit()
        with pytest.raises(ConversationMissing):
            await turns.append(
                scope=owner, **request, idempotency_key="fixture-user-turn-01"
            )
        connection.execute(
            "UPDATE app.sessions SET expires_at = issued_at + %s, "
            "revoked_at = clock_timestamp() WHERE id = %s",
            (timedelta(seconds=1), owner.session_id),
        )
        connection.commit()
        with pytest.raises(ConversationDenied):
            await turns.append(
                scope=owner, **request, idempotency_key="fixture-user-turn-01"
            )
    finally:
        await engine.dispose()
