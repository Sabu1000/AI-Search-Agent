"""Read-only, bounded Google Drive API client and file normalization."""

from __future__ import annotations

import math
import re
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import quote, urlparse

import httpx
from pydantic import SecretStr
from uas_connector_sdk import Credentials, DocumentPerson, NormalizedDocument, Provider
from uas_connector_sdk.errors import (
    AuthenticationError,
    MalformedItemError,
    PermissionDeniedError,
    ProviderUnavailableError,
    RateLimitError,
)

from .google import DRIVE_READONLY_SCOPE, GOOGLE_TOKEN_ENDPOINT

DRIVE_API_ROOT = "https://www.googleapis.com/drive/v3"
DRIVE_PAGE_SIZE = 100
DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
DRIVE_SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"
MAX_DRIVE_DOWNLOAD_BYTES = 20 * 1024 * 1024
_EXPIRY_SKEW = timedelta(minutes=2)
_FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,512}\Z")
_ALLOWED_LINK_HOSTS = frozenset(
    {"drive.google.com", "docs.google.com", "sheets.google.com", "slides.google.com"}
)


@dataclass(frozen=True)
class DriveItem:
    id: str
    name: str
    mime_type: str
    modified_at: datetime
    parent_ids: tuple[str, ...]
    owners: tuple[DocumentPerson, ...]
    web_view_link: str | None
    size: int | None
    drive_id: str | None
    shortcut_target_id: str | None
    shortcut_target_mime_type: str | None

    @property
    def is_folder(self) -> bool:
        return self.mime_type == DRIVE_FOLDER_MIME_TYPE

    @property
    def is_shortcut(self) -> bool:
        return self.mime_type == DRIVE_SHORTCUT_MIME_TYPE


@dataclass(frozen=True)
class DrivePage:
    items: tuple[DriveItem, ...]
    next_page_token: str | None


class DriveDownloadTooLargeError(Exception):
    """The provider file exceeded the connector's fixed download limit."""


def _provider_error(response: httpx.Response) -> Exception:
    if response.status_code == 401:
        return AuthenticationError()
    if response.status_code == 403:
        return PermissionDeniedError()
    if response.status_code == 429:
        retry_after: float | None = None
        with suppress(KeyError, ValueError):
            retry_after = float(response.headers["Retry-After"])
            if not math.isfinite(retry_after) or retry_after < 0:
                retry_after = None
        return RateLimitError(retry_after)
    if response.status_code >= 500:
        return ProviderUnavailableError()
    return MalformedItemError("Drive returned an invalid response")


def _json(response: httpx.Response) -> dict[str, object]:
    if not response.is_success:
        raise _provider_error(response)
    try:
        value = response.json()
    except ValueError as error:
        raise MalformedItemError("Drive returned malformed JSON") from error
    if not isinstance(value, dict):
        raise MalformedItemError("Drive returned an invalid payload")
    return cast(dict[str, object], value)


def _text(value: object, field: str, *, maximum: int = 2_000) -> str:
    if not isinstance(value, str):
        raise MalformedItemError(f"Drive {field} is invalid")
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFC", value)
        if character in "\n\t" or unicodedata.category(character) != "Cc"
    ).strip()
    if not normalized or len(normalized) > maximum:
        raise MalformedItemError(f"Drive {field} is invalid")
    return normalized


def parse_drive_item(value: object) -> DriveItem:
    if not isinstance(value, dict):
        raise MalformedItemError("Drive file is invalid")
    item_id = _text(value.get("id"), "file ID", maximum=512)
    if _FILE_ID.fullmatch(item_id) is None:
        raise MalformedItemError("Drive file ID is invalid")
    name = _text(value.get("name"), "file name")
    mime_type = _text(value.get("mimeType"), "MIME type", maximum=255)
    modified = _text(value.get("modifiedTime"), "modified time", maximum=64)
    try:
        modified_at = datetime.fromisoformat(modified.replace("Z", "+00:00"))
    except ValueError as error:
        raise MalformedItemError("Drive modified time is invalid") from error
    if modified_at.utcoffset() != UTC.utcoffset(modified_at):
        raise MalformedItemError("Drive modified time is invalid")

    parents = value.get("parents", [])
    if not isinstance(parents, list) or not all(
        isinstance(parent, str) and _FILE_ID.fullmatch(parent) for parent in parents
    ):
        raise MalformedItemError("Drive parents are invalid")
    raw_owners = value.get("owners", [])
    if not isinstance(raw_owners, list) or len(raw_owners) > 100:
        raise MalformedItemError("Drive owners are invalid")
    owners: list[DocumentPerson] = []
    for raw_owner in raw_owners:
        if not isinstance(raw_owner, dict):
            raise MalformedItemError("Drive owner is invalid")
        email, permission_id, display = (
            raw_owner.get("emailAddress"),
            raw_owner.get("permissionId"),
            raw_owner.get("displayName"),
        )
        identifier = email if isinstance(email, str) and email else permission_id
        if not isinstance(identifier, str) or not identifier:
            raise MalformedItemError("Drive owner identity is invalid")
        owners.append(
            DocumentPerson(
                relationship="owner",
                identity_kind="email" if identifier == email else "provider_user",
                normalized_identifier=identifier[:512],
                display_name=display[:500] if isinstance(display, str) else None,
            )
        )

    size_value = value.get("size")
    size: int | None = None
    if size_value is not None:
        try:
            size = int(size_value)
        except (TypeError, ValueError) as error:
            raise MalformedItemError("Drive file size is invalid") from error
        if isinstance(size_value, bool) or size < 0:
            raise MalformedItemError("Drive file size is invalid")
    drive_id = value.get("driveId")
    if drive_id is not None and (
        not isinstance(drive_id, str) or _FILE_ID.fullmatch(drive_id) is None
    ):
        raise MalformedItemError("Drive shared-drive ID is invalid")
    target_id: str | None = None
    target_mime: str | None = None
    shortcut = value.get("shortcutDetails")
    if mime_type == DRIVE_SHORTCUT_MIME_TYPE:
        if not isinstance(shortcut, dict):
            raise MalformedItemError("Drive shortcut is invalid")
        target_id = _text(shortcut.get("targetId"), "shortcut target", maximum=512)
        target_mime = _text(
            shortcut.get("targetMimeType"), "shortcut MIME type", maximum=255
        )
        if _FILE_ID.fullmatch(target_id) is None:
            raise MalformedItemError("Drive shortcut target is invalid")
    return DriveItem(
        id=item_id,
        name=name,
        mime_type=mime_type,
        modified_at=modified_at,
        parent_ids=tuple(sorted(set(parents))),
        owners=tuple(owners),
        web_view_link=_safe_link(value.get("webViewLink")),
        size=size,
        drive_id=drive_id,
        shortcut_target_id=target_id,
        shortcut_target_mime_type=target_mime,
    )


def normalize_drive_item(
    item: DriveItem, *, logical_path: tuple[str, ...]
) -> NormalizedDocument:
    if item.is_folder:
        raise MalformedItemError("Drive folders are traversal records")
    path = "/".join((*logical_path, item.name))
    details = [f"Name: {item.name}", f"Type: {item.mime_type}", f"Path: {path}"]
    if item.is_shortcut:
        details.append("Shortcut: indexed without following its target")
    metadata: dict[str, object] = {
        "file_id": item.id,
        "logical_path": path,
        "parent_ids": list(item.parent_ids),
    }
    for key, value in (
        ("drive_id", item.drive_id),
        ("size", item.size),
        ("shortcut_target_id", item.shortcut_target_id),
        ("shortcut_target_mime_type", item.shortcut_target_mime_type),
    ):
        if value is not None:
            metadata[key] = value
    return NormalizedDocument(
        external_id=item.id,
        provider=Provider.GOOGLE_DRIVE,
        source_type="file",
        title=item.name,
        content="\n".join(details),
        canonical_url=item.web_view_link
        or f"https://drive.google.com/open?id={quote(item.id, safe='')}",
        mime_type=item.mime_type,
        authors=tuple(
            person.display_name or person.normalized_identifier
            for person in item.owners
        ),
        modified_at=item.modified_at,
        people=item.owners,
        provider_metadata=metadata,
    )


def _safe_link(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MalformedItemError("Drive web link is invalid")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_LINK_HOSTS
        or parsed.username
        or parsed.password
    ):
        raise MalformedItemError("Drive web link is invalid")
    return value


class HttpDriveClient:
    """Read-only Drive list adapter with explicit page and response bounds."""

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

    async def ensure_fresh(self, credentials: Credentials) -> Credentials:
        if (
            credentials.expires_at is not None
            and credentials.expires_at > datetime.now(UTC) + _EXPIRY_SKEW
        ):
            return credentials
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
        if DRIVE_READONLY_SCOPE not in scopes:
            raise PermissionDeniedError()
        return Credentials(
            access_token=SecretStr(access_token),
            refresh_token=credentials.refresh_token,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            scopes=tuple(sorted(scopes)),
        )

    async def children_page(
        self,
        *,
        access_token: str,
        folder_id: str,
        page_token: str | None = None,
        drive_id: str | None = None,
    ) -> DrivePage:
        if _FILE_ID.fullmatch(folder_id) is None or (
            drive_id is not None and _FILE_ID.fullmatch(drive_id) is None
        ):
            raise MalformedItemError("Drive folder selection is invalid")
        params: dict[str, str | int] = {
            "fields": (
                "nextPageToken,files(id,name,mimeType,modifiedTime,parents,"
                "owners(displayName,emailAddress,permissionId),webViewLink,size,"
                "driveId,shortcutDetails(targetId,targetMimeType))"
            ),
            "includeItemsFromAllDrives": "true",
            "orderBy": "folder,name,id",
            "pageSize": DRIVE_PAGE_SIZE,
            "q": f"'{folder_id}' in parents and trashed = false",
            "supportsAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        if drive_id:
            params.update({"corpora": "drive", "driveId": drive_id})
        payload = await self._get("files", access_token=access_token, params=params)
        files = payload.get("files", [])
        if not isinstance(files, list) or len(files) > DRIVE_PAGE_SIZE:
            raise MalformedItemError("Drive file listing is invalid")
        items = tuple(parse_drive_item(value) for value in files)
        if len({item.id for item in items}) != len(items):
            raise MalformedItemError("Drive page contains duplicate file IDs")
        next_page_token = payload.get("nextPageToken")
        if next_page_token is not None and (
            not isinstance(next_page_token, str) or not next_page_token
        ):
            raise MalformedItemError("Drive page token is invalid")
        return DrivePage(items, next_page_token)

    async def download_file(self, *, access_token: str, file_id: str) -> bytes:
        """Download one regular Drive file with a strict response-size bound."""

        if _FILE_ID.fullmatch(file_id) is None:
            raise MalformedItemError("Drive file selection is invalid")
        return await self._download(
            access_token=access_token,
            path=f"files/{quote(file_id, safe='')}",
            params={"alt": "media", "supportsAllDrives": "true"},
        )

    async def export_file(
        self, *, access_token: str, file_id: str, mime_type: str
    ) -> bytes:
        """Export one native Google file into a bounded parseable representation."""

        if _FILE_ID.fullmatch(file_id) is None or not mime_type or len(mime_type) > 255:
            raise MalformedItemError("Drive export selection is invalid")
        return await self._download(
            access_token=access_token,
            path=f"files/{quote(file_id, safe='')}/export",
            params={"mimeType": mime_type},
        )

    async def _download(
        self, *, access_token: str, path: str, params: dict[str, str]
    ) -> bytes:
        try:
            async with (
                httpx.AsyncClient(timeout=30, transport=self._transport) as client,
                client.stream(
                    "GET",
                    f"{DRIVE_API_ROOT}/{path}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params=params,
                ) as response,
            ):
                if not response.is_success:
                    raise _provider_error(response)
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except ValueError as error:
                        raise MalformedItemError(
                            "Drive download length is invalid"
                        ) from error
                    if declared_size < 0:
                        raise MalformedItemError("Drive download length is invalid")
                    if declared_size > MAX_DRIVE_DOWNLOAD_BYTES:
                        raise DriveDownloadTooLargeError
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > MAX_DRIVE_DOWNLOAD_BYTES:
                        raise DriveDownloadTooLargeError
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as error:
            raise ProviderUnavailableError() from error

    async def _get(
        self,
        path: str,
        *,
        access_token: str,
        params: dict[str, str | int],
    ) -> dict[str, object]:
        try:
            async with httpx.AsyncClient(
                timeout=20, transport=self._transport
            ) as client:
                response = await client.get(
                    f"{DRIVE_API_ROOT}/{path}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params=params,
                )
        except httpx.HTTPError as error:
            raise ProviderUnavailableError() from error
        return _json(response)
