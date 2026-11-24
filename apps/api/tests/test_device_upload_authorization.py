from __future__ import annotations

import base64
import json
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from test_device_heartbeat import _KEY, _NOW
from test_device_manifests import ManifestMemoryStore, _headers
from universal_ai_search.api.device_envelope import DeviceEnvelopeError
from universal_ai_search.api.device_upload_authorization import authorize_device_upload
from universal_ai_search.devices.uploads import (
    DeviceUploadAuthorization,
    UploadGrant,
    UploadRejected,
    UploadRequestInvalid,
    UploadReserve,
    parse_upload_request,
)


def request_values() -> dict[str, Any]:
    return {
        "upload_id": str(uuid4()),
        "manifest_id": str(uuid4()),
        "folder_id": str(uuid4()),
        "external_id": "synthetic",
        "size_bytes": 15,
        "mime_type": "text/markdown",
        "content_sha256": base64.urlsafe_b64encode(bytes(range(32))).decode()[:-1],
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("upload_id", "00000000-0000-0000-0000-000000000000"),
        ("upload_id", "24DB0402-B264-41D1-ABA6-EE54B14BC3AB"),
        ("folder_id", "not-an-id"),
        ("size_bytes", True),
        ("size_bytes", -1),
        ("size_bytes", 100_000_001),
        ("size_bytes", "20"),
        ("mime_type", "application/octet-stream"),
        ("mime_type", "text/plain; charset=utf-8"),
        ("content_sha256", "A" * 42 + "B"),
        ("content_sha256", "A" * 43 + "="),
        ("external_id", "/absolute"),
        ("object_key", "attacker-chosen"),
    ],
)
def test_invalid_metadata(field: str, value: object) -> None:
    with pytest.raises(UploadRequestInvalid):
        parse_upload_request(json.dumps({**request_values(), field: value}).encode())


def test_malformed_json_does_not_accept_duplicates() -> None:
    body = json.dumps(request_values()).encode()
    for invalid in (
        b"",
        b" " * 2049,
        b"\x80",
        b"[]",
        body.replace(b'"size_bytes": 15', b'"size_bytes": 15, "size_bytes": 15'),
    ):
        with pytest.raises(UploadRequestInvalid):
            parse_upload_request(invalid)


class _AuthorizingStore(ManifestMemoryStore):
    reservation: UploadReserve | None = None

    async def reserve(self, data: UploadReserve) -> UploadGrant:
        self.reservation = data
        request = data.request
        return UploadGrant(
            request.upload_id,
            uuid4(),
            f"device-inbox/{uuid4()}",
            _NOW,
            _NOW + timedelta(minutes=10),
            request.size_bytes,
            request.mime_type,
            request.content_sha256,
        )


def test_envelope_authentication_and_mock_signer_constraints() -> None:
    store = _AuthorizingStore()
    app = FastAPI()
    service = DeviceUploadAuthorization(store, hash_key=_KEY)
    with pytest.raises(ValueError):
        DeviceUploadAuthorization(store, hash_key=b"short")
    target = f"/v1/devices/{store.device_id}/uploads:sign"
    received: list[dict[str, str]] = []

    @app.post(target)
    async def synthetic_transport(request: Request) -> Response:
        # Mock only: capture signing constraints, never mint a URL.
        try:
            authorization = await authorize_device_upload(
                store.device_id, request, service
            )
        except (DeviceEnvelopeError, UploadRejected, UploadRequestInvalid):
            return Response(status_code=401)
        received.append(authorization.required_headers())
        return Response(status_code=204)

    client = TestClient(app, base_url="https://device.test")
    body = json.dumps(request_values()).encode()
    headers = _headers(store, target, body)
    wrong_route = _headers(store, target.replace("uploads:sign", "manifests"), body)
    wrong_token = [("Authorization", "Device " + "A" * 43), *headers[1:]]
    assert client.post(target, content=body + b" ", headers=headers).status_code == 401
    assert client.post(target, content=body, headers=wrong_route).status_code == 401
    assert client.post(target, content=body, headers=wrong_token).status_code == 401
    assert (
        client.post(target, content=body, headers=headers + [headers[-1]]).status_code
        == 401
    )
    assert client.post(target, content=body, headers=headers[:-1]).status_code == 401
    assert not received and store.reservation is None
    assert client.post(target, content=body, headers=headers).status_code == 204
    assert received == [
        {
            "Content-Type": "text/markdown",
            "Content-Length": "15",
            "x-amz-checksum-sha256": base64.b64encode(bytes(range(32))).decode(),
            "If-None-Match": "*",
        }
    ]
