from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from conftest import database_dsn
from sqlalchemy.ext.asyncio import create_async_engine

from universal_ai_search.connections.crypto import EncryptedEnvelope
from universal_ai_search.connections.github import GitHubAuthorizationError
from universal_ai_search.connections.github_store import SQLAlchemyGitHubConnectionStore


async def test_git_hub_grants_are_bound_to_verified_manager_session(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    user, workspace, session, tx = (uuid4() for _ in range(4))
    connection.execute(
        """INSERT INTO app.users (id, email, full_name, status, email_verified_at)
           VALUES (%s, %s, 'GitHub Owner', 'active', now())""",
        (user, f"github-{user}@example.test"),
    )
    connection.execute(
        """INSERT INTO app.workspaces (id, name, plan, status)
           VALUES (%s, 'GitHub Workspace', 'free', 'active')""",
        (workspace,),
    )
    connection.execute(
        """INSERT INTO app.workspace_members (workspace_id, user_id, role, status)
           VALUES (%s, %s, 'owner', 'active')""",
        (workspace, user),
    )
    connection.execute(
        """INSERT INTO app.sessions
           (id, user_id, refresh_token_hash, family_id, issued_at, expires_at,
            csrf_token_hash)
           VALUES (%s, %s, %s, %s, now(), now() + interval '10 minutes', %s)""",
        (session, user, secrets.token_bytes(32), uuid4(), secrets.token_bytes(32)),
    )
    connection.commit()
    engine = create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )
    store = SQLAlchemyGitHubConnectionStore(engine, audit_key=b"a" * 32)
    state = secrets.token_bytes(32)
    envelope = EncryptedEnvelope(secrets.token_bytes(80), secrets.token_bytes(64))
    try:
        await store.create(
            transaction_id=tx,
            workspace_id=workspace,
            user_id=user,
            state_hash=state,
            nonce_hash=secrets.token_bytes(32),
            envelope=envelope,
            return_path="/settings",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        result = await store.consume(
            workspace_id=workspace, user_id=user, state_hash=state
        )
        assert result is not None and result.encrypted_payload == envelope
        assert (
            await store.consume(workspace_id=workspace, user_id=user, state_hash=state)
            is None
        )
        grant = dict(
            workspace_id=workspace,
            user_id=user,
            installation_id=42,
            repository_ids=frozenset({8, 10}),
        )
        with pytest.raises(GitHubAuthorizationError):
            await store.save_grants(**grant, session_id=uuid4())
        connection_id = await store.save_grants(**grant, session_id=session)
        with pytest.raises(GitHubAuthorizationError):
            await store.save_grants(**grant, session_id=session)
        connection.execute(
            "UPDATE app.workspace_members SET role='member' WHERE user_id=%s", (user,)
        )
        connection.commit()
        with pytest.raises(GitHubAuthorizationError):
            await store.save_grants(
                **{**grant, "installation_id": 43}, session_id=session
            )
    finally:
        await engine.dispose()
    assert connection.execute(
        "SELECT pkce_verifier_ciphertext IS NULL FROM app.oauth_transactions "
        "WHERE id=%s",
        (tx,),
    ).fetchone() == (True,)
    assert (
        connection.execute(
            """SELECT provider, status, github_installation_id, credential_ciphertext
           FROM app.connections WHERE id=%s""",
            (connection_id,),
        ).fetchone()
        == ("github", "pending", 42, None)
    )
    assert (
        connection.execute(
            """SELECT provider_external_id FROM app.source_collections
           WHERE connection_id=%s ORDER BY provider_external_id""",
            (connection_id,),
        ).fetchall()
        == [("10",), ("8",)]
    )
    assert connection.execute(
        "SELECT count(*) FROM app.audit_events WHERE workspace_id=%s",
        (workspace,),
    ).fetchone() == (1,)
