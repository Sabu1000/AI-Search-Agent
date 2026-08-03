import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

import httpx
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from universal_ai_search import __version__
from universal_ai_search.config import Settings

Probe = Callable[[], Awaitable[None]]


class ReadinessResponse(BaseModel):
    service: str
    status: Literal["ok", "degraded"]
    version: str
    dependencies: dict[str, Literal["ok", "unavailable"]]


async def _probe_database(settings: Settings) -> None:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


async def _probe_redis(settings: Settings) -> None:
    client = Redis.from_url(settings.redis_url, socket_connect_timeout=1)
    try:
        await client.ping()
    finally:
        await client.aclose()


async def _probe_object_storage(settings: Settings) -> None:
    async with httpx.AsyncClient(timeout=1) as client:
        response = await client.get(
            f"{settings.object_storage_endpoint.rstrip('/')}/minio/health/ready"
        )
        response.raise_for_status()


async def _run_probe(probe: Probe) -> Literal["ok", "unavailable"]:
    try:
        await probe()
    except Exception:  # Dependency details belong in sanitized structured logs later.
        return "unavailable"
    return "ok"


async def check_readiness(settings: Settings) -> ReadinessResponse:
    probes: dict[str, Probe] = {
        "database": lambda: _probe_database(settings),
        "redis": lambda: _probe_redis(settings),
        "object_storage": lambda: _probe_object_storage(settings),
    }
    results = await asyncio.gather(*(_run_probe(probe) for probe in probes.values()))
    dependencies = dict(zip(probes, results, strict=True))
    status: Literal["ok", "degraded"] = (
        "ok" if all(value == "ok" for value in dependencies.values()) else "degraded"
    )
    return ReadinessResponse(
        service="api",
        status=status,
        version=__version__,
        dependencies=dependencies,
    )
