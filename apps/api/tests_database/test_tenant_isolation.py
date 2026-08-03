from __future__ import annotations

from uuid import UUID

import psycopg
import pytest

WORKSPACE_ONE = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_TWO = UUID("00000000-0000-4000-8000-000000000002")
USER_ONE = UUID("10000000-0000-4000-8000-000000000001")
USER_TWO = UUID("10000000-0000-4000-8000-000000000002")
CONNECTION_ONE = UUID("20000000-0000-4000-8000-000000000001")
CONNECTION_TWO = UUID("20000000-0000-4000-8000-000000000002")
SOURCE_ONE = UUID("30000000-0000-4000-8000-000000000001")
COLLECTION_TWO = UUID("40000000-0000-4000-8000-000000000002")


def seed_two_workspaces(connection: psycopg.Connection[tuple[object, ...]]) -> None:
    connection.execute("DELETE FROM app.workspaces")
    connection.execute("DELETE FROM app.users")
    connection.execute(
        "INSERT INTO app.users (id, email, full_name, status) "
        "VALUES (%s, %s, %s, 'active'), (%s, %s, %s, 'active')",
        (
            USER_ONE,
            "one@example.test",
            "User One",
            USER_TWO,
            "two@example.test",
            "User Two",
        ),
    )
    connection.execute(
        "INSERT INTO app.workspaces (id, name, plan, status) "
        "VALUES (%s, %s, 'free', 'active'), (%s, %s, 'free', 'active')",
        (WORKSPACE_ONE, "One", WORKSPACE_TWO, "Two"),
    )
    connection.execute(
        """
        INSERT INTO app.workspace_members (workspace_id, user_id, role, status)
        VALUES
            (%s, %s, 'owner', 'active'),
            (%s, %s, 'owner', 'active')
        """,
        (WORKSPACE_ONE, USER_ONE, WORKSPACE_TWO, USER_TWO),
    )
    connection.execute(
        """
        INSERT INTO app.connections (
            id, workspace_id, owner_user_id, provider, display_label, status
        ) VALUES
            (%s, %s, %s, 'local_files', %s, 'active'),
            (%s, %s, %s, 'local_files', %s, 'active')
        """,
        (
            CONNECTION_ONE,
            WORKSPACE_ONE,
            USER_ONE,
            "One files",
            CONNECTION_TWO,
            WORKSPACE_TWO,
            USER_TWO,
            "Two files",
        ),
    )
    connection.execute(
        """
        INSERT INTO app.outbox_events (
            id, workspace_id, aggregate_type, aggregate_id, event_type
        ) VALUES
            (%s, %s, 'workspace', %s, 'workspace.seeded'),
            (%s, %s, 'workspace', %s, 'workspace.seeded')
        """,
        (
            UUID("50000000-0000-4000-8000-000000000001"),
            WORKSPACE_ONE,
            WORKSPACE_ONE,
            UUID("50000000-0000-4000-8000-000000000002"),
            WORKSPACE_TWO,
            WORKSPACE_TWO,
        ),
    )
    connection.commit()


def set_api_context(
    connection: psycopg.Connection[tuple[object, ...]],
    workspace_id: UUID | str,
    user_id: UUID | str,
) -> None:
    connection.execute("SET ROLE app_api")
    connection.execute(
        "SELECT set_config('app.workspace_id', %s, true)", (str(workspace_id),)
    )
    connection.execute("SELECT set_config('app.user_id', %s, true)", (str(user_id),))


def test_api_rls_returns_only_active_member_workspace(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    seed_two_workspaces(connection)
    set_api_context(connection, WORKSPACE_ONE, USER_ONE)

    assert connection.execute("SELECT id FROM app.workspaces").fetchall() == [
        (WORKSPACE_ONE,)
    ]
    assert connection.execute(
        "SELECT workspace_id, user_id FROM app.workspace_members"
    ).fetchall() == [(WORKSPACE_ONE, USER_ONE)]
    assert connection.execute(
        "SELECT workspace_id FROM app.outbox_events"
    ).fetchall() == [(WORKSPACE_ONE,)]


@pytest.mark.parametrize("workspace_context", ["", "not-a-uuid"])
def test_missing_or_malformed_context_fails_closed(
    connection: psycopg.Connection[tuple[object, ...]], workspace_context: str
) -> None:
    seed_two_workspaces(connection)
    connection.execute("SET ROLE app_api")
    connection.execute(
        "SELECT set_config('app.workspace_id', %s, true)", (workspace_context,)
    )
    connection.execute("SELECT set_config('app.user_id', %s, true)", (str(USER_ONE),))

    assert connection.execute("SELECT count(*) FROM app.outbox_events").fetchone() == (
        0,
    )


def test_api_cannot_insert_another_workspace_row(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    seed_two_workspaces(connection)
    set_api_context(connection, WORKSPACE_ONE, USER_ONE)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        connection.execute(
            """
            INSERT INTO app.outbox_events (
                id, workspace_id, aggregate_type, aggregate_id, event_type
            ) VALUES (%s, %s, 'workspace', %s, 'forbidden')
            """,
            (
                UUID("50000000-0000-4000-8000-000000000099"),
                WORKSPACE_TWO,
                WORKSPACE_TWO,
            ),
        )


def test_composite_foreign_key_rejects_cross_connection_membership(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    seed_two_workspaces(connection)
    connection.execute(
        """
        INSERT INTO app.sources (
            id, workspace_id, connection_id, provider, external_id, source_type,
            title, content_hash, permissions_hash, state
        ) VALUES (
            %s, %s, %s, 'local_files', 'file:one', 'file', 'One',
            %s, %s, 'active'
        )
        """,
        (SOURCE_ONE, WORKSPACE_ONE, CONNECTION_ONE, b"content", b"permissions"),
    )
    connection.execute(
        """
        INSERT INTO app.source_collections (
            id, workspace_id, connection_id, provider_external_id, kind, name, status
        ) VALUES (%s, %s, %s, 'folder:two', 'folder', 'Two folder', 'active')
        """,
        (COLLECTION_TWO, WORKSPACE_TWO, CONNECTION_TWO),
    )

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        connection.execute(
            """
            INSERT INTO app.source_collection_memberships (
                workspace_id, connection_id, source_id, collection_id, relationship
            ) VALUES (%s, %s, %s, %s, 'direct')
            """,
            (WORKSPACE_ONE, CONNECTION_ONE, SOURCE_ONE, COLLECTION_TWO),
        )
