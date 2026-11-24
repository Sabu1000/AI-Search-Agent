from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from universal_ai_search.api.problems import install_problem_handlers
from universal_ai_search.api.routes.device_heartbeat import router
from universal_ai_search.devices.heartbeat import (
    DeviceHeartbeatRateLimited,
    DeviceHeartbeatRejected,
    DeviceHeartbeatService,
    DeviceHeartbeatUnavailable,
    HeartbeatAuthority,
    HeartbeatClaim,
)
from universal_ai_search.devices.registration import hash_device_credential
from universal_ai_search.devices.security import DeviceProofHeaders

_KEY = b"k" * 32
_NOW = datetime(2030, 10, 1, 5, 33, 0, tzinfo=UTC)


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def signed_heartbeat(
    key: Ed25519PrivateKey,
    target: bytes,
    *,
    at: datetime,
    nonce: bytes = b"n" * 16,
) -> DeviceProofHeaders:
    timestamp = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce_text = _base64url(nonce)
    digest = "sha-256=:" + base64.b64encode(hashlib.sha256(b"").digest()).decode() + ":"
    request_id = str(uuid4())
    transcript = "\n".join(
        (
            "uas-device-request-v1",
            "POST",
            target.decode(),
            timestamp,
            nonce_text,
            digest,
            request_id,
        )
    )
    return DeviceProofHeaders(
        timestamp,
        nonce_text,
        digest,
        _base64url(key.sign(transcript.encode())),
        request_id,
    )


class MemoryStore:
    def __init__(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.device_id = uuid4()
        self.credential = secrets.token_urlsafe(32)
        self.hashed = hash_device_credential(self.credential, _KEY)
        self.authority = HeartbeatAuthority(
            self.key.public_key().public_bytes_raw(), _NOW
        )
        self.lookups: list[UUID] = []
        self.claim: HeartbeatClaim | None = None

    async def lookup(
        self, device_id: UUID, credential_hash: bytes
    ) -> HeartbeatAuthority | None:
        self.lookups.append(device_id)
        if device_id == self.device_id and credential_hash == self.hashed:
            return self.authority
        return None

    async def touch(self, claim: HeartbeatClaim) -> None:
        self.claim = claim


@pytest.mark.asyncio
async def test_service_authenticates_bound_token_before_accepting_proof() -> None:
    store = MemoryStore()
    service = DeviceHeartbeatService(store, hash_key=_KEY)
    target = f"/v1/devices/{store.device_id}/heartbeat".encode()
    proof = signed_heartbeat(store.key, target, at=_NOW)
    await service.accept(
        device_id=store.device_id,
        credential=store.credential,
        raw_target=target,
        headers=proof,
    )
    assert store.claim is not None
    assert store.claim.credential_hash == store.hashed
    assert (
        store.claim.nonce_hash
        == hashlib.sha256(b"uas-device-nonce-v1\0" + b"n" * 16).digest()
    )
    assert store.claim.signed_at == _NOW
    store.claim = None
    for credential, raw, headers in (
        (secrets.token_urlsafe(32), target, proof),
        ("not-a-canonical-token", target, proof),
        (store.credential, target.replace(b"heartbeat", b"manifests"), proof),
        (
            store.credential,
            target,
            signed_heartbeat(store.key, target, at=_NOW + timedelta(minutes=6)),
        ),
        (store.credential, target, replace(proof, request_id=str(uuid4()))),
    ):
        with pytest.raises(DeviceHeartbeatRejected):
            await service.accept(
                device_id=store.device_id,
                credential=credential,
                raw_target=raw,
                headers=headers,
            )
    assert store.claim is None
    with pytest.raises(ValueError):
        DeviceHeartbeatService(store, hash_key=b"short")


def _headers(store: MemoryStore, path: str) -> list[tuple[str, str]]:
    proof = signed_heartbeat(store.key, path.encode(), at=_NOW)
    return [
        ("Authorization", f"Device {store.credential}"),
        ("X-Device-Timestamp", proof.timestamp),
        ("X-Device-Nonce", proof.nonce),
        ("Content-Digest", proof.content_digest),
        ("X-Device-Signature", proof.signature),
        ("X-Request-ID", proof.request_id),
    ]


def test_router_fails_closed_on_ambiguous_transport() -> None:
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(router, prefix="/v1")
    store = MemoryStore()
    app.state.device_heartbeat_service = DeviceHeartbeatService(store, hash_key=_KEY)
    client = TestClient(app, base_url="https://device.test")
    route = f"/v1/devices/{store.device_id}/heartbeat"
    headers = _headers(store, route)
    reply = client.post(route, headers=headers)
    assert reply.status_code == 204
    assert reply.content == b""
    assert reply.headers["Cache-Control"] == "private, no-store"
    store.lookups.clear()
    for path, supplied, body in (
        (f"http://device.test{route}", headers, b""),
        (route + "?x=1", headers, b""),
        (route[:-9] + "%68eartbeat", headers, b""),
        (route, headers + [("Cookie", "session=synthetic")], b""),
        (route, headers + [("Origin", "https://device.test")], b""),
        (route, headers + [("Content-Type", "application/json")], b""),
        (route, headers + [("Transfer-Encoding", "chunked")], b""),
        (route, headers + [headers[0]], b""),
        (route, headers + [headers[-1]], b""),
        (route, headers[1:], b""),
        (route, [("Authorization", "Bearer wrong"), *headers[1:]], b""),
        (route, headers, b"payload"),
        (route, headers + [("Content-Length", "0")], b"payload"),
        (route, [("X-Device-Timestamp", "9" * 21), headers[0], *headers[2:]], b""),
    ):
        response = client.post(path, headers=supplied, content=body)
        assert response.status_code == 401, path
        assert response.headers["Cache-Control"] == "private, no-store"
        assert response.headers["WWW-Authenticate"] == "Device"
        assert store.credential not in response.text
    assert store.lookups == []


@pytest.mark.parametrize(
    ("error", "status"),
    ((DeviceHeartbeatRateLimited, 429), (DeviceHeartbeatUnavailable, 503)),
)
def test_safe_service_status(error: type[Exception], status: int) -> None:
    class FailingStore(MemoryStore):
        async def touch(self, claim: HeartbeatClaim) -> None:
            raise error

    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(router, prefix="/v1")
    store = FailingStore()
    route = f"/v1/devices/{store.device_id}/heartbeat"
    client = TestClient(app, base_url="https://device.test")
    assert client.post(route, headers=_headers(store, route)).status_code == 503
    app.state.device_heartbeat_service = DeviceHeartbeatService(store, hash_key=_KEY)
    reply = client.post(route, headers=_headers(store, route))
    assert reply.status_code == status
    assert "synthetic" not in reply.text
    if status == 429:
        assert reply.headers["Retry-After"] == "10"
