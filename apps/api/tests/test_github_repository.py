from __future__ import annotations

import base64
import hashlib
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

from universal_ai_search.connections.github import (
    GitHubAuthorizationError,
    GitHubInstallationToken,
)
from universal_ai_search.connections.github_repository import (
    GitHubScanLimits,
    HttpGitHubRepositoryClient,
    RepositoryScanLimitError,
)

NOW = datetime(2026, 11, 24, tzinfo=UTC)
INSTALLATION = 21
REPO = 42
TOKEN = "ghs_synthetic"
ROOT = "a" * 40
NESTED = "b" * 40
COMMIT = "e" * 40
BASE = "/repos/example/sample"
LIST = "/installation/repositories"
BRANCH = f"{BASE}/commits/heads/feature/default"
TREE = f"{BASE}/git/trees"
BLOBS = f"{BASE}/git/blobs"
type ResponsePayload = dict[str, object] | httpx.Response


class Gateway:
    def __init__(self, *, fail: bool = False, expired: bool = False) -> None:
        self.calls: list[tuple[int, frozenset[int]]] = []
        self.fail = fail
        self.expired = expired

    async def mint_repository_token(
        self, *, installation_id: int, repository_ids: frozenset[int]
    ) -> GitHubInstallationToken:
        self.calls.append((installation_id, repository_ids))
        if self.fail:
            raise GitHubAuthorizationError("must not leak")
        expiry = NOW if self.expired else NOW + timedelta(minutes=5)
        return GitHubInstallationToken(TOKEN, expiry)


def inventory(
    *, repository_id: int = REPO, name: str = "example/sample"
) -> dict[str, object]:
    return {
        "total_count": 1,
        "repositories": [
            {
                "id": repository_id,
                "full_name": name,
                "default_branch": "feature/default",
                "html_url": "https://never-follow.example",
            }
        ],
    }


def file(name: str, data: bytes) -> tuple[dict[str, object], dict[str, object]]:
    sha = hashlib.sha1(f"blob {len(data)}\0".encode() + data, usedforsecurity=False)
    value = sha.hexdigest()
    entry = {
        "path": name,
        "sha": value,
        "mode": "100644",
        "type": "blob",
        "size": len(data),
    }
    blob = {
        "sha": value,
        "size": len(data),
        "encoding": "base64",
        "content": base64.encodebytes(data).decode(),
        "url": "https://never-follow.example",
    }
    return entry, blob


def tree(sha: str, *entries: dict[str, object]) -> dict[str, object]:
    return {"sha": sha, "truncated": False, "tree": list(entries)}


def create(
    resources: dict[str, ResponsePayload],
    *,
    gateway: Gateway | None = None,
    limits: GitHubScanLimits | None = None,
    clock: Callable[[], datetime] = lambda: NOW,
) -> tuple[HttpGitHubRepositoryClient, Gateway, list[httpx.Request]]:
    token_gateway = gateway or Gateway()
    requests: list[httpx.Request] = []
    responses: dict[str, ResponsePayload] = {
        LIST: inventory(),
        BRANCH: {"sha": COMMIT, "commit": {"tree": {"sha": ROOT}}},
        **resources,
    }

    def respond(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        assert req.method == "GET"
        assert req.url.host == "api.github.com"
        assert req.headers["authorization"] == f"Bearer {TOKEN}"
        result = responses[req.url.path]
        return (
            result
            if isinstance(result, httpx.Response)
            else httpx.Response(200, json=result)
        )

    return (
        HttpGitHubRepositoryClient(
            gateway=token_gateway,
            transport=httpx.MockTransport(respond),
            limits=limits,
            clock=clock,
        ),
        token_gateway,
        requests,
    )


async def scan(client: HttpGitHubRepositoryClient) -> list[NormalizedDocument]:
    snapshot = await client.open_snapshot(
        installation_id=INSTALLATION, repository_id=REPO
    )
    assert TOKEN not in repr(snapshot)
    return [document async for document in client.iter_documents(snapshot)]


async def test_only_selected_repository_is_read_at_one_commit() -> None:
    readme, readme_blob = file("README.md", b"# Setup\r\nInstall the API.\r\n")
    code, code_blob = file("test #example.py", b"print('hello')\n")
    vendor = {"path": "vendor", "type": "tree", "mode": "040000", "sha": NESTED}
    dirs = {"path": "src", "type": "tree", "mode": "040000", "sha": NESTED}
    symlink = {"path": "pointer.py", "type": "blob", "mode": "120000"}
    submodule = {"path": "submodule", "type": "commit", "mode": "160000"}
    client, gateway, requests = create(
        {
            f"{TREE}/{ROOT}": tree(ROOT, readme, dirs, vendor, symlink, submodule),
            f"{TREE}/{NESTED}": tree(NESTED, code),
            f"{BLOBS}/{readme['sha']}": readme_blob,
            f"{BLOBS}/{code['sha']}": code_blob,
        }
    )
    docs = await scan(client)
    assert gateway.calls == [(INSTALLATION, frozenset({REPO}))]
    assert [r.url.path for r in requests] == [
        LIST,
        BRANCH,
        f"{TREE}/{ROOT}",
        f"{BLOBS}/{readme['sha']}",
        f"{TREE}/{NESTED}",
        f"{BLOBS}/{code['sha']}",
    ]
    assert requests[1].url.raw_path.endswith(b"heads%2Ffeature%2Fdefault?per_page=1")
    assert [doc.provider for doc in docs] == [Provider.GITHUB, Provider.GITHUB]
    assert [doc.source_type for doc in docs] == ["file", "code"]
    assert docs[0].mime_type == "text/markdown"
    assert docs[0].content == "# Setup\nInstall the API.\n"
    assert (
        docs[0].canonical_url
        == f"https://github.com/example/sample/blob/{COMMIT}/README.md"
    )
    assert docs[1].canonical_url == (
        f"https://github.com/example/sample/blob/{COMMIT}/src/test%20%23example.py"
    )
    assert docs[1].provider_metadata["repository_id"] == str(REPO)
    assert not docs[1].access_metadata.public
    assert TOKEN not in repr(docs)


async def test_file_identity_survives_repo_rename_and_blob_edits() -> None:
    first, first_blob = file("Dockerfile", b"FROM scratch")
    update, updated_blob = file("Dockerfile", b"FROM python:3.12")
    orig, _, _ = create(
        {f"{TREE}/{ROOT}": tree(ROOT, first), f"{BLOBS}/{first['sha']}": first_blob}
    )
    renamed = "/repos/example/renamed"
    second, _, _ = create(
        {
            LIST: inventory(name="example/renamed"),
            f"{renamed}/commits/heads/feature/default": {
                "sha": "c" * 40,
                "commit": {"tree": {"sha": ROOT}},
            },
            f"{renamed}/git/trees/{ROOT}": tree(ROOT, update),
            f"{renamed}/git/blobs/{update['sha']}": updated_blob,
        }
    )
    one, two = (await scan(orig))[0], (await scan(second))[0]
    assert one.external_id == two.external_id
    assert one.canonical_url != two.canonical_url
    assert one.content_hash != two.content_hash


async def test_excludes_prohibited_paths_and_sensitive_or_nontext_blobs() -> None:
    denied_names = (
        "package-lock.json",
        "bundle.min.js",
        "creds.bin",
        ".env",
        "private-key.txt",
        "generated/incorrect",
    )
    # Slash-delimited malformed paths are tested independently below.
    entries: list[dict[str, object]] = [
        {"path": name, "mode": "100644", "type": "blob"} for name in denied_names[:-1]
    ]
    entries.extend(
        [
            {"path": "generated", "mode": "040000", "type": "tree"},
            {"path": ".git", "mode": "040000", "type": "tree"},
            {
                "path": "large.py",
                "mode": "100644",
                "type": "blob",
                "sha": NESTED,
                "size": 200_000,
            },
        ]
    )
    bodies: dict[str, ResponsePayload] = {}
    for name, raw in (
        ("looks-safe.txt", b"private_key = '-----BEGIN PRIVATE KEY-----'"),
        ("note.txt", b"secret='syntheticplaceholder'"),
        ("binary.py", b"\x00binary"),
        ("bad.py", b"\xffnot utf8"),
        ("blank.txt", b" \n\t"),
        ("autowriter.py", b"# Code generated by test tool: do not edit"),
        ("large-pointer.md", b"version https://git-lfs.github.com/spec/v1"),
    ):
        entry, blob = file(name, raw)
        entries.append(entry)
        bodies[f"{BLOBS}/{entry['sha']}"] = blob
    client, _, requests = create(
        {**bodies, f"{TREE}/{ROOT}": tree(ROOT, *entries)},
        limits=GitHubScanLimits(max_file_bytes=1024),
    )
    assert await scan(client) == []
    assert len(requests) == 3 + len(bodies)


@pytest.mark.parametrize(
    "rows",
    [
        {"total_count": 0, "repositories": []},
        inventory(repository_id=REPO + 1),
        {"total_count": 2, "repositories": [{"id": REPO}, {"id": REPO + 1}]},
    ],
)
async def test_repository_authority_rejected_before_content_fetch(
    rows: dict[str, object],
) -> None:
    client, _, requests = create({LIST: rows})
    with pytest.raises(PermissionDeniedError):
        await scan(client)
    assert len(requests) == 1


@pytest.mark.parametrize("name", ["https://evil.test", "a/../b", "x/%2Ftest", "a/."])
async def test_repository_name_cannot_steer_provider_requests(name: str) -> None:
    client, _, requests = create({LIST: inventory(name=name)})
    with pytest.raises(MalformedItemError):
        await scan(client)
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("response", "expected_error"),
    [
        (httpx.Response(401), AuthenticationError),
        (httpx.Response(404), PermissionDeniedError),
        (httpx.Response(429, headers={"retry-after": "10"}), RateLimitError),
        (httpx.Response(403, headers={"x-ratelimit-remaining": "0"}), RateLimitError),
        (httpx.Response(503), ProviderUnavailableError),
        (
            httpx.Response(302, headers={"location": "https://evil.test"}),
            MalformedItemError,
        ),
        (httpx.Response(200, content=b"<invalid>"), MalformedItemError),
        (httpx.Response(200, json=[]), MalformedItemError),
    ],
)
async def test_status_mapping_fails_closed(
    response: httpx.Response, expected_error: type[Exception]
) -> None:
    client, _, requests = create({LIST: response})
    with pytest.raises(expected_error) as error:
        await scan(client)
    assert len(requests) == 1
    if response.status_code == 429:
        assert isinstance(error.value, RateLimitError)
        assert error.value.retry_after_seconds == 10


@pytest.mark.parametrize("mode", ["failed_mint", "expired_token"])
async def test_invalid_token_does_not_fetch_any_content(mode: str) -> None:
    client, _, requests = create(
        {},
        gateway=Gateway(fail=mode == "failed_mint", expired=mode == "expired_token"),
    )
    with pytest.raises(AuthenticationError, match="Connector operation failed"):
        await scan(client)
    assert not requests


@pytest.mark.parametrize(
    "values",
    [
        {**tree(ROOT), "truncated": True},
        tree(ROOT, {"path": "a"}, {"path": "b"}),
        tree(ROOT, {"path": "src", "mode": "040000", "type": "tree", "sha": NESTED}),
    ],
)
async def test_incomplete_tree_raises_and_cannot_signal_reconciliation(
    values: dict[str, object],
) -> None:
    client, _, requests = create(
        {f"{TREE}/{ROOT}": values},
        limits=GitHubScanLimits(max_entries=1, max_directories=1),
    )
    with pytest.raises(RepositoryScanLimitError):
        await scan(client)
    assert len(requests) == 3


@pytest.mark.parametrize("path", ["../file.py", "/bad", "bad\\path", ".", "a\x00b.py"])
async def test_tree_paths_cannot_escape_repository(path: str) -> None:
    entry, _ = file(path, b"sample")
    client, _, _ = create({f"{TREE}/{ROOT}": tree(ROOT, entry)})
    with pytest.raises(MalformedItemError, match="invalid tree path"):
        await scan(client)


@pytest.mark.parametrize(
    "overrides", [{"content": "not ## valid"}, {"content": "bm9pdGU="}, {"size": 65536}]
)
async def test_blob_integrity_failure_inhibits_reconciliation(
    overrides: dict[str, object],
) -> None:
    entry, blob = file("test.py", b"hello")
    client, _, _ = create(
        {
            f"{TREE}/{ROOT}": tree(ROOT, entry),
            f"{BLOBS}/{entry['sha']}": {**blob, **overrides},
        }
    )
    with pytest.raises(MalformedItemError):
        await scan(client)


async def test_blob_response_and_source_byte_budgets_are_distinct() -> None:
    entry, blob = file("README.md", b"hello")
    resources: dict[str, ResponsePayload] = {
        f"{TREE}/{ROOT}": tree(ROOT, entry),
        f"{BLOBS}/{entry['sha']}": {**blob, "extra": "x" * 5000},
    }
    client, _, _ = create(resources)
    with pytest.raises(RepositoryScanLimitError):
        await scan(client)
    client, _, requests = create(
        resources, limits=GitHubScanLimits(max_total_source_bytes=4)
    )
    with pytest.raises(RepositoryScanLimitError):
        await scan(client)
    assert len(requests) == 3
