from datetime import UTC, datetime, timedelta
from uuid import uuid4

from conftest import database_dsn
from sqlalchemy.ext.asyncio import create_async_engine

from universal_ai_search.auth.store import SQLAlchemyAuthStore
from universal_ai_search.auth.verification_resend import (
    SQLAlchemyVerificationResendStore,
)


async def test_public_resend_replaces_only_pending_confirmation() -> None:
    engine = create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )
    auth = SQLAlchemyAuthStore(engine)
    service = SQLAlchemyVerificationResendStore(engine)
    actor, next_token_id = uuid4(), uuid4()
    addr = f"resend-{actor}@example.test"
    old_hash, new_hash = actor.bytes * 2, next_token_id.bytes * 2
    try:
        assert await auth.register(
            user_id=actor,
            email=addr,
            password_hash="unused synthetic hash",
            full_name="Resend fixture",
            terms_version="test",
            locale="en-US",
            token_id=uuid4(),
            token_hash=old_hash,
            token_expiry=datetime.now(UTC) + timedelta(hours=1),
        )
        assert await service.issue(addr, next_token_id, new_hash)
        assert (
            await auth.verify_email(token_hash=old_hash, workspace_id=uuid4()) is None
        )
        assert (
            await auth.verify_email(token_hash=new_hash, workspace_id=uuid4())
            is not None
        )
        assert not await service.issue(addr, uuid4(), b"a" * 32)
        assert not await service.issue("unregistered@example.test", uuid4(), b"a" * 32)
    finally:
        await engine.dispose()
