from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from conftest import database_dsn
from sqlalchemy.ext.asyncio import create_async_engine
from test_conversation_repository import seed_user
from test_reauthentication_runtime import AllowSyntheticAttempts

from universal_ai_search.auth.reauth import (
    ReauthenticationFailed,
    ReauthenticationService,
)
from universal_ai_search.auth.reauth_store import ReauthenticationStore, ReauthScope
from universal_ai_search.auth.security import PasswordSecurity
from universal_ai_search.connections.disconnect import (
    ConnectionDisconnectStore,
    DisconnectConflict,
    DisconnectMissing,
    DisconnectUnsupported,
)
from universal_ai_search.sync.github_repository import (
    GitHubSyncClaimError,
    GitHubSyncRepository,
)


@pytest.mark.parametrize("start_worker", [False, True])
async def test_disconnect_keeps_other_workspace_installation_intact(
    connection: psycopg.Connection[tuple[object, ...]], start_worker: bool
) -> None:
    workspace, other, target, peer, collection, job = (uuid4() for _ in range(6))
    connection.execute(
        """INSERT INTO app.workspaces (id,name,plan,status)
           VALUES (%s,'Disconnect Workspace','free','active'),
                  (%s,'Other Disconnect Workspace','free','active')""",
        (workspace, other),
    )
    actor = seed_user(connection, workspace)
    outsider = seed_user(connection, other)
    connection.execute(
        "UPDATE app.workspace_members SET role = 'owner' WHERE user_id IN (%s, %s)",
        (actor.user_id, outsider.user_id),
    )
    security = PasswordSecurity()
    password = "Only synthetic Disconnect 2@!"
    connection.execute(
        "UPDATE app.users SET password_hash = %s WHERE id = %s",
        (security.hash(password), actor.user_id),
    )
    installation = target.int % (2**63 - 1)
    connection.execute(
        """INSERT INTO app.connections (id,workspace_id,owner_user_id,provider,
                 github_installation_id,github_app_id,display_label,status)
           VALUES (%s,%s,%s,'github',%s,17,'Fixture GitHub','pending'),
                  (%s,%s,%s,'github',%s,17,'Other Fixture GitHub','active')""",
        (
            target,
            workspace,
            actor.user_id,
            installation,
            peer,
            other,
            outsider.user_id,
            installation,
        ),
    )
    connection.execute(
        """INSERT INTO app.connection_scopes (workspace_id,connection_id,scope)
           VALUES (%s,%s,'contents:read'),(%s,%s,'contents:read')""",
        (workspace, target, other, peer),
    )
    connection.execute(
        """INSERT INTO app.source_collections
             (id,workspace_id,connection_id,provider_external_id,kind,name,
              selected,status)
           VALUES (%s,%s,%s,'123','repository','Fixture',true,'active')""",
        (collection, workspace, target),
    )
    connection.execute(
        """INSERT INTO app.jobs (id,workspace_id,connection_id,job_type,queue,
                 idempotency_key,status,payload,priority)
           VALUES (%s,%s,%s,'sync','sync','synthetic','pending',
                   jsonb_build_object('mode','github_repository',
                                      'collection_id',%s::TEXT), 1000)""",
        (job, workspace, target, collection),
    )
    source = uuid4()
    if start_worker:
        connection.execute(
            """INSERT INTO app.sources (
                   id,workspace_id,connection_id,provider,external_id,source_type,
                   title,state,content_hash,permissions_hash)
               VALUES (%s,%s,%s,'github','synthetic-local-source','code',
                       'Synthetic retained bytes','active',%s,%s)""",
            (source, workspace, target, bytes(32), bytes(32)),
        )
    connection.commit()

    worker = GitHubSyncRepository(database_dsn())
    claim = worker.claim("synthetic-disconnect") if start_worker else None
    if start_worker:
        assert claim is not None and claim.job_id == job
        assert worker.load(claim).collection_id == collection
    engine = create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )
    auth = ReauthenticationService(
        store=ReauthenticationStore(engine),
        security=security,
        limiter=AllowSyntheticAttempts(),
        hash_key=b"s" * 32,
    )
    store = ConnectionDisconnectStore(
        engine,
        reauth_service=auth,
        idempotency_secret=b"i" * 32,
        audit_secret=b"a" * 32,
    )
    scope = ReauthScope(actor.user_id, actor.session_id, workspace, 1, 1)
    try:
        wrong_proof = await auth.obtain(
            scope=scope, purpose="export", password=password, request_id=uuid4()
        )
        with pytest.raises(ReauthenticationFailed):
            await store.delete(
                scope=scope,
                connection_id=target,
                reauth_token=wrong_proof.token,
                idempotency_key="fixture-disconnect",
                request_id=uuid4(),
            )
        proof = await auth.obtain(
            scope=scope, purpose="disconnect", password=password, request_id=uuid4()
        )

        async def remove(
            *,
            current_scope: ReauthScope = scope,
            identifier=target,
            key: str = "fixture-disconnect",
            pending: bool = False,
        ):
            return await store.delete(
                scope=current_scope,
                connection_id=identifier,
                reauth_token=proof.token,
                idempotency_key=key,
                request_id=uuid4(),
                allow_pending=pending,
            )

        with pytest.raises(DisconnectMissing):
            await remove(identifier=peer)
        if start_worker:
            with pytest.raises(DisconnectUnsupported):
                await remove()
            assert connection.execute(
                "SELECT status FROM app.connections WHERE id = %s", (target,)
            ).fetchone() == ("pending",)
            assert (
                connection.execute(
                    "SELECT 1 FROM app.deletion_requests WHERE target_id = %s",
                    (target,),
                ).fetchone()
                is None
            )

        receipt = await remove(pending=start_worker)
        assert receipt.deadline_at - receipt.requested_at == timedelta(days=1)
        assert receipt.status == ("pending" if start_worker else "completed")
        assert (receipt.completed_at is None) is start_worker
        refreshed_scope = replace(scope, workspace_version=2)
        with pytest.raises(DisconnectMissing):
            await remove()
        assert (
            await remove(current_scope=refreshed_scope, pending=start_worker) == receipt
        )
        assert await store.read(scope=refreshed_scope, receipt_id=receipt.id) == receipt
        with pytest.raises(DisconnectConflict):
            await remove(
                current_scope=refreshed_scope, identifier=peer, pending=start_worker
            )
        with pytest.raises(ReauthenticationFailed):
            await remove(
                current_scope=refreshed_scope,
                key="fixture-another-key",
                pending=start_worker,
            )
        if claim is not None:
            with pytest.raises(GitHubSyncClaimError):
                worker.load(claim)
            assert connection.execute(
                "SELECT title FROM app.sources WHERE id = %s", (source,)
            ).fetchone() == ("Synthetic retained bytes",)
            assert (
                connection.execute(
                    """SELECT status,lease_owner,lease_expires_at FROM app.jobs
                   WHERE id = %s""",
                    (job,),
                ).fetchone()
                == ("failed", None, None)
            )
            assert (
                connection.execute(
                    """SELECT selected,status,github_verified_at
                       FROM app.source_collections
                   WHERE id = %s""",
                    (collection,),
                ).fetchone()
                == (False, "deleting", None)
            )
        else:
            assert (
                connection.execute(
                    "SELECT id FROM app.jobs WHERE id = %s",
                    (job,),
                ).fetchone()
                is None
            )
            assert (
                connection.execute(
                    "SELECT id FROM app.source_collections WHERE id = %s",
                    (collection,),
                ).fetchone()
                is None
            )
        assert (
            connection.execute(
                """SELECT status,github_installation_id,github_app_id,
                   credential_ciphertext
               FROM app.connections WHERE id = %s""",
                (target,),
            ).fetchone()
            == (
                "deleting" if start_worker else "deleted",
                installation if start_worker else None,
                None,
                None,
            )
        )
        assert (
            connection.execute(
                """SELECT status,github_installation_id,github_app_id
               FROM app.connections WHERE id = %s""",
                (peer,),
            ).fetchone()
            == ("active", installation, 17)
        )
        assert (
            connection.execute(
                "SELECT scope FROM app.connection_scopes WHERE connection_id = %s",
                (target,),
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                """SELECT count(*) FROM app.audit_events
               WHERE workspace_id = %s AND action = 'connection.disconnect'""",
                (workspace,),
            ).fetchone()
            == (1,)
        )
        assert (
            await store.read(
                scope=ReauthScope(outsider.user_id, outsider.session_id, other, 1, 1),
                receipt_id=receipt.id,
            )
            is None
        )
    finally:
        await engine.dispose()
