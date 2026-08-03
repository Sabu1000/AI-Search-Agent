from uuid import uuid4

import pytest

from uas_connector_sdk import (
    AuthenticationError,
    ConnectorRegistry,
    ContractViolationError,
    CursorInvalidError,
    MalformedItemError,
    Provider,
    RateLimitError,
    SyncContext,
)
from uas_connector_sdk.testing import FakeConnector, assert_credentials_are_redacted, raw_item


def context() -> SyncContext:
    return SyncContext(workspace_id=uuid4(), connection_id=uuid4(), sync_job_id=uuid4())


def test_registry_registers_and_creates_connector() -> None:
    registry = ConnectorRegistry()
    registry.register(Provider.GITHUB, FakeConnector)
    assert registry.providers == (Provider.GITHUB,)
    assert registry.create(Provider.GITHUB).provider == Provider.GITHUB
    with pytest.raises(ContractViolationError, match="already registered"):
        registry.register(Provider.GITHUB, FakeConnector)
    with pytest.raises(ContractViolationError, match="No connector"):
        registry.create(Provider.GMAIL)


def test_registry_rejects_factory_for_wrong_provider() -> None:
    registry = ConnectorRegistry()
    with pytest.raises(ContractViolationError, match="wrong provider"):
        registry.register(Provider.GMAIL, FakeConnector)


async def test_fake_connector_authentication_and_redaction() -> None:
    connector = FakeConnector()
    assert "state=csrf" in await connector.authorize_url("csrf")
    credentials = await connector.exchange_code("valid")
    assert_credentials_are_redacted(credentials)
    refreshed = await connector.refresh_credentials(credentials)
    assert refreshed.access_token.get_secret_value() == "refreshed"
    with pytest.raises(AuthenticationError):
        await connector.exchange_code("invalid")
    without_refresh = credentials.model_copy(update={"refresh_token": None})
    with pytest.raises(AuthenticationError, match="unavailable"):
        await connector.refresh_credentials(without_refresh)


async def test_fake_connector_fetch_normalize_and_health() -> None:
    connector = FakeConnector()
    item = await connector.fetch_item("item:1")
    normalized = await connector.normalize(item)
    assert normalized.external_id == "item:1"
    assert normalized.provider == Provider.GITHUB
    assert (await connector.health_check()).status == "healthy"
    with pytest.raises(MalformedItemError):
        await connector.fetch_item("malformed")
    with pytest.raises(MalformedItemError):
        await connector.normalize(raw_item(title=123, content=[]))


async def test_fake_connector_cursor_and_rate_limit_failures() -> None:
    connector = FakeConnector()
    with pytest.raises(CursorInvalidError):
        async for _ in connector.incremental_sync(context(), {"position": "bad"}):
            pass
    with pytest.raises(CursorInvalidError):
        async for _ in connector.incremental_sync(context(), {"position": 99}):
            pass
    connector.rate_limit_once = True
    with pytest.raises(RateLimitError):
        async for _ in connector.full_sync(context()):
            pass
    changes = [change async for change in connector.full_sync(context())]
    assert changes[-1].type == "CURSOR_ADVANCED"
