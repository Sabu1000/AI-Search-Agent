from __future__ import annotations

import asyncio
import base64
import json
from uuid import UUID, uuid4

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_device_manifest_runtime import _body, _folder, _signed
from test_device_registration_runtime import _HASH_KEY, _complete, _engine, _scope

from universal_ai_search.auth.reauth_store import ReauthScope
from universal_ai_search.devices.manifest_store import SQLAlchemyDeviceManifestStore
from universal_ai_search.devices.manifests import DeviceManifestService
from universal_ai_search.devices.registration import DeviceRegistrationService
from universal_ai_search.devices.registration_store import (
    SQLAlchemyDeviceRegistrationStore,
)
from universal_ai_search.devices.upload_completion import (
    ChangeConflict,
    ChangeInvalidObject,
    DeviceUploadCompletion,
    ImmutableObject,
    VerifiedChange,
)
from universal_ai_search.devices.upload_completion_store import (
    SQLAlchemyUploadCompletionStore,
)
from universal_ai_search.devices.upload_store import SQLAlchemyDeviceUploadStore
from universal_ai_search.devices.uploads import (
    DeviceUploadAuthorization,
    UploadRejected,
)


class BlockingReader:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.size = 9

    async def get(self, object_key: str) -> ImmutableObject:
        self.keys.append(object_key)
        self.started.set()
        await self.release.wait()
        return ImmutableObject(
            "fixture-immutable-version",
            self.size,
            base64.b64encode(bytes(32)).decode(),
            "text/markdown",
        )


async def test_scoped_changes_are_idempotent_and_recheck_live_grant(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    scope = _scope(connection)
    other = _scope(connection)
    engine = _engine()
    reader = BlockingReader()
    key = Ed25519PrivateKey.generate()
    try:
        registration = DeviceRegistrationService(
            SQLAlchemyDeviceRegistrationStore(engine, audit_key=b"a" * 32),
            hash_key=_HASH_KEY,
        )
        grant = await _complete(
            registration, scope, await registration.issue(scope), key
        )
        folder = _folder(connection, scope.workspace_id, grant.device_id)
        manifest_id = uuid4()
        connection.execute(
            "UPDATE app.connections SET status='active' WHERE workspace_id=%s",
            (scope.workspace_id,),
        )
        connection.execute(
            "INSERT INTO app.workspace_usage (workspace_id) VALUES (%s)",
            (scope.workspace_id,),
        )
        connection.commit()
        manifests = DeviceManifestService(
            SQLAlchemyDeviceManifestStore(engine), hash_key=_HASH_KEY
        )
        prefix = f"/v1/devices/{grant.device_id}"
        args = {"device_id": grant.device_id, "credential": grant.device_credential}
        manifest_target = f"{prefix}/manifests".encode()
        doc = json.loads(_body(folder, manifest_id, 1))
        doc["entries"].append(
            {**doc["entries"][0], "external_id": "second", "relative_path": "second.md"}
        )
        manifest_body = json.dumps(doc).encode()
        page = await manifests.accept(
            **args,
            raw_target=manifest_target,
            exact_body=manifest_body,
            headers=_signed(key, manifest_target, manifest_body, bytes([1]) * 16),
        )
        authorization = DeviceUploadAuthorization(
            SQLAlchemyDeviceUploadStore(engine),
            hash_key=_HASH_KEY,
        )
        sign_target = f"{prefix}/uploads:sign".encode()
        reservations = []
        for index, (intent, external_id) in enumerate(
            zip(page.requested_uploads, ("synthetic-file", "second"), strict=True),
            start=2,
        ):
            sign_body = json.dumps(
                {
                    "upload_id": str(intent.upload_id),
                    "manifest_id": str(manifest_id),
                    "folder_id": str(folder),
                    "external_id": external_id,
                    "size_bytes": 9,
                    "content_sha256": "A" * 43,
                    "mime_type": "text/markdown",
                }
            ).encode()
            reservations.append(
                await authorization.reserve(
                    **args,
                    raw_target=sign_target,
                    exact_body=sign_body,
                    headers=_signed(key, sign_target, sign_body, bytes([index]) * 16),
                )
            )
        first, second = reservations
        store = SQLAlchemyUploadCompletionStore(engine)
        service = DeviceUploadCompletion(store, reader=reader, hash_key=_HASH_KEY)
        change_target = f"{prefix}/changes".encode()
        batch_id = uuid4()

        async def change(
            nonce: int,
            *,
            upload: UUID = first.upload_id,
            batch: UUID = batch_id,
        ) -> VerifiedChange:
            body = json.dumps(
                {"change_batch_id": str(batch), "upload_ids": [str(upload)]}
            )
            raw = body.encode()
            return await service.accept(
                **args,
                raw_target=change_target,
                exact_body=raw,
                headers=_signed(key, change_target, raw, bytes([nonce]) * 16),
            )

        reader.size = 8
        reader.release.set()
        with pytest.raises(ChangeInvalidObject):
            await change(10)
        with pytest.raises(UploadRejected):
            await change(10)
        with pytest.raises(ChangeConflict):
            await change(11, upload=second.upload_id)
        assert connection.execute(
            "SELECT count(*) FROM app.device_upload_changes "
            "WHERE completed_at IS NOT NULL",
        ).fetchone() == (0,)
        connection.execute(
            """UPDATE app.device_upload_changes
               SET verification_started_at=now()-interval '21 seconds',
                   created_at=now()-interval '22 seconds'
               WHERE batch_id=%s""",
            (batch_id,),
        )
        connection.commit()
        reader.size = 9
        good = await change(12)
        assert await change(13) == good
        assert reader.keys == [first.object_key, first.object_key]
        assert "fixture-immutable-version" not in good.model_dump_json()
        assert (
            await store.read(
                scope=ReauthScope(
                    scope.user_id, scope.session_id, scope.workspace_id, 1, 1
                ),
                operation_id=good.id,
            )
            == good
        )
        assert (
            await store.read(
                scope=ReauthScope(
                    other.user_id, other.session_id, other.workspace_id, 1, 1
                ),
                operation_id=good.id,
            )
            is None
        )
        # Revoke the folder after HEAD started, before SQL commits verification.
        reader.started.clear()
        reader.release.clear()
        pending = asyncio.create_task(
            change(14, upload=second.upload_id, batch=uuid4())
        )
        await asyncio.wait_for(reader.started.wait(), timeout=10)
        connection.execute(
            "UPDATE app.device_folders SET status='removed' WHERE id=%s", (folder,)
        )
        connection.commit()
        reader.release.set()
        with pytest.raises(UploadRejected):
            await pending
        row = connection.execute(
            """SELECT immutable_version FROM app.device_upload_changes
                WHERE workspace_id=%s AND completed_at IS NOT NULL""",
            (scope.workspace_id,),
        ).fetchall()
        assert row == [("fixture-immutable-version",)]
        assert connection.execute(
            "SELECT count(*) FROM app.jobs WHERE workspace_id=%s", (scope.workspace_id,)
        ).fetchone() == (0,)
    finally:
        await engine.dispose()
