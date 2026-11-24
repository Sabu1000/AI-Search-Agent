from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from universal_ai_search.api.auth import (
    AuthenticationMode,
    Principal,
    WorkspaceContext,
    require_principal,
    require_workspace,
)
from universal_ai_search.api.problems import install_problem_handlers
from universal_ai_search.api.routes.device_registration import router
from universal_ai_search.devices.registration import (
    DeviceRegistrationRateLimited,
    DeviceRegistrationRejected,
    DeviceRegistrationService,
    DeviceRegistrationUnavailable,
    RegistrationChallenge,
    RegistrationScope,
    hash_device_credential,
)

_SCOPE = RegistrationScope(uuid4(), uuid4(), uuid4(), 2, 3)
_HEADERS = {
    "Authorization": "Bearer synthetic",
    "X-Workspace-ID": str(_SCOPE.workspace_id),
}
_KEY = b"s" * 32


class MemoryStore:
    def __init__(self) -> None:
        self.pending: dict[UUID, RegistrationScope] = {}
        self.issued: list[dict[str, Any]] = []
        self.issue_error: Exception | None = None
        self.complete_error: Exception | None = None

    async def issue(self, scope: RegistrationScope, challenge_id: UUID) -> datetime:
        if self.issue_error is not None:
            raise self.issue_error
        self.pending[challenge_id] = scope
        return datetime.now(UTC) + timedelta(seconds=60)

    async def is_live(self, scope: RegistrationScope, challenge_id: UUID) -> bool:
        return self.pending.get(challenge_id) == scope

    async def complete(self, **inputs: Any) -> None:
        if self.complete_error is not None:
            raise self.complete_error
        challenge_id = inputs["challenge_id"]
        if not await self.is_live(inputs["scope"], challenge_id):
            raise DeviceRegistrationRejected
        self.pending.pop(challenge_id)
        self.issued.append(inputs)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _body(challenge_id: str, challenge: str) -> dict[str, str]:
    key = Ed25519PrivateKey.from_private_bytes(b"\x42" * 32)
    public = _encode(key.public_key().public_bytes_raw())
    proof = key.sign(
        f"uas-device-registration-v1\n{challenge_id}\n{challenge}\n{public}".encode(
            "ascii"
        )
    )
    return {
        "challenge_id": challenge_id,
        "public_key": public,
        "signature": _encode(proof),
        "platform": "macos",
        "app_version": "1.2.3",
        "display_name": "Test Desktop",
    }


def _proof(challenge: RegistrationChallenge) -> dict[str, Any]:
    result: dict[str, Any] = _body(str(challenge.challenge_id), challenge.challenge)
    result["challenge_id"] = challenge.challenge_id
    return result


def _client(
    *,
    mode: AuthenticationMode = AuthenticationMode.BEARER,
    available: bool = True,
) -> tuple[TestClient, MemoryStore]:
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(router, prefix="/v1")
    store = MemoryStore()
    if available:
        app.state.device_registration_service = DeviceRegistrationService(
            store, hash_key=_KEY
        )
    app.dependency_overrides[require_principal] = lambda: Principal(
        subject_id=_SCOPE.user_id,
        mode=mode,
        session_id=_SCOPE.session_id,
        authorization_version=_SCOPE.user_authorization_version,
    )
    app.dependency_overrides[require_workspace] = lambda: WorkspaceContext(
        workspace_id=_SCOPE.workspace_id,
        user_id=_SCOPE.user_id,
        role="member",
        authorization_version=_SCOPE.workspace_authorization_version,
    )
    return TestClient(app), store


def test_returns_credential_exactly_once_and_never_caches_it() -> None:
    client, store = _client()
    with client:
        challenge = client.post("/v1/devices/registration-challenges", headers=_HEADERS)
        assert challenge.status_code == 201
        assert challenge.headers["Cache-Control"] == "private, no-store"
        assert len(challenge.json()["challenge"]) == 43
        payload = _body(challenge.json()["challenge_id"], challenge.json()["challenge"])
        completed = client.post("/v1/devices/register", headers=_HEADERS, json=payload)
        assert completed.status_code == 201
        assert completed.headers["Cache-Control"] == "private, no-store"
        assert "Authorization" in completed.headers["Vary"]
        credential = completed.json()["device_credential"]
        assert store.issued[0]["credential_hash"] == hash_device_credential(
            credential, _KEY
        )
        assert "device_credential" not in store.issued[0]
        assert len(store.issued) == 1
        replay = client.post("/v1/devices/register", headers=_HEADERS, json=payload)
        assert replay.status_code == 403
        assert credential not in replay.text


def test_browser_and_renderer_inputs_are_refused_before_issuance() -> None:
    client, store = _client()
    with client:
        for forbidden_header in (
            {"Origin": "https://renderer.test"},
            {"Cookie": "__Host-uas_refresh=synthetic"},
            {"Sec-Fetch-Mode": "cors"},
            {"Sec-Fetch-Site": "same-origin"},
        ):
            headers = {**_HEADERS, **forbidden_header}
            for endpoint in ("/registration-challenges", "/register"):
                assert (
                    client.post("/v1/devices" + endpoint, headers=headers).status_code
                    == 403
                )
        assert not store.pending
    browser, store = _client(mode=AuthenticationMode.BROWSER)
    with browser:
        denied = browser.post("/v1/devices/registration-challenges", headers=_HEADERS)
        assert denied.status_code == 403
        assert not store.pending


def test_enforces_bounded_original_json_and_strict_model() -> None:
    client, store = _client()
    json_headers = {**_HEADERS, "Content-Type": "application/json"}
    with client:
        for headers, content in [
            (json_headers, b'{"challenge_id":"one","challenge_id":"two"}'),
            (json_headers, b"z" * 2049),
            (json_headers, b"\xff"),
            (json_headers, b""),
            ({**json_headers, "Content-Encoding": "gzip"}, b"{}"),
            (_HEADERS, b"{}"),
            ({**_HEADERS, "Content-Type": "text/plain"}, b"{}"),
        ]:
            failed = client.post(
                "/v1/devices/register", headers=headers, content=content
            )
            assert failed.status_code == 422
        malformed = client.post("/v1/devices/register", headers=_HEADERS, json={})
        assert malformed.status_code == 422
        no_body_allowed = client.post(
            "/v1/devices/registration-challenges",
            headers=_HEADERS,
            json={"unknown": 1},
        )
        assert no_body_allowed.status_code == 422
        assert not store.issued and not store.pending


def test_missing_service_cooldown_and_errors_hide_internal_data() -> None:
    client, _ = _client(available=False)
    with client:
        unavailable = client.post(
            "/v1/devices/registration-challenges", headers=_HEADERS
        )
        assert unavailable.status_code == 503
    client, store = _client()
    with client:
        store.issue_error = DeviceRegistrationRateLimited("secret throttle detail")
        denied = client.post("/v1/devices/registration-challenges", headers=_HEADERS)
        assert denied.status_code == 429 and denied.headers["Retry-After"] == "10"
        assert "secret throttle detail" not in denied.text
        store.issue_error = DeviceRegistrationRejected("secret membership detail")
        denied = client.post("/v1/devices/registration-challenges", headers=_HEADERS)
        assert (
            denied.status_code == 403 and "secret membership detail" not in denied.text
        )
        store.issue_error = DeviceRegistrationUnavailable("secret SQL detail")
        denied = client.post("/v1/devices/registration-challenges", headers=_HEADERS)
        assert denied.status_code == 503 and "secret SQL detail" not in denied.text
        store.issue_error = None
        challenge = client.post("/v1/devices/registration-challenges", headers=_HEADERS)
        body = _body(challenge.json()["challenge_id"], challenge.json()["challenge"])
        store.complete_error = DeviceRegistrationUnavailable(
            "secret transaction detail"
        )
        denied = client.post("/v1/devices/register", headers=_HEADERS, json=body)
        assert (
            denied.status_code == 503 and "secret transaction detail" not in denied.text
        )


async def test_checks_scope_metadata_key_and_proof_before_persistence() -> None:
    store = MemoryStore()
    service = DeviceRegistrationService(store, hash_key=_KEY)
    challenge = await service.issue(_SCOPE)
    args = _proof(challenge)
    assert challenge.challenge not in repr(challenge)
    for scope in (
        replace(_SCOPE, workspace_id=uuid4()),
        replace(_SCOPE, user_id=uuid4()),
        replace(_SCOPE, session_id=uuid4()),
        replace(_SCOPE, user_authorization_version=4),
        replace(_SCOPE, workspace_authorization_version=5),
    ):
        with pytest.raises(DeviceRegistrationRejected):
            await service.complete(scope=scope, **args)
    for bad in (
        {"signature": "x" * 86},
        {"platform": "linux"},
        {"app_version": "release windows"},
        {"display_name": "Test\nDesktop"},
        {"challenge_id": UUID(int=0)},
    ):
        with pytest.raises(DeviceRegistrationRejected):
            await service.complete(scope=_SCOPE, **(args | bad))
    assert not store.issued
    complete = await service.complete(scope=_SCOPE, **args)
    assert len(complete.device_credential) == 43
    assert complete.device_credential not in repr(complete)
    assert complete.device_credential.encode() not in store.issued[0].values()
    with pytest.raises(DeviceRegistrationRejected):
        hash_device_credential("not a credential", _KEY)
    with pytest.raises(ValueError):
        DeviceRegistrationService(store, hash_key=b"x")
