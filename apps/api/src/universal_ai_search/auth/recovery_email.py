"""Account-recovery mail boundary; request responses never wait for SMTP."""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Protocol

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryMail:
    recipient: str = field(repr=False)
    reset_url: str | None = field(default=None, repr=False)


class RecoveryMailQueue(Protocol):
    def enqueue(self, mail: RecoveryMail) -> None: ...


class RecoveryMailer(Protocol):
    async def deliver(self, mail: RecoveryMail) -> None: ...


class BoundedRecoveryMailQueue:
    """Short-lived in-process dispatch; never persists raw reset tokens."""

    def __init__(self, mailer: RecoveryMailer, *, max_pending: int = 32) -> None:
        if not 1 <= max_pending <= 100:
            raise ValueError("Recovery mail backlog must be between 1 and 100")
        self._mailer = mailer
        self._max_pending = max_pending
        self._pending: set[asyncio.Task[None]] = set()

    def enqueue(self, mail: RecoveryMail) -> None:
        if len(self._pending) >= self._max_pending:
            _LOG.warning("Password recovery mail queue is full")
            return
        task = asyncio.create_task(self._deliver(mail))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _deliver(self, mail: RecoveryMail) -> None:
        try:
            await self._mailer.deliver(mail)
        except (OSError, ValueError):
            # Raw SMTP exceptions may include recipients and provider responses.
            _LOG.warning("Password recovery mail delivery failed")

    async def drain(self) -> None:
        """Let service shutdown/tests await already-queued sends."""
        if self._pending:
            await asyncio.gather(*tuple(self._pending))


class SMTPRecoveryMailer:
    def __init__(
        self, *, host: str, port: int, sender: str, require_starttls: bool = True
    ) -> None:
        self._require_starttls = require_starttls
        self._host = host
        self._port = port
        self._sender = sender

    async def deliver(self, mail: RecoveryMail) -> None:
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = mail.recipient
        if mail.reset_url is not None:
            message["Subject"] = "Reset your Universal AI Search password"
            message.set_content(
                "Use this private, single-use password reset link "
                "within 30 minutes:\n\n"
                f"{mail.reset_url}\n\n"
                "If you did not request this, you can ignore this email."
            )
        else:
            message["Subject"] = "Your Universal AI Search password was reset"
            message.set_content(
                "Your password was reset and your sessions were signed out. "
                "If you did not make this change, contact support."
            )
        await asyncio.to_thread(self._send, message)

    def _send(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self._host, self._port, timeout=10) as client:
            if self._require_starttls:
                client.starttls(context=ssl.create_default_context())
            client.send_message(message)
