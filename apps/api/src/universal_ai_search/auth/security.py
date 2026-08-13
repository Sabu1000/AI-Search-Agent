"""Password, opaque-token, and signed access-token primitives."""

from __future__ import annotations

import base64
import hmac
import json
import math
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_COMMON_PASSWORDS = frozenset(
    {
        "123456789012",
        "correct horse battery staple",
        "iloveyou12345",
        "letmein123456",
        "password1234",
        "qwerty123456",
        "welcome12345",
    }
)


def random_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str, key: bytes) -> bytes:
    return hmac.digest(key, token.encode(), "sha256")


class PasswordSecurity:
    def __init__(self) -> None:
        self._hasher = PasswordHasher(
            time_cost=3,
            memory_cost=65536,
            parallelism=1,
            hash_len=32,
            salt_len=16,
        )
        self._dummy_hash = self._hasher.hash("not-a-real-user-password")

    def validate(self, password: str, email: str) -> None:
        if not 12 <= len(password) <= 128:
            raise ValueError("password must contain between 12 and 128 characters")
        if any(ord(character) < 32 or ord(character) == 127 for character in password):
            raise ValueError("password cannot contain control characters")
        if password.casefold().strip() in _COMMON_PASSWORDS:
            raise ValueError("password is too common")
        normalized_identity = email.strip().casefold().split("@", maxsplit=1)[0]
        if normalized_identity and normalized_identity in password.casefold():
            raise ValueError("password cannot contain the email identity")

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str | None, password: str) -> bool:
        candidate = password_hash or self._dummy_hash
        try:
            verified = self._hasher.verify(candidate, password)
        except (InvalidHashError, VerifyMismatchError):
            return False
        return bool(verified and password_hash is not None)

    def needs_rehash(self, password_hash: str) -> bool:
        return self._hasher.check_needs_rehash(password_hash)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class AccessTokenCodec:
    def __init__(self, signing_key: bytes, lifetime: timedelta = timedelta(minutes=15)):
        if len(signing_key) < 32:
            raise ValueError("access-token signing key must be at least 32 bytes")
        self._signing_key = signing_key
        self._lifetime = lifetime

    def issue(
        self,
        *,
        user_id: UUID,
        session_id: UUID,
        authorization_version: int,
        now: datetime | None = None,
    ) -> tuple[str, datetime]:
        issued_at = (now or datetime.now(UTC)).astimezone(UTC)
        expires_at = issued_at + self._lifetime
        payload = {
            "aud": "universal-ai-search",
            "auth_version": authorization_version,
            "auth_time": math.floor(issued_at.timestamp()),
            "exp": math.floor(expires_at.timestamp()),
            "iat": math.floor(issued_at.timestamp()),
            "iss": "universal-ai-search-api",
            "jti": str(uuid4()),
            "session_id": str(session_id),
            "sub": str(user_id),
        }
        header = _encode(b'{"alg":"HS256","kid":"auth-v1","typ":"JWT"}')
        encoded_payload = _encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        )
        signed = f"{header}.{encoded_payload}"
        signature = _encode(hmac.digest(self._signing_key, signed.encode(), "sha256"))
        return f"{signed}.{signature}", expires_at

    def verify(self, token: str, now: datetime | None = None) -> dict[str, Any] | None:
        try:
            encoded_header, encoded_payload, supplied_signature = token.split(".")
            header = json.loads(_decode(encoded_header))
            if header != {"alg": "HS256", "kid": "auth-v1", "typ": "JWT"}:
                return None
            signed = f"{encoded_header}.{encoded_payload}"
            expected = hmac.digest(self._signing_key, signed.encode(), "sha256")
            if not hmac.compare_digest(expected, _decode(supplied_signature)):
                return None
            payload = json.loads(_decode(encoded_payload))
            if not isinstance(payload, dict):
                return None
            if payload.get("aud") != "universal-ai-search":
                return None
            if payload.get("iss") != "universal-ai-search-api":
                return None
            current = math.floor((now or datetime.now(UTC)).timestamp())
            if not isinstance(payload.get("exp"), int) or payload["exp"] <= current:
                return None
            if not isinstance(payload.get("iat"), int) or payload["iat"] > current:
                return None
            if not isinstance(payload.get("auth_time"), int):
                return None
            UUID(payload["sub"])
            UUID(payload["session_id"])
            UUID(payload["jti"])
            if not isinstance(payload.get("auth_version"), int):
                return None
            return cast(dict[str, Any], payload)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
