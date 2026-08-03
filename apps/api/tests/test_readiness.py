from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from universal_ai_search.config import Settings
from universal_ai_search.platform.readiness import (
    _probe_database,
    _probe_object_storage,
    _probe_redis,
    _run_probe,
    check_readiness,
)


@pytest.mark.parametrize("exception", [OSError("offline"), TimeoutError()])
async def test_run_probe_sanitizes_dependency_failures(exception: Exception) -> None:
    probe = AsyncMock(side_effect=exception)

    assert await _run_probe(probe) == "unavailable"


async def test_check_readiness_reports_all_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = AsyncMock(return_value=None)
    monkeypatch.setattr("universal_ai_search.platform.readiness._probe_database", probe)
    monkeypatch.setattr("universal_ai_search.platform.readiness._probe_redis", probe)
    monkeypatch.setattr(
        "universal_ai_search.platform.readiness._probe_object_storage", probe
    )

    result = await check_readiness(Settings(environment="test"))

    assert result.status == "ok"
    assert result.dependencies == {
        "database": "ok",
        "redis": "ok",
        "object_storage": "ok",
    }
    assert probe.await_count == 3


async def test_database_probe_executes_query_and_disposes_engine() -> None:
    connection = AsyncMock()
    result = MagicMock()
    result.scalar_one.return_value = "0001_initial_schema"
    connection.execute.return_value = result
    connection_context = MagicMock()
    connection_context.__aenter__ = AsyncMock(return_value=connection)
    connection_context.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.connect.return_value = connection_context
    engine.dispose = AsyncMock()

    with patch(
        "universal_ai_search.platform.readiness.create_async_engine",
        return_value=engine,
    ):
        await _probe_database(Settings(environment="test"))

    connection.execute.assert_awaited_once()
    engine.dispose.assert_awaited_once()


async def test_database_probe_rejects_incompatible_schema() -> None:
    connection = AsyncMock()
    result = MagicMock()
    result.scalar_one.return_value = "old_revision"
    connection.execute.return_value = result
    connection_context = MagicMock()
    connection_context.__aenter__ = AsyncMock(return_value=connection)
    connection_context.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.connect.return_value = connection_context
    engine.dispose = AsyncMock()

    with (
        patch(
            "universal_ai_search.platform.readiness.create_async_engine",
            return_value=engine,
        ),
        pytest.raises(RuntimeError, match="schema revision"),
    ):
        await _probe_database(Settings(environment="test"))

    engine.dispose.assert_awaited_once()


async def test_redis_probe_pings_and_closes_client() -> None:
    client = AsyncMock()

    with patch(
        "universal_ai_search.platform.readiness.Redis.from_url",
        return_value=client,
    ):
        await _probe_redis(Settings(environment="test"))

    client.ping.assert_awaited_once()
    client.aclose.assert_awaited_once()


async def test_object_storage_probe_uses_minio_readiness_endpoint() -> None:
    response = MagicMock()
    client = AsyncMock()
    client.get.return_value = response
    client_context = MagicMock()
    client_context.__aenter__ = AsyncMock(return_value=client)
    client_context.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "universal_ai_search.platform.readiness.httpx.AsyncClient",
        return_value=client_context,
    ):
        await _probe_object_storage(
            Settings(
                environment="test",
                object_storage_endpoint="http://storage.example/",
            )
        )

    client.get.assert_awaited_once_with("http://storage.example/minio/health/ready")
    response.raise_for_status.assert_called_once()
