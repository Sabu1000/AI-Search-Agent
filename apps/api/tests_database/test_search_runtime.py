from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import psycopg
import pytest
from conftest import database_dsn
from sqlalchemy.ext.asyncio import create_async_engine
from uas_connector_sdk import DocumentPerson, Provider, RawItem
from uas_connector_sdk.testing import FakeConnector

from test_indexing_runtime import CONNECTION_ID, USER_ID, WORKSPACE_ID, seed_workspace
from universal_ai_search.indexing.pipeline import IndexingPipeline
from universal_ai_search.indexing.repository import IndexRepository
from universal_ai_search.indexing.runtime import IndexingRuntime
from universal_ai_search.search.planner import SearchFilters
from universal_ai_search.search.repository import SearchRepository
from universal_ai_search.search.service import SearchInput, SearchService


async def _indexed_document(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    seed_workspace(connection)
    document = await FakeConnector().normalize(
        RawItem(
            external_id="repo:1:payments.md",
            payload={
                "title": "Payment retry decision",
                "content": (
                    "# Decision\n\nMaya selected capped exponential payment retries "
                    "with a maximum of five attempts."
                ),
            },
        )
    )
    document = document.model_copy(
        update={
            "people": (
                DocumentPerson(
                    relationship="author",
                    identity_kind="email",
                    normalized_identifier="maya@example.test",
                    display_name="Maya Chen",
                ),
            )
        }
    )
    repository = IndexRepository(database_dsn())
    enqueued = repository.enqueue(WORKSPACE_ID, CONNECTION_ID, document)
    collection_id = uuid4()
    connection.execute(
        """INSERT INTO app.source_collections
           (id, workspace_id, connection_id, provider_external_id, kind, name,
            selected, status, github_verified_at)
           VALUES (%s, %s, %s, '1', 'repository', 'Demo', true, 'active', now())""",
        (collection_id, WORKSPACE_ID, CONNECTION_ID),
    )
    connection.execute(
        """INSERT INTO app.source_collection_memberships
           (workspace_id, connection_id, source_id, collection_id, relationship)
           VALUES (%s, %s, %s, %s, 'direct')""",
        (WORKSPACE_ID, CONNECTION_ID, enqueued.source_id, collection_id),
    )
    connection.commit()
    assert IndexingRuntime(repository, IndexingPipeline()).run_once("search-test")


def _database_url() -> str:
    return database_dsn().replace("postgresql://", "postgresql+asyncpg://")


@pytest.mark.asyncio
async def test_hybrid_search_finds_indexed_content_and_records_request(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    await _indexed_document(connection)
    engine = create_async_engine(
        _database_url(), connect_args={"server_settings": {"role": "app_api"}}
    )
    try:
        output = await SearchService(SearchRepository(engine)).search(
            workspace_id=WORKSPACE_ID,
            user_id=USER_ID,
            authorization_version=1,
            search=SearchInput(
                "capped exponential payment retries",
                "answer",
                SearchFilters(providers=("github",), file_types=("md",)),
                20,
            ),
        )
    finally:
        await engine.dispose()

    assert output.ranked
    assert output.ranked[0].candidate.title == "Payment retry decision"
    assert output.context
    assert output.insufficient_reason is None
    assert connection.execute(
        "SELECT mode, status, result_count FROM app.search_requests WHERE id = %s",
        (output.request_id,),
    ).fetchone() == ("answer", "completed", len(output.ranked))


@pytest.mark.asyncio
async def test_search_filters_and_rls_fail_closed(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    await _indexed_document(connection)
    engine = create_async_engine(
        _database_url(), connect_args={"server_settings": {"role": "app_api"}}
    )
    service = SearchService(SearchRepository(engine))
    try:
        filtered = await service.search(
            workspace_id=WORKSPACE_ID,
            user_id=USER_ID,
            authorization_version=1,
            search=SearchInput(
                "payment retries",
                "results",
                SearchFilters(providers=("gmail",)),
                20,
            ),
        )
        filtered_by_person = await service.search(
            workspace_id=WORKSPACE_ID,
            user_id=USER_ID,
            authorization_version=1,
            search=SearchInput(
                "payment retries",
                "results",
                SearchFilters(people=("maya chen",)),
                20,
            ),
        )
        missing_person = await service.search(
            workspace_id=WORKSPACE_ID,
            user_id=USER_ID,
            authorization_version=1,
            search=SearchInput(
                "payment retries",
                "results",
                SearchFilters(people=("nobody@example.test",)),
                20,
            ),
        )
        with pytest.raises(RuntimeError, match="workspace disappeared"):
            await service.search(
                workspace_id=uuid4(),
                user_id=USER_ID,
                authorization_version=1,
                search=SearchInput("payment retries", "results", SearchFilters(), 20),
            )
        with pytest.raises(RuntimeError, match="workspace disappeared"):
            await service.search(
                workspace_id=WORKSPACE_ID,
                user_id=UUID("99999999-0000-4000-8000-000000000999"),
                authorization_version=1,
                search=SearchInput("payment retries", "results", SearchFilters(), 20),
            )
    finally:
        await engine.dispose()

    assert filtered.ranked == ()
    assert filtered_by_person.ranked
    assert missing_person.ranked == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("also_google", [True, False])
async def test_final_search_fence_excludes_repository_revoked_after_retrieval(
    connection: psycopg.Connection[tuple[object, ...]],
    monkeypatch: pytest.MonkeyPatch,
    also_google: bool,
) -> None:
    await _indexed_document(connection)
    if also_google:
        google = uuid4()
        connection.execute(
            """INSERT INTO app.connections
            (id, workspace_id, owner_user_id, provider, display_label, status)
            VALUES (%s, %s, %s, 'google', 'Synthetic Gmail', 'active')""",
            (google, WORKSPACE_ID, USER_ID),
        )
        connection.commit()
        normalize = await FakeConnector().normalize(
            RawItem(
                external_id="search-fence-email",
                payload={
                    "title": "Payment retries follow-up",
                    "content": "Payment retries are paused during infrastructure work.",
                },
            )
        )
        document = normalize.model_copy(
            update={"provider": Provider.GMAIL, "source_type": "email"}
        )
        index_repository = IndexRepository(database_dsn())
        index_repository.enqueue(WORKSPACE_ID, google, document)
        assert IndexingRuntime(index_repository, IndexingPipeline()).run_once(
            "search-fence-test"
        )
    engine = create_async_engine(
        _database_url(), connect_args={"server_settings": {"role": "app_api"}}
    )
    repository = SearchRepository(engine)
    record = repository.record

    async def revoke_after_record(**values: object) -> None:
        # First prove the revoked result was present during ranked retrieval.
        assert values["result_count"] == (2 if also_google else 1)
        await record(**values)  # type: ignore[arg-type]
        connection.execute(
            "UPDATE app.source_collections SET github_verified_at = NULL "
            "WHERE workspace_id = %s AND connection_id = %s",
            (WORKSPACE_ID, CONNECTION_ID),
        )
        connection.commit()

    record_with_fence_race = AsyncMock(side_effect=revoke_after_record)
    monkeypatch.setattr(repository, "record", record_with_fence_race)
    try:
        output = await SearchService(repository).search(
            workspace_id=WORKSPACE_ID,
            user_id=USER_ID,
            authorization_version=1,
            search=SearchInput("payment retries", "answer", SearchFilters(), 10),
        )
    finally:
        await engine.dispose()

    record_with_fence_race.assert_awaited_once()
    if also_google:
        assert [hit.candidate.provider for hit in output.ranked] == ["gmail"]
        assert [hit.candidate.provider for hit in output.context] == ["gmail"]
        assert output.insufficient_reason is None
    else:
        assert output.ranked == output.context == ()
        assert output.insufficient_reason == "no_authorized_results"


@pytest.mark.asyncio
async def test_recheck_does_not_accept_a_stale_chunk_of_current_source(
    connection: psycopg.Connection[tuple[object, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _indexed_document(connection)
    engine = create_async_engine(
        _database_url(), connect_args={"server_settings": {"role": "app_api"}}
    )
    repository = SearchRepository(engine)
    record = repository.record

    async def replace_version_after_record(**values: object) -> None:
        assert values["result_count"] == 1
        await record(**values)  # type: ignore[arg-type]
        changed = await FakeConnector().normalize(
            RawItem(
                external_id="repo:1:payments.md",
                payload={
                    "title": "New payment retries",
                    "content": "Payment retry policy changed in the latest document.",
                },
            )
        )
        index_repository = IndexRepository(database_dsn())
        index_repository.enqueue(WORKSPACE_ID, CONNECTION_ID, changed)
        assert IndexingRuntime(index_repository, IndexingPipeline()).run_once(
            "search-current-pair-test"
        )

    monkeypatch.setattr(repository, "record", replace_version_after_record)
    try:
        output = await SearchService(repository).search(
            workspace_id=WORKSPACE_ID,
            user_id=USER_ID,
            authorization_version=1,
            search=SearchInput("payment retries", "results", SearchFilters(), 10),
        )
    finally:
        await engine.dispose()

    assert output.ranked == output.context == ()
    assert output.insufficient_reason is None
