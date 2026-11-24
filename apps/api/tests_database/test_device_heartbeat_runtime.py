from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_device_registration_runtime import _HASH_KEY, _complete, _engine, _scope

from universal_ai_search.devices.heartbeat import (
    DeviceHeartbeatRateLimited,
    DeviceHeartbeatRejected,
    DeviceHeartbeatService,
    HeartbeatClaim,
)
from universal_ai_search.devices.heartbeat_store import SQLAlchemyDeviceHeartbeatStore
from universal_ai_search.devices.registration import DeviceRegistrationService
from universal_ai_search.devices.registration_store import (
    SQLAlchemyDeviceRegistrationStore,
)
from universal_ai_search.devices.security import DeviceProofHeaders


def _sign(
    key: Ed25519PrivateKey, target: bytes, raw_nonce: bytes, at: datetime | None = None
) -> DeviceProofHeaders:
    timestamp = (at or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = base64.urlsafe_b64encode(raw_nonce).rstrip(b"=").decode()
    digest = "sha-256=:" + base64.b64encode(hashlib.sha256(b"").digest()).decode() + ":"
    request_id = str(uuid4())
    transcript = "\n".join(
        [
            "uas-device-request-v1",
            "POST",
            target.decode(),
            timestamp,
            nonce,
            digest,
            request_id,
        ]
    )
    signature = (
        base64.urlsafe_b64encode(key.sign(transcript.encode())).rstrip(b"=").decode()
    )
    return DeviceProofHeaders(timestamp, nonce, digest, signature, request_id)


def _connection_state(
    connection: psycopg.Connection[tuple[object, ...]], device_id: UUID
) -> tuple[object, ...] | None:
    result = connection.execute(
        """SELECT d.last_seen_at, c.status, d.updated_at, c.updated_at,
                  (SELECT count(*) FROM app.device_request_nonces n
                   WHERE n.device_id = d.id)
           FROM app.devices d JOIN app.connections c ON c.id = d.connection_id
           WHERE d.id = %s""",
        (device_id,),
    ).fetchone()
    connection.commit()
    return result


@pytest.mark.asyncio
async def test_pending_heartbeat_has_atomic_replay_and_never_promotes_connection(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    scope = _scope(connection)
    engine = _engine()
    try:
        registration = DeviceRegistrationService(
            SQLAlchemyDeviceRegistrationStore(engine, audit_key=b"a" * 32),
            hash_key=_HASH_KEY,
        )
        key = Ed25519PrivateKey.generate()
        grant = await _complete(
            registration, scope, await registration.issue(scope), key
        )
        before = _connection_state(connection, grant.device_id)
        assert (
            before is not None and before[:2] == (None, "pending") and before[-1] == 0
        )
        service = DeviceHeartbeatService(
            SQLAlchemyDeviceHeartbeatStore(engine), hash_key=_HASH_KEY
        )
        path = f"/v1/devices/{grant.device_id}/heartbeat".encode()

        async def touch(headers: DeviceProofHeaders) -> None:
            await service.accept(
                device_id=grant.device_id,
                credential=grant.device_credential,
                raw_target=path,
                headers=headers,
            )

        first = _sign(key, path, b"1" * 16)
        outcomes = await asyncio.gather(
            touch(first), touch(first), return_exceptions=True
        )
        assert sum(outcome is None for outcome in outcomes) == 1
        assert (
            sum(isinstance(outcome, DeviceHeartbeatRejected) for outcome in outcomes)
            == 1
        )
        # New request ID/signature does not make a consumed nonce available.
        with pytest.raises(DeviceHeartbeatRejected):
            await touch(_sign(key, path, b"1" * 16))
        with pytest.raises(DeviceHeartbeatRateLimited):
            await touch(_sign(key, path, b"2" * 16))
        with pytest.raises(DeviceHeartbeatRejected):
            await touch(
                _sign(key, path, b"3" * 16, datetime.now(UTC) - timedelta(minutes=6))
            )

        after = _connection_state(connection, grant.device_id)
        assert after is not None and isinstance(after[0], datetime)
        assert after[1:] == ("pending", *before[2:4], 1)
        connection.execute(
            "UPDATE app.devices SET last_seen_at = now() - interval '11 seconds' "
            "WHERE id = %s",
            (grant.device_id,),
        )
        connection.execute(
            "UPDATE app.device_request_nonces "
            "SET expires_at = now() - interval '1 second' WHERE device_id = %s",
            (grant.device_id,),
        )
        connection.commit()
        await touch(_sign(key, path, b"2" * 16))
        cleaned = _connection_state(connection, grant.device_id)
        assert cleaned is not None and cleaned[-1] == 1
        assert (
            connection.execute(
                """SELECT relrowsecurity, relforcerowsecurity,
                      pg_get_userbyid(relowner),
                      has_table_privilege('app_api', oid,
                                          'select,insert,update,delete'),
                      has_table_privilege('app_worker', oid,
                                          'select,insert,update,delete')
               FROM pg_class WHERE oid = 'app.device_request_nonces'::regclass"""
            ).fetchone()
            == (True, True, "app_migrator", False, False)
        )
        assert (
            connection.execute(
                """SELECT has_function_privilege('app_worker',
                  'app.lookup_device_heartbeat(uuid,bytea)', 'execute')"""
            ).fetchone()
            == (False,)
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("deny_at_claim", [True, False])
async def test_lookup_and_atomic_claim_recheck_revoked_or_stale_authority(
    connection: psycopg.Connection[tuple[object, ...]], deny_at_claim: bool
) -> None:
    scope = _scope(connection)
    engine = _engine()
    try:
        key = Ed25519PrivateKey.generate()
        registration = DeviceRegistrationService(
            SQLAlchemyDeviceRegistrationStore(engine, audit_key=b"a" * 32),
            hash_key=_HASH_KEY,
        )
        grant = await _complete(
            registration, scope, await registration.issue(scope), key
        )
        path = f"/v1/devices/{grant.device_id}/heartbeat".encode()

        class FencedStore(SQLAlchemyDeviceHeartbeatStore):
            async def touch(self, claim: HeartbeatClaim) -> None:
                # The other case never reaches the write.
                assert deny_at_claim
                connection.execute(
                    "UPDATE app.workspaces SET status = 'suspended' WHERE id = %s",
                    (scope.workspace_id,),
                )
                connection.commit()
                await super().touch(claim)

        if not deny_at_claim:
            connection.execute(
                "UPDATE app.devices SET status = 'revoked', revoked_at = now() "
                "WHERE id = %s",
                (grant.device_id,),
            )
            connection.commit()
        service = DeviceHeartbeatService(FencedStore(engine), hash_key=_HASH_KEY)
        with pytest.raises(DeviceHeartbeatRejected):
            await service.accept(
                device_id=grant.device_id,
                credential=grant.device_credential,
                raw_target=path,
                headers=_sign(key, path, b"1" * 16),
            )
        result = _connection_state(connection, grant.device_id)
        assert result is not None and result[0] is None and result[-1] == 0
    finally:
        await engine.dispose()
