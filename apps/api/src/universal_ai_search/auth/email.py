"""Minimal SMTP boundary for security-sensitive account email."""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from typing import Protocol


class VerificationSender(Protocol):
    async def send_verification(self, *, recipient: str, url: str) -> None: ...


class SMTPVerificationSender:
    def __init__(self, *, host: str, port: int, sender: str) -> None:
        self._host = host
        self._port = port
        self._sender = sender

    async def send_verification(self, *, recipient: str, url: str) -> None:
        message = EmailMessage()
        message["Subject"] = "Verify your Universal AI Search account"
        message["From"] = self._sender
        message["To"] = recipient
        message.set_content(
            "Verify your account using this private, single-use link:\n\n"
            f"{url}\n\nThe link expires in 24 hours."
        )
        await asyncio.to_thread(self._send, message)

    def _send(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self._host, self._port, timeout=10) as client:
            client.send_message(message)


class NullVerificationSender:
    async def send_verification(self, *, recipient: str, url: str) -> None:
        del recipient, url
