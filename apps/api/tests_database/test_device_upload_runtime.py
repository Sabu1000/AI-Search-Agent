from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_device_manifest_runtime import _body, _folder, _signed
from test_device_registration_runtime import _HASH_KEY, _complete, _engine, _scope

from universal_ai_search.devices.manifest_store import SQLAlchemyDeviceManifestStore
from universal_ai_search.devices.manifests import DeviceManifestService
from universal_ai_search.devices.registration import DeviceRegistrationService
from universal_ai_search.devices.registration_store import (
    SQLAlchemyDeviceRegistrationStore,
)
from universal_ai_search.devices.upload_store import SQLAlchemyDeviceUploadStore
from universal_ai_search.devices.uploads import (
    DeviceUploadAuthorization,
    UploadGrant,
    UploadQuotaExceeded,
    UploadRejected,
    UploadReservationExpired,
)


@pytest.mark.asyncio
async def test_upload_manifest_scope_constraints_quota_and_non_extensible_grant(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    scope = _scope(connection)
    other = _scope(connection)
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
        other_key = Ed25519PrivateKey.generate()
        other_grant = await _complete(
            registration, other, await registration.issue(other), other_key
        )
        folder = _folder(connection, scope.workspace_id, grant.device_id)
        _folder(connection, other.workspace_id, other_grant.device_id)
        manifest = uuid4()
        manifest_service = DeviceManifestService(
            SQLAlchemyDeviceManifestStore(engine), hash_key=_HASH_KEY
        )
        manifest_target = f"/v1/devices/{grant.device_id}/manifests".encode()
        authorization = DeviceUploadAuthorization(
            SQLAlchemyDeviceUploadStore(engine), hash_key=_HASH_KEY
        )
        target = f"/v1/devices/{grant.device_id}/uploads:sign".encode()

        async def post_upload(body: bytes, nonce: int) -> UploadGrant:
            return await authorization.reserve(
                device_id=grant.device_id,
                credential=grant.device_credential,
                raw_target=target,
                headers=_signed(key, target, body, bytes([nonce]) * 16),
                exact_body=body,
            )

        def upload_body(upload_id: UUID, **overrides: object) -> bytes:
            return json.dumps(
                {
                    "upload_id": str(upload_id),
                    "manifest_id": str(manifest),
                    "folder_id": str(folder),
                    "external_id": "synthetic-file",
                    "size_bytes": 9,
                    "content_sha256": "A" * 43,
                    "mime_type": "text/markdown",
                    **overrides,
                }
            ).encode()

        with pytest.raises(UploadRejected):
            await post_upload(upload_body(uuid4()), 1)
        connection.execute(
            "UPDATE app.connections SET status = 'active' "
            "WHERE workspace_id IN (%s, %s)",
            (scope.workspace_id, other.workspace_id),
        )
        connection.execute(
            """INSERT INTO app.workspace_usage (workspace_id, indexed_source_count)
               VALUES (%s, 24999)""",
            (scope.workspace_id,),
        )
        connection.commit()
        doc = json.loads(_body(folder, manifest, 1))
        original = doc["entries"][0]
        doc["entries"].append(
            {**original, "external_id": "second-file", "relative_path": "second.md"}
        )
        manifest_body = json.dumps(doc).encode()
        page = await manifest_service.accept(
            device_id=grant.device_id,
            credential=grant.device_credential,
            raw_target=manifest_target,
            headers=_signed(key, manifest_target, manifest_body, b"\x01" * 16),
            exact_body=manifest_body,
        )
        intent1, intent2 = (item.upload_id for item in page.requested_uploads)
        first = upload_body(intent1)
        second = upload_body(intent2, external_id="second-file")
        with pytest.raises(UploadRejected):  # manifest nonce is shared
            await post_upload(first, 1)
        for changed in (
            upload_body(intent1, size_bytes=8),
            upload_body(intent1, mime_type="text/plain"),
            upload_body(intent1, content_sha256="E" * 42 + "A"),
            upload_body(intent1, external_id="other"),
            upload_body(intent1, manifest_id=str(uuid4())),
            upload_body(intent1, folder_id=str(uuid4())),
        ):
            with pytest.raises(UploadRejected):
                await post_upload(changed, 2)
        other_target = f"/v1/devices/{other_grant.device_id}/uploads:sign".encode()
        with pytest.raises(UploadRejected):
            await authorization.reserve(
                device_id=other_grant.device_id,
                credential=other_grant.device_credential,
                raw_target=other_target,
                headers=_signed(other_key, other_target, first, b"\x03" * 16),
                exact_body=first,
            )

        results = await asyncio.gather(
            post_upload(first, 4), post_upload(second, 5), return_exceptions=True
        )
        assert sum(isinstance(x, UploadQuotaExceeded) for x in results) == 1
        reserved = [x for x in results if isinstance(x, UploadGrant)]
        assert len(reserved) == 1
        winner = reserved[0]
        assert winner.expires_at - winner.issued_at == timedelta(minutes=10)
        assert winner.workspace_id == scope.workspace_id and winner.size_bytes == 9
        assert (
            winner.object_key.startswith("device-inbox/")
            and scope.workspace_id.hex not in winner.object_key
        )
        winner_body, loser_body = (
            (first, second) if winner.upload_id == intent1 else (second, first)
        )
        winner_nonce = 4 if winner.upload_id == intent1 else 5
        loser_nonce = 5 if winner.upload_id == intent1 else 4
        assert await post_upload(winner_body, 6) == winner
        for body, nonce in ((winner_body, winner_nonce), (loser_body, loser_nonce)):
            with pytest.raises(UploadRejected):
                await post_upload(body, nonce)
        with pytest.raises(UploadQuotaExceeded):
            await post_upload(loser_body, 7)

        connection.execute(
            """WITH expiry AS (SELECT clock_timestamp() - INTERVAL '1 second' AS ts)
               UPDATE app.device_upload_reservations
               SET expires_at = expiry.ts, issued_at = expiry.ts - INTERVAL '10 minutes'
               FROM expiry WHERE upload_id = %s""",
            (winner.upload_id,),
        )
        connection.commit()
        with pytest.raises(UploadReservationExpired):
            await post_upload(winner_body, 8)
        with pytest.raises(UploadQuotaExceeded):  # expiry does not imply empty staging
            await post_upload(loser_body, 9)
        connection.execute("DELETE FROM app.device_folders WHERE id = %s", (folder,))
        assert (
            connection.execute(
                """SELECT count(*) FROM app.device_upload_reservations
               WHERE workspace_id = %s""",
                (scope.workspace_id,),
            ).fetchone()
            == (1,)
        )
        connection.commit()
        with pytest.raises(UploadRejected):
            await post_upload(winner_body, 10)
    finally:
        await engine.dispose()
