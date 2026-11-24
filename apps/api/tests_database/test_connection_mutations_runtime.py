from __future__ import annotations

import secrets
from dataclasses import replace
from uuid import UUID, uuid4

import psycopg
import pytest
from conftest import database_dsn
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from universal_ai_search.connections.github import READ_PERMISSIONS
from universal_ai_search.connections.google import (
    DRIVE_READONLY_SCOPE,
    GMAIL_READONLY_SCOPE,
)
from universal_ai_search.connections.mutations import (
    ConnectionMutationDenied,
    ConnectionMutationKeyConflict,
    ConnectionMutationMissing,
    ConnectionMutationNotSupported,
    ConnectionMutationScope,
    ConnectionMutationStale,
    ConnectionSelectionInvalid,
    SQLAlchemyConnectionMutationStore,
    mutation_etag,
)
from universal_ai_search.sync.github_repository import GitHubSyncRepository
from universal_ai_search.sync.repository import GoogleSyncRepository


def _authority(
    connection: psycopg.Connection[tuple[object, ...]], role: str = "owner"
) -> ConnectionMutationScope:
    workspace, user, session = (uuid4() for _ in range(3))
    connection.execute(
        "INSERT INTO app.users (id,email,full_name,status,email_verified_at) "
        "VALUES (%s,%s,'Fixture','active',now())",
        (user, f"{user}@example.test"),
    )
    connection.execute(
        "INSERT INTO app.workspaces (id,name,plan,status) "
        "VALUES (%s,'Fixture','free','active')",
        (workspace,),
    )
    connection.execute(
        "INSERT INTO app.workspace_members (workspace_id,user_id,role,status) "
        "VALUES (%s,%s,%s,'active')",
        (workspace, user, role),
    )
    connection.execute(
        """INSERT INTO app.sessions
           (id,user_id,family_id,refresh_token_hash,csrf_token_hash,issued_at,expires_at)
           VALUES (%s,%s,%s,%s,%s,now(),now() + interval '10 minutes')""",
        (session, user, uuid4(), secrets.token_bytes(32), secrets.token_bytes(32)),
    )
    connection.commit()
    return ConnectionMutationScope(workspace, user, session, 1, 1)


def _provider_connection(
    connection: psycopg.Connection[tuple[object, ...]],
    scope: ConnectionMutationScope,
    provider: str,
    scopes: list[str],
) -> UUID:
    identifier = uuid4()
    if provider == "github":
        connection.execute(
            """INSERT INTO app.connections
               (id,workspace_id,owner_user_id,provider,display_label,status,
                github_installation_id,github_app_id)
               VALUES (%s,%s,%s,%s,'GitHub','active',42,7)""",
            (identifier, scope.workspace_id, scope.user_id, provider),
        )
    else:
        connection.execute(
            """INSERT INTO app.connections
               (id,workspace_id,owner_user_id,provider,display_label,status,
                credential_ciphertext,encrypted_data_key,key_version)
               VALUES (%s,%s,%s,%s,'Fixture','active',%s,%s,1)""",
            (identifier, scope.workspace_id, scope.user_id, provider, b"fake", b"fake"),
        )
    for grant in scopes:
        connection.execute(
            "INSERT INTO app.connection_scopes (workspace_id,connection_id,scope) "
            "VALUES (%s,%s,%s)",
            (scope.workspace_id, identifier, grant),
        )
    connection.commit()
    return identifier


def _store(
    *, enabled: bool = True
) -> tuple[AsyncEngine, SQLAlchemyConnectionMutationStore]:
    engine = create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )
    return engine, SQLAlchemyConnectionMutationStore(
        engine,
        idempotency_secret=b"a" * 32,
        audit_secret=b"b" * 32,
        google_sync_enabled=enabled,
        github_sync_enabled=enabled,
    )


async def test_sync_remembers_real_job_and_never_substitutes_unsupported_scope(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    scope = _authority(connection)
    outsider = _authority(connection)
    gmail = _provider_connection(connection, scope, "google", [GMAIL_READONLY_SCOPE])
    drive = _provider_connection(connection, scope, "google", [DRIVE_READONLY_SCOPE])
    engine, store = _store()
    try:

        async def sync(
            target: UUID = gmail,
            key: str = "synthetic-sync-1",
            actor: ConnectionMutationScope = scope,
        ):
            return await store.sync(
                scope=actor,
                connection_id=target,
                idempotency_key=key,
                request_id=uuid4(),
            )

        with pytest.raises(ConnectionMutationMissing):
            await sync(actor=outsider)
        with pytest.raises(ConnectionMutationDenied):
            await sync(actor=replace(scope, user_version=2))
        with pytest.raises(ConnectionMutationNotSupported):
            await sync(drive)
        disabled_engine, disabled_store = _store(enabled=False)
        try:
            with pytest.raises(ConnectionMutationNotSupported):
                await disabled_store.sync(
                    scope=scope,
                    connection_id=gmail,
                    idempotency_key="disabled-gmail-sync",
                    request_id=uuid4(),
                )
        finally:
            await disabled_engine.dispose()
        first = await sync()
        assert first == await sync()
        assert first == await sync(key="synthetic-coalesce-key")
        assert (
            connection.execute(
                """SELECT payload, job_type, queue, status FROM app.jobs
               WHERE connection_id = %s""",
                (gmail,),
            ).fetchall()
            == [
                (
                    {"mode": "full", "source_families": ["gmail"]},
                    "sync",
                    "sync",
                    "pending",
                )
            ]
        )
        assert connection.execute(
            "SELECT payload FROM app.outbox_events WHERE aggregate_id=%s",
            (gmail,),
        ).fetchall() == [({"job_id": str(first.id), "source_family": "gmail"},)]
        connection.execute("UPDATE app.jobs SET priority=1000 WHERE id=%s", (first.id,))
        connection.commit()
        worker = GoogleSyncRepository(database_dsn())
        claim = worker.claim("synthetic-manual-gmail")
        assert claim is not None and claim.job_id == first.id
        assert worker.load(claim).payload == {
            "mode": "full",
            "source_families": ["gmail"],
        }
        connection.execute(
            "UPDATE app.sessions SET revoked_at = now() WHERE id=%s",
            (scope.session_id,),
        )
        connection.commit()
        with pytest.raises(ConnectionMutationDenied):
            await sync()  # Replay never returns metadata to an expired session.
    finally:
        await engine.dispose()


async def test_github_narrowing_fences_removed_jobs_and_queues_only_retained_scope(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    scope = _authority(connection)
    unauthorized = _authority(connection, role="member")
    github = _provider_connection(
        connection,
        scope,
        "github",
        [f"{permission}:{level}" for permission, level in READ_PERMISSIONS.items()],
    )
    keep, drop, old_job = (uuid4() for _ in range(3))
    for repo, selection in ((99, keep), (100, drop)):
        connection.execute(
            """INSERT INTO app.source_collections
               (id,workspace_id,connection_id,provider_external_id,
                kind,name,selected,status,github_verified_at)
               VALUES (%s,%s,%s,%s,'repository','Example repository',true,
                       'active',now())""",
            (selection, scope.workspace_id, github, str(repo)),
        )
    connection.execute(
        """INSERT INTO app.jobs
           (id,workspace_id,connection_id,job_type,queue,idempotency_key,status,payload)
           VALUES (%s,%s,%s,'sync','sync','synthetic-old-job','pending',
             jsonb_build_object('mode','github_repository','collection_id',%s::TEXT))""",
        (old_job, scope.workspace_id, github, str(drop)),
    )
    connection.commit()
    engine, store = _store()
    etag = mutation_etag(github, 1)
    try:

        async def narrow(
            selections: tuple[UUID, ...] = (keep,),
            key: str = "synthetic-narrow-1",
            actor: ConnectionMutationScope = scope,
            match: str = etag,
        ):
            return await store.narrow(
                scope=actor,
                connection_id=github,
                collection_ids=selections,
                if_match=match,
                idempotency_key=key,
                request_id=uuid4(),
            )

        with pytest.raises(ConnectionMutationDenied):
            await narrow(actor=unauthorized)
        with pytest.raises(ConnectionMutationNotSupported):
            await store.sync(
                scope=scope,
                connection_id=github,
                idempotency_key="sync-no-broadening",
                request_id=uuid4(),
            )
        with pytest.raises(ConnectionMutationStale):
            await narrow(match=mutation_etag(github, 31))
        with pytest.raises(ConnectionSelectionInvalid):
            await narrow((keep, uuid4()))
        updated = await narrow()
        assert updated.lock_version == 2
        assert [item.id for item in updated.body.selected_collections] == [keep]
        assert (
            connection.execute(
                """SELECT selected, github_verified_at, github_authorization_generation
               FROM app.source_collections WHERE id=%s""",
                (drop,),
            ).fetchone()
            == (False, None, 2)
        )
        assert connection.execute(
            "SELECT status,error_code FROM app.jobs WHERE id=%s",
            (old_job,),
        ).fetchone() == ("failed", "COLLECTION_DESELECTED")
        assert connection.execute(
            "SELECT authorization_version,search_index_generation "
            "FROM app.workspaces WHERE id=%s",
            (scope.workspace_id,),
        ).fetchone() == (2, 2)
        current_scope = replace(scope, workspace_version=2)
        assert await narrow(actor=current_scope) == updated
        with pytest.raises(ConnectionMutationKeyConflict):
            await narrow((drop,), actor=current_scope)
        with pytest.raises(ConnectionSelectionInvalid):
            await narrow(
                (drop,),
                key="other-synthetic-key",
                actor=current_scope,
                match=mutation_etag(github, 2),
            )
        receipt = await store.sync(
            scope=current_scope,
            connection_id=github,
            idempotency_key="synthetic-single-sync",
            request_id=uuid4(),
        )
        assert connection.execute(
            "SELECT id,payload FROM app.jobs WHERE connection_id=%s "
            "AND status='pending'",
            (github,),
        ).fetchall() == [
            (receipt.id, {"mode": "github_repository", "collection_id": str(keep)})
        ]
        connection.execute(
            "UPDATE app.jobs SET priority=1000 WHERE id=%s", (receipt.id,)
        )
        connection.commit()
        worker = GitHubSyncRepository(database_dsn())
        claim = worker.claim("synthetic-manual-github")
        assert claim is not None and claim.job_id == receipt.id
        target = worker.load(claim)
        assert target.repository_id == 99 and target.collection_id == keep
    finally:
        await engine.dispose()
