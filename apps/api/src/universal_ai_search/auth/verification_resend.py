"""Rate-limited generic resend with a short-lived in-memory delivery queue."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from ipaddress import ip_address
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .recovery import RecoveryRequestLimiter
from .security import hash_token, random_token

_LOG = logging.getLogger(__name__)
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+")


class VerificationResendStore(Protocol):
    async def issue(
        self, recipient: str, token_id: UUID, token_hash: bytes
    ) -> bool: ...


@dataclass(frozen=True)
class VerificationCode:
    recipient: str = field(repr=False)
    token: str = field(repr=False)


class VerificationCodeDelivery(Protocol):
    async def send(self, code: VerificationCode) -> None: ...


class SQLAlchemyVerificationResendStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def issue(self, recipient: str, token_id: UUID, token_hash: bytes) -> bool:
        async with self._engine.begin() as conn:
            issued = await conn.scalar(
                text("SELECT app.auth_resend_verification(:email, :id, :hash)"),
                {"email": recipient, "id": token_id, "hash": token_hash},
            )
        return issued is True


class SMTPVerificationCodeDelivery:
    def __init__(
        self, *, host: str, port: int, sender: str, require_starttls: bool
    ) -> None:
        self._host = host
        self._port = port
        self._sender = sender
        self._require_starttls = require_starttls

    async def send(self, code: VerificationCode) -> None:
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = code.recipient
        message["Subject"] = "New Universal AI Search verification code"
        message.set_content(
            "Use this private, single-use verification code in the trusted "
            "Universal AI Search client. It expires in 24 hours.\n\n"
            f"{code.token}\n\nIgnore this message if you did not request it."
        )
        await asyncio.to_thread(self._deliver, message)

    def _deliver(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self._host, self._port, timeout=10) as client:
            if self._require_starttls:
                client.starttls(context=ssl.create_default_context())
            client.send_message(message)


class VerificationResendService:
    def __init__(
        self,
        *,
        store: VerificationResendStore,
        limiter: RecoveryRequestLimiter,
        delivery: VerificationCodeDelivery,
        key: bytes,
    ) -> None:
        self._store = store
        self._limiter = limiter
        self._delivery = delivery
        self._key = key
        self._pending: set[asyncio.Task[None]] = set()

    async def request(self, email: str, peer: str) -> None:
        loop = asyncio.get_running_loop()
        floor = loop.time() + 0.100 + secrets.randbelow(26) / 1000
        try:
            await self._request(email, peer)
        finally:
            remaining = floor - loop.time()
            if remaining > 0:
                await asyncio.sleep(remaining)

    async def _request(self, email: str, peer: str) -> None:
        recipient = email.strip().casefold()
        if (
            len(recipient) > 320
            or not _EMAIL.fullmatch(recipient)
            or any(ord(char) < 32 or ord(char) == 127 for char in recipient)
            or len(peer) > 45
            or "%" in peer
        ):
            return
        try:
            network = ip_address(peer).compressed
        except ValueError:
            return
        if len(self._pending) >= 32 or not await self._limiter.allow(
            ip_key=hash_token("resend-ip:" + network, self._key),
            email_key=hash_token("resend-email:" + recipient, self._key),
        ):
            return
        token = random_token()
        if (
            await self._store.issue(recipient, uuid4(), hash_token(token, self._key))
            and len(self._pending) < 32
        ):
            pending = asyncio.create_task(
                self._send(VerificationCode(recipient, token))
            )
            self._pending.add(pending)
            pending.add_done_callback(self._pending.discard)

    async def _send(self, code: VerificationCode) -> None:
        try:
            await self._delivery.send(code)
        except (ValueError, OSError):
            _LOG.warning("Verification resend failed")

    async def drain(self) -> None:
        if self._pending:
            await asyncio.gather(*tuple(self._pending))
