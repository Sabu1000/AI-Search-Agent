from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr
from uas_connector_sdk import Credentials, Provider
from uas_connector_sdk.errors import (
    AuthenticationError,
    CursorInvalidError,
    RateLimitError,
)

from universal_ai_search.connections.gmail import (
    HttpGmailClient,
    normalize_gmail_message,
)
from universal_ai_search.connections.google import (
    GMAIL_READONLY_SCOPE,
    GOOGLE_TOKEN_ENDPOINT,
)


def encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).rstrip(b"=").decode()


def message_payload(body: str = "Hello from Gmail") -> dict[str, object]:
    return {
        "historyId": "900",
        "id": "message-1",
        "internalDate": "1704067200000",
        "labelIds": ["INBOX", "IMPORTANT"],
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Quarterly plan"},
                {"name": "From", "value": "Owner <owner@example.test>"},
                {"name": "To", "value": "team@example.test"},
            ],
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": encoded(body)}},
                {
                    "mimeType": "text/html",
                    "body": {"data": encoded("<b>duplicate html</b>")},
                },
            ],
        },
        "sizeEstimate": 512,
        "threadId": "thread-1",
    }


def test_normalize_gmail_message_preserves_stable_searchable_fields() -> None:
    document = normalize_gmail_message(message_payload())

    assert document.provider is Provider.GMAIL
    assert document.external_id == "message-1"
    assert document.title == "Quarterly plan"
    assert "Hello from Gmail" in document.content
    assert "duplicate html" not in document.content
    assert document.authors == ("owner@example.test",)
    assert document.created_at == datetime(2024, 1, 1, tzinfo=UTC)
    assert document.provider_metadata == {
        "history_id": "900",
        "label_ids": ["IMPORTANT", "INBOX"],
        "size_estimate": 512,
        "thread_id": "thread-1",
    }
    assert "access_token" not in repr(document)


@pytest.mark.asyncio
async def test_client_refreshes_lists_and_fetches_one_bounded_page() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url == httpx.URL(GOOGLE_TOKEN_ENDPOINT):
            assert b"refresh_token=refresh-secret" in request.content
            return httpx.Response(
                200,
                json={
                    "access_token": "fresh-access",
                    "expires_in": 3600,
                    "scope": GMAIL_READONLY_SCOPE,
                },
            )
        assert request.headers["authorization"] == "Bearer fresh-access"
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"historyId": "901"})
        if request.url.path.endswith("/messages"):
            assert request.url.params["maxResults"] == "25"
            return httpx.Response(
                200,
                json={
                    "messages": [{"id": "message-1"}],
                    "nextPageToken": "next-page",
                },
            )
        return httpx.Response(200, json=message_payload())

    client = HttpGmailClient(
        client_id="client-id",
        client_secret="client-secret",
        transport=httpx.MockTransport(handler),
    )
    expired = Credentials(
        access_token=SecretStr("expired-access"),
        refresh_token=SecretStr("refresh-secret"),
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        scopes=(GMAIL_READONLY_SCOPE,),
    )
    credentials = await client.ensure_fresh(expired)

    assert credentials.access_token.get_secret_value() == "fresh-access"
    assert await client.history_id("fresh-access") == "901"
    page = await client.page(access_token="fresh-access")
    assert page.next_page_token == "next-page"
    assert [document.external_id for document in page.documents] == ["message-1"]
    assert len(requests) == 4


@pytest.mark.asyncio
async def test_client_classifies_authentication_and_rate_limit_failures() -> None:
    def auth_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    client = HttpGmailClient(
        client_id="client",
        client_secret="secret",
        transport=httpx.MockTransport(auth_handler),
    )
    credentials = Credentials(
        access_token=SecretStr("expired"),
        refresh_token=SecretStr("revoked"),
        scopes=(GMAIL_READONLY_SCOPE,),
    )
    with pytest.raises(AuthenticationError):
        await client.refresh_credentials(credentials)

    def rate_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "7"})

    limited = HttpGmailClient(
        client_id="client",
        client_secret="secret",
        transport=httpx.MockTransport(rate_handler),
    )
    with pytest.raises(RateLimitError) as error:
        await limited.history_id("access")
    assert error.value.retry_after_seconds == 7


@pytest.mark.asyncio
async def test_client_reads_history_changes_and_detects_expired_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/history"):
            assert request.url.params["startHistoryId"] == "900"
            assert request.url.params["maxResults"] == "100"
            return httpx.Response(
                200,
                json={
                    "historyId": "905",
                    "nextPageToken": "history-page-2",
                    "history": [
                        {
                            "id": "901",
                            "messagesAdded": [{"message": {"id": "message-1"}}],
                            "labelsRemoved": [{"message": {"id": "message-1"}}],
                            "messagesDeleted": [{"message": {"id": "message-deleted"}}],
                        }
                    ],
                },
            )
        return httpx.Response(200, json=message_payload("Updated through history"))

    client = HttpGmailClient(
        client_id="client",
        client_secret="secret",
        transport=httpx.MockTransport(handler),
    )
    page = await client.history_page(access_token="access", start_history_id="900")

    assert page.history_id == "905"
    assert page.next_page_token == "history-page-2"
    assert [document.external_id for document in page.documents] == ["message-1"]
    assert page.deleted_external_ids == ("message-deleted",)

    expired = HttpGmailClient(
        client_id="client",
        client_secret="secret",
        transport=httpx.MockTransport(lambda _: httpx.Response(404)),
    )
    with pytest.raises(CursorInvalidError):
        await expired.history_page(access_token="access", start_history_id="old")
