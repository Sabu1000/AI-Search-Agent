from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from universal_ai_search.api.problems import install_problem_handlers
from universal_ai_search.api.routes import github_webhooks
from universal_ai_search.api.routes.github_webhooks import GitHubWebhookUnavailable
from universal_ai_search.connections.github_webhook import GitHubWebhookWork

SECRET = b"synthetic-webhook-secret-for-testing"
DELIVERY = "abcd0000-0000-4000-8000-123456789012"
BODY = b'{"installation":{"id":123},"repository":{"id":456},"private":"secret"}'
PATH = "/v1/webhooks/github"


class Service:
    def __init__(self) -> None:
        self.record = AsyncMock()


def fixture(
    *, secret: bytes | None = SECRET, enabled: bool = True
) -> tuple[FastAPI, Service]:
    app = FastAPI()
    service = Service()
    app.state.github_webhook_secret = secret
    if enabled:
        app.state.github_webhook_service = service
    install_problem_handlers(app)
    app.include_router(github_webhooks.router, prefix="/v1")
    return app, service


def headers(body: bytes = BODY, *, event: str = "push") -> dict[str, str]:
    return {
        "Content-Type": "application/json; charset=utf-8",
        "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET, body, "sha256").hexdigest(),
        "X-GitHub-Delivery": DELIVERY,
        "X-GitHub-Event": event,
    }


def test_awaits_durable_record_and_returns_empty_non_enumerating_response() -> None:
    app, service = fixture()
    client = TestClient(app)
    first = client.post(PATH, headers=headers(), content=BODY)
    same_delivery = client.post(PATH, headers=headers(), content=BODY)
    ignored = client.post(PATH, headers=headers(event="ping"), content=BODY)
    for response in (first, same_delivery, ignored):
        assert response.status_code == 202
        assert response.content == b""
        assert response.headers["cache-control"] == "no-store"
    assert service.record.await_count == 2
    hint = service.record.await_args.args[0]
    assert hint.delivery_id == UUID(DELIVERY)
    assert hint.installation_id == 123
    assert hint.repository_id == 456
    assert hint.work is GitHubWebhookWork.REPOSITORY
    assert "secret" not in repr(hint)


@pytest.mark.parametrize(
    ("secret", "enabled"), [(None, True), (b"short", True), (SECRET, False)]
)
def test_disabled_ingress_fails_closed(secret: bytes | None, enabled: bool) -> None:
    app, service = fixture(secret=secret, enabled=enabled)
    response = TestClient(app).post(PATH, headers=headers(), content=BODY)
    assert response.status_code == 503
    assert response.json()["code"] == "GITHUB_WEBHOOK_UNAVAILABLE"
    service.record.assert_not_awaited()


def test_bad_signature_does_not_reveal_payload_or_record() -> None:
    app, service = fixture()
    response = TestClient(app).post(PATH, headers=headers(), content=BODY + b" ")
    assert response.status_code == 401
    assert response.json()["code"] == "GITHUB_WEBHOOK_AUTHENTICATION_FAILED"
    assert "secret" not in response.text
    service.record.assert_not_awaited()


def test_signed_malformed_payload_gets_generic_error() -> None:
    app, service = fixture()
    body = b'{"installation": "sensitive-detail"}'
    response = TestClient(app).post(PATH, headers=headers(body), content=body)
    assert response.status_code == 400
    assert "sensitive-detail" not in response.text
    service.record.assert_not_awaited()


@pytest.mark.parametrize(
    "name", ["X-Hub-Signature-256", "X-GitHub-Delivery", "X-GitHub-Event"]
)
def test_rejects_missing_or_duplicate_identity_headers(name: str) -> None:
    app, service = fixture()
    client = TestClient(app)
    original = headers()
    response = client.post(
        PATH,
        headers=[*original.items(), (name.lower(), original[name])],
        content=BODY,
    )
    assert response.status_code == 400
    del original[name]
    response = client.post(PATH, headers=original, content=BODY)
    assert response.status_code == 400
    service.record.assert_not_awaited()


@pytest.mark.parametrize(
    "extra",
    [
        {"Content-Type": "application/x-www-form-urlencoded"},
        {"Content-Encoding": "gzip"},
    ],
)
def test_rejects_unsupported_content_format(extra: dict[str, str]) -> None:
    app, service = fixture()
    response = TestClient(app).post(PATH, headers={**headers(), **extra}, content=BODY)
    assert response.status_code == 415
    service.record.assert_not_awaited()


@pytest.mark.parametrize(
    ("length", "expected"), [("9999999", 413), ("not-a-number", 400), ("1", 400)]
)
def test_checks_declared_size_without_trusting_it(length: str, expected: int) -> None:
    app, service = fixture()
    response = TestClient(app).post(
        PATH, headers={**headers(), "Content-Length": length}, content=BODY
    )
    assert response.status_code == expected
    service.record.assert_not_awaited()


async def test_caps_chunked_body_without_waiting_for_entire_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, service = fixture()
    monkeypatch.setattr(github_webhooks, "MAX_GITHUB_WEBHOOK_BYTES", 16)
    read = 0

    async def chunks() -> AsyncIterator[bytes]:
        nonlocal read
        for _ in range(4):
            read += 1
            yield b"x" * 9

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(PATH, headers=headers(), content=chunks())
    assert response.status_code == 413
    assert read == 2
    service.record.assert_not_awaited()


def test_durable_failure_is_retryable_and_does_not_echo_original_error() -> None:
    app, service = fixture()
    service.record.side_effect = GitHubWebhookUnavailable("sensitive persistence")
    response = TestClient(app).post(PATH, headers=headers(), content=BODY)
    assert response.status_code == 503
    assert response.json()["retryable"]
    assert "sensitive persistence" not in response.text
