from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from conftest import database_dsn
from sqlalchemy.ext.asyncio import create_async_engine

from universal_ai_search.conversations.repository import (
    ROUTE,
    ConversationBusy,
    ConversationDenied,
    ConversationKeyConflict,
    ConversationMissing,
    ConversationRepository,
    ConversationScope,
)

NOW = datetime(2026, 11, 24, tzinfo=UTC)


def seed_user(
    connection: psycopg.Connection[tuple[object, ...]], workspace_id: UUID
) -> ConversationScope:
    user_id, session_id = uuid4(), uuid4()
    connection.execute(
        """INSERT INTO app.users (id, email, full_name, status, email_verified_at)
           VALUES (%s, %s, 'Conversation User', 'active', now())""",
        (user_id, f"conversation-{user_id}@example.test"),
    )
    connection.execute(
        """INSERT INTO app.workspace_members (workspace_id,user_id,role,status)
           VALUES (%s,%s,'member','active')""",
        (workspace_id, user_id),
    )
    connection.execute(
        """INSERT INTO app.sessions
             (id,user_id,refresh_token_hash,family_id,issued_at,expires_at,csrf_token_hash)
           VALUES (%s,%s,%s,%s,now(),now() + interval '10 minutes',%s)""",
        (
            session_id,
            user_id,
            secrets.token_bytes(32),
            uuid4(),
            secrets.token_bytes(32),
        ),
    )
    return ConversationScope(workspace_id, user_id, session_id, 1, 1)


async def test_owner_scope_replay_and_message_projection(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    workspace = uuid4()
    connection.execute(
        """INSERT INTO app.workspaces (id,name,plan,status)
           VALUES (%s,'Conversation Workspace','free','active')""",
        (workspace,),
    )
    owner = seed_user(connection, workspace)
    outsider = seed_user(connection, workspace)
    connection.commit()
    engine = create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )
    repo = ConversationRepository(engine)
    key, request_hash = secrets.token_bytes(32), secrets.token_bytes(32)
    try:
        created = await repo.create(
            scope=owner, title="Private title", key_hash=key, request_hash=request_hash
        )
        assert created == await repo.create(
            scope=owner, title="Private title", key_hash=key, request_hash=request_hash
        )
        assert (
            connection.execute(
                """SELECT resource_id, response_body FROM app.api_idempotency_keys
               WHERE workspace_id = %s AND user_id = %s""",
                (workspace, owner.user_id),
            ).fetchone()
            == (created.id, None)
        )
        with pytest.raises(ConversationKeyConflict):
            await repo.create(
                scope=owner, title="Other title", key_hash=key, request_hash=b"x" * 32
            )
        with pytest.raises(ConversationMissing):
            await repo.read(
                scope=outsider, conversation_id=created.id, limit=1, after=None
            )
        assert await repo.list_owned(scope=outsider, limit=5, after=None) == []
        assert [
            row.id for row in await repo.list_owned(scope=owner, limit=5, after=None)
        ] == [created.id]
        assert (
            await repo.list_owned(
                scope=owner, limit=5, after=(created.created_at, created.id)
            )
            == []
        )

        user_message, tied_assistant = sorted((uuid4(), uuid4()))
        connection.execute(
            """INSERT INTO app.messages
                 (id,workspace_id,conversation_id,role,status,content,created_at,completed_at)
               VALUES (%s,%s,%s,'user','complete','Private owner message',%s,%s),
                      (%s,%s,%s,'assistant','complete','Old private answer',%s,%s),
                      (%s,%s,%s,'assistant','interrupted','Private draft',%s,NULL)""",
            (
                user_message,
                workspace,
                created.id,
                NOW,
                NOW,
                tied_assistant,
                workspace,
                created.id,
                NOW,
                NOW,
                uuid4(),
                workspace,
                created.id,
                NOW + timedelta(seconds=2),
            ),
        )
        connection.commit()
        result, messages = await repo.read(
            scope=owner, conversation_id=created.id, limit=1, after=None
        )
        assert result == created
        assert messages[0].id == user_message
        assert messages[0].content == "Private owner message"
        _, messages = await repo.read(
            scope=owner, conversation_id=created.id, limit=5, after=(NOW, user_message)
        )
        assert [(message.status, message.content) for message in messages] == [
            ("insufficient_evidence", None),
            ("interrupted", None),
        ]

        connection.execute(
            "UPDATE app.conversations SET status='deleted',deleted_at=now() "
            "WHERE id=%s",
            (created.id,),
        )
        connection.commit()
        with pytest.raises(ConversationMissing):
            await repo.create(
                scope=owner,
                title="Private title",
                key_hash=key,
                request_hash=request_hash,
            )
        connection.execute(
            "UPDATE app.sessions SET revoked_at=now() WHERE id=%s", (owner.session_id,)
        )
        connection.commit()
        with pytest.raises(ConversationDenied):
            await repo.list_owned(scope=owner, limit=5, after=None)
    finally:
        await engine.dispose()


async def test_advisory_lock_returns_busy_without_creation(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    workspace = uuid4()
    connection.execute(
        """INSERT INTO app.workspaces (id,name,plan,status)
           VALUES (%s,'Conversation Workspace','free','active')""",
        (workspace,),
    )
    owner = seed_user(connection, workspace)
    connection.commit()
    engine = create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )
    key = secrets.token_bytes(32)
    lock_id = int.from_bytes(
        hashlib.sha256(
            ROUTE.encode() + workspace.bytes + owner.user_id.bytes + key
        ).digest()[:8],
        byteorder="big",
        signed=True,
    )
    connection.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))
    repo = ConversationRepository(engine)
    try:
        with pytest.raises(ConversationBusy):
            await repo.create(
                scope=owner, title="Not yet", key_hash=key, request_hash=b"p" * 32
            )
        assert connection.execute(
            "SELECT count(*) FROM app.conversations WHERE workspace_id = %s",
            (workspace,),
        ).fetchone() == (0,)
        connection.commit()
        created = await repo.create(
            scope=owner, title="Now", key_hash=key, request_hash=b"p" * 32
        )
        assert created.title == "Now"
        connection.execute(
            """UPDATE app.api_idempotency_keys
               SET created_at = now() - INTERVAL '25 hours', expires_at = now()
               WHERE resource_id = %s""",
            (created.id,),
        )
        connection.commit()
        again = await repo.create(
            scope=owner, title="Next day", key_hash=key, request_hash=b"q" * 32
        )
        assert again.id != created.id
    finally:
        await engine.dispose()
