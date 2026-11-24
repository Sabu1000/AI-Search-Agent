from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from fastapi.testclient import TestClient

from universal_ai_search.api.app import create_app
from universal_ai_search.auth.security import hash_token
from universal_ai_search.auth.verification_resend import VerificationResendService


async def test_resend_delivers_only_when_authorized_and_never_stores_raw_token() -> (
    None
):
    store = SimpleNamespace(issue=AsyncMock(return_value=True))
    limiter = SimpleNamespace(allow=AsyncMock(return_value=True))
    delivery = SimpleNamespace(send=AsyncMock())
    key = b"q" * 32
    service = VerificationResendService(
        store=store, limiter=limiter, delivery=delivery, key=key
    )
    await service.request("  Example@EXAMPLE.TEST ", "127.0.0.1")
    await service.drain()
    record = delivery.send.await_args.args[0]
    assert record.recipient == "example@example.test"
    email, token_id, stored_hash = store.issue.await_args.args
    assert email == record.recipient and isinstance(token_id, UUID)
    assert stored_hash == hash_token(record.token, key)
    assert record.token not in repr(record)
    limiter.allow.return_value = False
    await service.request("example@example.test", "127.0.0.1")
    await service.request("invalid", "127.0.0.1")
    await service.request("example@example.test", "invalid")
    store.issue.assert_awaited_once()


def test_resend_uniform_public_response() -> None:
    app = create_app()
    service = SimpleNamespace(request=AsyncMock())
    app.state.verification_resend_service = service
    with TestClient(app) as client:
        same = client.post("/v1/auth/email/resend", json={"email": "test@example.test"})
        other = client.post(
            "/v1/auth/email/resend",
            json={"email": "test@example.test"},
            headers={"Origin": "https://outside.example"},
        )
    assert same.status_code == other.status_code == 202
    assert same.json() == other.json() and "test@example.test" not in same.text
    assert same.headers["cache-control"] == "no-store"
    service.request.assert_awaited_once()
