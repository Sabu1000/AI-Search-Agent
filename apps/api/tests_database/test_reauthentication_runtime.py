from __future__ import annotations

import asyncio
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from conftest import database_dsn
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_conversation_repository import seed_user

from universal_ai_search.auth.reauth import (
    ReauthenticationFailed,
    ReauthenticationService,
)
from universal_ai_search.auth.reauth_store import ReauthenticationStore, ReauthScope
from universal_ai_search.auth.security import PasswordSecurity


class AllowSyntheticAttempts:
    async def allow(self, *, user_key: bytes) -> bool:
        return len(user_key) == 32


class RollbackSyntheticAction(Exception):
    pass


async def test_reauthentication_is_purpose_locked_and_transactionally_single_use(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    primary, other = uuid4(), uuid4()
    connection.execute(
        """INSERT INTO app.workspaces (id,name,plan,status)
           VALUES (%s,'Reauth Workspace','free','active'),
                  (%s,'Other Reauth Workspace','free','active')""",
        (primary, other),
    )
    seeded = seed_user(connection, primary)
    second_session = uuid4()
    connection.execute(
        """INSERT INTO app.sessions
             (id,user_id,refresh_token_hash,family_id,issued_at,expires_at,csrf_token_hash)
           VALUES (%s,%s,%s,%s,now(),now() + interval '10 minutes',%s)""",
        (
            second_session,
            seeded.user_id,
            secrets.token_bytes(32),
            uuid4(),
            secrets.token_bytes(32),
        ),
    )
    connection.execute(
        """INSERT INTO app.workspace_members (workspace_id,user_id,role,status)
           VALUES (%s,%s,'owner','active')""",
        (other, seeded.user_id),
    )
    security = PasswordSecurity()
    password = "Only synthetic input 3@-!b"
    connection.execute(
        "UPDATE app.users SET password_hash = %s WHERE id = %s",
        (security.hash(password), seeded.user_id),
    )
    connection.commit()

    engine = create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )
    store = ReauthenticationStore(engine)
    auth = ReauthenticationService(
        store=store,
        security=security,
        limiter=AllowSyntheticAttempts(),
        hash_key=b"s" * 32,
    )
    scope = ReauthScope(seeded.user_id, seeded.session_id, primary, 1, 1)

    async def issue_export() -> str:
        return (
            await auth.obtain(
                scope=scope, purpose="export", password=password, request_id=uuid4()
            )
        ).token

    async def try_consume(token: str) -> bool:
        try:
            async with engine.begin() as transaction:
                await auth.consume(
                    transaction, scope=scope, token=token, purpose="export"
                )
        except ReauthenticationFailed:
            return False
        return True

    try:
        assert await store.password_hash(scope=scope, purpose="delete_account") is None
        assert await store.password_hash(scope=scope, purpose="disconnect") is None
        assert await store.password_hash(
            scope=replace(scope, workspace_id=other), purpose="delete_account"
        )

        with pytest.raises(ReauthenticationFailed):
            await auth.obtain(
                scope=scope,
                purpose="export",
                password="wrong password",
                request_id=uuid4(),
            )
        since = datetime.now(UTC)
        proof = await auth.obtain(
            scope=scope, purpose="export", password=password, request_id=uuid4()
        )
        assert since < proof.expires_at <= datetime.now(UTC) + timedelta(minutes=5)
        record = connection.execute(
            """SELECT token_hash,reauth_session_id,reauth_workspace_id,reauth_purpose,
                      expires_at-created_at,consumed_at
               FROM app.one_time_tokens
               WHERE user_id=%s AND purpose='reauthenticate'""",
            (scope.user_id,),
        ).fetchone()
        assert record is not None
        assert isinstance(record[0], bytes) and len(record[0]) == 32
        assert proof.token.encode() not in record[0]
        assert record[1:] == (
            scope.session_id,
            primary,
            "export",
            timedelta(minutes=5),
            None,
        )
        connection.commit()

        async with engine.begin() as wrong_workspace:
            await wrong_workspace.execute(
                text(
                    """SELECT set_config('app.workspace_id', :workspace, true),
                              set_config('app.user_id', :user, true)"""
                ),
                {"workspace": str(other), "user": str(scope.user_id)},
            )
            assert (
                await wrong_workspace.scalar(
                    text(
                        "SELECT count(*) FROM app.one_time_tokens "
                        "WHERE purpose='reauthenticate'"
                    )
                )
                == 0
            )
        for wrong_scope, wrong_purpose in (
            (replace(scope, workspace_id=other), "export"),
            (replace(scope, session_id=second_session), "export"),
            (replace(scope, workspace_id=other), "delete_account"),
        ):
            async with engine.begin() as transaction:
                with pytest.raises(ReauthenticationFailed):
                    await auth.consume(
                        transaction,
                        scope=wrong_scope,
                        purpose=wrong_purpose,
                        token=proof.token,
                    )

        with pytest.raises(RollbackSyntheticAction):
            async with engine.begin() as transaction:
                await auth.consume(
                    transaction, scope=scope, purpose="export", token=proof.token
                )
                raise RollbackSyntheticAction
        assert sorted(
            await asyncio.gather(try_consume(proof.token), try_consume(proof.token))
        ) == [False, True]

        expired = await issue_export()
        connection.execute(
            """UPDATE app.one_time_tokens SET expires_at=now() - INTERVAL '1 minute',
                      created_at=now() - INTERVAL '6 minutes'
               WHERE user_id=%s AND purpose='reauthenticate' AND consumed_at IS NULL""",
            (scope.user_id,),
        )
        connection.commit()
        assert not await try_consume(expired)

        stale = await issue_export()
        connection.execute(
            """UPDATE app.sessions SET revoked_at=now() WHERE id=%s""",
            (scope.session_id,),
        )
        connection.commit()
        assert not await try_consume(stale)
        assert (
            connection.execute(
                """SELECT count(*) FROM app.audit_events
               WHERE workspace_id=%s AND action='auth.reauthenticate'""",
                (primary,),
            ).fetchone()
            == (3,)
        )
    finally:
        await engine.dispose()
