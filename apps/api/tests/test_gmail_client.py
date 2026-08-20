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
    MalformedItemError,
    RateLimitError,
)

from universal_ai_search.connections.gmail import (
    HttpGmailClient,
    normalize_gmail_documents,
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
        "attachment_count": 0,
        "body_format": "plain",
        "history_id": "900",
        "internal_date": "2024-01-01T00:00:00Z",
        "label_ids": ["IMPORTANT", "INBOX"],
        "recipients": {"to": [{"address": "team@example.test"}]},
        "sender": [{"address": "owner@example.test", "display_name": "Owner"}],
        "size_estimate": 512,
        "thread_id": "thread-1",
    }
    assert "access_token" not in repr(document)


@pytest.mark.asyncio
async def test_client_fetches_text_attachments_as_separate_stable_documents() -> None:
    message = message_payload("Message body only")
    payload = message["payload"]
    assert isinstance(payload, dict)
    payload["mimeType"] = "multipart/mixed"
    payload["parts"] = [
        {
            "partId": "0",
            "mimeType": "text/plain",
            "body": {"data": encoded("Message body only")},
        },
        {
            "partId": "1",
            "filename": "notes.txt",
            "mimeType": "text/plain",
            "headers": [{"name": "Content-Type", "value": "text/plain; charset=utf-8"}],
            "body": {"data": encoded("Inline attachment text"), "size": 22},
        },
        {
            "partId": "2",
            "filename": "data.json",
            "mimeType": "application/json",
            "headers": [
                {"name": "Content-Type", "value": "application/json; charset=utf-8"}
            ],
            "body": {"attachmentId": "external-json", "size": 18},
        },
        {
            "partId": "3",
            "filename": "report.pdf",
            "mimeType": "application/pdf",
            "body": {"attachmentId": "external-pdf", "size": 900},
        },
        {
            "partId": "4",
            "filename": "large.txt",
            "mimeType": "text/plain",
            "body": {"attachmentId": "external-large", "size": 5_000_001},
        },
    ]
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "message-1"}]})
        if request.url.path.endswith("/attachments/external-json"):
            return httpx.Response(
                200, json={"data": encoded('{"searchable": true}'), "size": 18}
            )
        return httpx.Response(200, json=message)

    client = HttpGmailClient(
        client_id="client",
        client_secret="secret",
        transport=httpx.MockTransport(handler),
    )
    page = await client.page(access_token="access")

    assert [document.external_id for document in page.documents] == [
        "message-1",
        "message-1:attachment:1",
        "message-1:attachment:2",
        "message-1:attachment:3",
        "message-1:attachment:4",
    ]
    inline, external, unsupported, oversized = page.documents[1:]
    assert inline.source_type == "attachment"
    assert inline.content.endswith("Inline attachment text")
    assert external.mime_type == "application/json"
    assert external.content.endswith('{"searchable": true}')
    assert unsupported.provider_metadata["extraction_status"] == "unsupported"
    assert unsupported.provider_metadata["original_mime_type"] == "application/pdf"
    assert oversized.provider_metadata["extraction_status"] == "too_large"
    assert (
        requests.count(
            "/gmail/v1/users/me/messages/message-1/attachments/external-json"
        )
        == 1
    )
    assert not any("external-pdf" in path for path in requests)
    assert not any("external-large" in path for path in requests)


@pytest.mark.asyncio
async def test_client_hydrates_a_separately_stored_message_body() -> None:
    message = message_payload()
    payload = message["payload"]
    assert isinstance(payload, dict)
    payload["parts"] = [
        {
            "partId": "0",
            "mimeType": "text/plain",
            "body": {"attachmentId": "large-body", "size": 21},
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "message-1"}]})
        if request.url.path.endswith("/attachments/large-body"):
            return httpx.Response(
                200, json={"data": encoded("Externally stored body"), "size": 21}
            )
        return httpx.Response(200, json=message)

    client = HttpGmailClient(
        client_id="client",
        client_secret="secret",
        transport=httpx.MockTransport(handler),
    )

    page = await client.page(access_token="access")

    assert len(page.documents) == 1
    assert "Externally stored body" in page.documents[0].content


def test_attachment_normalization_rejects_duplicate_part_identity() -> None:
    message = message_payload()
    payload = message["payload"]
    assert isinstance(payload, dict)
    attachment = {
        "partId": "1",
        "filename": "duplicate.txt",
        "mimeType": "text/plain",
        "body": {"data": encoded("content"), "size": 7},
    }
    payload["mimeType"] = "multipart/mixed"
    payload["parts"] = [attachment, dict(attachment)]

    with pytest.raises(MalformedItemError):
        normalize_gmail_documents(message)


def test_normalization_preserves_structured_people_and_message_relationships() -> None:
    message = message_payload("Metadata body")
    payload = message["payload"]
    assert isinstance(payload, dict)
    headers = payload["headers"]
    assert isinstance(headers, list)
    headers.extend(
        [
            {"name": "To", "value": "Second <SECOND@example.test>"},
            {"name": "Cc", "value": "Carbon <cc@example.test>"},
            {"name": "Bcc", "value": "Hidden <bcc@example.test>"},
            {"name": "Reply-To", "value": "Support <reply@example.test>"},
            {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
            {"name": "Message-ID", "value": "<message@example.test>"},
            {"name": "In-Reply-To", "value": "<parent@example.test>"},
            {
                "name": "References",
                "value": "<root@example.test> <parent@example.test>",
            },
        ]
    )
    payload["mimeType"] = "multipart/mixed"
    parts = payload["parts"]
    assert isinstance(parts, list)
    parts.append(
        {
            "partId": "9",
            "filename": "photo.png",
            "mimeType": "image/png",
            "headers": [
                {"name": "Content-ID", "value": "<inline-photo>"},
                {"name": "Content-Disposition", "value": "inline"},
            ],
            "body": {"attachmentId": "photo-data", "size": 700},
        }
    )

    email, attachment = normalize_gmail_documents(message)

    assert {
        (person.relationship, person.normalized_identifier) for person in email.people
    } == {
        ("sender", "owner@example.test"),
        ("recipient", "team@example.test"),
        ("recipient", "second@example.test"),
        ("recipient", "cc@example.test"),
        ("recipient", "bcc@example.test"),
        ("participant", "reply@example.test"),
    }
    assert email.provider_metadata["rfc_message_id"] == "<message@example.test>"
    assert email.provider_metadata["in_reply_to"] == "<parent@example.test>"
    assert email.provider_metadata["references"] == [
        "<root@example.test>",
        "<parent@example.test>",
    ]
    assert email.provider_metadata["attachment_count"] == 1
    assert attachment.people == email.people
    assert attachment.provider_metadata["filename"] == "photo.png"
    assert attachment.provider_metadata["content_id"] == "<inline-photo>"
    assert attachment.provider_metadata["content_disposition"] == "inline"


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
