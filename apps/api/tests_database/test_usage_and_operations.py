from __future__ import annotations

import secrets
from uuid import UUID, uuid4

import httpx
import psycopg
from conftest import database_dsn

from test_indexing_runtime import CONNECTION_ID, USER_ID, WORKSPACE_ID, seed_workspace
from universal_ai_search.api.app import create_app
from universal_ai_search.api.auth import AuthenticationMode, Principal, WorkspaceContext
from universal_ai_search.config import Settings


class Actor:
    def __init__(self) -> None:
        self.user_id = USER_ID
        self.session_id = uuid4()

    async def authenticate(self, request: object) -> Principal:
        return Principal(
            subject_id=self.user_id,
            session_id=self.session_id,
            mode=AuthenticationMode.BEARER,
            authorization_version=1,
        )

    async def authorize(
        self, *, principal: Principal, workspace_id: UUID
    ) -> WorkspaceContext | None:
        if workspace_id != WORKSPACE_ID:
            return None
        return WorkspaceContext(
            workspace_id=workspace_id,
            user_id=principal.subject_id,
            authorization_version=1,
            role="member" if self.user_id != USER_ID else "owner",
        )


async def test_usage_and_operation_retry_are_project_scoped(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    seed_workspace(connection)
    failed_job, repo, foreign_user = uuid4(), uuid4(), uuid4()
    actor = Actor()
    foreign_session = uuid4()
    connection.execute(
        """INSERT INTO app.users (id,email,full_name,status)
           VALUES (%s,'peer@example.test','Peer','active')""",
        (foreign_user,),
    )
    connection.execute(
        """INSERT INTO app.workspace_members (user_id,workspace_id,role,status)
           VALUES (%s,%s,'member','active')""",
        (foreign_user, WORKSPACE_ID),
    )
    connection.execute(
        """INSERT INTO app.source_collections
           (id,workspace_id,connection_id,provider_external_id,kind,name,selected,status)
           VALUES (%s,%s,%s,'42','repository','Fixture',true,'active')""",
        (repo, WORKSPACE_ID, CONNECTION_ID),
    )
    connection.execute(
        """INSERT INTO app.jobs
           (id,workspace_id,connection_id,job_type,queue,idempotency_key,
            status,error_code,payload)
           VALUES (%s,%s,%s,'sync','sync','prior','failed','PROVIDER_UNAVAILABLE',
                   jsonb_build_object('mode','github_repository','collection_id',%s::TEXT,
                                      'ignored_internal','private internal note'))""",
        (failed_job, WORKSPACE_ID, CONNECTION_ID, str(repo)),
    )
    connection.execute(
        "UPDATE app.workspace_usage SET indexed_source_count=4,extracted_bytes=128"
        " WHERE workspace_id=%s",
        (WORKSPACE_ID,),
    )
    connection.execute(
        "UPDATE app.users SET email_verified_at=clock_timestamp() WHERE id=ANY(%s)",
        ([USER_ID, foreign_user],),
    )
    for owner, session_id in (
        (USER_ID, actor.session_id),
        (foreign_user, foreign_session),
    ):
        connection.execute(
            """INSERT INTO app.sessions
               (id,user_id,refresh_token_hash,csrf_token_hash,family_id,issued_at,expires_at)
               VALUES (%s,%s,%s,%s,%s,now(),now()+interval '10 minutes')""",
            (
                session_id,
                owner,
                secrets.token_bytes(32),
                secrets.token_bytes(32),
                uuid4(),
            ),
        )
    connection.commit()

    app = create_app(
        Settings(
            database_url=database_dsn().replace(
                "postgresql://", "postgresql+asyncpg://"
            )
        )
    )
    app.state.authentication_backend = actor
    app.state.workspace_authorization_backend = actor
    headers = {"X-Workspace-ID": str(WORKSPACE_ID), "Idempotency-Key": "job-retry-01"}
    path = f"/v1/operations/{failed_job}"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            usage = await client.get("/v1/usage", headers=headers)
            assert usage.status_code == 200
            assert usage.json()["indexed_source_count"] == 4
            before = await client.get(path, headers=headers)
            assert before.status_code == 200
            assert before.json()["operation"]["error"]["retryable"] is True
            started = await client.post(f"{path}:retry", headers=headers)
            assert started.status_code == 202
            location = started.headers["location"]
            same = await client.post(f"{path}:retry", headers=headers)
            assert same.headers["location"] == location
            conflict = await client.post(
                f"{path}:retry", headers={**headers, "Idempotency-Key": "job-retry-02"}
            )
            assert conflict.status_code == 409
            assert started.json()["operation"]["links"]["previous"] == path
            assert "private internal note" not in started.text
            actor.user_id = foreign_user
            actor.session_id = foreign_session
            denied = await client.get(location, headers=headers)
            assert denied.status_code == 404
    finally:
        await app.state.database_engine.dispose()

    assert connection.execute(
        "SELECT count(*) FROM app.jobs WHERE payload ->> 'previous_operation_id'=%s",
        (str(failed_job),),
    ).fetchone() == (1,)
