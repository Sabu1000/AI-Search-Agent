from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_device_registration_runtime import _HASH_KEY, _complete, _engine, _scope

from universal_ai_search.devices.heartbeat import (
    DeviceHeartbeatRejected,
    DeviceHeartbeatService,
)
from universal_ai_search.devices.heartbeat_store import SQLAlchemyDeviceHeartbeatStore
from universal_ai_search.devices.manifest_store import SQLAlchemyDeviceManifestStore
from universal_ai_search.devices.manifests import (
    DeviceManifestService,
    ManifestConflict,
    ManifestRejected,
    ManifestResponse,
    ManifestWrite,
)
from universal_ai_search.devices.registration import DeviceRegistrationService
from universal_ai_search.devices.registration_store import (
    SQLAlchemyDeviceRegistrationStore,
)
from universal_ai_search.devices.security import DeviceProofHeaders


def _signed(
    key: Ed25519PrivateKey, target: bytes, body: bytes, raw_nonce: bytes
) -> DeviceProofHeaders:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = base64.urlsafe_b64encode(raw_nonce).rstrip(b"=").decode()
    digest = (
        "sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()).decode() + ":"
    )
    request_id = str(uuid4())
    transcript = "\n".join(
        (
            "uas-device-request-v1",
            "POST",
            target.decode(),
            timestamp,
            nonce,
            digest,
            request_id,
        )
    )
    signature = (
        base64.urlsafe_b64encode(key.sign(transcript.encode())).rstrip(b"=").decode()
    )
    return DeviceProofHeaders(timestamp, nonce, digest, signature, request_id)


def _folder(
    connection: psycopg.Connection[tuple[object, ...]], workspace: UUID, device: UUID
) -> UUID:
    folder = uuid4()
    connection.execute(
        """INSERT INTO app.device_folders (
             id, workspace_id, device_id, external_root_id, display_name,
             absolute_path_hash, ignore_rules_hash, status
           ) VALUES (%s, %s, %s, %s, 'Synthetic', %s, %s, 'active')""",
        (folder, workspace, device, str(folder), b"r" * 32, b"i" * 32),
    )
    connection.commit()
    return folder


def _body(folder: UUID, manifest: UUID, sequence: int) -> bytes:
    return json.dumps(
        {
            "folder_id": str(folder),
            "manifest_id": str(manifest),
            "sequence": sequence,
            "final": True,
            "entries": [
                {
                    "external_id": "synthetic-file",
                    "relative_path": "private/test.md",
                    "size_bytes": 9,
                    "modified_at": "2026-01-01T00:00:00Z",
                    "content_sha256": "A" * 43,
                    "mime_type": "text/markdown",
                }
            ],
        },
        separators=(",", ":"),
    ).encode()


@pytest.mark.asyncio
async def test_checked_manifest_authority_replay_sequence_and_content_free_intents(
    connection: psycopg.Connection[tuple[object, ...]],
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
        folder = _folder(connection, scope.workspace_id, grant.device_id)
        manifest = uuid4()
        path = f"/v1/devices/{grant.device_id}/manifests".encode()
        service = DeviceManifestService(
            SQLAlchemyDeviceManifestStore(engine), hash_key=_HASH_KEY
        )

        async def post(body: bytes, nonce: int) -> ManifestResponse:
            return await service.accept(
                device_id=grant.device_id,
                credential=grant.device_credential,
                raw_target=path,
                headers=_signed(key, path, body, bytes([nonce]) * 16),
                exact_body=body,
            )

        first_body = _body(folder, manifest, 1)
        with pytest.raises(ManifestRejected):
            await post(first_body, 1)
        connection.execute(
            "UPDATE app.connections SET status = 'active' WHERE workspace_id = %s",
            (scope.workspace_id,),
        )
        connection.commit()
        with pytest.raises(ManifestRejected):
            await post(_body(uuid4(), manifest, 1), 2)
        results = await asyncio.gather(
            post(first_body, 1), post(first_body, 1), return_exceptions=True
        )
        pages = [result for result in results if isinstance(result, ManifestResponse)]
        assert (
            len(pages) == 1
            and sum(isinstance(x, ManifestRejected) for x in results) == 1
        )
        first = pages[0]
        assert first.next_sequence == 2 and not first.acknowledged_deletions
        heartbeat_path = f"/v1/devices/{grant.device_id}/heartbeat".encode()
        heartbeat = DeviceHeartbeatService(
            SQLAlchemyDeviceHeartbeatStore(engine), hash_key=_HASH_KEY
        )
        with pytest.raises(DeviceHeartbeatRejected):
            await heartbeat.accept(
                device_id=grant.device_id,
                credential=grant.device_credential,
                raw_target=heartbeat_path,
                headers=_signed(key, heartbeat_path, b"", b"\x01" * 16),
            )
        with pytest.raises(ManifestConflict) as conflict:
            await post(first_body + b" ", 3)
        assert conflict.value.next_sequence == 2
        with pytest.raises(ManifestRejected):
            await post(first_body, 3)
        with pytest.raises(ManifestConflict) as gap:
            await post(_body(folder, uuid4(), 3), 4)
        assert gap.value.next_sequence == 2
        races = await asyncio.gather(
            post(_body(folder, uuid4(), 2), 5),
            post(_body(folder, uuid4(), 2), 6),
            return_exceptions=True,
        )
        assert sum(isinstance(x, ManifestResponse) for x in races) == 1
        assert sum(isinstance(x, ManifestConflict) for x in races) == 1
        assert (
            await post(first_body, 7) == first
        )  # durable original response, not current cursor
        persisted = connection.execute(
            """SELECT upload_intents, body_hash,
                      has_table_privilege('app_api',
                                          'app.device_manifest_pages', 'select')
               FROM app.device_manifest_pages
               WHERE device_id = %s AND manifest_id = %s""",
            (grant.device_id, manifest),
        ).fetchone()
        assert persisted is not None
        intents, body_hash, accessible = persisted
        assert str(first.requested_uploads[0].upload_id) in str(intents)
        assert "private/test.md" not in str(intents)
        assert hashlib.sha256(b"private/test.md").hexdigest() in str(intents)
        assert hashlib.sha256(first_body).digest() == body_hash
        assert not accessible

        class RevokedFolderStore(SQLAlchemyDeviceManifestStore):
            async def commit(self, data: ManifestWrite) -> ManifestResponse:
                connection.execute(
                    "UPDATE app.device_folders SET status = 'removed' WHERE id = %s",
                    (folder,),
                )
                connection.commit()
                return await super().commit(data)

        service = DeviceManifestService(RevokedFolderStore(engine), hash_key=_HASH_KEY)
        with pytest.raises(ManifestRejected):
            await post(first_body, 8)
        count = connection.execute(
            "SELECT count(*) FROM app.device_manifest_pages WHERE device_id = %s",
            (grant.device_id,),
        ).fetchone()
        assert count == (2,)
        connection.execute("DELETE FROM app.device_folders WHERE id = %s", (folder,))
        assert connection.execute(
            "SELECT count(*) FROM app.device_manifest_pages WHERE device_id = %s",
            (grant.device_id,),
        ).fetchone() == (0,)
        connection.commit()
    finally:
        await engine.dispose()
