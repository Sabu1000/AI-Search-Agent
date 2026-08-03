"""Deterministic fake connector and assertions for connector authors."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

from pydantic import JsonValue

from .errors import AuthenticationError, CursorInvalidError, MalformedItemError, RateLimitError
from .models import (
    AccessMetadata,
    Change,
    Credentials,
    CursorAdvanced,
    DeleteSource,
    HealthResult,
    HealthStatus,
    JsonObject,
    NormalizedDocument,
    PermissionChanged,
    Provider,
    RawItem,
    SyncContext,
    UpsertSource,
    make_change_id,
)


class FakeConnector:
    """Small reference implementation that exercises the full SDK lifecycle."""

    provider = Provider.GITHUB

    def __init__(self) -> None:
        self.rate_limit_once = False
        self._rate_limited = False

    async def authorize_url(self, state: str) -> str:
        return f"https://example.test/oauth/authorize?state={state}"

    async def exchange_code(self, code: str) -> Credentials:
        if code == "invalid":
            raise AuthenticationError()
        return Credentials(access_token=f"access-{code}", refresh_token="refresh-token")

    async def refresh_credentials(self, credentials: Credentials) -> Credentials:
        if credentials.refresh_token is None:
            raise AuthenticationError("Refresh credential is unavailable")
        return Credentials(access_token="refreshed", refresh_token=credentials.refresh_token)

    async def _changes(self, cursor_position: int) -> AsyncIterator[Change]:
        if self.rate_limit_once and not self._rate_limited:
            self._rate_limited = True
            raise RateLimitError(0)
        if cursor_position < 0 or cursor_position > 2:
            raise CursorInvalidError()
        if cursor_position == 0:
            document = await self.normalize(
                RawItem(
                    external_id="repo:1:README.md",
                    payload={"title": "README", "content": "Connector SDK reference"},
                )
            )
            yield UpsertSource(
                provider=self.provider,
                change_id=make_change_id(
                    self.provider, document.external_id, document.content_hash
                ),
                document=document,
            )
        if cursor_position <= 1:
            yield PermissionChanged(
                provider=self.provider,
                change_id=make_change_id(self.provider, "repo:1:README.md", "permissions-v2"),
                external_id="repo:1:README.md",
                access_metadata=AccessMetadata(user_ids=("user:1",)),
            )
        if cursor_position <= 2:
            yield DeleteSource(
                provider=self.provider,
                change_id=make_change_id(self.provider, "repo:1:old.md", "deleted-v1"),
                external_id="repo:1:old.md",
            )
        yield CursorAdvanced(
            provider=self.provider,
            change_id=make_change_id(self.provider, "cursor", "3"),
            cursor={"position": 3},
        )

    def full_sync(self, ctx: SyncContext) -> AsyncIterator[Change]:
        del ctx
        return self._changes(0)

    def incremental_sync(self, ctx: SyncContext, cursor: JsonObject) -> AsyncIterator[Change]:
        del ctx
        position = cursor.get("position")
        if not isinstance(position, int):

            async def invalid() -> AsyncIterator[Change]:
                raise CursorInvalidError()
                yield  # pragma: no cover

            return invalid()
        return self._changes(position)

    async def fetch_item(self, external_id: str) -> RawItem:
        if external_id == "malformed":
            raise MalformedItemError()
        return RawItem(external_id=external_id, payload={"title": external_id, "content": "body"})

    async def normalize(self, item: RawItem) -> NormalizedDocument:
        title = item.payload.get("title")
        content = item.payload.get("content")
        if not isinstance(title, str) or not isinstance(content, str):
            raise MalformedItemError()
        now = datetime(2026, 1, 1, tzinfo=UTC)
        return NormalizedDocument(
            external_id=item.external_id,
            provider=self.provider,
            source_type="repository_file",
            title=title,
            content=content,
            canonical_url=f"https://example.test/items/{item.external_id}",
            mime_type="text/markdown",
            created_at=now,
            modified_at=now,
            provider_metadata={"fixture": True},
        )

    async def health_check(self) -> HealthResult:
        return HealthResult(status=HealthStatus.HEALTHY, latency_ms=1, detail="fake provider")


def assert_credentials_are_redacted(credentials: Credentials) -> None:
    serialized = credentials.model_dump_json()
    representation = repr(credentials)
    for field in (credentials.access_token, credentials.refresh_token):
        if field is not None and field.get_secret_value() in serialized + representation:
            raise AssertionError("credential was exposed by serialization or repr")


def raw_item(**payload: JsonValue) -> RawItem:
    """Convenience fixture for third-party connector contract suites."""

    return RawItem(external_id="fixture:1", payload=payload)
