"""GitHub discussion adapter contracts with synthetic provider responses."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from uas_connector_sdk import NormalizedDocument, Provider
from uas_connector_sdk.errors import (
    AuthenticationError,
    MalformedItemError,
    PermissionDeniedError,
    ProviderUnavailableError,
    RateLimitError,
)

from universal_ai_search.connections.github import GitHubInstallationToken
from universal_ai_search.connections.github_issues import GitHubDiscussionClient

NOW = datetime(2026, 11, 24, tzinfo=UTC)
ROOT = "/repos/owner/project"
API = f"https://api.github.com{ROOT}"
HTML = "https://github.com/owner/project"
CREDENTIAL = GitHubInstallationToken("synthetic-bearer", NOW + timedelta(minutes=55))
TIMES = {
    "created_at": "2026-11-01T10:00:00Z",
    "updated_at": "2026-11-02T09:00:00Z",
}


def item(number: int = 1, **overrides: object) -> dict[str, object]:
    return {
        **TIMES,
        "id": 1000 + number,
        "number": number,
        "title": " a discussion ",
        "body": "Useful info\r\n\u202edetails.",
        "state": "open",
        "user": {"login": "TestUser"},
        **overrides,
    }


def comment(
    identifier: int,
    url: str = f"{API}/issues/1",
    *,
    review: bool = False,
) -> dict[str, object]:
    return {
        **TIMES,
        "id": identifier,
        "body": "Follow-up discussion.",
        "user": {"login": "review-helper[bot]"},
        "pull_request_url" if review else "issue_url": url,
    }


def client(
    respond: Callable[[httpx.Request], httpx.Response],
) -> GitHubDiscussionClient:
    return GitHubDiscussionClient(
        repository_ids=frozenset({123}),
        transport=httpx.MockTransport(respond),
        now=lambda: NOW,
    )


async def scan(
    gateway: GitHubDiscussionClient,
    *,
    repository_id: int = 123,
    repository: str = "project",
    token: GitHubInstallationToken = CREDENTIAL,
) -> tuple[NormalizedDocument, ...]:
    return await gateway.scan(
        installation_id=14,
        repository_id=repository_id,
        owner="owner",
        repository=repository,
        token=token,
    )


async def test_scans_distinct_namespaces_with_canonical_citations() -> None:
    seen: list[httpx.Request] = []
    responses = {
        ROOT: {"id": 123, "private": True},
        f"{ROOT}/issues": [item(), item(2, pull_request={"url": "ignored"})],
        f"{ROOT}/pulls": [item(2, id=2002, body=None)],
        f"{ROOT}/issues/comments": [
            comment(3001),
            comment(3002, f"{API}/issues/2"),
        ],
        f"{ROOT}/pulls/comments": [comment(4002, f"{API}/pulls/2", review=True)],
    }

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=responses[request.url.path])

    documents = await scan(client(respond))
    assert [document.source_type for document in documents] == [
        "issue",
        "pull_request",
        "issue",
        "pull_request",
        "review",
    ]
    assert [document.external_id for document in documents] == [
        "github:14:123:issue:1001",
        "github:14:123:pull_request:2002",
        "github:14:123:issue_comment:3001",
        "github:14:123:issue_comment:3002",
        "github:14:123:review_comment:4002",
    ]
    assert [document.canonical_url for document in documents] == [
        f"{HTML}/issues/1",
        f"{HTML}/pull/2",
        f"{HTML}/issues/1#issuecomment-3001",
        f"{HTML}/pull/2#issuecomment-3002",
        f"{HTML}/pull/2#discussion_r4002",
    ]
    assert documents[0].content == "Useful info\ndetails."
    assert documents[0].people[0].normalized_identifier == "testuser"
    assert documents[1].content == "a discussion"
    assert documents[-1].people[0].relationship == "reviewer"
    for document in documents:
        assert document.provider is Provider.GITHUB
        assert not document.access_metadata.public
        assert "synthetic-bearer" not in document.model_dump_json()
    assert all(request.method == "GET" for request in seen)
    assert all(
        request.headers["Authorization"] == "Bearer synthetic-bearer"
        for request in seen
    )
    assert all(request.url.host == "api.github.com" for request in seen)


async def test_excludes_secret_binary_and_oversized_text() -> None:
    secret_like = "ghp_" + "0" * 32
    oversized = "é" * (64 * 1024 + 1)

    def respond(request: httpx.Request) -> httpx.Response:
        payload: object = []
        if request.url.path == ROOT:
            payload = {"id": 123}
        elif request.url.path == f"{ROOT}/issues":
            payload = [
                item(body=secret_like),
                item(2, title=secret_like),
                item(3, body=oversized),
                item(4, body="embedded\x00binary"),
                item(5, user={"login": secret_like}),
            ]
        elif request.url.path == f"{ROOT}/issues/comments":
            payload = [comment(1234)]
        return httpx.Response(200, json=payload)

    documents = await scan(client(respond))
    assert len(documents) == 1
    assert documents[0].provider_metadata["number"] == 5
    assert not documents[0].authors


@pytest.mark.parametrize("repository", ["a/b", ".", "..", "x?token=z"])
async def test_scope_and_path_rejected_before_network(repository: str) -> None:
    gateway = client(lambda _: pytest.fail("Unexpected request"))
    with pytest.raises(PermissionDeniedError):
        await scan(gateway, repository=repository)
    with pytest.raises(PermissionDeniedError):
        await scan(gateway, repository_id=777)
    with pytest.raises(AuthenticationError):
        await scan(gateway, token=GitHubInstallationToken("expired", NOW))
    with pytest.raises(AuthenticationError):
        await scan(gateway, token=GitHubInstallationToken("not valid", NOW))


async def test_metadata_id_mismatch_and_redirect_stop_scan() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        assert request.url.path == ROOT
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"id": 456})
        return httpx.Response(302, headers={"Location": "https://wrong.example/"})

    with pytest.raises(PermissionDeniedError):
        await scan(client(respond))
    with pytest.raises(PermissionDeniedError):
        await scan(client(respond))
    assert calls == 2


async def test_pagination_uses_fixed_endpoint_and_ignores_arbitrary_link() -> None:
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == ROOT:
            return httpx.Response(200, json={"id": 123})
        assert request.url.host == "api.github.com"
        if request.url.path == f"{ROOT}/issues":
            page = request.url.params["page"]
            seen.append(page)
            if page == "1":
                return httpx.Response(
                    200,
                    json=[item(i) for i in range(1, 51)],
                    headers={"Link": '<https://wrong.example/>; rel="next"'},
                )
            return httpx.Response(200, json=[item(51)])
        return httpx.Response(200, json=[])

    assert len(await scan(client(respond))) == 51
    assert seen == ["1", "2"]


async def test_truncated_or_duplicate_scan_never_returns_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("universal_ai_search.connections.github_issues._MAX_PAGES", 1)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == ROOT:
            return httpx.Response(200, json={"id": 123})
        return httpx.Response(
            200,
            json=[item(i) for i in range(1, 51)],
            headers={"Link": "<https://api.github.com/unused>; rel=next"},
        )

    with pytest.raises(ProviderUnavailableError, match="page limit"):
        await scan(client(respond))

    def duplicate(request: httpx.Request) -> httpx.Response:
        if request.url.path == ROOT:
            return httpx.Response(200, json={"id": 123})
        return httpx.Response(200, json=[item(), item()])

    with pytest.raises(ProviderUnavailableError, match="scan changed"):
        await scan(client(duplicate))


@pytest.mark.parametrize(
    "row",
    [
        item(created_at="now"),
        item(created_at=None),
        item(updated_at="2026-10-01T12:00:00Z"),
        item(updated_at="2026-11-02T12:00:00+03:00"),
        item(id=True),
        item(state="draft"),
        item(title=""),
        item(body={}),
        False,
    ],
)
async def test_malformed_items_do_not_silently_delete(row: object) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": 123} if request.url.path == ROOT else [row]
        )

    with pytest.raises(MalformedItemError):
        await scan(client(respond))


@pytest.mark.parametrize(
    ("url", "exception"),
    [
        ("https://wrong.example/repos/owner/project/issues/1", MalformedItemError),
        (f"{API}/issues/2", ProviderUnavailableError),
    ],
)
async def test_rejects_cross_repository_or_missing_comment_parent(
    url: str, exception: type[Exception]
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                ROOT: {"id": 123},
                f"{ROOT}/issues": [item()],
                f"{ROOT}/pulls": [],
                f"{ROOT}/issues/comments": [comment(2, url)],
                f"{ROOT}/pulls/comments": [],
            }[request.url.path],
        )

    with pytest.raises(exception):
        await scan(client(respond))


@pytest.mark.parametrize(
    ("status", "headers", "exception"),
    [
        (401, {}, AuthenticationError),
        (403, {}, PermissionDeniedError),
        (404, {}, PermissionDeniedError),
        (403, {"Retry-After": "9"}, RateLimitError),
        (429, {"Retry-After": "invalid"}, RateLimitError),
        (503, {}, ProviderUnavailableError),
        (400, {}, MalformedItemError),
    ],
)
async def test_provider_errors_are_sanitized(
    status: int, headers: dict[str, str], exception: type[Exception]
) -> None:
    gateway = client(
        lambda _: httpx.Response(
            status, headers=headers, text="provider private details"
        )
    )
    with pytest.raises(exception) as raised:
        await scan(gateway)
    assert "provider private details" not in str(raised.value)
    if isinstance(raised.value, RateLimitError):
        assert raised.value.retry_after_seconds == (9 if status == 403 else None)


async def test_response_body_and_total_snapshot_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "universal_ai_search.connections.github_issues._MAX_PAGE_BYTES", 30
    )
    with pytest.raises(MalformedItemError, match="page is too large"):
        await scan(
            client(lambda _: httpx.Response(200, content=b"{" + b" " * 200 + b"}"))
        )
    monkeypatch.setattr(
        "universal_ai_search.connections.github_issues._MAX_PAGE_BYTES", 4 * 1024 * 1024
    )
    monkeypatch.setattr(
        "universal_ai_search.connections.github_issues._MAX_SNAPSHOT_TEXT", 1
    )
    gateway = client(
        lambda request: httpx.Response(
            200, json={"id": 123} if request.url.path == ROOT else [item()]
        )
    )
    with pytest.raises(ProviderUnavailableError, match="scan is too large"):
        await scan(gateway)


async def test_request_and_overall_timeout_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def disconnect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("provider private details", request=request)

    with pytest.raises(ProviderUnavailableError) as raised:
        await scan(client(disconnect))
    assert not raised.value.__cause__
    monkeypatch.setattr(
        "universal_ai_search.connections.github_issues._SNAPSHOT_TIMEOUT_SECONDS", 0
    )

    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        return httpx.Response(200, json={"id": 123})

    gateway = GitHubDiscussionClient(
        repository_ids=frozenset({123}),
        transport=httpx.MockTransport(slow),
        now=lambda: NOW,
    )
    with pytest.raises(ProviderUnavailableError):
        await scan(gateway)
