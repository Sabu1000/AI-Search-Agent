from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from universal_ai_search.connections.github import (
    READ_PERMISSIONS,
    GitHubAuthorizationError,
    HttpGitHubAppGateway,
)

NOW = datetime(2026, 11, 24, tzinfo=UTC)
REDIRECT = "https://search.example/settings/github/callback"


@pytest.fixture(scope="module")
def pem() -> bytes:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def gateway(
    pem: bytes, *responses: object
) -> tuple[HttpGitHubAppGateway, list[httpx.Request]]:
    requests: list[httpx.Request] = []
    queue = iter(responses)

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        result = next(queue)
        if isinstance(result, int):
            return httpx.Response(result, json={"provider": "do not expose"})
        return httpx.Response(200, json=result)

    return (
        HttpGitHubAppGateway(
            app_id=7,
            private_key_pem=pem,
            transport=httpx.MockTransport(respond),
            clock=lambda: NOW,
        ),
        requests,
    )


def installation(**overrides: object) -> dict[str, Any]:
    return {
        "id": 3,
        "app_id": 7,
        "suspended_at": None,
        "repository_selection": "selected",
        "permissions": READ_PERMISSIONS,
        **overrides,
    }


def repos(*ids: int) -> dict[str, object]:
    return {"total_count": len(ids), "repositories": [{"id": i} for i in ids]}


async def test_app_and_user_must_authorize_same_repositories(pem: bytes) -> None:
    client, seen = gateway(pem, installation(), repos(2, 3))
    await client.verify_selected_repositories(
        installation_id=3, repository_ids=frozenset({2}), user_access_token="ghu_test"
    )
    assert seen[0].url == "https://api.github.com/app/installations/3"
    assert seen[0].headers["authorization"].count(".") == 2
    assert seen[1].url.path == "/user/installations/3/repositories"
    assert seen[1].headers["authorization"] == "Bearer ghu_test"


@pytest.mark.parametrize(
    "response",
    [
        installation(app_id=8),
        installation(id=4),
        installation(suspended_at="date"),
        installation(repository_selection="all"),
        installation(permissions={"contents": "write"}),
        404,
        [],
    ],
)
async def test_rejects_invalid_installation(pem: bytes, response: object) -> None:
    client, seen = gateway(pem, response)
    with pytest.raises(GitHubAuthorizationError, match="^$"):
        await client.verify_selected_repositories(
            installation_id=3,
            repository_ids=frozenset({2}),
            user_access_token="ghu_test",
        )
    assert len(seen) == 1


@pytest.mark.parametrize("response", [repos(1), repos(2, 2), repos(), 403])
async def test_rejects_missing_user_access(pem: bytes, response: object) -> None:
    client, _ = gateway(pem, installation(), response)
    with pytest.raises(GitHubAuthorizationError):
        await client.verify_selected_repositories(
            installation_id=3,
            repository_ids=frozenset({2}),
            user_access_token="ghu_test",
        )


async def test_pages_repository_access(pem: bytes) -> None:
    client, seen = gateway(
        pem,
        installation(),
        {**repos(*range(1, 101)), "total_count": 101},
        {**repos(101), "total_count": 101},
    )
    await client.verify_selected_repositories(
        installation_id=3,
        repository_ids=frozenset({101}),
        user_access_token="ghu_test",
    )
    assert seen[-1].url.params["page"] == "2"


def token_response(**overrides: object) -> dict[str, object]:
    return {
        "token": "ghs_test",
        "expires_at": "2026-11-24T01:00:00Z",
        "permissions": READ_PERMISSIONS,
        "repositories": [{"id": 2}],
        **overrides,
    }


async def test_mints_read_only_exact_repository_token(pem: bytes) -> None:
    client, seen = gateway(pem, token_response())
    token = await client.mint_repository_token(
        installation_id=3, repository_ids=frozenset({2})
    )
    assert b'"repository_ids":[2]' in seen[0].content
    assert seen[0].url.path == "/app/installations/3/access_tokens"
    assert token.token == "ghs_test"
    assert token.token not in repr(token)


@pytest.mark.parametrize(
    "response",
    [
        token_response(repositories=[{"id": 3}]),
        token_response(repositories=[{"id": True}]),
        token_response(permissions={"contents": "write"}),
        token_response(expires_at="2026-11-24T02:00:00Z"),
        token_response(expires_at="2026-11-24"),
        token_response(token=""),
        {},
    ],
)
async def test_rejects_broader_or_invalid_token(pem: bytes, response: object) -> None:
    client, _ = gateway(pem, response)
    with pytest.raises(GitHubAuthorizationError):
        await client.mint_repository_token(
            installation_id=3, repository_ids=frozenset({2})
        )


async def test_rejects_empty_scope_before_request(pem: bytes) -> None:
    client, seen = gateway(pem)
    with pytest.raises(GitHubAuthorizationError):
        await client.mint_repository_token(
            installation_id=3, repository_ids=frozenset()
        )
    with pytest.raises(GitHubAuthorizationError):
        await client.verify_selected_repositories(
            installation_id=3, repository_ids=frozenset({2}), user_access_token=""
        )
    assert not seen


def test_rejects_bad_key_or_app_id(pem: bytes) -> None:
    with pytest.raises(ValueError):
        HttpGitHubAppGateway(app_id=7, private_key_pem=b"bad")
    with pytest.raises(ValueError):
        HttpGitHubAppGateway(app_id=0, private_key_pem=pem)


def test_oauth_url_requires_https_and_pkce() -> None:
    url = HttpGitHubAppGateway.authorization_url(
        client_id="abc", redirect_uri=REDIRECT, state="bound", challenge="hash"
    )
    parsed = urlsplit(url)
    assert parsed.netloc == "github.com"
    assert parse_qs(parsed.query)["code_challenge_method"] == ["S256"]
    with pytest.raises(ValueError):
        HttpGitHubAppGateway.authorization_url(
            client_id="abc",
            redirect_uri="http://search.example/",
            state="bound",
            challenge="hash",
        )


async def test_oauth_code_exchange_posts_pkce_and_discards_refresh(pem: bytes) -> None:
    client, seen = gateway(
        pem,
        {
            "access_token": "ghu_example",
            "refresh_token": "ghr_discard",
            "token_type": "bearer",
            "scope": "",
            "expires_in": 28800,
        },
    )
    token = await client.exchange_user_code(
        client_id="abc",
        client_secret="unused",
        redirect_uri=REDIRECT,
        code="code",
        verifier="verifier",
    )
    assert token == "ghu_example"
    assert seen[0].url == "https://github.com/login/oauth/access_token"
    assert b"code_verifier=verifier" in seen[0].content


@pytest.mark.parametrize("response", [{"error": "bad_verification_code"}, 302, []])
async def test_rejects_oauth_exchange_error(pem: bytes, response: object) -> None:
    client, _ = gateway(pem, response)
    with pytest.raises(GitHubAuthorizationError, match="^$"):
        await client.exchange_user_code(
            client_id="abc",
            client_secret="unused",
            redirect_uri=REDIRECT,
            code="code",
            verifier="verifier",
        )
