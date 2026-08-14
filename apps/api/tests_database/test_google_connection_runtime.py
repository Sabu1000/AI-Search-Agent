from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from conftest import database_dsn
from sqlalchemy.ext.asyncio import create_async_engine

from universal_ai_search.connections.crypto import EncryptedEnvelope
from universal_ai_search.connections.google import GMAIL_READONLY_SCOPE
from universal_ai_search.connections.store import SQLAlchemyGoogleConnectionStore


@pytest.mark.asyncio
async def test_google_oauth_transaction_and_connection_handoff_are_durable(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    user_id = uuid4()
    workspace_id = uuid4()
    transaction_id = uuid4()
    connection_id = uuid4()
    state_hash = secrets.token_bytes(32)
    envelope = EncryptedEnvelope(secrets.token_bytes(80), secrets.token_bytes(64), 1)
    connection.execute(
        """INSERT INTO app.users (
        id, email, full_name, status, email_verified_at
        ) VALUES (%s, %s, 'Google Owner', 'active', clock_timestamp())""",
        (user_id, f"google-{user_id}@example.test"),
    )
    connection.execute(
        "INSERT INTO app.workspaces (id, name, plan, status) "
        "VALUES (%s, 'Google Workspace', 'free', 'active')",
        (workspace_id,),
    )
    connection.execute(
        """INSERT INTO app.workspace_members (
        workspace_id, user_id, role, status
        ) VALUES (%s, %s, 'owner', 'active')""",
        (workspace_id, user_id),
    )
    connection.commit()

    engine = create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )
    store = SQLAlchemyGoogleConnectionStore(engine)
    try:
        await store.create_transaction(
            transaction_id=transaction_id,
            workspace_id=workspace_id,
            user_id=user_id,
            state_hash=state_hash,
            nonce_hash=secrets.token_bytes(32),
            encrypted_payload=envelope,
            redirect_path="/settings/connections",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        transaction = await store.consume_transaction(
            workspace_id=workspace_id, user_id=user_id, state_hash=state_hash
        )
        replay = await store.consume_transaction(
            workspace_id=workspace_id, user_id=user_id, state_hash=state_hash
        )
        resolved_id = await store.connection_id_for_account(
            workspace_id=workspace_id,
            user_id=user_id,
            external_account_hash=b"account-hash",
            proposed_id=connection_id,
        )
        saved_id = await store.save_connection(
            connection_id=resolved_id,
            workspace_id=workspace_id,
            user_id=user_id,
            external_account_hash=b"account-hash",
            display_label="owner@example.test",
            credentials=envelope,
            scopes=frozenset({GMAIL_READONLY_SCOPE}),
            source_families=("gmail",),
        )
        reconnect_id = await store.connection_id_for_account(
            workspace_id=workspace_id,
            user_id=user_id,
            external_account_hash=b"account-hash",
            proposed_id=uuid4(),
        )
        saved_again_id = await store.save_connection(
            connection_id=reconnect_id,
            workspace_id=workspace_id,
            user_id=user_id,
            external_account_hash=b"account-hash",
            display_label="renamed@example.test",
            credentials=envelope,
            scopes=frozenset({GMAIL_READONLY_SCOPE}),
            source_families=("gmail",),
        )
    finally:
        await engine.dispose()

    assert transaction is not None
    assert transaction.encrypted_payload == envelope
    assert replay is None
    assert saved_id == connection_id
    assert saved_again_id == connection_id
    assert connection.execute(
        "SELECT provider, status, credential_ciphertext FROM app.connections "
        "WHERE id = %s",
        (connection_id,),
    ).fetchone() == ("google", "active", envelope.ciphertext)
    assert connection.execute(
        "SELECT scope FROM app.connection_scopes WHERE connection_id = %s",
        (connection_id,),
    ).fetchone() == (GMAIL_READONLY_SCOPE,)
    assert connection.execute(
        "SELECT job_type, queue, status, payload ->> 'mode' FROM app.jobs "
        "WHERE connection_id = %s",
        (connection_id,),
    ).fetchone() == ("sync", "sync", "pending", "full")
    assert connection.execute(
        "SELECT count(*) FROM app.jobs WHERE connection_id = %s",
        (connection_id,),
    ).fetchone() == (1,)
    assert connection.execute(
        "SELECT event_type, payload ? 'job_id' FROM app.outbox_events "
        "WHERE aggregate_id = %s",
        (connection_id,),
    ).fetchall() == [
        ("connection.sync.requested", True),
        ("connection.sync.requested", True),
    ]
    assert (
        connection.execute(
            """SELECT count(*) FROM app.outbox_events AS event
        JOIN app.jobs AS job ON job.id = (event.payload ->> 'job_id')::UUID
        WHERE event.aggregate_id = %s""",
            (connection_id,),
        ).fetchone()
        == (2,)
    )
