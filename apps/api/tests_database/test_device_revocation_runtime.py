from __future__ import annotations

import secrets
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest
from conftest import database_dsn
from sqlalchemy.ext.asyncio import create_async_engine
from uas_connector_sdk import Provider, RawItem
from uas_connector_sdk.testing import FakeConnector

from universal_ai_search.devices.registration import RegistrationScope
from universal_ai_search.devices.revocation import (
    DeviceCleanupUnsupported,
    DeviceRevocationConflict,
    DeviceRevocationRejected,
    DeviceRevocationStore,
)
from universal_ai_search.indexing.pipeline import IndexingPipeline
from universal_ai_search.indexing.repository import IndexRepository
from universal_ai_search.indexing.runtime import IndexingRuntime


def seed_devices(
    conn: psycopg.Connection[tuple[object, ...]],
) -> tuple[RegistrationScope, UUID, UUID, UUID, UUID, bytes, bytes]:
    workspace, user, session, first, second, first_conn, second_conn = (
        uuid4() for _ in range(7)
    )
    public_key, credential = secrets.token_bytes(32), secrets.token_bytes(32)
    conn.execute(
        """INSERT INTO app.users (id,email,full_name,status,email_verified_at)
           VALUES (%s,%s,'Synthetic Owner','active',now())""",
        (user, f"device-revoke-{user}@example.test"),
    )
    conn.execute(
        """INSERT INTO app.workspaces (id,name,plan,status)
           VALUES (%s,'Synthetic Workspace','free','active')""",
        (workspace,),
    )
    conn.execute(
        """INSERT INTO app.workspace_members (workspace_id,user_id,status,role)
           VALUES (%s,%s,'active','owner')""",
        (workspace, user),
    )
    conn.execute(
        "INSERT INTO app.workspace_usage (workspace_id) VALUES (%s)", (workspace,)
    )
    conn.execute(
        """INSERT INTO app.sessions
           (id,user_id,refresh_token_hash,family_id,issued_at,expires_at,
            csrf_token_hash,authorization_version)
           VALUES (%s,%s,%s,%s,now(),now()+interval '20 minutes',%s,1)""",
        (session, user, secrets.token_bytes(32), uuid4(), secrets.token_bytes(32)),
    )
    for device_id, connection_id, key, secret in (
        (first, first_conn, public_key, credential),
        (second, second_conn, secrets.token_bytes(32), secrets.token_bytes(32)),
    ):
        conn.execute(
            """INSERT INTO app.connections
               (id, workspace_id, owner_user_id, provider, display_label, status)
               VALUES (%s,%s,%s,'local_files','Local files','active')""",
            (connection_id, workspace, user),
        )
        conn.execute(
            """INSERT INTO app.devices
               (id, workspace_id, user_id, connection_id, name, platform,
                app_version, public_key, credential_hash, status)
               VALUES (%s,%s,%s,%s,'Synthetic Device','macos','1',%s,%s,'active')""",
            (device_id, workspace, user, connection_id, key, secret),
        )
    conn.commit()
    return (
        RegistrationScope(workspace, user, session, 1, 1),
        first,
        second,
        first_conn,
        second_conn,
        public_key,
        credential,
    )


async def indexed(scope: RegistrationScope, connection_id: UUID, suffix: str):
    document = (
        await FakeConnector().normalize(
            RawItem(
                external_id=f"private-document-{suffix}",
                payload={
                    "title": "Fixture private document",
                    "content": "Synthetic normalized private content for deletion.",
                },
            )
        )
    ).model_copy(update={"provider": Provider.LOCAL_FILES})
    repository = IndexRepository(database_dsn())
    work = repository.enqueue(scope.workspace_id, connection_id, document)
    assert work.job_id is not None
    assert IndexingRuntime(repository, IndexingPipeline()).run_once(
        "device-revoke-test"
    )
    return work


def revocation_store():
    engine = create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )
    return engine, DeviceRevocationStore(
        engine, idempotency_secret=b"i" * 32, audit_secret=b"a" * 32
    )


async def test_confirmed_revoke_purges_index_and_replays_completed_receipt(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    scope, first, second, first_conn, second_conn, public_key, credential = (
        seed_devices(connection)
    )
    indexed_first = await indexed(scope, first_conn, "first")
    indexed_second = await indexed(scope, second_conn, "second")
    assert connection.execute(
        "SELECT count(*) FROM app.chunks WHERE document_version_id=%s",
        (indexed_first.document_version_id,),
    ).fetchone() == (1,)
    conversation, user_message, answer, claim = (uuid4() for _ in range(4))
    connection.execute(
        "INSERT INTO app.conversations (id, workspace_id, user_id, title, status) "
        "VALUES (%s,%s,%s,'Fixture conversation','active')",
        (conversation, scope.workspace_id, scope.user_id),
    )
    connection.execute(
        "INSERT INTO app.messages "
        "(id,workspace_id,conversation_id,role,status,content) "
        "VALUES (%s,%s,%s,'user','complete','fixture request'), "
        "(%s,%s,%s,'assistant','complete','quoted synthetic private content')",
        (
            user_message,
            scope.workspace_id,
            conversation,
            answer,
            scope.workspace_id,
            conversation,
        ),
    )
    connection.execute(
        "INSERT INTO app.message_claims "
        "(id,workspace_id,message_id,claim_index,text,material) "
        "VALUES (%s,%s,%s,0,'fixture claim',true)",
        (claim, scope.workspace_id, answer),
    )
    connection.execute(
        "INSERT INTO app.citations "
        "(id,workspace_id,message_id,claim_id,source_id,document_version_id,"
        "chunk_id,citation_index,excerpt) "
        "SELECT %s,%s,%s,%s,%s,%s,id,0,'fixture excerpt' FROM app.chunks "
        "WHERE workspace_id=%s AND document_version_id=%s",
        (
            uuid4(),
            scope.workspace_id,
            answer,
            claim,
            indexed_first.source_id,
            indexed_first.document_version_id,
            scope.workspace_id,
            indexed_first.document_version_id,
        ),
    )
    connection.commit()
    engine, store = revocation_store()
    params = {
        "scope": scope,
        "device_id": first,
        "idempotency_key": "fixture-device-delete-1",
        "request_id": uuid4(),
    }
    try:
        receipt = await store.delete(**params)
        assert await store.delete(**params) == receipt
        assert await store.read(scope=scope, receipt_id=receipt.id) == receipt
        with pytest.raises(DeviceRevocationConflict):
            await store.delete(**{**params, "device_id": second})
        with pytest.raises(DeviceRevocationRejected):
            await store.delete(
                **{
                    **params,
                    "device_id": uuid4(),
                    "idempotency_key": "fixture-new-delete",
                }
            )
    finally:
        await engine.dispose()

    observed = connection.execute(
        """SELECT status, public_key, credential_hash, revoked_at, name
           FROM app.devices WHERE id=%s""",
        (first,),
    ).fetchone()
    assert observed is not None and observed[0] == "revoked"
    assert observed[1] != public_key
    assert observed[2] != credential
    assert isinstance(observed[3], datetime)
    assert observed[3] <= datetime.now(UTC)
    assert observed[4] == "Revoked device"
    assert connection.execute(
        "SELECT status FROM app.connections WHERE id=%s", (first_conn,)
    ).fetchone() == ("deleted",)
    assert connection.execute(
        "SELECT count(*) FROM app.sources WHERE id=%s", (indexed_first.source_id,)
    ).fetchone() == (0,)
    assert connection.execute(
        "SELECT count(*) FROM app.chunks WHERE document_version_id=%s",
        (indexed_first.document_version_id,),
    ).fetchone() == (0,)
    assert connection.execute(
        "SELECT count(*) FROM app.jobs WHERE id=%s", (indexed_first.job_id,)
    ).fetchone() == (0,)
    assert connection.execute(
        "SELECT count(*) FROM app.sources WHERE id=%s", (indexed_second.source_id,)
    ).fetchone() == (1,)
    assert connection.execute(
        "SELECT id FROM app.messages WHERE conversation_id=%s", (conversation,)
    ).fetchall() == [(user_message,)]
    assert (
        connection.execute(
            "SELECT id FROM app.citations WHERE claim_id=%s", (claim,)
        ).fetchall()
        == []
    )

    assert connection.execute(
        "SELECT status FROM app.devices WHERE id=%s", (second,)
    ).fetchone() == ("active",)
    assert connection.execute(
        "SELECT indexed_source_count FROM app.workspace_usage WHERE workspace_id=%s",
        (scope.workspace_id,),
    ).fetchone() == (1,)
    assert connection.execute(
        "SELECT count(*) FROM app.deletion_requests WHERE target_id=%s", (first,)
    ).fetchone() == (1,)
    assert connection.execute(
        "SELECT count(*) FROM app.audit_events WHERE workspace_id=%s AND action=%s",
        (scope.workspace_id, "device.revoked"),
    ).fetchone() == (1,)


async def test_external_blob_refuses_without_partial_mutation(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    scope, device, _, connection_id, _, _, credential = seed_devices(connection)
    work = await indexed(scope, connection_id, "external")
    connection.execute(
        "UPDATE app.document_versions SET object_storage_key='external/object-fixture' "
        "WHERE id=%s",
        (work.document_version_id,),
    )
    connection.commit()
    engine, store = revocation_store()
    try:
        with pytest.raises(DeviceCleanupUnsupported):
            await store.delete(
                scope=scope,
                device_id=device,
                idempotency_key="fixture-external-cleanup",
                request_id=uuid4(),
            )
    finally:
        await engine.dispose()
    assert connection.execute(
        "SELECT status,credential_hash FROM app.devices WHERE id=%s", (device,)
    ).fetchone() == ("active", credential)
    assert connection.execute(
        "SELECT count(*) FROM app.deletion_requests WHERE target_id=%s", (device,)
    ).fetchone() == (0,)
