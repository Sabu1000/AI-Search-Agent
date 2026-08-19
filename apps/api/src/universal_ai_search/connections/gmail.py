"""Read-only Gmail API client and deterministic message normalization."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from typing import cast

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

from .email_parser import decode_mime_header, parse_gmail_payload
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


def _headers(payload: dict[str, object]) -> dict[str, str]:
    headers = payload.get("headers")
    if not isinstance(headers, list):
        return {}
    result: dict[str, str] = {}
    for header in headers:
        if not isinstance(header, dict):
            continue
        name, value = header.get("name"), header.get("value")
        if isinstance(name, str) and isinstance(value, str):
            result.setdefault(name.casefold(), decode_mime_header(value))
    return result


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

    fields = _headers(cast(dict[str, object], payload))
    subject = fields.get("subject") or "(no subject)"
    sender = fields.get("from", "")
    author = parseaddr(sender)[1] or sender
    parsed_body = parse_gmail_payload(cast(dict[str, object], payload))
    body = parsed_body.text
    preamble = [
        f"Subject: {subject}",
        *((f"From: {sender}",) if sender else ()),
        *((f"To: {fields['to']}",) if fields.get("to") else ()),
        *((f"Cc: {fields['cc']}",) if fields.get("cc") else ()),
        *((f"Date: {fields['date']}",) if fields.get("date") else ()),
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
        authors=(author[:500],) if author else (),
        created_at=sent_at,
        provider_metadata=metadata,
    )


class HttpGmailClient:
    """Small async adapter around only the Gmail endpoints required for full sync."""

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
        if not isinstance(references, list):
            raise MalformedItemError("Gmail message listing is invalid")
        documents: list[NormalizedDocument] = []
        for reference in references:
            if not isinstance(reference, dict) or not isinstance(
                reference.get("id"), str
            ):
                raise MalformedItemError("Gmail message reference is invalid")
            message = await self._get(
                f"messages/{reference['id']}",
                access_token=access_token,
                params={"format": "full"},
            )
            documents.append(normalize_gmail_message(message))
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
        if not isinstance(records, list):
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
            documents.append(normalize_gmail_message(_json(message_response)))

        history_id = listing.get("historyId")
        next_page_token = listing.get("nextPageToken")
        if not isinstance(history_id, str) or not history_id:
            raise MalformedItemError("Gmail history cursor is invalid")
        if next_page_token is not None and not isinstance(next_page_token, str):
            raise MalformedItemError("Gmail history page token is invalid")
        return GmailHistoryPage(
            tuple(documents), tuple(sorted(deleted)), history_id, next_page_token
        )

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
