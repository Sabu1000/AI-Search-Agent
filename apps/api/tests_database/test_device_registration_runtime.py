from __future__ import annotations

import asyncio
import base64
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from conftest import database_dsn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from universal_ai_search.devices.registration import (
    DeviceRegistrationRateLimited,
    DeviceRegistrationRejected,
    DeviceRegistrationService,
    RegisteredDevice,
    RegistrationChallenge,
    RegistrationScope,
    hash_device_credential,
)
from universal_ai_search.devices.registration_store import (
    SQLAlchemyDeviceRegistrationStore,
)

_HASH_KEY = b"d" * 32


def _scope(connection: psycopg.Connection[tuple[object, ...]]) -> RegistrationScope:
    user_id, workspace_id, session_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    connection.execute(
        """INSERT INTO app.users (id, email, full_name, status, email_verified_at)
           VALUES (%s, %s, 'Device Owner', 'active', %s)""",
        (user_id, f"device-{user_id}@example.test", now),
    )
    connection.execute(
        """INSERT INTO app.workspaces (id, name, plan, status)
           VALUES (%s, 'Device Test', 'free', 'active')""",
        (workspace_id,),
    )
    connection.execute(
        """INSERT INTO app.workspace_members (workspace_id, user_id, role, status)
           VALUES (%s, %s, 'owner', 'active')""",
        (workspace_id, user_id),
    )
    connection.execute(
        """INSERT INTO app.sessions (
            id, user_id, refresh_token_hash, family_id, issued_at, expires_at,
            csrf_token_hash, authorization_version
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 1)""",
        (
            session_id,
            user_id,
            secrets.token_bytes(32),
            uuid4(),
            now,
            now + timedelta(hours=1),
            secrets.token_bytes(32),
        ),
    )
    connection.commit()
    return RegistrationScope(workspace_id, user_id, session_id, 1, 1)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


async def _complete(
    service: DeviceRegistrationService,
    scope: RegistrationScope,
    challenge: RegistrationChallenge,
    key: Ed25519PrivateKey,
) -> RegisteredDevice:
    public_key = _b64url(key.public_key().public_bytes_raw())
    signature = key.sign(
        (
            f"uas-device-registration-v1\n{challenge.challenge_id}\n"
            f"{challenge.challenge}\n{public_key}"
        ).encode("ascii")
    )
    return await service.complete(
        scope=scope,
        challenge_id=challenge.challenge_id,
        public_key=public_key,
        signature=_b64url(signature),
        platform="macos",
        app_version="0.0.1+synthetic",
        display_name="Synthetic desktop",
    )


def _expire(
    connection: psycopg.Connection[tuple[object, ...]], challenge_id: UUID
) -> None:
    connection.execute(
        """WITH frozen AS (SELECT clock_timestamp() AS ts)
        UPDATE app.device_registration_challenges
        SET issued_at = frozen.ts - INTERVAL '61 seconds',
            expires_at = frozen.ts - INTERVAL '1 second'
        FROM frozen WHERE id = %s""",
        (challenge_id,),
    )
    connection.commit()


def _engine() -> AsyncEngine:
    return create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )


@pytest.mark.asyncio
async def test_device_challenge_is_scope_bound_one_use_and_never_stores_credential(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    scope = _scope(connection)
    foreign = _scope(connection)
    engine = _engine()
    store = SQLAlchemyDeviceRegistrationStore(engine, audit_key=b"a" * 32)
    service = DeviceRegistrationService(store, hash_key=_HASH_KEY)
    try:
        challenge = await service.issue(scope)
        with pytest.raises(DeviceRegistrationRateLimited):
            await service.issue(scope)
        with pytest.raises(DeviceRegistrationRejected):
            await service.issue(replace(scope, session_id=foreign.session_id))
        key = Ed25519PrivateKey.generate()
        with pytest.raises(DeviceRegistrationRejected):
            await _complete(service, foreign, challenge, key)
        results = await asyncio.gather(
            _complete(service, scope, challenge, key),
            _complete(service, scope, challenge, key),
            return_exceptions=True,
        )
        succeeded = [item for item in results if isinstance(item, RegisteredDevice)]
        rejected = [
            item for item in results if isinstance(item, DeviceRegistrationRejected)
        ]
        assert len(succeeded) == len(rejected) == 1
        grant = succeeded[0]
        row = connection.execute(
            """SELECT d.credential_hash, d.workspace_id, d.status, d.public_key,
                      c.provider, c.status, c.credential_ciphertext
               FROM app.devices AS d JOIN app.connections AS c ON c.id = d.connection_id
               WHERE d.id = %s""",
            (grant.device_id,),
        ).fetchone()
        assert row == (
            hash_device_credential(grant.device_credential, _HASH_KEY),
            scope.workspace_id,
            "active",
            key.public_key().public_bytes_raw(),
            "local_files",
            "pending",
            None,
        )
        challenge_row = connection.execute(
            """SELECT consumed_at IS NOT NULL,
                      EXTRACT(EPOCH FROM expires_at - issued_at)
               FROM app.device_registration_challenges WHERE id = %s""",
            (challenge.challenge_id,),
        ).fetchone()
        assert challenge_row == (True, 60)
        assert (
            connection.execute(
                """SELECT count(*) FROM app.audit_events
               WHERE workspace_id = %s AND action = 'device.registered'""",
                (scope.workspace_id,),
            ).fetchone()
            == (1,)
        )
        with pytest.raises(DeviceRegistrationRejected):
            await _complete(service, scope, challenge, key)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_expiry_replacement_authorization_and_uniqueness_are_atomic(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    scope = _scope(connection)
    engine = _engine()
    store = SQLAlchemyDeviceRegistrationStore(engine, audit_key=b"a" * 32)
    service = DeviceRegistrationService(store, hash_key=_HASH_KEY)
    try:
        challenge = await service.issue(scope)
        _expire(connection, challenge.challenge_id)
        key = Ed25519PrivateKey.generate()
        with pytest.raises(DeviceRegistrationRejected):
            await _complete(service, scope, challenge, key)
        second = await service.issue(scope)
        assert await store.is_live(scope, second.challenge_id)
        assert not await store.is_live(scope, challenge.challenge_id)
        assert (
            connection.execute(
                """SELECT count(*) FROM app.device_registration_challenges
               WHERE workspace_id = %s AND session_id = %s""",
                (scope.workspace_id, scope.session_id),
            ).fetchone()
            == (1,)
        )
        await _complete(service, scope, second, key)
        # Move database clock-independent fixture timestamps enough to pass cooldown.
        connection.execute(
            """UPDATE app.device_registration_challenges
               SET issued_at = issued_at - INTERVAL '11 seconds',
                   expires_at = expires_at - INTERVAL '11 seconds',
                   consumed_at = consumed_at - INTERVAL '11 seconds'
               WHERE id = %s""",
            (second.challenge_id,),
        )
        connection.commit()
        third = await service.issue(scope)
        with pytest.raises(DeviceRegistrationRejected):
            await _complete(service, scope, third, key)
        assert await store.is_live(scope, third.challenge_id)
        assert connection.execute(
            "SELECT count(*) FROM app.connections WHERE workspace_id = %s",
            (scope.workspace_id,),
        ).fetchone() == (1,)
        connection.execute(
            "UPDATE app.workspaces SET authorization_version = 2 WHERE id = %s",
            (scope.workspace_id,),
        )
        connection.commit()
        with pytest.raises(DeviceRegistrationRejected):
            await _complete(service, scope, third, Ed25519PrivateKey.generate())
    finally:
        await engine.dispose()
