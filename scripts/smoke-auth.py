#!/usr/bin/env python3
"""Exercise the local authentication flow without printing credentials or tokens."""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

API_URL = "http://localhost:8000/v1/auth"
MAILPIT_URL = "http://localhost:8025/api/v1"
VERIFY_PATTERN = re.compile(r"verify_token=([A-Za-z0-9_-]+)")


def request_json(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    bearer: str | None = None,
    expected: int = 200,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    request = Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=10) as response:
            status = response.status
            content = response.read()
    except HTTPError as error:
        status = error.code
        content = error.read()
    if status != expected:
        raise AssertionError(f"{method} {url} returned {status}, expected {expected}")
    return json.loads(content) if content else {}


def strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in strings(child)]
    if isinstance(value, dict):
        return [item for child in value.values() for item in strings(child)]
    return []


def verification_token(recipient: str) -> str:
    for _ in range(20):
        listing = request_json("GET", f"{MAILPIT_URL}/messages")
        for message in listing.get("messages", []):
            if recipient not in strings(message):
                continue
            message_id = message.get("ID") or message.get("id")
            detail = request_json("GET", f"{MAILPIT_URL}/message/{message_id}")
            for value in strings(detail):
                match = VERIFY_PATTERN.search(value)
                if match:
                    return match.group(1)
        time.sleep(0.25)
    raise AssertionError("verification email did not arrive in Mailpit")


def main() -> None:
    email = f"auth-smoke-{uuid4()}@example.test"
    password = "correct horse battery staple 47!"
    request_json(
        "POST",
        f"{API_URL}/register",
        body={
            "email": email,
            "password": password,
            "full_name": "Authentication Smoke Test",
            "terms_version": "2026-08-12",
            "locale": "en-US",
        },
        expected=202,
    )
    request_json(
        "POST",
        f"{API_URL}/email/verify",
        body={"token": verification_token(email)},
        expected=204,
    )
    login = request_json(
        "POST",
        f"{API_URL}/login",
        body={"email": email, "password": password, "client_type": "native"},
    )
    current = request_json("GET", f"{API_URL}/me", bearer=login["access_token"])
    assert current["user"]["email"] == email
    assert current["user"]["email_verified"] is True
    assert len(current["memberships"]) == 1
    assert current["memberships"][0]["role"] == "owner"

    refreshed = request_json(
        "POST",
        f"{API_URL}/refresh",
        body={"client_type": "native", "refresh_token": login["refresh_token"]},
    )
    assert refreshed["access_token"] != login["access_token"]
    assert refreshed["refresh_token"] != login["refresh_token"]
    request_json("GET", f"{API_URL}/me", bearer=refreshed["access_token"])
    request_json(
        "POST", f"{API_URL}/logout", bearer=refreshed["access_token"], expected=204
    )
    request_json(
        "GET", f"{API_URL}/me", bearer=refreshed["access_token"], expected=401
    )
    print("Authentication smoke test passed.")


if __name__ == "__main__":
    main()
