"""Read-only Gmail API client and deterministic message normalization."""

from __future__ import annotations

import math
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import quote

import httpx
from pydantic import SecretStr
from uas_connector_sdk import Credentials, NormalizedDocument, Provider
from uas_connector_sdk.errors import (
    AuthenticationError,
    CursorInvalidError,
    MalformedItemError,
    PermissionDeniedError,
    ProviderUnavailableError,
    RateLimitError,
)

from .email_parser import parse_gmail_payload
from .gmail_attachments import (
    MAX_ATTACHMENT_BYTES,
    apply_external_part_data,
    external_text_part_ids,
    find_gmail_attachments,
    normalize_gmail_attachments,
)
from .gmail_metadata import extract_gmail_metadata
from .google import GMAIL_READONLY_SCOPE, GOOGLE_TOKEN_ENDPOINT

GMAIL_API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"
GMAIL_PAGE_SIZE = 25
GMAIL_HISTORY_PAGE_SIZE = 100
_EXPIRY_SKEW = timedelta(minutes=2)


@dataclass(frozen=True)
class GmailPage:
    documents: tuple[NormalizedDocument, ...]
    next_page_token: str | None


@dataclass(frozen=True)
class GmailHistoryPage:
    documents: tuple[NormalizedDocument, ...]
    deleted_external_ids: tuple[str, ...]
    history_id: str
    next_page_token: str | None


def _provider_error(response: httpx.Response) -> Exception:
    if response.status_code == 401:
        return AuthenticationError()
    if response.status_code == 429:
        retry_after: float | None = None
        with suppress(KeyError, ValueError):
            retry_after = float(response.headers["Retry-After"])
            if not math.isfinite(retry_after) or retry_after < 0:
                retry_after = None
        return RateLimitError(retry_after)
    if response.status_code == 403:
        return PermissionDeniedError()
    if response.status_code >= 500:
        return ProviderUnavailableError()
    return MalformedItemError("Gmail returned an invalid response")


def _json(response: httpx.Response) -> dict[str, object]:
    if not response.is_success:
        raise _provider_error(response)
    try:
        payload = response.json()
    except ValueError as error:
        raise MalformedItemError("Gmail returned malformed JSON") from error
    if not isinstance(payload, dict):
        raise MalformedItemError("Gmail returned an invalid payload")
    return cast(dict[str, object], payload)


def normalize_gmail_message(message: dict[str, object]) -> NormalizedDocument:
    """Convert a Gmail full-format message without executing embedded content."""

    message_id = message.get("id")
    thread_id = message.get("threadId")
    payload = message.get("payload")
    internal_date = message.get("internalDate")
    if (
        not isinstance(message_id, str)
        or not message_id
        or not isinstance(thread_id, str)
        or not isinstance(payload, dict)
        or not isinstance(internal_date, str)
    ):
        raise MalformedItemError("Gmail message is missing required fields")
    try:
        sent_at = datetime.fromtimestamp(int(internal_date) / 1000, tz=UTC)
    except (ValueError, OverflowError) as error:
        raise MalformedItemError("Gmail message timestamp is invalid") from error

    typed_payload = cast(dict[str, object], payload)
    attachment_count = len(find_gmail_attachments(typed_payload))
    parsed_metadata = extract_gmail_metadata(
        typed_payload, sent_at=sent_at, attachment_count=attachment_count
    )
    subject = parsed_metadata.subject
    parsed_body = parse_gmail_payload(typed_payload)
    body = parsed_body.text
    preamble = [
        f"Subject: {subject}",
        *(
            (f"From: {parsed_metadata.sender_header}",)
            if parsed_metadata.sender_header
            else ()
        ),
        *((f"To: {parsed_metadata.to_header}",) if parsed_metadata.to_header else ()),
        *((f"Cc: {parsed_metadata.cc_header}",) if parsed_metadata.cc_header else ()),
        *(
            (f"Date: {parsed_metadata.date_header}",)
            if parsed_metadata.date_header
            else ()
        ),
    ]
    content = "\n".join(preamble) + (f"\n\n{body}" if body else "")
    if len(content) > 5_000_000:
        raise MalformedItemError("Gmail message exceeds the normalized size limit")

    label_ids = message.get("labelIds", [])
    if not isinstance(label_ids, list) or not all(
        isinstance(label, str) for label in label_ids
    ):
        raise MalformedItemError("Gmail message labels are invalid")
    history_id = message.get("historyId")
    size_estimate = message.get("sizeEstimate")
    metadata: dict[str, object] = {
        "thread_id": thread_id,
        "label_ids": sorted(label_ids),
        **parsed_metadata.provider_metadata,
    }
    if isinstance(history_id, str):
        metadata["history_id"] = history_id
    if isinstance(size_estimate, int) and size_estimate >= 0:
        metadata["size_estimate"] = size_estimate
    if parsed_body.body_format is not None:
        metadata["body_format"] = parsed_body.body_format
    if parsed_body.quoted_history_removed:
        metadata["quoted_history_removed"] = True
    if parsed_body.signature_removed:
        metadata["signature_removed"] = True
    if parsed_body.skipped_attachment_count:
        metadata["skipped_attachment_count"] = parsed_body.skipped_attachment_count

    return NormalizedDocument(
        external_id=message_id,
        provider=Provider.GMAIL,
        source_type="email",
        title=subject[:2000],
        content=content,
        canonical_url=f"https://mail.google.com/mail/u/0/#all/{message_id}",
        mime_type="text/plain",
        authors=tuple(author[:500] for author in parsed_metadata.authors),
        created_at=sent_at,
        people=parsed_metadata.people,
        provider_metadata=metadata,
    )


def normalize_gmail_documents(
    message: dict[str, object],
) -> tuple[NormalizedDocument, ...]:
    """Normalize a Gmail message and each of its stable attachment sources."""

    document = normalize_gmail_message(message)
    payload = message.get("payload")
    thread_id = message.get("threadId")
    if not isinstance(payload, dict) or not isinstance(thread_id, str):
        raise MalformedItemError("Gmail message is missing required fields")
    if document.created_at is None:
        raise MalformedItemError("Gmail message timestamp is invalid")
    attachments = normalize_gmail_attachments(
        message_id=document.external_id,
        thread_id=thread_id,
        subject=document.title,
        sender=document.authors[0] if document.authors else "",
        author=document.authors[0] if document.authors else "",
        sent_at=document.created_at,
        payload=cast(dict[str, object], payload),
        people=document.people,
    )
    return (document, *attachments)


class HttpGmailClient:
    """Read-only bounded Gmail message, history, and attachment adapter."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport

    async def refresh_credentials(self, credentials: Credentials) -> Credentials:
        if credentials.refresh_token is None:
            raise AuthenticationError()
        try:
            async with httpx.AsyncClient(
                timeout=15, transport=self._transport
            ) as client:
                response = await client.post(
                    GOOGLE_TOKEN_ENDPOINT,
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "grant_type": "refresh_token",
                        "refresh_token": credentials.refresh_token.get_secret_value(),
                    },
                )
        except httpx.HTTPError as error:
            raise ProviderUnavailableError() from error
        if response.status_code in {400, 401}:
            raise AuthenticationError()
        payload = _json(response)
        access_token, expires_in = payload.get("access_token"), payload.get(
            "expires_in"
        )
        if not isinstance(access_token, str) or not isinstance(expires_in, int):
            raise MalformedItemError("Google token response is invalid")
        scopes_value = payload.get("scope")
        scopes = (
            tuple(str(scopes_value).split())
            if isinstance(scopes_value, str)
            else credentials.scopes
        )
        if GMAIL_READONLY_SCOPE not in scopes:
            raise PermissionDeniedError()
        return Credentials(
            access_token=SecretStr(access_token),
            refresh_token=credentials.refresh_token,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            scopes=tuple(sorted(scopes)),
        )

    async def ensure_fresh(self, credentials: Credentials) -> Credentials:
        if (
            credentials.expires_at is None
            or credentials.expires_at <= datetime.now(UTC) + _EXPIRY_SKEW
        ):
            return await self.refresh_credentials(credentials)
        return credentials

    async def history_id(self, access_token: str) -> str:
        payload = await self._get("profile", access_token=access_token)
        history_id = payload.get("historyId")
        if not isinstance(history_id, str) or not history_id:
            raise MalformedItemError("Gmail profile has no history cursor")
        return history_id

    async def page(
        self, *, access_token: str, page_token: str | None = None
    ) -> GmailPage:
        params: dict[str, str | int] = {"maxResults": GMAIL_PAGE_SIZE}
        if page_token:
            params["pageToken"] = page_token
        listing = await self._get("messages", access_token=access_token, params=params)
        references = listing.get("messages", [])
        if not isinstance(references, list) or len(references) > GMAIL_PAGE_SIZE:
            raise MalformedItemError("Gmail message listing is invalid")
        documents: list[NormalizedDocument] = []
        for reference in references:
            if not isinstance(reference, dict) or not isinstance(
                reference.get("id"), str
            ):
                raise MalformedItemError("Gmail message reference is invalid")
            message_response = await self._request(
                f"messages/{reference['id']}",
                access_token=access_token,
                params={"format": "full"},
            )
            if message_response.status_code == 404:
                continue
            documents.extend(
                await self._message_documents(
                    _json(message_response), access_token=access_token
                )
            )
        next_page_token = listing.get("nextPageToken")
        if next_page_token is not None and not isinstance(next_page_token, str):
            raise MalformedItemError("Gmail page token is invalid")
        return GmailPage(tuple(documents), next_page_token)

    async def history_page(
        self,
        *,
        access_token: str,
        start_history_id: str,
        page_token: str | None = None,
    ) -> GmailHistoryPage:
        params: dict[str, str | int] = {
            "maxResults": GMAIL_HISTORY_PAGE_SIZE,
            "startHistoryId": start_history_id,
        }
        if page_token:
            params["pageToken"] = page_token
        response = await self._request(
            "history", access_token=access_token, params=params
        )
        if response.status_code == 404:
            raise CursorInvalidError()
        listing = _json(response)
        records = listing.get("history", [])
        if not isinstance(records, list) or len(records) > GMAIL_HISTORY_PAGE_SIZE:
            raise MalformedItemError("Gmail history listing is invalid")

        actions: dict[str, bool] = {}
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                raise MalformedItemError("Gmail history record is invalid")
            for field in ("messagesAdded", "labelsAdded", "labelsRemoved"):
                for message_id in _history_message_ids(record.get(field)):
                    actions[message_id] = False
            for message_id in _history_message_ids(record.get("messagesDeleted")):
                actions[message_id] = True

        documents: list[NormalizedDocument] = []
        deleted = {
            message_id for message_id, is_deleted in actions.items() if is_deleted
        }
        for message_id, is_deleted in actions.items():
            if is_deleted:
                continue
            message_response = await self._request(
                f"messages/{message_id}",
                access_token=access_token,
                params={"format": "full"},
            )
            if message_response.status_code == 404:
                deleted.add(message_id)
                continue
            documents.extend(
                await self._message_documents(
                    _json(message_response), access_token=access_token
                )
            )

        history_id = listing.get("historyId")
        next_page_token = listing.get("nextPageToken")
        if not isinstance(history_id, str) or not history_id:
            raise MalformedItemError("Gmail history cursor is invalid")
        if next_page_token is not None and not isinstance(next_page_token, str):
            raise MalformedItemError("Gmail history page token is invalid")
        return GmailHistoryPage(
            tuple(documents), tuple(sorted(deleted)), history_id, next_page_token
        )

    async def _message_documents(
        self, message: dict[str, object], *, access_token: str
    ) -> tuple[NormalizedDocument, ...]:
        hydrated = deepcopy(message)
        payload = hydrated.get("payload")
        message_id = hydrated.get("id")
        if not isinstance(payload, dict) or not isinstance(message_id, str):
            raise MalformedItemError("Gmail message is missing required fields")
        data_by_attachment_id: dict[str, str] = {}
        for attachment_id in external_text_part_ids(cast(dict[str, object], payload)):
            response = await self._get(
                "messages/"
                f"{quote(message_id, safe='')}/attachments/"
                f"{quote(attachment_id, safe='')}",
                access_token=access_token,
            )
            data = response.get("data")
            size = response.get("size", 0)
            if (
                not isinstance(data, str)
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or size > MAX_ATTACHMENT_BYTES
            ):
                raise MalformedItemError("Gmail attachment response is invalid")
            data_by_attachment_id[attachment_id] = data
        apply_external_part_data(
            cast(dict[str, object], payload), data_by_attachment_id
        )
        return normalize_gmail_documents(hydrated)

    async def _request(
        self,
        path: str,
        *,
        access_token: str,
        params: dict[str, str | int] | None = None,
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                timeout=20, transport=self._transport
            ) as client:
                return await client.get(
                    f"{GMAIL_API_ROOT}/{path}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params=params,
                )
        except httpx.HTTPError as error:
            raise ProviderUnavailableError() from error

    async def _get(
        self,
        path: str,
        *,
        access_token: str,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, object]:
        response = await self._request(path, access_token=access_token, params=params)
        return _json(response)


def _history_message_ids(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MalformedItemError("Gmail history change is invalid")
    result: list[str] = []
    for change in value:
        if not isinstance(change, dict):
            raise MalformedItemError("Gmail history change is invalid")
        message = change.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("id"), str):
            raise MalformedItemError("Gmail history message is invalid")
        result.append(message["id"])
    return tuple(result)
