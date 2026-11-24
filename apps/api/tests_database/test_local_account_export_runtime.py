from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from conftest import database_dsn
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_conversation_repository import seed_user
from test_reauthentication_runtime import AllowSyntheticAttempts

from universal_ai_search.auth.local_account_export import (
    ExportBusy,
    ExportMissing,
    ExportUnsupported,
    LocalAccountExportStore,
)
from universal_ai_search.auth.reauth import (
    ReauthenticationFailed,
    ReauthenticationService,
)
from universal_ai_search.auth.reauth_store import (
    ReauthenticationStore,
    ReauthPurpose,
    ReauthScope,
)
from universal_ai_search.auth.security import PasswordSecurity

_PASSWORD = "Synthetic fixture pass 3@-!b"


def _fixture(
    connection: psycopg.Connection[tuple[object, ...]],
) -> tuple[ReauthScope, AsyncEngine, LocalAccountExportStore, ReauthenticationService]:
    workspace = uuid4()
    connection.execute(
        "INSERT INTO app.workspaces (id,name,plan,status) "
        "VALUES (%s,'Export Fixture','free','active')",
        (workspace,),
    )
    user = seed_user(connection, workspace)
    security = PasswordSecurity()
    connection.execute(
        "UPDATE app.users SET password_hash = %s WHERE id = %s",
        (security.hash(_PASSWORD), user.user_id),
    )
    connection.execute(
        "UPDATE app.workspace_members SET role = 'owner' "
        "WHERE workspace_id = %s AND user_id = %s",
        (workspace, user.user_id),
    )
    connection.commit()
    engine = create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )
    auth = ReauthenticationService(
        store=ReauthenticationStore(engine),
        security=security,
        limiter=AllowSyntheticAttempts(),
        hash_key=b"s" * 32,
    )
    store = LocalAccountExportStore(engine, reauth=auth, export_secret=b"e" * 32)
    return (
        ReauthScope(user.user_id, user.session_id, workspace, 1, 1),
        engine,
        store,
        auth,
    )


def _fill(
    connection: psycopg.Connection[tuple[object, ...]],
    scope: ReauthScope,
) -> UUID:
    conversation = uuid4()
    connection.execute(
        """INSERT INTO app.conversations (id,workspace_id,user_id,title,status)
           VALUES (%s,%s,%s,'Portable fixture chat','active')""",
        (conversation, scope.workspace_id, scope.user_id),
    )
    connection.execute(
        """INSERT INTO app.messages
             (id,workspace_id,conversation_id,role,status,content)
           VALUES (%s,%s,%s,'user','complete','synthetic private user message'),
                  (%s,%s,%s,'assistant','complete',
                   'must not export generated material')""",
        (
            uuid4(),
            scope.workspace_id,
            conversation,
            uuid4(),
            scope.workspace_id,
            conversation,
        ),
    )
    connection.execute(
        """INSERT INTO app.search_requests
           (id,workspace_id,user_id,mode,query_text,normalized_plan,planner_version,
            ranker_version,index_generation,authorization_version,status,created_at,purge_after)
           VALUES (%s,%s,%s,'results','private synthetic search','{}','1','1',1,1,
                   'completed',now(),now()+interval '2 hours'),
                  (%s,%s,%s,'results','expired synthetic search','{}','1','1',1,1,
                   'completed',now()-interval '2 hours',now()-interval '1 hour')""",
        (
            uuid4(),
            scope.workspace_id,
            scope.user_id,
            uuid4(),
            scope.workspace_id,
            scope.user_id,
        ),
    )
    connection.commit()
    return conversation


async def _proof(
    auth: ReauthenticationService, scope: ReauthScope, purpose: ReauthPurpose = "export"
) -> str:
    return (
        await auth.obtain(
            scope=scope, purpose=purpose, password=_PASSWORD, request_id=uuid4()
        )
    ).token


async def test_export_receipt_is_private_and_download_exactly_once(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    scope, engine, store, auth = _fixture(connection)
    _fill(connection, scope)
    foreign, foreign_engine, _, _ = _fixture(connection)
    _fill(connection, foreign)
    second_session = uuid4()
    connection.execute(
        """INSERT INTO app.sessions
            (id,user_id,refresh_token_hash,family_id,issued_at,expires_at,csrf_token_hash)
           VALUES (%s,%s,%s,%s,now(),now()+interval '10 minutes',%s)""",
        (
            second_session,
            scope.user_id,
            secrets.token_bytes(32),
            uuid4(),
            secrets.token_bytes(32),
        ),
    )
    connection.commit()

    async def redeem() -> bytes | None:
        try:
            return (
                await store.download(
                    scope=scope, operation_id=receipt.id, request_id=uuid4()
                )
            ).content
        except ExportMissing:
            return None

    try:
        wrong_purpose = await _proof(auth, scope, "disconnect")
        with pytest.raises(ReauthenticationFailed):
            await store.start(
                scope=scope,
                proof=wrong_purpose,
                idempotency_key="export-fixture",
                request_id=uuid4(),
            )
        proof = await _proof(auth, scope)
        kwargs: dict[str, Any] = {
            "scope": scope,
            "proof": proof,
            "idempotency_key": "export-fixture",
            "request_id": uuid4(),
        }
        receipt = await store.start(**kwargs)
        assert receipt == await store.start(**kwargs)
        assert not receipt.downloadable
        assert (await store.read(scope=scope, operation_id=receipt.id)).downloadable
        with pytest.raises(ExportMissing):
            await store.read(
                scope=replace(scope, session_id=second_session), operation_id=receipt.id
            )
        with pytest.raises(ExportMissing):
            await store.download(
                scope=foreign, operation_id=receipt.id, request_id=uuid4()
            )
        with pytest.raises(ReauthenticationFailed):
            await store.start(**{**kwargs, "idempotency_key": "unused-key"})
        unused_proof = await _proof(auth, scope)
        with pytest.raises(ExportBusy):
            await store.start(
                **{**kwargs, "proof": unused_proof, "idempotency_key": "new-unused-key"}
            )
        payloads = [
            payload for payload in await asyncio.gather(redeem(), redeem()) if payload
        ]
        assert len(payloads) == 1
        archive = json.loads(payloads[0])
        assert archive["format"] == "universal-ai-search/local-account/v1"
        assert (
            archive["profile"]["email"] == f"conversation-{scope.user_id}@example.test"
        )
        assert archive["workspace"]["id"] == str(scope.workspace_id)
        assert [m["content"] for m in archive["user_messages"]] == [
            "synthetic private user message"
        ]
        assert [h["query_text"] for h in archive["search_history"]] == [
            "private synthetic search"
        ]
        assert (
            b"must not export" not in payloads[0]
            and b"expired synthetic" not in payloads[0]
            and b"password" not in payloads[0]
            and str(foreign.user_id).encode() not in payloads[0]
        )
        after = await store.read(scope=scope, operation_id=receipt.id)
        assert not after.downloadable
        assert await store.start(**kwargs) == receipt
        row = connection.execute(
            """SELECT octet_length(snapshot_hash),octet_length(request_key_hash),
                      consumed_at IS NOT NULL, to_jsonb(e)::text
               FROM app.local_account_exports e WHERE id=%s""",
            (receipt.id,),
        ).fetchone()
        assert row is not None and row[:3] == (32, 32, True)
        assert isinstance(row[3], str)
        for excluded in (
            "snapshot_content",
            "private synthetic",
            proof,
            "export-fixture",
        ):
            assert excluded not in row[3]
        operation = await store.start(
            **{**kwargs, "proof": unused_proof, "idempotency_key": "new-unused-key"}
        )
        assert operation.id != receipt.id
        names = connection.execute(
            """SELECT action FROM app.audit_events WHERE workspace_id=%s
               AND action LIKE 'account.export%%' ORDER BY created_at""",
            (scope.workspace_id,),
        ).fetchall()
        assert names == [
            ("account.export",),
            ("account.export_download",),
            ("account.export",),
        ]
    finally:
        await engine.dispose()
        await foreign_engine.dispose()


async def test_export_fails_closed_for_changed_content_expiry_and_deletion(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    scope, engine, store, auth = _fixture(connection)
    conversation = _fill(connection, scope)
    try:
        receipt = await store.start(
            scope=scope,
            proof=await _proof(auth, scope),
            idempotency_key="cutoff-fixture",
            request_id=uuid4(),
        )
        connection.execute(
            "UPDATE app.conversations SET status='deleted', deleted_at=now() "
            "WHERE id=%s",
            (conversation,),
        )
        connection.commit()
        with pytest.raises(ExportMissing):
            await store.download(
                scope=scope, operation_id=receipt.id, request_id=uuid4()
            )
        connection.execute(
            """UPDATE app.local_account_exports
               SET created_at=now()-interval '10 minutes',
               completed_at=now()-interval '10 minutes',
               download_expires_at=now()-interval '5 minutes' WHERE id=%s""",
            (receipt.id,),
        )
        connection.commit()
        assert not (await store.read(scope=scope, operation_id=receipt.id)).downloadable
        new_receipt = await store.start(
            scope=scope,
            proof=await _proof(auth, scope),
            idempotency_key="cutoff-fixture-new",
            request_id=uuid4(),
        )
        connection.execute(
            """INSERT INTO app.deletion_requests
                 (id,workspace_id,requested_by_user_id,target_type,target_id,status,
                  idempotency_key,deadline_at,receipt_token_hash)
               VALUES (%s,%s,%s,'account',%s,'pending','fixture',
                       now()+interval '1 hour',%s)""",
            (
                uuid4(),
                scope.workspace_id,
                scope.user_id,
                scope.user_id,
                secrets.token_bytes(32),
            ),
        )
        connection.commit()
        with pytest.raises(ExportMissing):
            await store.read(scope=scope, operation_id=new_receipt.id)
        with pytest.raises(ExportMissing):
            await store.download(
                scope=scope, operation_id=new_receipt.id, request_id=uuid4()
            )
    finally:
        await engine.dispose()


async def test_export_rejects_hidden_memberships_and_large_snapshot(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    scope, engine, store, auth = _fixture(connection)
    other = uuid4()
    connection.execute(
        """INSERT INTO app.workspaces (id,name,plan,status)
           VALUES (%s,'Hidden Fixture','free','active')""",
        (other,),
    )
    connection.execute(
        """INSERT INTO app.workspace_members (workspace_id,user_id,role,status)
           VALUES (%s,%s,'member','suspended')""",
        (other, scope.user_id),
    )
    connection.commit()
    try:
        proof = await _proof(auth, scope)
        kwargs: dict[str, Any] = {
            "scope": scope,
            "proof": proof,
            "idempotency_key": "too-many-fixtures",
            "request_id": uuid4(),
        }
        with pytest.raises(ExportUnsupported):
            await store.start(**kwargs)
        connection.execute("DELETE FROM app.workspaces WHERE id=%s", (other,))
        connection.execute(
            """INSERT INTO app.conversations (id,workspace_id,user_id,title,status)
               SELECT gen_random_uuid(),%s,%s,'Over limit','active'
               FROM generate_series(1,501)""",
            (scope.workspace_id, scope.user_id),
        )
        connection.commit()
        with pytest.raises(ExportUnsupported):
            await store.start(**kwargs)
        connection.execute(
            "DELETE FROM app.conversations WHERE workspace_id=%s", (scope.workspace_id,)
        )
        connection.commit()
        await store.start(**kwargs)
        async with engine.begin() as authorized:
            # No session-local tenant settings means no receipt rows at all.
            assert (
                await authorized.scalar(
                    text("SELECT count(*) FROM app.local_account_exports")
                )
                == 0
            )
    finally:
        await engine.dispose()
