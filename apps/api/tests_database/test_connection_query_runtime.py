from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import psycopg
from conftest import database_dsn
from sqlalchemy.ext.asyncio import create_async_engine

from universal_ai_search.connections.query import SQLAlchemyConnectionQueryReader

NOW = datetime(2026, 11, 24, tzinfo=UTC)


async def test_connection_projection_and_keyset_are_postgres_tenant_scoped(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    user, workspace, private_workspace, newest, older, private_id = (
        uuid4() for _ in range(6)
    )
    connection.execute(
        "INSERT INTO app.users (id, email, full_name, status) "
        "VALUES (%s, %s, 'Member', 'active')",
        (user, f"member-{user}@example.test"),
    )
    connection.execute(
        "INSERT INTO app.workspaces (id, name, plan, status) "
        "VALUES (%s, 'Allowed Workspace', 'free', 'active'), "
        "(%s, 'Other Workspace', 'free', 'active')",
        (workspace, private_workspace),
    )
    connection.execute(
        "INSERT INTO app.workspace_members (workspace_id, user_id, role, status) "
        "VALUES (%s, %s, 'member', 'active'), (%s, %s, 'member', 'suspended')",
        (workspace, user, private_workspace, user),
    )
    connection.execute(
        """INSERT INTO app.connections
           (id, workspace_id, owner_user_id, provider, display_label, status,
            external_account_id_hash, credential_ciphertext, encrypted_data_key,
            key_version, created_at, last_error_code)
           VALUES (%s, %s, %s, 'github', 'GitHub', 'active',
                   'external'::bytea, 'ciphertext'::bytea, 'wrapped'::bytea, 1, %s,
                   'UNTRUSTED_PROVIDER_VALUE'),
                  (%s, %s, %s, 'local_files', '/Users/private/device', 'pending',
                   NULL, NULL, NULL, NULL, %s - interval '1 day', NULL),
                  (%s, %s, %s, 'google', 'Private label', 'active',
                   NULL, NULL, NULL, NULL, %s, NULL)
        """,
        (
            newest,
            workspace,
            user,
            NOW,
            older,
            workspace,
            user,
            NOW,
            private_id,
            private_workspace,
            user,
            NOW,
        ),
    )
    connection.execute(
        """INSERT INTO app.connection_scopes (workspace_id, connection_id, scope)
           VALUES (%s, %s, 'metadata:read'), (%s, %s, 'contents:write')""",
        (workspace, newest, workspace, newest),
    )
    folder = uuid4()
    connection.execute(
        """INSERT INTO app.source_collections
           (id, workspace_id, connection_id, provider_external_id, kind, name,
            selected, status)
           VALUES (%s, %s, %s, '123456789', 'repository',
                   'example/repo', true, 'active'),
                  (%s, %s, %s, '/Users/private/folder', 'folder',
                   '/Users/private/folder', true, 'active')""",
        (uuid4(), workspace, newest, folder, workspace, older),
    )
    connection.execute(
        """INSERT INTO app.jobs
           (id, workspace_id, connection_id, job_type, queue, idempotency_key,
            status, payload, lease_owner, lease_expires_at)
           VALUES (%s, %s, %s, 'sync', 'sync', %s, 'leased',
                   '{"credential":"private job content"}'::jsonb, 'worker',
                   now() + interval '5 minutes')""",
        (uuid4(), workspace, newest, f"job-{newest}"),
    )
    connection.commit()
    engine = create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )
    reader = SQLAlchemyConnectionQueryReader(engine)
    try:
        request = {"workspace_id": workspace, "user_id": user}
        page = await reader.list_connections(
            **request, provider=None, limit=1, before=None
        )
        assert [row.body.id for row in page] == [newest]
        page = await reader.list_connections(
            **request, provider=None, limit=1, before=(NOW, newest)
        )
        assert [row.body.id for row in page] == [older]
        assert page[0].body.display_label == "Local files"
        assert page[0].body.selected_collections[0].name == "Local folder"
        assert page[0].body.selected_collections[0].id == folder

        row = await reader.get(**request, connection_id=newest)
        assert row is not None
        assert row.body.scopes == ["metadata:read"]
        assert row.body.selected_collections[0].name == "example/repo"
        assert row.body.error_code == "CONNECTION_NEEDS_ATTENTION"
        assert row.body.current_operation is not None
        assert row.body.current_operation.status == "running"
        body = row.body.model_dump_json()
        for hidden in (
            "ciphertext",
            "credential",
            "provider_external_id",
            "123456789",
            "lease_owner",
            "private job content",
            "UNTRUSTED_PROVIDER_VALUE",
        ):
            assert hidden not in body

        assert await reader.get(**request, connection_id=private_id) is None
        assert (
            await reader.get(
                workspace_id=private_workspace, user_id=user, connection_id=private_id
            )
            is None
        )
        assert (
            await reader.list_connections(
                workspace_id=private_workspace,
                user_id=user,
                provider=None,
                limit=1,
                before=None,
            )
            == []
        )
    finally:
        await engine.dispose()
