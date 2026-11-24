"""Opt-in TLS + PostgreSQL test; never uses real device or storage credentials."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import ssl
from collections.abc import Mapping
from importlib import import_module
from typing import cast
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_device_manifest_runtime import _body, _folder, _signed
from test_device_registration_runtime import _b64url, _complete, _engine, _scope

from universal_ai_search.api.app import create_app
from universal_ai_search.auth.reauth_store import ReauthScope
from universal_ai_search.config import Settings
from universal_ai_search.devices.registration import DeviceRegistrationService
from universal_ai_search.devices.registration_store import (
    SQLAlchemyDeviceRegistrationStore,
)
from universal_ai_search.devices.staging_read import (
    S3HeadClient,
    S3VersionedStageReader,
)
from universal_ai_search.devices.upload_index import S3PinnedGetClient
from universal_ai_search.devices.upload_index_operation import (
    read_native_upload_index_operation,
)
from universal_ai_search.devices.upload_index_runtime import (
    build_native_upload_index_runtime,
)
from universal_ai_search.devices.upload_signer import (
    S3PresignClient,
    VerifiedS3UploadSigner,
)
from universal_ai_search.indexing.pipeline import IndexingPipeline
from universal_ai_search.indexing.repository import IndexRepository
from universal_ai_search.indexing.runtime import IndexingRuntime

pytestmark = pytest.mark.skipif(
    not os.environ.get("UAS_VERIFY_S3_CA"),
    reason="requires ephemeral TLS object storage",
)


class ProbedFixturePolicy:
    """Available only here after the separate provider-policy contract succeeds."""

    def verified(self, *, origin: str, bucket: str, access_key_id: str) -> bool:
        return (origin, bucket, access_key_id) == (
            os.environ["UAS_VERIFY_S3_URL"],
            os.environ["UAS_OBJECT_STORAGE_BUCKET"],
            os.environ["UAS_VERIFY_S3_ACCESS_KEY"],
        )


def sdk(key: str, secret: str) -> object:
    boto = import_module("boto3")
    config = import_module("botocore.config")
    return boto.client(
        "s3",
        endpoint_url=os.environ["UAS_VERIFY_S3_URL"],
        verify=os.environ["UAS_VERIFY_S3_CA"],
        aws_access_key_id=os.environ[key],
        aws_secret_access_key=os.environ[secret],
        region_name="us-east-1",
        config=config.Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
        ),
    )


@pytest.mark.asyncio
async def test_signed_device_upload_and_verified_indexing(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    settings = Settings(
        environment="test",
        desktop_registration_enabled=True,
        database_url=os.environ["UAS_TEST_DATABASE_DSN"].replace(
            "postgresql://", "postgresql+asyncpg://"
        ),
    )
    signer = VerifiedS3UploadSigner(
        cast(
            S3PresignClient,
            sdk("UAS_VERIFY_S3_ACCESS_KEY", "UAS_VERIFY_S3_SECRET_KEY"),
        ),
        ProbedFixturePolicy(),
        origin=os.environ["UAS_VERIFY_S3_URL"],
        bucket=os.environ["UAS_OBJECT_STORAGE_BUCKET"],
        access_key_id=os.environ["UAS_VERIFY_S3_ACCESS_KEY"],
        region="us-east-1",
    )
    reader = sdk("UAS_VERIFY_S3_READ_KEY", "UAS_VERIFY_S3_READ_SECRET")
    bucket = os.environ["UAS_OBJECT_STORAGE_BUCKET"]
    app = create_app(
        settings,
        device_upload_signer=signer,
        device_upload_stage_reader=S3VersionedStageReader(
            cast(S3HeadClient, reader), bucket=bucket
        ),
    )
    signing_key = settings.auth_hash_key.get_secret_value().encode()
    scope = _scope(connection)
    engine = _engine()
    try:
        key = Ed25519PrivateKey.generate()
        registration = DeviceRegistrationService(
            SQLAlchemyDeviceRegistrationStore(engine, audit_key=signing_key),
            hash_key=signing_key,
        )
        device = await _complete(
            registration, scope, await registration.issue(scope), key
        )
        folder = _folder(connection, scope.workspace_id, device.device_id)
        connection.execute(
            "UPDATE app.connections SET status = 'active' WHERE workspace_id = %s",
            (scope.workspace_id,),
        )
        connection.execute(
            "INSERT INTO app.workspace_usage (workspace_id) VALUES (%s) "
            "ON CONFLICT (workspace_id) DO NOTHING",
            (scope.workspace_id,),
        )
        connection.commit()
        data = b"Local synthetic document for device upload acceptance."
        digest = _b64url(hashlib.sha256(data).digest())
        manifest_id = uuid4()
        manifest = json.loads(_body(folder, manifest_id, 1))
        manifest["entries"][0].update(size_bytes=len(data), content_sha256=digest)
        target = f"/v1/devices/{device.device_id}"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://api.example.test"
        ) as api:

            async def post(path: str, body: Mapping[str, object]) -> httpx.Response:
                payload = json.dumps(body, separators=(",", ":")).encode()
                proof = _signed(key, path.encode(), payload, uuid4().bytes)
                return await api.post(
                    path,
                    content=payload,
                    headers={
                        "Authorization": "Device " + device.device_credential,
                        "Content-Type": "application/json",
                        "X-Device-Timestamp": proof.timestamp,
                        "X-Device-Nonce": proof.nonce,
                        "Content-Digest": proof.content_digest,
                        "X-Device-Signature": proof.signature,
                        "X-Request-ID": proof.request_id,
                    },
                )

            manifest_response = await post(target + "/manifests", manifest)
            assert manifest_response.status_code == 200, (
                "manifest",
                manifest_response.json().get("code"),
            )
            upload_id = manifest_response.json()["requested_uploads"][0]["upload_id"]
            signed = await post(
                target + "/uploads:sign",
                {
                    "upload_id": upload_id,
                    "manifest_id": str(manifest_id),
                    "folder_id": str(folder),
                    "external_id": "synthetic-file",
                    "size_bytes": len(data),
                    "content_sha256": digest,
                    "mime_type": "text/markdown",
                },
            )
            assert signed.status_code == 200, ("sign", signed.json().get("code"))
            reply = signed.json()
            tls = ssl.create_default_context(cafile=os.environ["UAS_VERIFY_S3_CA"])
            async with httpx.AsyncClient(
                verify=tls, timeout=5.0, trust_env=False
            ) as object_store:
                uploaded = await object_store.put(
                    reply["upload_url"], content=data, headers=reply["required_headers"]
                )
                duplicate = await object_store.put(
                    reply["upload_url"], content=data, headers=reply["required_headers"]
                )
            assert uploaded.status_code == 200
            assert duplicate.status_code in {409, 412}
            change_id = uuid4()
            change = {"change_batch_id": str(change_id), "upload_ids": [upload_id]}
            accepted = await post(target + "/changes", change)
            assert accepted.status_code == 202, ("changes", accepted.json().get("code"))
            operation = accepted.json()["operation"]
            assert operation["status"] == "pending"
            assert operation["type"] == "device_upload_index"
            replay = await post(target + "/changes", change)
            assert replay.status_code == 202
            assert replay.json()["operation"]["id"] == operation["id"]
        operation_id = UUID(operation["id"])
        receipt_scope = ReauthScope(
            scope.user_id, scope.session_id, scope.workspace_id, 1, 1
        )
        pending = await read_native_upload_index_operation(
            app.state.database_engine, scope=receipt_scope, operation_id=operation_id
        )
        assert pending is not None and pending.status == "pending"

        staged = build_native_upload_index_runtime(
            settings.database_url, cast(S3PinnedGetClient, reader), bucket
        )
        indexing = IndexingRuntime(
            IndexRepository(settings.database_url), IndexingPipeline()
        )
        assert await asyncio.to_thread(staged.run_once, "fixture-staging")
        assert await asyncio.to_thread(indexing.run_once, "fixture-indexing")
        complete = await read_native_upload_index_operation(
            app.state.database_engine, scope=receipt_scope, operation_id=operation_id
        )
        assert complete is not None and complete.status == "completed", (
            None if complete is None else complete.error_code
        )
    finally:
        await app.state.database_engine.dispose()
        await engine.dispose()
