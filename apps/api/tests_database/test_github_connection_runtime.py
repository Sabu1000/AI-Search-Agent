from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from conftest import database_dsn
from sqlalchemy.ext.asyncio import create_async_engine
from uas_connector_sdk import RawItem
from uas_connector_sdk.testing import FakeConnector

from universal_ai_search.connections.crypto import EncryptedEnvelope
from universal_ai_search.connections.github import GitHubAuthorizationError
from universal_ai_search.connections.github_event_store import (
    SQLAlchemyGitHubWebhookService,
)
from universal_ai_search.connections.github_store import SQLAlchemyGitHubConnectionStore
from universal_ai_search.connections.github_webhook import (
    GitHubWebhookHint,
    GitHubWebhookWork,
)
from universal_ai_search.sync.github_repository import (
    GitHubSyncClaimError,
    GitHubSyncRepository,
)


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
            github_app_id=7,
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

    runtime = GitHubSyncRepository(database_dsn())
    claim = runtime.claim("github-test")
    assert claim is not None
    target = runtime.load(claim)
    assert target.repository_id in {8, 10}
    doc = await FakeConnector().normalize(
        RawItem(
            external_id="repo:1:gh-test.md",
            payload={"title": "Repository test", "content": "Repository test content"},
        )
    )
    with pytest.raises(GitHubSyncClaimError):
        runtime.commit(claim, target, (doc,))
    doc = doc.model_copy(
        update={"provider_metadata": {"repository_id": target.repository_id}}
    )
    runtime.commit(claim, target, (doc,))
    with pytest.raises(GitHubSyncClaimError):
        runtime.commit(claim, target, (doc,))
    assert (
        connection.execute(
            """SELECT github_verified_at IS NOT NULL FROM app.source_collections
           WHERE id=%s""",
            (target.collection_id,),
        ).fetchone()
        == (True,)
    )
    assert (
        connection.execute(
            """SELECT count(*) FROM app.source_collection_memberships
           WHERE collection_id=%s""",
            (target.collection_id,),
        ).fetchone()
        == (1,)
    )

    connection.execute(
        """UPDATE app.jobs SET available_at = now() - interval '1 minute'
           WHERE job_type = 'sync' AND status = 'pending'
             AND payload ->> 'collection_id' = %s""",
        (str(target.collection_id),),
    )
    connection.commit()
    next_claim = runtime.claim("github-test")
    assert next_claim is not None
    assert runtime.load(next_claim) == target
    runtime.commit(next_claim, target, ())
    assert connection.execute(
        "SELECT state FROM app.sources WHERE connection_id=%s", (connection_id,)
    ).fetchone() == ("deleted",)
    assert (
        connection.execute(
            """SELECT count(*) FROM app.document_versions v JOIN app.sources s
           ON s.id=v.source_id WHERE s.connection_id=%s""",
            (connection_id,),
        ).fetchone()
        == (0,)
    )

    connection.execute(
        """UPDATE app.jobs SET available_at = now() - interval '2 minutes'
           WHERE job_type = 'sync' AND status = 'pending'
             AND payload ->> 'collection_id' = %s""",
        (str(target.collection_id),),
    )
    connection.commit()
    restored = runtime.claim("github-test")
    assert restored is not None
    runtime.commit(restored, target, (doc,))
    assert connection.execute(
        """SELECT state, deleted_at FROM app.sources WHERE connection_id=%s""",
        (connection_id,),
    ).fetchone() == ("active", None)
    assert (
        connection.execute(
            """SELECT count(*) FROM app.jobs WHERE connection_id=%s AND job_type='index'
           AND status = 'pending' AND idempotency_key LIKE 'index-restored:%%'""",
            (connection_id,),
        ).fetchone()
        == (1,)
    )

    repository = 8 if target.repository_id == 8 else 10
    await_conn = create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )
    inbox = SQLAlchemyGitHubWebhookService(await_conn)
    delivery, payload_hash = uuid4(), secrets.token_bytes(32)
    inflight = runtime.claim("github-test")
    assert inflight is not None
    current = runtime.load(inflight)
    try:
        for delivered_id in (delivery, delivery, uuid4()):
            await inbox.record(
                GitHubWebhookHint(
                    delivery_id=delivered_id,
                    payload_hash=payload_hash,
                    installation_id=42,
                    event_name="installation_repositories",
                    action="removed",
                    work=GitHubWebhookWork.INSTALLATION,
                    repository_id=repository,
                )
            )
        assert (
            connection.execute(
                """SELECT github_verified_at, github_authorization_generation
               FROM app.source_collections WHERE id=%s""",
                (target.collection_id,),
            ).fetchone()
            == (None, 2)
        )
        assert connection.execute(
            "SELECT count(*) FROM app.provider_events WHERE connection_id=%s",
            (connection_id,),
        ).fetchone() == (1,)
        with pytest.raises(GitHubSyncClaimError):
            runtime.commit(inflight, current, ())
        runtime.fail(inflight)
    finally:
        await await_conn.dispose()

    connection.execute(
        """UPDATE app.jobs SET available_at = now() - interval '3 minutes'
           WHERE job_type = 'sync' AND status = 'pending'
             AND payload ->> 'collection_id' = %s""",
        (str(target.collection_id),),
    )
    connection.commit()
    removal_claim = runtime.claim("github-test")
    assert removal_claim is not None
    removed_target = runtime.load(removal_claim)
    assert removed_target.collection_id == target.collection_id
    assert removed_target.github_app_id == 7
    runtime.fail(removal_claim, removed_target=removed_target)
    assert connection.execute(
        "SELECT selected, status FROM app.source_collections WHERE id=%s",
        (target.collection_id,),
    ).fetchone() == (False, "removed")
    assert connection.execute(
        "SELECT state FROM app.sources WHERE connection_id=%s", (connection_id,)
    ).fetchone() == ("deleted",)
