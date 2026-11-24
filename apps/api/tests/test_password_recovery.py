from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import pytest
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncEngine

from universal_ai_search.auth.recovery import (
    InvalidRecoveryToken,
    PasswordRecoveryService,
    RecoveryPasswordRejected,
)
from universal_ai_search.auth.recovery_email import (
    BoundedRecoveryMailQueue,
    RecoveryMail,
    SMTPRecoveryMailer,
)
from universal_ai_search.auth.recovery_limiter import RedisRecoveryRequestLimiter
from universal_ai_search.auth.recovery_store import (
    RecoveryTarget,
    SQLAlchemyRecoveryStore,
)
from universal_ai_search.auth.security import PasswordSecurity, hash_token, random_token

KEY = b"k" * 32
EMAIL = "person@example.test"
PASSWORD = "A strong password you can change 37"


class Capture:
    def __init__(self) -> None:
        self.messages: list[RecoveryMail] = []

    def enqueue(self, mail: RecoveryMail) -> None:
        self.messages.append(mail)


def setup_service(
    *, web_origin: str = "https://local.example.test", key: bytes = KEY
) -> tuple[PasswordRecoveryService, AsyncMock, AsyncMock, Capture]:
    store = AsyncMock(issue=None, lookup=None, complete=None)
    store.issue = AsyncMock(return_value=None)
    store.lookup = AsyncMock(return_value=None)
    store.complete = AsyncMock(return_value=None)
    limiter = AsyncMock(allow=None)
    limiter.allow = AsyncMock(return_value=True)
    mail = Capture()
    return (
        PasswordRecoveryService(
            store=store,
            security=PasswordSecurity(),
            limiter=limiter,
            mail=mail,
            token_hash_key=key,
            web_origin=web_origin,
        ),
        store,
        limiter,
        mail,
    )


async def test_request_is_generic_and_emails_only_an_eligible_stored_identity() -> None:
    service, store, limiter, mail = setup_service()
    assert await service.request_reset(email=EMAIL, client_ip="192.0.2.1") is None
    assert mail.messages == []
    store.issue.return_value = EMAIL
    before = datetime.now(UTC)
    result = await service.request_reset(
        email=" person@EXAMPLE.test ", client_ip="::ffff:192.0.2.1"
    )
    assert result is None
    assert len(mail.messages) == 1
    delivered = mail.messages[0]
    assert delivered.recipient == EMAIL
    assert delivered.reset_url is not None
    assert delivered.reset_url.startswith("https://local.example.test/#reset_token=")
    token = delivered.reset_url.rsplit("=", 1)[1]
    assert (
        len(token) == 43
        and token not in repr(delivered)
        and EMAIL not in repr(delivered)
    )
    parameters = store.issue.call_args.kwargs
    assert token not in repr(parameters)
    assert parameters["email"] == EMAIL
    assert parameters["token_hash"] == hash_token(
        f"password-recovery:reset-token:{token}", KEY
    )
    assert before + timedelta(minutes=30) <= parameters["expires_at"]
    assert limiter.allow.call_args_list[0] == limiter.allow.call_args_list[1]
    assert EMAIL not in repr(limiter.allow.call_args)


@pytest.mark.parametrize(
    ("email", "ip"),
    [
        ("missing-at", "192.0.2.1"),
        ("x" * 321, "192.0.2.1"),
        ("a\0b@example.test", "192.0.2.1"),
        (EMAIL, "unsafe"),
        (EMAIL, "fe80::1%eth0"),
    ],
)
async def test_malformed_request_still_returns_generic(email: str, ip: str) -> None:
    service, store, limiter, mail = setup_service()
    assert await service.request_reset(email=email, client_ip=ip) is None
    store.issue.assert_not_awaited()
    limiter.allow.assert_not_awaited()
    assert not mail.messages


async def test_rate_limited_request_does_not_issue_token() -> None:
    service, store, limiter, mail = setup_service(web_origin="http://localhost:3000")
    limiter.allow.return_value = False
    assert await service.request_reset(email=EMAIL, client_ip="192.0.2.1") is None
    store.issue.assert_not_awaited()
    assert not mail.messages


async def test_reset_uses_account_password_policy_and_only_notifies_after_commit() -> (
    None
):
    service, store, _, mail = setup_service()
    identity = RecoveryTarget(uuid4(), EMAIL, 13)
    store.lookup.return_value = identity
    store.complete.return_value = EMAIL
    token = random_token()
    request_id = uuid4()
    await service.reset_password(token=token, password=PASSWORD, request_id=request_id)
    args = store.complete.call_args.kwargs
    assert args["expected_authorization_version"] == 13
    assert args["expected_email"] == EMAIL
    assert args["user_id"] == identity.user_id
    assert args["request_id"] == request_id
    assert len(args["audit_subject_hash"]) == 32
    assert PasswordSecurity().verify(args["password_hash"], PASSWORD)
    assert args["token_hash"] == store.lookup.call_args.kwargs["token_hash"]
    assert token not in repr(args) and PASSWORD not in repr(args)
    assert mail.messages == [RecoveryMail(EMAIL)]
    assert EMAIL not in repr(identity)


async def test_invalid_replayed_expired_or_changed_account_is_not_notified() -> None:
    service, store, _, mail = setup_service()
    with pytest.raises(InvalidRecoveryToken):
        await service.reset_password(token="bad", password=PASSWORD, request_id=uuid4())
    store.lookup.assert_not_awaited()
    with pytest.raises(InvalidRecoveryToken):
        await service.reset_password(
            token=random_token(), password=PASSWORD, request_id=uuid4()
        )
    store.complete.assert_not_awaited()
    store.lookup.return_value = RecoveryTarget(uuid4(), EMAIL, 2)
    with pytest.raises(RecoveryPasswordRejected):
        await service.reset_password(
            token=random_token(), password="person-strong-123", request_id=uuid4()
        )
    store.complete.assert_not_awaited()
    with pytest.raises(InvalidRecoveryToken):
        await service.reset_password(
            token=random_token(), password=PASSWORD, request_id=uuid4()
        )
    store.complete.assert_awaited_once()
    assert not mail.messages


@pytest.mark.parametrize(
    "origin",
    [
        "http://public.example",
        "https://trusted.example@evil.example",
        "https://trusted.example/path",
        "https://trusted.example/#hash",
        "https://trusted.example?bad=1",
        "https://trusted.example:65536",
        "https://trusted.example\\evil",
    ],
)
def test_invalid_origins_rejected(origin: str) -> None:
    with pytest.raises(ValueError):
        setup_service(web_origin=origin)


def test_short_hash_key_rejected() -> None:
    with pytest.raises(ValueError):
        setup_service(key=b"short")


async def test_redis_budget_uses_only_opaque_keys_and_fails_closed() -> None:
    redis = SimpleNamespace(eval=AsyncMock(return_value=1))
    limiter = RedisRecoveryRequestLimiter(redis)
    assert await limiter.allow(ip_key=b"i" * 32, email_key=b"e" * 32)
    call = redis.eval.call_args.args
    assert "INCR" in call[0] and call[1] == 2
    assert call[2] == f"uas:{{password-recovery}}:ip:{(b'i'*32).hex()}"
    assert call[3] == f"uas:{{password-recovery}}:email:{(b'e'*32).hex()}"
    assert call[4:] == ("20", "3", "900")
    assert not await limiter.allow(ip_key=b"raw", email_key=b"raw")
    redis.eval.side_effect = RedisError("opaque")
    assert not await limiter.allow(ip_key=b"i" * 32, email_key=b"e" * 32)


async def test_bounded_mail_queue_is_nonblocking_and_logs_no_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    event = asyncio.Event()
    deliveries: list[RecoveryMail] = []

    class CaptureDelivery:
        async def deliver(self, mail: RecoveryMail) -> None:
            deliveries.append(mail)
            await event.wait()
            raise OSError(
                f"untrusted transport detail {mail.reset_url} {mail.recipient}"
            )

    with pytest.raises(ValueError):
        BoundedRecoveryMailQueue(CaptureDelivery(), max_pending=0)
    queue = BoundedRecoveryMailQueue(CaptureDelivery(), max_pending=1)
    message = RecoveryMail(EMAIL, "https://app.example.test/#private")
    queue.enqueue(message)
    queue.enqueue(RecoveryMail("ignored@example.test"))
    assert deliveries == []
    await asyncio.sleep(0)
    assert deliveries == [message]
    event.set()
    await queue.drain()
    await queue.drain()
    assert "mail queue is full" in caplog.text
    assert "mail delivery failed" in caplog.text
    assert "#private" not in caplog.text and EMAIL not in caplog.text


async def test_smtp_message_contents_use_fake_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[EmailMessage] = []
    fake = Mock()
    fake.__enter__ = Mock(return_value=fake)
    fake.__exit__ = Mock()
    fake.send_message.side_effect = sent.append
    smtp = Mock(return_value=fake)
    monkeypatch.setattr("smtplib.SMTP", smtp)
    handler = SMTPRecoveryMailer(
        host="synthetic", port=1025, sender="from@example.test"
    )
    await handler.deliver(
        RecoveryMail(EMAIL, "https://app.example.test/#reset_token=once")
    )
    await handler.deliver(RecoveryMail(EMAIL))
    smtp.assert_called_with("synthetic", 1025, timeout=10)
    assert fake.starttls.call_count == 2
    assert sent[0]["To"] == sent[1]["To"] == EMAIL
    assert "#reset_token=once" in sent[0].get_content()
    assert "30 minutes" in sent[0].get_content()
    assert "signed out" in sent[1].get_content()
    assert "#reset_token=" not in sent[1].get_content()


async def test_sql_adapter_only_passes_hashed_tokens_and_bound_fields() -> None:
    engine = MagicMock(spec=AsyncEngine)
    conn = AsyncMock()
    engine.begin.return_value.__aenter__.return_value = conn
    engine.connect.return_value.__aenter__.return_value = conn
    store = SQLAlchemyRecoveryStore(engine)
    args = {
        "email": EMAIL,
        "token_id": uuid4(),
        "token_hash": b"x" * 32,
        "expires_at": datetime.now(UTC),
    }
    conn.scalar.return_value = EMAIL
    assert await store.issue(**args) == EMAIL
    assert conn.scalar.call_args.args[1] == args
    user_id = uuid4()
    target = {"user_id": user_id, "email": EMAIL, "authorization_version": 9}
    conn.execute.return_value = Mock()
    mapped = conn.execute.return_value.mappings.return_value
    mapped.one_or_none.return_value = target
    assert await store.lookup(token_hash=b"x" * 32) == RecoveryTarget(user_id, EMAIL, 9)
    params = {
        "token_hash": b"x" * 32,
        "user_id": user_id,
        "expected_authorization_version": 9,
        "expected_email": EMAIL,
        "password_hash": "already-hashed",
        "audit_id": uuid4(),
        "audit_subject_hash": b"x" * 32,
        "request_id": uuid4(),
    }
    assert await store.complete(**params) == EMAIL
    assert conn.scalar.call_args.args[1]["version"] == 9
    mapped.one_or_none.return_value = None
    assert await store.lookup(token_hash=b"x" * 32) is None
