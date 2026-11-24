from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from conftest import database_dsn
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine
from test_conversation_repository import seed_user
from test_reauthentication_runtime import AllowSyntheticAttempts

from universal_ai_search.auth.local_account_deletion import (
    IssuedLocalAccountDeletion,
    LocalAccountDeletionReceiptInvalid,
    LocalAccountDeletionStore,
    LocalAccountDeletionUnsupported,
)
from universal_ai_search.auth.reauth import (
    ReauthenticationFailed,
    ReauthenticationService,
)
from universal_ai_search.auth.reauth_store import ReauthenticationStore, ReauthScope
from universal_ai_search.auth.security import PasswordSecurity
from universal_ai_search.conversations.repository import ConversationRepository


async def test_local_delete_denies_shared_and_external_state_and_never_retains_secrets(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    workspace = uuid4()
    connection.execute(
        """INSERT INTO app.workspaces (id,name,plan,status)
           VALUES (%s,'Synthetic workspace','free','active')""",
        (workspace,),
    )
    owner, other = seed_user(connection, workspace), seed_user(connection, workspace)
    security = PasswordSecurity()
    password = "Only synthetic input 3@-!b"
    connection.execute(
        "UPDATE app.users SET password_hash = %s WHERE id = %s",
        (security.hash(password), owner.user_id),
    )
    connection.execute(
        """UPDATE app.workspace_members SET role = 'owner'
           WHERE workspace_id = %s AND user_id = %s""",
        (workspace, owner.user_id),
    )
    connection.execute(
        """UPDATE app.workspace_members SET status = 'suspended'
           WHERE workspace_id = %s AND user_id = %s""",
        (workspace, other.user_id),
    )
    connection.commit()
    engine = create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )
    reauth = ReauthenticationService(
        store=ReauthenticationStore(engine),
        security=security,
        limiter=AllowSyntheticAttempts(),
        hash_key=b"s" * 32,
    )
    store = LocalAccountDeletionStore(engine, reauth=reauth, hash_key=b"s" * 32)
    scope = ReauthScope(owner.user_id, owner.session_id, workspace, 1, 1)
    try:
        valid = await reauth.obtain(
            scope=scope, purpose="delete_account", password=password, request_id=uuid4()
        )
        wrong_purpose = await reauth.obtain(
            scope=scope, purpose="export", password=password, request_id=uuid4()
        )

        async def run() -> IssuedLocalAccountDeletion:
            return await store.delete(
                scope=scope,
                reauth_token=valid.token,
                idempotency_key="synthetic-only-test",
                correlation_id=uuid4(),
            )

        for bad_scope, proof in (
            (replace(scope, session_id=other.session_id), valid.token),
            (scope, wrong_purpose.token),
        ):
            with pytest.raises(ReauthenticationFailed):
                await store.delete(
                    scope=bad_scope,
                    reauth_token=proof,
                    idempotency_key="synthetic-only-test",
                    correlation_id=uuid4(),
                )
        with pytest.raises(LocalAccountDeletionUnsupported):
            await run()
        connection.execute(
            "DELETE FROM app.workspace_members "
            "WHERE workspace_id = %s AND user_id = %s",
            (workspace, other.user_id),
        )
        upload_id = uuid4()
        connection.execute(
            """INSERT INTO app.device_upload_reservations
                   (upload_id, workspace_id, device_id, folder_id, manifest_id,
                    external_id, object_key, size_bytes, content_sha256, mime_type,
                    issued_at, expires_at)
               VALUES (%s, %s, %s, %s, %s, 'fixture', %s, 16, %s,
                       'text/plain', now(), now() + INTERVAL '10 minutes')""",
            (
                upload_id,
                workspace,
                uuid4(),
                uuid4(),
                uuid4(),
                f"device-inbox/{uuid4()}",
                "a" * 43,
            ),
        )
        connection.commit()
        with pytest.raises(LocalAccountDeletionUnsupported):
            await run()
        connection.execute(
            "DELETE FROM app.device_upload_reservations WHERE upload_id = %s",
            (upload_id,),
        )
        connection.commit()
        thread = await ConversationRepository(engine).create(
            scope=owner,
            title="Fixture conversation to remove",
            key_hash=b"k" * 32,
            request_hash=b"r" * 32,
        )
        receipt_rows = await asyncio.gather(run(), run(), return_exceptions=True)
        issued = next(
            row for row in receipt_rows if isinstance(row, IssuedLocalAccountDeletion)
        )
        assert sum(isinstance(row, ReauthenticationFailed) for row in receipt_rows) == 1
        assert issued.token not in repr(issued)
        assert issued.deletion.deadline_at - issued.deletion.requested_at == timedelta(
            hours=24
        )
        assert await store.read_receipt(issued.token) == issued.deletion
        with pytest.raises(LocalAccountDeletionReceiptInvalid):
            await store.read_receipt("a" * 43)
        for relation, identifier in (
            ("users", owner.user_id),
            ("sessions", owner.session_id),
            ("workspaces", workspace),
            ("conversations", thread.id),
        ):
            assert not connection.execute(
                f"SELECT id FROM app.{relation} WHERE id = %s", (identifier,)
            ).fetchone()
        assert connection.execute(
            "SELECT id FROM app.users WHERE id = %s", (other.user_id,)
        ).fetchone() == (other.user_id,)
        assert (
            connection.execute(
                "SELECT id FROM app.one_time_tokens WHERE user_id = %s",
                (owner.user_id,),
            ).fetchall()
            == []
        )
        saved = connection.execute(
            """SELECT receipt_hash, request_key_hash
               FROM app.local_account_deletion_receipts WHERE id = %s""",
            (issued.deletion.id,),
        ).fetchone()
        assert saved and all(isinstance(v, bytes) and len(v) == 32 for v in saved)
        assert issued.token.encode() not in saved
        with pytest.raises(ProgrammingError):
            async with engine.begin() as transaction:
                await transaction.execute(
                    text("SELECT * FROM app.local_account_deletion_receipts")
                )
        connection.execute(
            """UPDATE app.local_account_deletion_receipts
                SET requested_at = now() - INTERVAL '1 day',
                    completed_at = now() - INTERVAL '1 day',
                    deadline_at = now(), expires_at = now() - INTERVAL '1 hour'
                WHERE id = %s""",
            (issued.deletion.id,),
        )
        connection.commit()
        with pytest.raises(LocalAccountDeletionReceiptInvalid):
            await store.read_receipt(issued.token)
    finally:
        await engine.dispose()
