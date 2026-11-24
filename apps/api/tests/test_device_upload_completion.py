from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_device_heartbeat import _KEY, _NOW
from test_device_manifests import ManifestMemoryStore, _headers
from universal_ai_search.api.problems import install_problem_handlers
from universal_ai_search.api.routes.device_upload_completion import router
from universal_ai_search.devices.staging_read import S3VersionedStageReader
from universal_ai_search.devices.upload_completion import (
    ChangeCandidate,
    ChangeClaim,
    ChangeInvalidObject,
    DeviceUploadCompletion,
    ImmutableObject,
    VerifiedChange,
    parse_change,
)
from universal_ai_search.devices.uploads import UploadRequestInvalid, UploadUnavailable

_CHECKSUM = base64.b64encode(bytes(32)).decode()
_UPLOAD, _BATCH = uuid4(), uuid4()
_OBJECT = f"device-inbox/{uuid4()}"
_BODY = json.dumps({"change_batch_id": str(_BATCH), "upload_ids": [str(_UPLOAD)]})


@pytest.mark.parametrize(
    "changes",
    [
        {"upload_ids": []},
        {"upload_ids": [str(_UPLOAD), str(uuid4())]},
        {"upload_ids": [str(_UPLOAD).upper()]},
        {"object_key": "injected"},
        {"change_batch_id": "00000000-0000-0000-0000-000000000000"},
    ],
)
def test_changes_contract_is_exactly_one_canonical_upload(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(UploadRequestInvalid):
        parse_change(json.dumps({**json.loads(_BODY), **changes}).encode())
    with pytest.raises(UploadRequestInvalid):
        parse_change(
            _BODY[:-1].encode() + f', "change_batch_id": "{_BATCH}"}}'.encode()
        )


class _Store(ManifestMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[ChangeClaim] = []
        self.verified = VerifiedChange(
            id=uuid4(),
            status="verified",
            created_at=_NOW,
            completed_at=_NOW,
        )
        self.completed = False

    async def prepare(self, claim: ChangeClaim) -> ChangeCandidate | VerifiedChange:
        self.calls.append(claim)
        return (
            self.verified
            if self.completed
            else ChangeCandidate(_UPLOAD, _OBJECT, 9, "A" * 43, "text/markdown")
        )

    async def complete(
        self,
        claim: ChangeClaim,
        candidate: ChangeCandidate,
        version: str,
    ) -> VerifiedChange:
        assert claim == self.calls[-1]
        assert candidate.object_key == _OBJECT and version == "immutable-version"
        self.completed = True
        return self.verified


def test_route_binds_rendered_bytes_and_reports_pending_indexing() -> None:
    store = _Store()
    app = FastAPI()
    reader = type("MockReader", (), {})()
    reader.get = AsyncMock(
        return_value=ImmutableObject("null", 9, _CHECKSUM, "text/markdown")
    )
    app.state.device_upload_completion = DeviceUploadCompletion(
        store, reader=reader, hash_key=_KEY
    )
    install_problem_handlers(app)
    app.include_router(router, prefix="/v1")
    client = TestClient(app, base_url="https://devices.example.test")
    target = f"/v1/devices/{store.device_id}/changes"
    raw = _BODY.encode()
    headers = _headers(store, target, raw)
    assert client.post(target, content=raw + b" ", headers=headers).status_code == 401
    assert client.post(target + "?x=1", content=raw, headers=headers).status_code == 401
    assert not store.calls
    response = client.post(target, content=raw, headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "DEVICE_CHANGE_UNVERIFIED"
    assert not store.completed and _OBJECT not in response.text
    reader.get.return_value = ImmutableObject(
        "immutable-version", 9, _CHECKSUM, "text/markdown"
    )
    response = client.post(target, content=raw, headers=headers)
    assert response.status_code == 202 and store.completed
    assert response.headers["cache-control"] == "private, no-store"
    operation = response.json()["operation"]
    assert operation["status"] == "pending"
    assert operation["type"] == "device_upload_index"
    assert operation["progress"]["unit"] == "indexed_uploads"
    assert "immutable-version" not in response.text and _OBJECT not in response.text
    assert response.json() == client.post(target, content=raw, headers=headers).json()
    assert reader.get.await_count == 2
    del app.state.device_upload_completion
    assert client.post(target, content=raw, headers=headers).status_code == 503


class _Head:
    def __init__(self) -> None:
        self.first = {
            "VersionId": "immutable-version",
            "ContentLength": 9,
            "ChecksumSHA256": _CHECKSUM,
            "ContentType": "text/markdown",
            "ChecksumType": "FULL_OBJECT",
        }
        self.pinned = dict(self.first)
        self.calls: list[dict[str, str]] = []
        self.error: Exception | None = None

    def head_object(self, **kwargs: str) -> dict[str, object]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.pinned if "VersionId" in kwargs else self.first


async def test_s3_reader_pins_version_and_rejects_bad_metadata() -> None:
    client = _Head()
    reader = S3VersionedStageReader(client, bucket="staging-fixture")
    assert (await reader.get(_OBJECT)).checksum_sha256 == _CHECKSUM
    assert client.calls[0] == {
        "Bucket": "staging-fixture",
        "Key": _OBJECT,
        "ChecksumMode": "ENABLED",
    }
    assert client.calls[1] == {**client.calls[0], "VersionId": "immutable-version"}
    client.pinned["ContentLength"] = 0
    with pytest.raises(ChangeInvalidObject):
        await reader.get(_OBJECT)
    client.first["ChecksumType"] = "COMPOSITE"
    with pytest.raises(ChangeInvalidObject):
        await reader.get(_OBJECT)
    with pytest.raises(ChangeInvalidObject):
        await reader.get("outside")
    client.first.pop("ChecksumSHA256")
    with pytest.raises(ChangeInvalidObject):
        await reader.get(_OBJECT)
    client.error = RuntimeError("never expose fixture provider detail")
    with pytest.raises(UploadUnavailable) as error:
        await reader.get(_OBJECT)
    assert "fixture" not in str(error.value)


@pytest.mark.parametrize(
    ("error", "expected_status", "code"),
    [
        ("conflict", 409, "CONFLICT"),
        ("expired", 409, "EXPIRED"),
        ("rate", 429, "RATE_LIMITED"),
        ("provider", 503, "UNAVAILABLE"),
    ],
)
def test_changes_return_safe_failures(
    error: str, expected_status: int, code: str
) -> None:
    from universal_ai_search.devices.upload_completion import (
        ChangeConflict,
        ChangeExpired,
        ChangeRateLimited,
    )

    store = _Store()
    app = FastAPI()
    app.state.device_upload_completion = type("Unavailable", (), {})()
    exc = {
        "conflict": ChangeConflict,
        "expired": ChangeExpired,
        "rate": ChangeRateLimited,
        "provider": UploadUnavailable,
    }[error]
    accept = AsyncMock(side_effect=exc("must not expose fixture secret"))
    app.state.device_upload_completion.accept = accept
    install_problem_handlers(app)
    app.include_router(router, prefix="/v1")
    client = TestClient(app, base_url="https://devices.example.test")
    target = f"/v1/devices/{store.device_id}/changes"
    body = _BODY.encode()
    headers = _headers(store, target, body)
    result = client.post(target, content=body, headers=headers)
    assert result.status_code == expected_status
    assert result.json()["code"] == f"DEVICE_CHANGE_{code}"
    assert "fixture secret" not in result.text
    invalid = _headers(store, target, b" " * 513)
    assert client.post(target, content=b" " * 513, headers=invalid).status_code == 413
    accept.assert_awaited_once()
