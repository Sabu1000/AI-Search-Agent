"""Protocol every provider connector implements."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from .models import (
    Change,
    Credentials,
    HealthResult,
    JsonObject,
    NormalizedDocument,
    Provider,
    RawItem,
    SyncContext,
)


class Connector(Protocol):
    provider: Provider

    async def authorize_url(self, state: str) -> str: ...

    async def exchange_code(self, code: str) -> Credentials: ...

    async def refresh_credentials(self, credentials: Credentials) -> Credentials: ...

    def full_sync(self, ctx: SyncContext) -> AsyncIterator[Change]: ...

    def incremental_sync(self, ctx: SyncContext, cursor: JsonObject) -> AsyncIterator[Change]: ...

    async def fetch_item(self, external_id: str) -> RawItem: ...

    async def normalize(self, item: RawItem) -> NormalizedDocument: ...

    async def health_check(self) -> HealthResult: ...
