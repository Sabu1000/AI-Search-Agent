from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from conftest import database_dsn
from sqlalchemy.ext.asyncio import create_async_engine
from test_conversation_repository import seed_user

from universal_ai_search.conversations.repository import (
    ConversationDenied,
    ConversationRepository,
    ConversationScope,
)
from universal_ai_search.search.history import HISTORY_PATH, SearchHistoryStore

NOW = datetime.now(UTC)


def seed_workspace(connection: psycopg.Connection[tuple[object, ...]]) -> UUID:
    workspace = uuid4()
    connection.execute(
        """INSERT INTO app.workspaces (id,name,plan,status)
           VALUES (%s,'Search History Workspace','free','active')""",
        (workspace,),
    )
    return workspace


def seed_history(
    connection: psycopg.Connection[tuple[object, ...]],
    scope: ConversationScope,
    value: str,
    *,
    created: datetime = NOW,
    expired: bool = False,
    conversation: UUID | None = None,
) -> UUID:
    identifier = uuid4()
    connection.execute(
        """INSERT INTO app.search_requests
             (id,workspace_id,user_id,conversation_id,mode,query_text,normalized_plan,
              planner_version,ranker_version,index_generation,authorization_version,
              status,result_count,created_at,purge_after)
           VALUES (%s,%s,%s,%s,'results',%s,'{}','p','r',1,1,'completed',1,%s,%s)""",
        (
            identifier,
            scope.workspace_id,
            scope.user_id,
            conversation,
            value,
            created,
            created + (timedelta(microseconds=1) if expired else timedelta(days=2)),
        ),
    )
    return identifier


async def test_history_scope_retention_exact_prefix_keyset_and_atomic_clear(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    workspace, second = seed_workspace(connection), seed_workspace(connection)
    owner, other = seed_user(connection, workspace), seed_user(connection, workspace)
    connection.execute(
        """INSERT INTO app.workspace_members (workspace_id,user_id,role,status)
           VALUES (%s,%s,'member','active')""",
        (second, owner.user_id),
    )
    second_scope = replace(owner, workspace_id=second)
    connection.commit()
    engine = create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )
    store = SearchHistoryStore(
        engine, idempotency_secret=b"s" * 32, audit_secret=b"a" * 32
    )
    try:
        conversation = await ConversationRepository(engine).create(
            scope=owner,
            title="Private thread",
            key_hash=b"k" * 32,
            request_hash=b"r" * 32,
        )
        linked = seed_history(
            connection, owner, "Secret conversation topic", conversation=conversation.id
        )
        expired = seed_history(
            connection,
            owner,
            "Secret expired topic",
            created=NOW - timedelta(days=2),
            expired=True,
        )
        outsider = seed_history(connection, other, "Secret other user's search")
        other_workspace = seed_history(
            connection, second_scope, "Secret other workspace"
        )
        lowercase = seed_history(
            connection, owner, "secret query", created=NOW - timedelta(minutes=1)
        )
        latest = seed_history(connection, owner, "Secret Query")
        literal = seed_history(connection, owner, "Secret_100% literal")
        overlong = seed_history(connection, owner, "Secret " + "X" * 250)
        # A message's nullable search request reference should detach, not keep
        # the private search row around or erase the conversation message itself.
        message = uuid4()
        connection.execute(
            """INSERT INTO app.messages
                 (id,workspace_id,conversation_id,search_request_id,role,status,content)
               VALUES (%s,%s,%s,%s,'user','complete','Retained message')""",
            (message, workspace, conversation.id, literal),
        )
        connection.commit()

        first = await store.list_owned(scope=owner, after=None, limit=2)
        second_page = await store.list_owned(
            scope=owner, after=(first[-1].created_at, first[-1].id), limit=2
        )
        assert [r.id for r in first + second_page] == [
            *sorted([latest, literal, overlong], reverse=True),
            lowercase,
        ]
        assert await store.suggest_owned(scope=owner, prefix="secret ", limit=10) == [
            "Secret Query"
        ]
        assert await store.suggest_owned(
            scope=owner, prefix="secret_100%", limit=10
        ) == ["Secret_100% literal"]
        assert [
            r.id for r in await store.list_owned(scope=other, after=None, limit=10)
        ] == [outsider]
        with pytest.raises(ConversationDenied):
            await store.list_owned(
                scope=replace(owner, session_id=other.session_id), after=None, limit=10
            )
        with pytest.raises(ConversationDenied):
            await store.suggest_owned(
                scope=replace(owner, workspace_version=42), prefix="secret", limit=10
            )

        key = "test-search-clear-key"
        await asyncio.gather(
            *(
                store.clear_owned(scope=owner, idempotency_key=key, request_id=uuid4())
                for _ in range(3)
            )
        )
        assert await store.list_owned(scope=owner, after=None, limit=10) == []
        assert await store.suggest_owned(scope=owner, prefix="Secret", limit=10) == []
        retained = connection.execute(
            "SELECT id FROM app.search_requests WHERE id = ANY(%s)",
            (
                [
                    linked,
                    expired,
                    outsider,
                    other_workspace,
                    latest,
                    literal,
                    overlong,
                    lowercase,
                ],
            ),
        ).fetchall()
        assert {item[0] for item in retained} == {linked, outsider, other_workspace}
        assert connection.execute(
            "SELECT search_request_id, content FROM app.messages WHERE id = %s",
            (message,),
        ).fetchone() == (None, "Retained message")
        assert (
            connection.execute(
                """SELECT response_status, response_body, resource_id
               FROM app.api_idempotency_keys
               WHERE workspace_id=%s AND user_id=%s AND route_template=%s""",
                (workspace, owner.user_id, HISTORY_PATH),
            ).fetchall()
            == [(204, None, None)]
        )
        assert (
            connection.execute(
                """SELECT metadata FROM app.audit_events
               WHERE workspace_id = %s AND actor_user_id = %s
                 AND action = 'search_history.clear'""",
                (workspace, owner.user_id),
            ).fetchall()
            == [({},)]
        )

        new = seed_history(connection, owner, "A later query")
        connection.commit()
        await store.clear_owned(scope=owner, idempotency_key=key, request_id=uuid4())
        assert [
            r.id for r in await store.list_owned(scope=owner, after=None, limit=10)
        ] == [new]
        with pytest.raises(ConversationDenied):
            await store.clear_owned(
                scope=replace(owner, user_version=42),
                idempotency_key=key,
                request_id=uuid4(),
            )
    finally:
        await engine.dispose()
