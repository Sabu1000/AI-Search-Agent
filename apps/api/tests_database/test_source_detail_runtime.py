from __future__ import annotations

import secrets
from uuid import UUID, uuid4

import psycopg
import pytest
from conftest import database_dsn
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from uas_connector_sdk import NormalizedDocument, Provider

from test_indexing_runtime import CONNECTION_ID, USER_ID, WORKSPACE_ID, seed_workspace
from universal_ai_search.indexing.pipeline import IndexingPipeline
from universal_ai_search.indexing.repository import IndexRepository
from universal_ai_search.indexing.runtime import IndexingRuntime
from universal_ai_search.search.source_detail import (
    SourceDetail,
    SourceDetailRepository,
)

_PRIVATE = "Private source body must never be selected or serialized."
_GITHUB_URL = f"https://github.com/o/repo/blob/{'a' * 40}/file.md"


def _engine() -> AsyncEngine:
    return create_async_engine(
        database_dsn().replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"role": "app_api"}},
    )


def _session(
    connection: psycopg.Connection[tuple[object, ...]],
    user_id: UUID,
) -> UUID:
    session = uuid4()
    connection.execute(
        """INSERT INTO app.sessions
           (id, user_id, refresh_token_hash, family_id, issued_at,
            expires_at, csrf_token_hash, authorization_version)
           VALUES (%s, %s, %s, %s, now(), now() + interval '1 hour', %s, 1)""",
        (session, user_id, secrets.token_bytes(32), uuid4(), secrets.token_bytes(32)),
    )
    connection.commit()
    return session


def _index(
    *,
    account: UUID,
    workspace: UUID = WORKSPACE_ID,
    provider: Provider = Provider.GITHUB,
    title: str = "file.md",
    canonical_url: str = _GITHUB_URL,
    content: str = _PRIVATE,
    promote: bool = True,
    external_id: str = "search-evidence",
) -> tuple[UUID, UUID]:
    index = IndexRepository(database_dsn())
    document = NormalizedDocument(
        external_id=external_id,
        provider=provider,
        source_type="file",
        title=title,
        content=content,
        canonical_url=canonical_url,
        mime_type="text/markdown",
        provider_metadata={"unsafe_debug": "/Users/test/never_expose"},
    )
    result = index.enqueue(workspace, account, document)
    if promote:
        assert IndexingRuntime(index, IndexingPipeline()).run_once("source-detail-test")
    return result.source_id, result.document_version_id


def _chunk(connection: psycopg.Connection[tuple[object, ...]], version: UUID) -> UUID:
    row = connection.execute(
        "SELECT id FROM app.chunks WHERE document_version_id=%s ORDER BY chunk_index "
        "LIMIT 1",
        (version,),
    ).fetchone()
    assert row is not None and isinstance(row[0], UUID)
    return row[0]


def _grant(connection: psycopg.Connection[tuple[object, ...]], source: UUID) -> UUID:
    collection = uuid4()
    connection.execute(
        """INSERT INTO app.source_collections
           (id, workspace_id, connection_id, provider_external_id, kind, name, selected,
            status, github_verified_at)
           VALUES (%s, %s, %s, '7', 'repository', 'Test repo',
                   true, 'active', now())""",
        (collection, WORKSPACE_ID, CONNECTION_ID),
    )
    connection.execute(
        """INSERT INTO app.source_collection_memberships
           (workspace_id, connection_id, source_id, collection_id, relationship)
           VALUES (%s, %s, %s, %s, 'direct')""",
        (WORKSPACE_ID, CONNECTION_ID, source, collection),
    )
    connection.commit()
    return collection


async def _get(
    repository: SourceDetailRepository,
    *,
    source: UUID,
    session: UUID,
    user: UUID = USER_ID,
    workspace: UUID = WORKSPACE_ID,
    chunk: UUID | None = None,
    authorization_version: int = 1,
) -> SourceDetail | None:
    return await repository.get(
        workspace_id=workspace,
        user_id=user,
        session_id=session,
        authorization_version=authorization_version,
        user_authorization_version=1,
        source_id=source,
        chunk_id=chunk,
    )


@pytest.mark.asyncio
async def test_source_detail_rejects_revoked_stale_deleted_and_superseded(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    seed_workspace(connection)
    session = _session(connection, USER_ID)
    source, version = _index(account=CONNECTION_ID)
    collection = _grant(connection, source)
    chunk = _chunk(connection, version)
    engine = _engine()
    repository = SourceDetailRepository(engine)
    try:
        found = await _get(repository, source=source, session=session, chunk=chunk)
        assert found is not None and found.open_url == _GITHUB_URL
        assert found.citation_location is not None
        assert found.citation_location.chunk_id == chunk
        assert found.title == "file.md" and found.index_state == "ready"
        assert _PRIVATE not in repr(found) and "/Users/test/never_expose" not in repr(
            found
        )
        assert (
            await _get(repository, source=source, session=session, chunk=uuid4())
            is None
        )
        assert (
            await _get(
                repository,
                source=source,
                session=session,
                authorization_version=2,
            )
            is None
        )

        connection.execute(
            "UPDATE app.source_collections SET github_verified_at = "
            "now() - interval '16 minutes' WHERE id=%s",
            (collection,),
        )
        connection.commit()
        assert await _get(repository, source=source, session=session) is None
        connection.execute(
            "UPDATE app.source_collections SET github_verified_at=now() WHERE id=%s",
            (collection,),
        )
        connection.commit()
        assert await _get(repository, source=source, session=session) is not None

        same_source, new_version = _index(
            account=CONNECTION_ID, content="Replacement version with a different chunk."
        )
        assert same_source == source and new_version != version
        assert (
            await _get(repository, source=source, session=session, chunk=chunk) is None
        )
        new_chunk = _chunk(connection, new_version)
        assert (
            await _get(
                repository,
                source=source,
                session=session,
                chunk=new_chunk,
            )
            is not None
        )

        connection.execute(
            "UPDATE app.connections SET status='reauthorization_required' WHERE id=%s",
            (CONNECTION_ID,),
        )
        connection.commit()
        assert await _get(repository, source=source, session=session) is None
        connection.execute(
            "UPDATE app.connections SET status='active' WHERE id=%s",
            (CONNECTION_ID,),
        )
        connection.execute(
            "UPDATE app.sources SET state='deleted', deleted_at=now() WHERE id=%s",
            (source,),
        )
        connection.commit()
        assert await _get(repository, source=source, session=session) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_source_detail_rechecks_session_tenant_and_local_owner(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    seed_workspace(connection)
    actor_session = _session(connection, USER_ID)
    peer, foreign_workspace, local_connection = (uuid4() for _ in range(3))
    connection.execute(
        "INSERT INTO app.users (id,email,full_name,status) "
        "VALUES (%s,'second-fixture@example.test','Other fixture','active')",
        (peer,),
    )
    connection.execute(
        "INSERT INTO app.workspaces (id,name,plan,status) "
        "VALUES (%s,'Foreign fixture','free','active')",
        (foreign_workspace,),
    )
    # Another active member can use the workspace, but not this owner's local file.
    connection.execute(
        "INSERT INTO app.workspace_members (workspace_id,user_id,role,status) "
        "VALUES (%s,%s,'member','active'),(%s,%s,'owner','active')",
        (WORKSPACE_ID, peer, foreign_workspace, peer),
    )
    connection.execute(
        "INSERT INTO app.connections "
        "(id,workspace_id,owner_user_id,provider,display_label,status) "
        "VALUES (%s,%s,%s,'local_files','Fixture local','active')",
        (local_connection, WORKSPACE_ID, USER_ID),
    )
    connection.commit()
    peer_session = _session(connection, peer)
    source, version = _index(
        account=local_connection,
        provider=Provider.LOCAL_FILES,
        title=r"C:\Users\Test\Private\file.md",
    )
    chunk = _chunk(connection, version)
    engine = _engine()
    repository = SourceDetailRepository(engine)
    try:
        owned = await _get(
            repository, source=source, session=actor_session, chunk=chunk
        )
        assert owned is not None and owned.title == "Local file"
        assert owned.sync_state == "not_applicable" and owned.open_url is None
        assert "Private" not in repr(owned)
        assert (
            await _get(
                repository,
                source=source,
                session=peer_session,
                user=peer,
            )
            is None
        )
        assert (
            await _get(
                repository,
                source=source,
                session=peer_session,
                user=peer,
                workspace=foreign_workspace,
            )
            is None
        )
        assert (
            await _get(
                repository,
                source=source,
                session=peer_session,
                user=USER_ID,
            )
            is None
        )

        connection.execute(
            "UPDATE app.sessions SET revoked_at=now() WHERE id=%s", (actor_session,)
        )
        connection.commit()
        assert await _get(repository, source=source, session=actor_session) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_metadata_reports_safe_refresh_failure_without_reopening_old_citation(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    seed_workspace(connection)
    session = _session(connection, USER_ID)
    source, ready_version = _index(account=CONNECTION_ID)
    _grant(connection, source)
    old_chunk = _chunk(connection, ready_version)
    _, pending_version = _index(
        account=CONNECTION_ID,
        content="Still indexing new content.",
        canonical_url="https://unapproved.example/open?resourcekey=private",
        promote=False,
    )
    engine = _engine()
    repository = SourceDetailRepository(engine)
    try:
        pending = await _get(repository, source=source, session=session)
        assert pending is not None and pending.index_state == "refresh_pending"
        assert pending.failure_code is None and pending.open_url is None
        assert (
            await _get(
                repository,
                source=source,
                session=session,
                chunk=old_chunk,
            )
            is None
        )

        connection.execute(
            "UPDATE app.document_versions SET state='failed', failure_code=%s "
            "WHERE id=%s",
            ("provider debug /Users/test/do-not-reveal", pending_version),
        )
        connection.commit()
        failed = await _get(repository, source=source, session=session)
        assert failed is not None and failed.index_state == "refresh_failed"
        assert failed.failure_code == "INDEXING_FAILED"
        assert "provider debug" not in repr(failed)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_source_list_keysets_filter_revoked_and_nonowner_local_rows(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    seed_workspace(connection)
    actor_session = _session(connection, USER_ID)
    remote, _ = _index(account=CONNECTION_ID)
    collection = _grant(connection, remote)
    for provider, account, label in (
        ("google", google := uuid4(), "Fixture Gmail"),
        ("local_files", local := uuid4(), "Fixture local"),
    ):
        connection.execute(
            "INSERT INTO app.connections "
            "(id,workspace_id,owner_user_id,provider,display_label,status) "
            "VALUES (%s,%s,%s,%s,%s,'active')",
            (account, WORKSPACE_ID, USER_ID, provider, label),
        )
    peer = uuid4()
    connection.execute(
        "INSERT INTO app.users (id,email,full_name,status) "
        "VALUES (%s,'listing-peer-fixture@example.test','Other fixture','active')",
        (peer,),
    )
    connection.execute(
        "INSERT INTO app.workspace_members (workspace_id,user_id,role,status) "
        "VALUES (%s,%s,'member','active')",
        (WORKSPACE_ID, peer),
    )
    connection.commit()
    peer_session = _session(connection, peer)
    mail, _ = _index(
        account=google,
        provider=Provider.GMAIL,
        external_id="mail-source",
        canonical_url="https://mail.google.com/mail/u/0/#all/synthetic",
    )
    local_source, _ = _index(
        account=local,
        provider=Provider.LOCAL_FILES,
        title="/private/build/notes.md",
    )
    connection.execute(
        "UPDATE app.sources SET created_at = '2026-01-01T00:00:00Z' "
        "WHERE workspace_id=%s",
        (WORKSPACE_ID,),
    )
    connection.commit()
    engine = _engine()
    repository = SourceDetailRepository(engine)
    try:
        first = await repository.list(
            workspace_id=WORKSPACE_ID,
            user_id=USER_ID,
            session_id=actor_session,
            authorization_version=1,
            user_authorization_version=1,
            providers=(),
            source_types=(),
            before=None,
            limit=1,
        )
        pivot = first[0]
        rest = await repository.list(
            workspace_id=WORKSPACE_ID,
            user_id=USER_ID,
            session_id=actor_session,
            authorization_version=1,
            user_authorization_version=1,
            providers=(),
            source_types=(),
            before=(pivot.created_at, pivot.source.source_id),
            limit=10,
        )
        expected = sorted((mail, remote, local_source), reverse=True)
        assert [entry.source.source_id for entry in first + rest] == expected
        protected_local = [
            entry.source
            for entry in first + rest
            if entry.source.source_id == local_source
        ][0]
        assert (
            protected_local.title == "Local file" and protected_local.open_url is None
        )
        assert "/private/" not in repr(protected_local)

        everyone = await repository.list(
            workspace_id=WORKSPACE_ID,
            user_id=peer,
            session_id=peer_session,
            authorization_version=1,
            user_authorization_version=1,
            providers=(),
            source_types=(),
            before=None,
            limit=10,
        )
        assert {entry.source.source_id for entry in everyone} == {mail, remote}
        filtered = await repository.list(
            workspace_id=WORKSPACE_ID,
            user_id=peer,
            session_id=peer_session,
            authorization_version=1,
            user_authorization_version=1,
            providers=("github",),
            source_types=("file",),
            before=None,
            limit=10,
        )
        assert [entry.source.source_id for entry in filtered] == [remote]
        wrong_type = await repository.list(
            workspace_id=WORKSPACE_ID,
            user_id=peer,
            session_id=peer_session,
            authorization_version=1,
            user_authorization_version=1,
            providers=("github",),
            source_types=("code",),
            before=None,
            limit=10,
        )
        assert wrong_type == []

        connection.execute(
            "UPDATE app.source_collections SET github_verified_at=NULL WHERE id=%s",
            (collection,),
        )
        connection.commit()
        current = await repository.list(
            workspace_id=WORKSPACE_ID,
            user_id=peer,
            session_id=peer_session,
            authorization_version=1,
            user_authorization_version=1,
            providers=(),
            source_types=(),
            before=None,
            limit=10,
        )
        assert [entry.source.source_id for entry in current] == [mail]
    finally:
        await engine.dispose()
