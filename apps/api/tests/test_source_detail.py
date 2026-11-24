from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from universal_ai_search.api.auth import AuthenticationMode, Principal, WorkspaceContext
from universal_ai_search.api.pagination import CursorSigner
from universal_ai_search.api.problems import install_problem_handlers
from universal_ai_search.api.routes.source_detail import router
from universal_ai_search.search.source_detail import (
    CitationLocation,
    Provider,
    SourceDetail,
    SourceListEntry,
    safe_open_url,
)

SOURCE = uuid4()
WORKSPACE = uuid4()
USER = uuid4()
SESSION = uuid4()
CHUNK = uuid4()
URL = "https://github.com/sample/repository/pull/37#discussion_r12"


@pytest.mark.parametrize(
    ("provider", "url", "expected"),
    [
        ("github", URL, URL),
        (
            "github",
            "https://github.com/owner/repo/issues/17",
            "https://github.com/owner/repo/issues/17",
        ),
        (
            "github",
            f"https://github.com/owner/r/blob/{'f' * 40}/x%20y.py#L1-L9",
            f"https://github.com/owner/r/blob/{'f' * 40}/x%20y.py#L1-L9",
        ),
        (
            "github",
            f"https://github.com/owner/r/commit/{'f' * 40}",
            f"https://github.com/owner/r/commit/{'f' * 40}",
        ),
        (
            "gmail",
            "https://mail.google.com/mail/u/0/#all/thread-17",
            "https://mail.google.com/mail/u/0/#all/thread-17",
        ),
        (
            "google_drive",
            "https://drive.google.com/open?id=sample-17",
            "https://drive.google.com/open?id=sample-17",
        ),
        (
            "google_drive",
            "https://drive.google.com/file/d/id/view?resourcekey=private",
            "https://drive.google.com/file/d/id/view",
        ),
        (
            "google_drive",
            "https://drive.google.com/drive/folders/id/",
            "https://drive.google.com/drive/folders/id/",
        ),
        (
            "google_drive",
            "https://docs.google.com/document/d/id/edit?usp=sharing",
            "https://docs.google.com/document/d/id/edit",
        ),
        (
            "google_drive",
            "https://sheets.google.com/spreadsheets/d/id/edit#gid=0",
            "https://sheets.google.com/spreadsheets/d/id/edit",
        ),
        ("local_files", URL, None),
        ("github", "https://github.com.evil.example/sample/repository/pull/37", None),
        ("github", "https://github.com@evil.example/sample/repository/pull/37", None),
        ("github", "https://user@github.com/sample/repository/pull/37", None),
        ("github", "https://github.com:443/sample/repository/pull/37", None),
        ("github", "https://github.com/sample/repository/pull/37?next=evil", None),
        ("github", "https://github.com/login/device/code", None),
        (
            "github",
            "https://github.com/sample/repository/pull/37#http://evil.example",
            None,
        ),
        ("github", f"https://github.com/o/r/blob/{'f' * 40}/%2e%2e/x.txt", None),
        ("github", f"https://github.com/o/r/blob/{'f' * 40}/%252e%252e", None),
        ("github", f"https://github.com/o/r/blob/{'f' * 40}/s%2fpath", None),
        ("github", f"https://github.com/o/r/blob/{'f' * 40}/s%5cpath", None),
        ("github", f"https://github.com/o/r/blob/{'f' * 40}/%00", None),
        ("github", f"https://github.com/o/r/blob/{'f' * 40}/%ff", None),
        ("github", f"https://github.com/o/r/blob/{'f' * 40}/🚀.txt", None),
        ("github", "https://[invalid", None),
        ("github", "http://github.com/owner/repository/pull/37", None),
        ("github", None, None),
        ("github", "/" * 5000, None),
        ("gmail", "https://mail.google.com/mail/u/0/#all/../../private", None),
        ("google_drive", "https://drive.google.com/open?id=id&next=evil", None),
        ("google_drive", "https://docs.google.com/redirect?url=http://evil", None),
    ],
)
def test_provider_targets_require_exact_resource_paths(
    provider: str, url: str | None, expected: str | None
) -> None:
    assert safe_open_url(cast(Provider, provider), url) == expected


class _Authentication:
    def __init__(self, mode: AuthenticationMode, has_session: bool = True) -> None:
        self.mode = mode
        self.has_session = has_session

    async def authenticate(self, _request: object) -> Principal:
        return Principal(
            subject_id=USER,
            mode=self.mode,
            session_id=SESSION if self.has_session else None,
            authorization_version=1,
        )


class _AuthorizeWorkspace:
    def __init__(self, other_user: bool = False) -> None:
        self.user = uuid4() if other_user else USER

    async def authorize(
        self, *, principal: Principal, workspace_id: object
    ) -> WorkspaceContext | None:
        if workspace_id != WORKSPACE or principal.subject_id != USER:
            return None
        return WorkspaceContext(
            workspace_id=WORKSPACE,
            user_id=self.user,
            role="owner",
            authorization_version=1,
        )


def _app(
    *,
    mode: AuthenticationMode = AuthenticationMode.BEARER,
    has_session: bool = True,
    other_user: bool = False,
) -> tuple[AsyncMock, TestClient]:
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    install_problem_handlers(app)
    repository = AsyncMock()
    app.state.source_detail_repository = repository
    app.state.source_cursor_signer = CursorSigner(b"s" * 32)
    app.state.authentication_backend = _Authentication(mode, has_session)
    app.state.workspace_authorization_backend = _AuthorizeWorkspace(other_user)
    return repository, TestClient(app)


def test_detail_route_materializes_only_the_safe_schema() -> None:
    repository, client = _app()
    repository.get.return_value = SourceDetail(
        SOURCE,
        "github",
        "review",
        "Synthetic change",
        "text/markdown",
        datetime(2026, 8, 2, tzinfo=UTC),
        "modified",
        "ready",
        "idle",
        None,
        URL,
        CitationLocation(CHUNK, None, 5, 18),
    )
    result = client.get(
        f"/v1/sources/{SOURCE}",
        params={"chunk_id": str(CHUNK)},
        headers={"X-Workspace-ID": str(WORKSPACE)},
    )
    assert result.status_code == 200
    assert result.headers["cache-control"] == "private, no-store"
    assert result.json() == {
        "source_id": str(SOURCE),
        "provider": "github",
        "source_type": "review",
        "title": "Synthetic change",
        "mime_type": "text/markdown",
        "source_timestamp": "2026-08-02T00:00:00Z",
        "source_timestamp_kind": "modified",
        "index_state": "ready",
        "sync_state": "idle",
        "failure_code": None,
        "target": {"kind": "provider_url", "open_url": URL},
        "citation_location": {
            "chunk_id": str(CHUNK),
            "page": None,
            "line_start": 5,
            "line_end": 18,
        },
    }
    repository.get.assert_awaited_once_with(
        workspace_id=WORKSPACE,
        user_id=USER,
        session_id=SESSION,
        authorization_version=1,
        user_authorization_version=1,
        source_id=SOURCE,
        chunk_id=CHUNK,
    )


def test_detail_route_uses_one_not_found_for_absent_stale_or_forbidden() -> None:
    repository, client = _app()
    repository.get.return_value = None
    result = client.get(
        f"/v1/sources/{SOURCE}", headers={"X-Workspace-ID": str(WORKSPACE)}
    )
    assert result.status_code == 404
    assert result.json()["code"] == "SOURCE_NOT_FOUND"


@pytest.mark.parametrize("mode", [AuthenticationMode.DEVICE, AuthenticationMode.BEARER])
def test_detail_refuses_non_session_authentication(mode: AuthenticationMode) -> None:
    repository, client = _app(
        mode=mode, has_session=(mode == AuthenticationMode.DEVICE)
    )
    result = client.get(
        f"/v1/sources/{SOURCE}", headers={"X-Workspace-ID": str(WORKSPACE)}
    )
    assert result.status_code == 401
    repository.get.assert_not_awaited()


def test_detail_rejects_mismatched_workspace_authority_and_malformed_chunk() -> None:
    repository, client = _app(other_user=True)
    path = f"/v1/sources/{SOURCE}"
    headers = {"X-Workspace-ID": str(WORKSPACE)}
    assert client.get(path, headers=headers).status_code == 404
    assert (
        client.get(path, headers=headers, params={"chunk_id": "bad"}).status_code == 422
    )
    repository.get.assert_not_awaited()


def test_source_list_signed_keyset_resumes_same_normalized_filter() -> None:
    repository, client = _app()
    utc = datetime(2026, 9, 1, tzinfo=UTC)
    first = SourceDetail(
        SOURCE,
        "github",
        "file",
        "Synthetic source",
        "text/markdown",
        utc,
        "modified",
        "ready",
        "idle",
        None,
        URL,
        None,
    )
    second = SourceDetail(
        uuid4(),
        "gmail",
        "email",
        "Synthetic mail",
        "text/plain",
        utc,
        "sent",
        "ready",
        "idle",
        None,
        None,
        None,
    )
    repository.list.side_effect = [
        [SourceListEntry(first, utc), SourceListEntry(second, utc)],
        [SourceListEntry(second, utc)],
    ]
    headers = {"X-Workspace-ID": str(WORKSPACE)}
    response = client.get(
        "/v1/sources",
        headers=headers,
        params=[("provider", "github"), ("provider", "gmail"), ("limit", "1")],
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    payload = response.json()
    assert [item["source_id"] for item in payload["items"]] == [str(SOURCE)]
    assert payload["page"]["has_more"]
    cursor = payload["page"]["next_cursor"]
    repository.list.assert_awaited_once_with(
        workspace_id=WORKSPACE,
        user_id=USER,
        session_id=SESSION,
        authorization_version=1,
        user_authorization_version=1,
        providers=("github", "gmail"),
        source_types=(),
        before=None,
        limit=2,
    )
    changed_filter = client.get(
        "/v1/sources",
        headers=headers,
        params={"provider": "github", "cursor": cursor, "limit": 1},
    )
    assert changed_filter.status_code == 400
    assert changed_filter.json()["code"] == "CURSOR_CONTEXT_MISMATCH"
    modified = ("a" if cursor[0] != "a" else "b") + cursor[1:]
    tampered = client.get(
        "/v1/sources",
        headers=headers,
        params=[("provider", "gmail"), ("provider", "github"), ("cursor", modified)],
    )
    assert tampered.status_code == 400
    assert tampered.json()["code"] == "CURSOR_INVALID"

    resumed = client.get(
        "/v1/sources",
        headers=headers,
        params=[
            ("provider", "gmail"),
            ("provider", "github"),
            ("cursor", cursor),
            ("limit", "1"),
        ],
    )
    assert resumed.status_code == 200
    assert [item["source_id"] for item in resumed.json()["items"]] == [
        str(second.source_id)
    ]
    assert resumed.json()["page"] == {"has_more": False, "next_cursor": None}
    assert repository.list.await_args.kwargs["before"] == (utc, SOURCE)


@pytest.mark.parametrize(
    "params", [{"provider": "unrecognized"}, {"source_type": "bad"}, {"limit": 101}]
)
def test_source_list_limits_and_enum_filters_are_validated(
    params: dict[str, str | int],
) -> None:
    repo, client = _app()
    result = client.get(
        "/v1/sources",
        headers={"X-Workspace-ID": str(WORKSPACE)},
        params=params,
    )
    assert result.status_code == 422
    repo.list.assert_not_awaited()
