from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from conftest import database_dsn
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from universal_ai_search.auth.recovery_store import SQLAlchemyRecoveryStore
from universal_ai_search.auth.store import RotationState, SQLAlchemyAuthStore

OLD_HASH = "$argon2id$v=19$old$synthetic"
NEW_HASH = "$argon2id$v=19$new$synthetic"


async def test_recovery_is_single_use_atomic_and_revokes_all_families(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    user_id, stranger_id = uuid4(), uuid4()
    email, stranger_email = (
        f"recover-{user_id}@example.test",
        f"{stranger_id}@test.example",
    )
    for account_id, address in ((user_id, email), (stranger_id, stranger_email)):
        connection.execute(
            """INSERT INTO app.users
               (id, email, password_hash, full_name, status, email_verified_at)
               VALUES (%s, %s, %s, 'Fixture', 'active', clock_timestamp())""",
            (account_id, address, OLD_HASH),
        )
    connection.commit()

    engine = create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )
    recovery = SQLAlchemyRecoveryStore(engine)
    auth = SQLAlchemyAuthStore(engine)
    old, current, verification = (secrets.token_bytes(32) for _ in range(3))

    async def session(account_id: UUID, version: int) -> tuple[bool, UUID, bytes]:
        session_id, refresh_hash = uuid4(), secrets.token_bytes(32)
        accepted = await auth.create_session(
            session_id=session_id,
            user_id=account_id,
            family_id=uuid4(),
            refresh_hash=refresh_hash,
            csrf_hash=secrets.token_bytes(32),
            authorization_version=version,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            device_metadata={},
        )
        return accepted, session_id, refresh_hash

    expires = datetime.now(UTC) + timedelta(minutes=30)
    try:
        ok, first, old_refresh = await session(user_id, 1)
        assert ok
        ok, latest, _ = await session(user_id, 1)
        assert ok
        ok, stranger, _ = await session(stranger_id, 1)
        assert ok
        assert (
            await recovery.issue(
                email="does-not-exist@example.test",
                token_id=uuid4(),
                token_hash=secrets.token_bytes(32),
                expires_at=expires,
            )
            is None
        )
        assert (
            await recovery.issue(
                email=email, token_id=uuid4(), token_hash=old, expires_at=expires
            )
            == email
        )
        assert (
            await recovery.issue(
                email=email, token_id=uuid4(), token_hash=current, expires_at=expires
            )
            == email
        )
        assert await recovery.lookup(token_hash=old) is None
        assert await recovery.lookup(token_hash=current) is not None

        connection.execute(
            """INSERT INTO app.one_time_tokens
               (id, user_id, purpose, token_hash, expires_at)
               VALUES (%s, %s, 'verify_email', %s, %s)""",
            (uuid4(), user_id, verification, expires),
        )
        duplicate_audit = uuid4()
        connection.execute(
            """INSERT INTO app.audit_events
               (id, workspace_ref_hash, action, target_type, outcome)
               VALUES (%s, %s, 'fixture', 'fixture', 'succeeded')""",
            (duplicate_audit, b"a" * 32),
        )
        connection.commit()
        assert await recovery.lookup(token_hash=verification) is None

        async def complete(token_hash: bytes, audit_id: UUID) -> str | None:
            return await recovery.complete(
                token_hash=token_hash,
                user_id=user_id,
                expected_authorization_version=1,
                expected_email=email,
                password_hash=NEW_HASH,
                audit_id=audit_id,
                audit_subject_hash=b"a" * 32,
                request_id=uuid4(),
            )

        assert await complete(verification, uuid4()) is None
        with pytest.raises(IntegrityError):
            await complete(current, duplicate_audit)
        assert await recovery.lookup(token_hash=current) is not None
        assert await auth.validate_session(
            user_id=user_id, session_id=first, authorization_version=1
        )

        results = await asyncio.gather(
            complete(current, uuid4()), complete(current, uuid4())
        )
        assert results.count(email) == 1 and results.count(None) == 1

        await auth.update_password_hash(user_id, OLD_HASH, expected_version=1)
        old_session, _, _ = await session(user_id, 1)
        assert not old_session
        new_session, _, _ = await session(user_id, 2)
        assert new_session
        assert await auth.validate_session(
            user_id=stranger_id, session_id=stranger, authorization_version=1
        )
        assert (
            await auth.validate_session(
                user_id=user_id, session_id=latest, authorization_version=1
            )
            is None
        )
        rotation = await auth.rotate_session(
            refresh_hash=old_refresh,
            presented_csrf_hash=None,
            successor_id=uuid4(),
            successor_refresh_hash=secrets.token_bytes(32),
            successor_csrf_hash=secrets.token_bytes(32),
            expires_at=expires,
        )
        assert rotation.state == RotationState.INVALID
        assert connection.execute(
            "SELECT password_hash, lock_version FROM app.users WHERE id = %s",
            (user_id,),
        ).fetchone() == (NEW_HASH, 2)
        assert (
            connection.execute(
                """SELECT count(*) FROM app.sessions WHERE user_id = %s
               AND authorization_version = 1 AND revoked_at IS NOT NULL""",
                (user_id,),
            ).fetchone()
            == (2,)
        )
        assert (
            connection.execute(
                """SELECT action, workspace_id, target_type, outcome, metadata
               FROM app.audit_events WHERE actor_user_id = %s""",
                (user_id,),
            ).fetchall()
            == [("auth.password_reset", None, "user", "succeeded", {})]
        )
        assert connection.execute(
            "SELECT has_function_privilege("
            "'public', 'app.auth_lookup_password_reset(bytea)', 'EXECUTE')"
        ).fetchone() == (False,)
    finally:
        await engine.dispose()
