"""Read-only GitHub App credential boundary; no persistence or callback routing."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, TypeGuard
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

OAUTH_ORIGIN = "https://github.com"
API_ORIGIN = "https://api.github.com"
READ_PERMISSIONS = {
    "metadata": "read",
    "contents": "read",
    "issues": "read",
    "pull_requests": "read",
}
_MAX_REPOSITORIES = 500


class GitHubAuthorizationError(Exception):
    """Stable failure without provider data or credentials."""


@dataclass(frozen=True)
class GitHubInstallationToken:
    token: str = field(repr=False)
    expires_at: datetime


def _segment(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _valid_id(value: object) -> TypeGuard[int]:
    return type(value) is int and value > 0


class HttpGitHubAppGateway:
    def __init__(
        self,
        *,
        app_id: int,
        private_key_pem: bytes,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not _valid_id(app_id):
            raise ValueError("Invalid GitHub App ID")
        try:
            key = serialization.load_pem_private_key(private_key_pem, password=None)
        except (ValueError, TypeError):
            raise ValueError("Invalid GitHub App key") from None
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError("GitHub App key must be RSA with at least 2048 bits")
        self._key = key
        self._app_id = app_id
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def authorization_url(
        *, client_id: str, redirect_uri: str, state: str, challenge: str
    ) -> str:
        redirect = urlsplit(redirect_uri)
        if (
            redirect.scheme != "https"
            or not redirect.hostname
            or redirect.username is not None
            or redirect.fragment
            or not all((client_id, state, challenge))
        ):
            raise ValueError("Invalid GitHub authorization configuration")
        query = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{OAUTH_ORIGIN}/login/oauth/authorize?{query}"

    async def exchange_user_code(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        code: str,
        verifier: str,
    ) -> str:
        """Return only an expiring App user token; discard any refresh token."""
        try:
            async with httpx.AsyncClient(
                timeout=15, follow_redirects=False, transport=self._transport
            ) as client:
                response = await client.post(
                    f"{OAUTH_ORIGIN}/login/oauth/access_token",
                    headers={"Accept": "application/json"},
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "redirect_uri": redirect_uri,
                        "code": code,
                        "code_verifier": verifier,
                    },
                )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                token = payload.get("access_token")
                expiry = payload.get("expires_in")
                if (
                    "error" not in payload
                    and isinstance(token, str)
                    and token.startswith("ghu_")
                    and len(token) > 4
                    and _valid_id(expiry)
                    and expiry <= 28800
                    and payload.get("scope") == ""
                    and payload.get("token_type") == "bearer"
                ):
                    return token
        except (httpx.HTTPError, ValueError):
            pass
        raise GitHubAuthorizationError

    def _app_jwt(self) -> str:
        issued = int(self._clock().timestamp())
        claims = {"iat": issued - 60, "exp": issued + 480, "iss": str(self._app_id)}
        header = _segment(b'{"alg":"RS256","typ":"JWT"}')
        body = _segment(json.dumps(claims, separators=(",", ":")).encode())
        message = f"{header}.{body}".encode("ascii")
        signature = self._key.sign(message, padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{body}.{_segment(signature)}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        credential: str,
        params: dict[str, int] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=API_ORIGIN,
                timeout=15,
                follow_redirects=False,
                transport=self._transport,
                headers={
                    "Authorization": f"Bearer {credential}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            ) as client:
                response = await client.request(method, path, params=params, json=body)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                return data
        except (httpx.HTTPError, ValueError):
            pass
        raise GitHubAuthorizationError

    async def verify_selected_repositories(
        self,
        *,
        installation_id: int,
        repository_ids: frozenset[int],
        user_access_token: str,
    ) -> None:
        """Check the app installation AND the authorizing user's repository access.

        The user token must come from this App's server-side OAuth exchange, not
        from callback parameters or an API client. Recheck before saving authority.
        """
        self._check_ids(installation_id, repository_ids)
        if not user_access_token:
            raise GitHubAuthorizationError
        installation = await self._request(
            "GET", f"/app/installations/{installation_id}", credential=self._app_jwt()
        )
        if (
            not _valid_id(installation.get("id"))
            or installation["id"] != installation_id
            or not _valid_id(installation.get("app_id"))
            or installation["app_id"] != self._app_id
            or "suspended_at" not in installation
            or installation["suspended_at"] is not None
            or installation.get("repository_selection") != "selected"
            or not _allowed_permissions(installation.get("permissions"))
        ):
            raise GitHubAuthorizationError

        remaining = set(repository_ids)
        seen: set[int] = set()
        total: int | None = None
        for page in range(1, 6):
            result = await self._request(
                "GET",
                f"/user/installations/{installation_id}/repositories",
                credential=user_access_token,
                params={"per_page": 100, "page": page},
            )
            count, rows = result.get("total_count"), result.get("repositories")
            if (
                not _valid_id(count)
                or count > _MAX_REPOSITORIES
                or (total is not None and count != total)
                or not isinstance(rows, list)
                or len(rows) != min(100, count - len(seen))
            ):
                raise GitHubAuthorizationError
            total = count
            for row in rows:
                repo_id = row.get("id") if isinstance(row, dict) else None
                if not _valid_id(repo_id) or repo_id in seen:
                    raise GitHubAuthorizationError
                seen.add(repo_id)
                remaining.discard(repo_id)
            if len(seen) == total:
                if remaining:
                    raise GitHubAuthorizationError
                return
        raise GitHubAuthorizationError  # pragma: no cover - bounded by total_count

    async def mint_repository_token(
        self, *, installation_id: int, repository_ids: frozenset[int]
    ) -> GitHubInstallationToken:
        """Use durable, previously verified repository IDs, never caller input."""
        self._check_ids(installation_id, repository_ids)
        payload = await self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            credential=self._app_jwt(),
            body={
                "repository_ids": sorted(repository_ids),
                "permissions": READ_PERMISSIONS,
            },
        )
        now = self._clock()
        try:
            token = payload["token"]
            expiration = datetime.fromisoformat(payload["expires_at"])
            repos = payload["repositories"]
            if (
                not isinstance(token, str)
                or not token
                or expiration.tzinfo is None
                or not now < expiration <= now + timedelta(minutes=65)
                or not _allowed_permissions(payload.get("permissions"))
                or not isinstance(repos, list)
                or len(repos) != len(repository_ids)
                or any(
                    not isinstance(row, dict) or not _valid_id(row.get("id"))
                    for row in repos
                )
                or {row["id"] for row in repos} != repository_ids
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise GitHubAuthorizationError from None
        return GitHubInstallationToken(token, expiration)

    @staticmethod
    def _check_ids(installation_id: int, repository_ids: frozenset[int]) -> None:
        if (
            not _valid_id(installation_id)
            or not 0 < len(repository_ids) <= _MAX_REPOSITORIES
            or not all(_valid_id(value) for value in repository_ids)
        ):
            raise GitHubAuthorizationError


def _allowed_permissions(candidate: object) -> bool:
    return isinstance(candidate, dict) and candidate == READ_PERMISSIONS
