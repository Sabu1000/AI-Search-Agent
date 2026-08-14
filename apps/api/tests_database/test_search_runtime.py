from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
import pytest
from conftest import database_dsn
from sqlalchemy.ext.asyncio import create_async_engine
from test_indexing_runtime import CONNECTION_ID, USER_ID, WORKSPACE_ID, seed_workspace
from uas_connector_sdk import RawItem
from uas_connector_sdk.testing import FakeConnector

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
    repository = IndexRepository(database_dsn())
    repository.enqueue(WORKSPACE_ID, CONNECTION_ID, document)
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
