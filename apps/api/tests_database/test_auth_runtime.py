from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg


def test_registration_verification_and_workspace_bootstrap_are_atomic(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    user_id = uuid4()
    workspace_id = uuid4()
    token_id = uuid4()
    email = f"auth-{uuid4()}@example.test"
    token_hash = secrets.token_bytes(32)
    values = (
        user_id,
        email,
        "$argon2id$v=19$m=65536,t=3,p=1$synthetic$synthetic",
        "Auth Runtime Owner",
        "2026-08-12",
        "en-US",
        token_id,
        token_hash,
        datetime.now(UTC) + timedelta(hours=24),
    )

    created = connection.execute(
        "SELECT * FROM app.auth_register_user(" "%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        values,
    ).fetchone()
    duplicate = connection.execute(
        "SELECT * FROM app.auth_register_user(" "%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            uuid4(),
            email,
            values[2],
            values[3],
            values[4],
            values[5],
            uuid4(),
            secrets.token_bytes(32),
            values[8],
        ),
    ).fetchone()
    login = connection.execute(
        "SELECT * FROM app.auth_login_lookup(%s)", (email.upper(),)
    ).fetchone()
    verified = connection.execute(
        "SELECT * FROM app.auth_verify_email(%s, %s)", (token_hash, workspace_id)
    ).fetchone()
    replay = connection.execute(
        "SELECT * FROM app.auth_verify_email(%s, %s)", (token_hash, uuid4())
    ).fetchone()

    assert created == (True, user_id)
    assert duplicate == (False, None)
    assert (
        login is not None and login[0] == user_id and login[3] == "pending_verification"
    )
    assert (
        verified is not None and verified[0] == user_id and verified[3] == workspace_id
    )
    assert replay is None
    assert connection.execute(
        "SELECT role, status FROM app.workspace_members "
        "WHERE workspace_id = %s AND user_id = %s",
        (workspace_id, user_id),
    ).fetchone() == ("owner", "active")

    connection.execute("SET LOCAL ROLE app_api")
    connection.execute("SELECT set_config('app.user_id', %s, true)", (str(user_id),))
    assert connection.execute(
        "SELECT workspace_id, role FROM app.auth_memberships(%s)", (user_id,)
    ).fetchone() == (workspace_id, "owner")
    assert (
        connection.execute(
            "SELECT * FROM app.auth_memberships(%s)", (uuid4(),)
        ).fetchone()
        is None
    )


def test_auth_functions_are_not_public_and_session_security_columns_exist(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    columns = {
        row[0]
        for row in connection.execute(
            """SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'app' AND table_name = 'sessions'"""
        ).fetchall()
    }
    public_execute = connection.execute(
        """SELECT has_function_privilege(
        'public', 'app.auth_login_lookup(text)', 'EXECUTE')"""
    ).fetchone()
    public_memberships_execute = connection.execute(
        """SELECT has_function_privilege(
        'public', 'app.auth_memberships(uuid)', 'EXECUTE')"""
    ).fetchone()

    assert {"csrf_token_hash", "authorization_version"} <= columns
    assert public_execute == (False,)
    assert public_memberships_execute == (False,)
