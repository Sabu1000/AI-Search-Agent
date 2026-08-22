from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr
from uas_connector_sdk import Credentials, Provider
from uas_connector_sdk.errors import (
    AuthenticationError,
    MalformedItemError,
    PermissionDeniedError,
    ProviderUnavailableError,
    RateLimitError,
)

from universal_ai_search.connections.drive import (
    DRIVE_FOLDER_MIME_TYPE,
    DRIVE_SHORTCUT_MIME_TYPE,
    MAX_DRIVE_DOWNLOAD_BYTES,
    DriveDownloadTooLargeError,
    HttpDriveClient,
    normalize_drive_item,
    parse_drive_item,
)
from universal_ai_search.connections.google import (
    DRIVE_READONLY_SCOPE,
    GOOGLE_TOKEN_ENDPOINT,
)


def file_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "file_1",
        "name": "Cafe\u0301 plan.txt",
        "mimeType": "text/plain",
        "modifiedTime": "2026-08-20T12:30:00Z",
        "parents": ["folder_1"],
        "owners": [
            {
                "displayName": "Owner",
                "emailAddress": "Owner@Example.test",
                "permissionId": "permission_1",
            }
        ],
        "size": "42",
        "webViewLink": "https://drive.google.com/file/d/file_1/view",
    }
    payload.update(updates)
    return payload


def test_drive_item_normalizes_stable_metadata_and_inert_descriptor() -> None:
    item = parse_drive_item(file_payload())
    document = normalize_drive_item(item, logical_path=("Projects",))

    assert item.name == "Café plan.txt"
    assert document.provider is Provider.GOOGLE_DRIVE
    assert document.external_id == "file_1"
    assert document.title == "Café plan.txt"
    assert document.content == (
        "Name: Café plan.txt\nType: text/plain\nPath: Projects/Café plan.txt"
    )
    assert document.modified_at == datetime(2026, 8, 20, 12, 30, tzinfo=UTC)
    assert document.authors == ("Owner",)
    assert document.people[0].normalized_identifier == "owner@example.test"
    assert document.provider_metadata == {
        "file_id": "file_1",
        "logical_path": "Projects/Café plan.txt",
        "parent_ids": ["folder_1"],
        "size": 42,
    }


def test_drive_shortcut_is_described_without_following_target() -> None:
    item = parse_drive_item(
        file_payload(
            id="shortcut_1",
            name="Outside shortcut",
            mimeType=DRIVE_SHORTCUT_MIME_TYPE,
            shortcutDetails={"targetId": "outside_1", "targetMimeType": "text/plain"},
        )
    )
    document = normalize_drive_item(item, logical_path=("Selected",))

    assert item.is_shortcut
    assert "without following" in document.content
    assert document.provider_metadata["shortcut_target_id"] == "outside_1"


@pytest.mark.asyncio
async def test_client_lists_one_bounded_selected_folder_page() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "files": [
                    file_payload(),
                    file_payload(
                        id="folder_2",
                        name="Subfolder",
                        mimeType=DRIVE_FOLDER_MIME_TYPE,
                    ),
                ],
                "nextPageToken": "page_2",
            },
        )

    page = await HttpDriveClient(
        client_id="client",
        client_secret="secret",
        transport=httpx.MockTransport(handler),
    ).children_page(access_token="access", folder_id="folder_1", drive_id="shared_1")

    assert [item.id for item in page.items] == ["file_1", "folder_2"]
    assert page.items[1].is_folder
    assert page.next_page_token == "page_2"
    request = seen[0]
    assert request.headers["Authorization"] == "Bearer access"
    assert request.url.params["pageSize"] == "100"
    assert request.url.params["q"] == "'folder_1' in parents and trashed = false"
    assert request.url.params["corpora"] == "drive"
    assert request.url.params["driveId"] == "shared_1"


@pytest.mark.asyncio
async def test_client_downloads_file_media_with_read_only_bounded_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"pdf-data", headers={"Content-Length": "8"})

    client = HttpDriveClient(
        client_id="client",
        client_secret="secret",
        transport=httpx.MockTransport(handler),
    )

    assert (
        await client.download_file(access_token="access", file_id="file_1")
        == b"pdf-data"
    )
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/drive/v3/files/file_1"
    assert dict(request.url.params) == {
        "alt": "media",
        "supportsAllDrives": "true",
    }
    assert request.headers["Authorization"] == "Bearer access"


@pytest.mark.asyncio
async def test_client_rejects_declared_and_streamed_oversized_downloads() -> None:
    declared = HttpDriveClient(
        client_id="c",
        client_secret="s",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"Content-Length": str(MAX_DRIVE_DOWNLOAD_BYTES + 1)}
            )
        ),
    )
    with pytest.raises(DriveDownloadTooLargeError):
        await declared.download_file(access_token="access", file_id="file_1")

    streamed = HttpDriveClient(
        client_id="c",
        client_secret="s",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"x" * (MAX_DRIVE_DOWNLOAD_BYTES + 1))
        ),
    )
    with pytest.raises(DriveDownloadTooLargeError):
        await streamed.download_file(access_token="access", file_id="file_1")

    malformed = HttpDriveClient(
        client_id="c",
        client_secret="s",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"Content-Length": "invalid"})
        ),
    )
    with pytest.raises(MalformedItemError):
        await malformed.download_file(access_token="access", file_id="file_1")


@pytest.mark.asyncio
async def test_client_rejects_unsafe_selection_and_malformed_pages() -> None:
    client = HttpDriveClient(
        client_id="client",
        client_secret="secret",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"files": []})
        ),
    )
    with pytest.raises(MalformedItemError):
        await client.children_page(access_token="access", folder_id="root' or true")

    duplicate = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"files": [file_payload(), file_payload()]})
    )
    client = HttpDriveClient(client_id="c", client_secret="s", transport=duplicate)
    with pytest.raises(MalformedItemError):
        await client.children_page(access_token="access", folder_id="root")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (429, RateLimitError),
        (503, ProviderUnavailableError),
        (400, MalformedItemError),
    ],
)
async def test_client_classifies_provider_failures(
    status: int, expected: type[Exception]
) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(status, headers={"Retry-After": "7"})
    )
    client = HttpDriveClient(client_id="c", client_secret="s", transport=transport)

    with pytest.raises(expected) as failure:
        await client.children_page(access_token="access", folder_id="root")
    if status == 429:
        assert isinstance(failure.value, RateLimitError)
        assert failure.value.retry_after_seconds == 7


@pytest.mark.asyncio
async def test_client_refreshes_drive_credentials_and_requires_scope() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == GOOGLE_TOKEN_ENDPOINT
        return httpx.Response(
            200,
            json={
                "access_token": "fresh",
                "expires_in": 3600,
                "scope": DRIVE_READONLY_SCOPE,
            },
        )

    client = HttpDriveClient(
        client_id="client",
        client_secret="secret",
        transport=httpx.MockTransport(handler),
    )
    stale = Credentials(
        access_token=SecretStr("old"),
        refresh_token=SecretStr("refresh"),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        scopes=(DRIVE_READONLY_SCOPE,),
    )
    fresh = await client.ensure_fresh(stale)
    assert fresh.access_token.get_secret_value() == "fresh"
    assert len(requests) == 1
    assert await client.ensure_fresh(fresh) == fresh

    no_refresh = Credentials(access_token=SecretStr("old"))
    with pytest.raises(AuthenticationError):
        await client.ensure_fresh(no_refresh)

    denied_client = HttpDriveClient(
        client_id="client",
        client_secret="secret",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"access_token": "fresh", "expires_in": 3600, "scope": "x"}
            )
        ),
    )
    with pytest.raises(PermissionDeniedError):
        await denied_client.ensure_fresh(stale)


def test_drive_item_rejects_unsafe_links_folders_and_invalid_fields() -> None:
    with pytest.raises(MalformedItemError):
        parse_drive_item(file_payload(webViewLink="https://evil.example/file"))
    with pytest.raises(MalformedItemError):
        parse_drive_item(file_payload(id="bad id"))
    folder = parse_drive_item(file_payload(mimeType=DRIVE_FOLDER_MIME_TYPE))
    with pytest.raises(MalformedItemError):
        normalize_drive_item(folder, logical_path=())
