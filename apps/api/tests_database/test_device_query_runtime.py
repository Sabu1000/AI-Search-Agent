from __future__ import annotations

import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from conftest import database_dsn
from sqlalchemy.ext.asyncio import create_async_engine

from universal_ai_search.devices.query import (
    DeviceQueryDenied,
    SQLAlchemyDeviceQueryReader,
)
from universal_ai_search.devices.registration import RegistrationScope


async def test_device_reads_require_device_owner_and_current_tenant_session(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    workspace, other_workspace, owner, member, session = (uuid4() for _ in range(5))
    for person in (owner, member):
        connection.execute(
            """INSERT INTO app.users (id, email, full_name, status, email_verified_at)
               VALUES (%s, %s, 'Synthetic User', 'active', now())""",
            (person, f"device-query-{person}@example.test"),
        )
    for tenant in (workspace, other_workspace):
        connection.execute(
            """INSERT INTO app.workspaces (id, name, plan, status)
               VALUES (%s, 'Synthetic Workspace', 'free', 'active')""",
            (tenant,),
        )
        for person in (owner, member):
            connection.execute(
                """INSERT INTO app.workspace_members
                   (workspace_id, user_id, role, status)
                   VALUES (%s, %s, %s, 'active')""",
                (tenant, person, "owner" if person == owner else "member"),
            )
    connection.execute(
        """INSERT INTO app.sessions
           (id, user_id, refresh_token_hash, family_id, issued_at, expires_at,
            csrf_token_hash, authorization_version)
           VALUES (%s, %s, %s, %s, now(), now() + interval '20 minutes', %s, 1)""",
        (session, owner, secrets.token_bytes(32), uuid4(), secrets.token_bytes(32)),
    )

    now = datetime.now(UTC)
    newer, older, colleague, cross_workspace = (uuid4() for _ in range(4))
    for device, tenant, person, label, moment in (
        (newer, workspace, owner, "/Users/private/raw/path", now),
        (older, workspace, owner, "Travel Windows", now - timedelta(seconds=1)),
        (colleague, workspace, member, "Another person’s device", now),
        (cross_workspace, other_workspace, owner, "Other workspace", now),
    ):
        connection_id = uuid4()
        connection.execute(
            """INSERT INTO app.connections
               (id, workspace_id, owner_user_id, provider, display_label, status)
               VALUES (%s, %s, %s, 'local_files', 'Local device', 'active')""",
            (connection_id, tenant, person),
        )
        connection.execute(
            """INSERT INTO app.devices
               (id, workspace_id, user_id, connection_id, name, platform, app_version,
                public_key, credential_hash, status, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, '1.0', %s, %s, 'active', %s)""",
            (
                device,
                tenant,
                person,
                connection_id,
                label,
                "windows" if device == older else "macos",
                secrets.token_bytes(32),
                secrets.token_bytes(32),
                moment,
            ),
        )
    connection.commit()

    engine = create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )
    reader = SQLAlchemyDeviceQueryReader(engine)
    scope = RegistrationScope(workspace, owner, session, 1, 1)
    try:
        page = await reader.list_devices(scope=scope, before=None, limit=1)
        assert [entry.id for entry in page] == [newer]
        assert page[0].name == "Mac device"
        next_page = await reader.list_devices(scope=scope, before=(now, newer), limit=2)
        assert [entry.id for entry in next_page] == [older]
        assert next_page[0].name == "Travel Windows"
        assert await reader.get(scope=scope, device_id=colleague) is None
        assert await reader.get(scope=scope, device_id=cross_workspace) is None
        assert await reader.get(scope=scope, device_id=older) == next_page[0]

        with pytest.raises(DeviceQueryDenied):
            await reader.get(scope=replace(scope, session_id=uuid4()), device_id=older)
        with pytest.raises(DeviceQueryDenied):
            await reader.list_devices(
                scope=replace(scope, workspace_authorization_version=2),
                before=None,
                limit=1,
            )
        connection.execute(
            "UPDATE app.sessions SET revoked_at=now() WHERE id=%s", (session,)
        )
        connection.commit()
        with pytest.raises(DeviceQueryDenied):
            await reader.get(scope=scope, device_id=older)
    finally:
        await engine.dispose()
