from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_device_heartbeat import _KEY, _NOW
from test_device_manifests import _headers
from test_device_upload_authorization import _AuthorizingStore, request_values
from universal_ai_search.api.problems import install_problem_handlers
from universal_ai_search.api.routes.device_uploads import router
from universal_ai_search.devices.upload_signer import VerifiedS3UploadSigner
from universal_ai_search.devices.uploads import (
    DeviceUploadAuthorization,
    UploadGrant,
    UploadQuotaExceeded,
    UploadRateLimited,
    UploadRejected,
    UploadRequestInvalid,
    UploadReservationExpired,
    UploadReserve,
    UploadUnavailable,
)

_ORIGIN = "https://staging.example.test"
_BUCKET = "synthetic-stage"
_REGION = "us-east-1"
_ACCESS = "fixture-put-only"
_NOW = _NOW.replace(tzinfo=UTC)
_FIELDS = "content-length;content-type;host;if-none-match;x-amz-checksum-sha256"


class Policy:
    enabled = True
    failures = False

    def verified(self, *, origin: str, bucket: str, access_key_id: str) -> bool:
        assert (origin, bucket, access_key_id) == (_ORIGIN, _BUCKET, _ACCESS)
        if self.failures:
            raise RuntimeError("secret should not appear externally")
        return self.enabled


class SDK:
    origin = _ORIGIN
    path_override: str | None = None
    extra_query = ""
    mutation: tuple[str, str] | None = None
    time = _NOW
    params: dict[str, str | int] | None = None

    def generate_presigned_url(
        self,
        ClientMethod: str,
        *,
        Params: dict[str, str | int],
        ExpiresIn: int,
        HttpMethod: str,
    ) -> str:
        assert ClientMethod == "put_object" and HttpMethod == "PUT"
        assert Params["Bucket"] == _BUCKET
        self.params = Params
        query = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": (
                f"{_ACCESS}/{self.time.strftime('%Y%m%d')}/{_REGION}/s3/aws4_request"
            ),
            "X-Amz-Date": self.time.strftime("%Y%m%dT%H%M%SZ"),
            "X-Amz-Expires": str(ExpiresIn),
            "X-Amz-SignedHeaders": _FIELDS,
            "X-Amz-Signature": "a" * 64,
        }
        if self.mutation:
            query[self.mutation[0]] = self.mutation[1]
        path = self.path_override or f"/{_BUCKET}/{Params['Key']}"
        return self.origin + path + "?" + urlencode(query) + self.extra_query


def _signer(
    client: SDK, policy: Policy, now: datetime = _NOW
) -> VerifiedS3UploadSigner:
    return VerifiedS3UploadSigner(
        client,
        policy,
        origin=_ORIGIN,
        bucket=_BUCKET,
        region=_REGION,
        access_key_id=_ACCESS,
        now=lambda: now,
    )


def _grant(age: int = 0) -> UploadGrant:
    created_at = _NOW - timedelta(seconds=age)
    return UploadGrant(
        uuid4(),
        uuid4(),
        f"device-inbox/{uuid4()}",
        created_at,
        created_at + timedelta(minutes=10),
        31,
        "text/plain",
        "A" * 43,
    )


def test_presign_binds_exact_scope_and_ages_with_reservation() -> None:
    sdk = SDK()
    grant = _grant(age=41)
    result = _signer(sdk, Policy()).sign(grant)
    assert result.expires_at == grant.expires_at
    assert sdk.params == {
        "Bucket": _BUCKET,
        "Key": grant.object_key,
        "ContentLength": grant.size_bytes,
        "ContentType": grant.mime_type,
        "IfNoneMatch": "*",
        "ChecksumSHA256": grant.required_headers()["x-amz-checksum-sha256"],
    }
    assert "X-Amz-Expires=559" in result.url
    assert result.url not in repr(result)
    # With a fractional time the URL expires earlier, never extends its grant.
    sdk.time = _NOW + timedelta(milliseconds=500)
    assert _signer(sdk, Policy(), sdk.time).sign(grant).expires_at < grant.expires_at


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("X-Amz-Algorithm", "AWS4-HMAC-SHA1"),
        ("X-Amz-Credential", "another-signing-identity"),
        ("X-Amz-Date", "20301002T053300Z"),
        ("X-Amz-Expires", "601"),
        (
            "X-Amz-SignedHeaders",
            "content-type;host;if-none-match;x-amz-checksum-sha256",
        ),
        ("X-Amz-Signature", "a" * 31),
        ("uploads", "multipart"),
    ],
)
def test_reject_sdk_url_with_changed_signing_constraints(key: str, value: str) -> None:
    sdk = SDK()
    sdk.mutation = (key, value)
    with pytest.raises(UploadUnavailable):
        _signer(sdk, Policy()).sign(_grant())


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("origin", "https://another.example.test"),
        ("origin", "http://staging.example.test"),
        ("path_override", "/different-bucket/caller-object"),
        ("extra_query", "&X-Amz-Expires=600"),
        ("extra_query", "#local-fragment"),
    ],
)
def test_reject_sdk_url_for_other_destination(attribute: str, value: str) -> None:
    sdk = SDK()
    setattr(sdk, attribute, value)
    with pytest.raises(UploadUnavailable):
        _signer(sdk, Policy()).sign(_grant())


def test_expired_or_unsafe_grant_does_not_reach_sdk() -> None:
    sdk = SDK()
    signer = _signer(sdk, Policy())
    current = _grant()
    for grant in (
        _grant(age=600),
        _grant(age=-3),
        replace(current, object_key="device-inbox/../elsewhere"),
        replace(current, size_bytes=100_000_001),
        replace(current, expires_at=current.expires_at + timedelta(seconds=1)),
    ):
        with pytest.raises(UploadUnavailable):
            signer.sign(grant)
    assert sdk.params is None
    with pytest.raises(ValueError):
        VerifiedS3UploadSigner(
            sdk,
            Policy(),
            origin="http://staging.example.test",
            bucket=_BUCKET,
            access_key_id=_ACCESS,
            region=_REGION,
        )


def _route(
    store: _AuthorizingStore,
) -> tuple[FastAPI, TestClient, str, bytes, list[tuple[str, str]]]:
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(router, prefix="/v1")
    app.state.device_upload_authorization = DeviceUploadAuthorization(
        store, hash_key=_KEY
    )
    client = TestClient(app, base_url="https://api.example.test")
    path = f"/v1/devices/{store.device_id}/uploads:sign"
    body = json.dumps(request_values()).encode()
    return app, client, path, body, _headers(store, path, body)


def test_route_fails_closed_before_reservation_and_never_caches_success() -> None:
    store = _AuthorizingStore()
    app, client, path, body, headers = _route(store)
    assert client.post(path, content=body, headers=headers).status_code == 503
    policy = Policy()
    policy.enabled = False
    app.state.device_upload_signer = _signer(SDK(), policy)
    assert client.post(path, content=body, headers=headers).status_code == 503
    policy.failures = True
    blocked = client.post(path, content=body, headers=headers)
    assert blocked.status_code == 503 and "secret" not in blocked.text
    assert store.reservation is None
    policy.failures = False
    policy.enabled = True
    invalid = client.post(path, content=body, headers=headers[:-1])
    assert invalid.status_code == 401
    assert invalid.headers["WWW-Authenticate"] == "Device"
    oversize = client.post(path, content=b" " * 2049, headers=headers)
    assert oversize.status_code == 413
    reply = client.post(path, content=body, headers=headers)
    assert reply.status_code == 200
    assert reply.headers["Cache-Control"] == "private, no-store"
    assert reply.headers["Referrer-Policy"] == "no-referrer"
    data = reply.json()
    assert data["method"] == "PUT"
    assert data["upload_id"] == json.loads(body)["upload_id"]
    assert data["required_headers"]["If-None-Match"] == "*"
    assert data["expires_at"] == "2030-10-01T05:43:00Z"
    assert data["upload_url"].startswith(_ORIGIN)
    assert store.reservation is not None


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (UploadRejected, 401),
        (UploadRequestInvalid, 422),
        (UploadQuotaExceeded, 409),
        (UploadReservationExpired, 409),
        (UploadRateLimited, 429),
    ],
)
def test_route_maps_fixed_privacy_safe_errors(
    failure: type[Exception], status: int
) -> None:
    class DenyingStore(_AuthorizingStore):
        async def reserve(self, data: UploadReserve) -> UploadGrant:
            raise failure("sensitive provider message")

    app, client, path, body, headers = _route(DenyingStore())
    app.state.device_upload_signer = _signer(SDK(), Policy())
    reply = client.post(path, content=body, headers=headers)
    assert reply.status_code == status
    assert "sensitive provider" not in reply.text
    assert reply.headers["Cache-Control"] == "private, no-store"
