"""Google OAuth HTTP adapter with a deliberately narrow public contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode

import httpx

GOOGLE_AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


@dataclass(frozen=True)
class GoogleTokens:
    access_token: str
    refresh_token: str | None
    expires_at: datetime
    scopes: frozenset[str]


@dataclass(frozen=True)
class GoogleAccount:
    external_id: str
    display_label: str


class GoogleProviderError(Exception):
    pass


class GoogleGateway(Protocol):
    def authorization_url(
        self, *, state: str, challenge: str, scopes: frozenset[str]
    ) -> str: ...

    async def exchange_code(self, *, code: str, verifier: str) -> GoogleTokens: ...

    async def account(
        self, *, access_token: str, source_families: tuple[str, ...]
    ) -> GoogleAccount: ...


class HttpGoogleGateway:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._transport = transport

    def authorization_url(
        self, *, state: str, challenge: str, scopes: frozenset[str]
    ) -> str:
        query = urlencode(
            {
                "access_type": "offline",
                "client_id": self._client_id,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "include_granted_scopes": "false",
                "prompt": "consent",
                "redirect_uri": self._redirect_uri,
                "response_type": "code",
                "scope": " ".join(sorted(scopes)),
                "state": state,
            }
        )
        return f"{GOOGLE_AUTHORIZE_ENDPOINT}?{query}"

    async def exchange_code(self, *, code: str, verifier: str) -> GoogleTokens:
        try:
            async with httpx.AsyncClient(
                timeout=15, transport=self._transport
            ) as client:
                response = await client.post(
                    GOOGLE_TOKEN_ENDPOINT,
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "code": code,
                        "code_verifier": verifier,
                        "grant_type": "authorization_code",
                        "redirect_uri": self._redirect_uri,
                    },
                )
            response.raise_for_status()
            payload = response.json()
            access_token = payload["access_token"]
            expires_in = int(payload["expires_in"])
            scopes = frozenset(str(payload["scope"]).split())
            if not isinstance(access_token, str) or expires_in <= 0:
                raise ValueError
            refresh = payload.get("refresh_token")
            if refresh is not None and not isinstance(refresh, str):
                raise ValueError
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            raise GoogleProviderError from error
        return GoogleTokens(
            access_token=access_token,
            refresh_token=refresh,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            scopes=scopes,
        )

    async def account(
        self, *, access_token: str, source_families: tuple[str, ...]
    ) -> GoogleAccount:
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            async with httpx.AsyncClient(
                timeout=15, headers=headers, transport=self._transport
            ) as client:
                if "gmail" in source_families:
                    response = await client.get(
                        "https://gmail.googleapis.com/gmail/v1/users/me/profile"
                    )
                    response.raise_for_status()
                    payload = response.json()
                    external_id = str(payload["emailAddress"])
                    display_label = external_id
                else:
                    response = await client.get(
                        "https://www.googleapis.com/drive/v3/about",
                        params={
                            "fields": "user(permissionId,displayName,emailAddress)"
                        },
                    )
                    response.raise_for_status()
                    user = response.json()["user"]
                    external_id = str(user["permissionId"])
                    display_label = str(
                        user.get("emailAddress") or user.get("displayName") or "Google"
                    )
            if not external_id or not display_label:
                raise ValueError
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            raise GoogleProviderError from error
        return GoogleAccount(external_id=external_id, display_label=display_label[:200])
