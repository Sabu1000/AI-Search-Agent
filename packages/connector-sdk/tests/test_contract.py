from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from uas_connector_sdk import (
    Change,
    ContractViolationError,
    CursorAdvanced,
    DeleteSource,
    Provider,
    SyncContext,
    validate_change_stream,
)
from uas_connector_sdk.testing import FakeConnector


def context() -> SyncContext:
    return SyncContext(workspace_id=uuid4(), connection_id=uuid4(), sync_job_id=uuid4())


async def stream(*changes: Change) -> AsyncIterator[Change]:
    for change in changes:
        yield change


async def test_fake_full_and_incremental_streams_pass_contract() -> None:
    connector = FakeConnector()
    full = await validate_change_stream(
        connector.full_sync(context()), expected_provider=Provider.GITHUB
    )
    incremental = await validate_change_stream(
        connector.incremental_sync(context(), {"position": 2}),
        expected_provider=Provider.GITHUB,
    )
    assert [change.type for change in full] == [
        "UPSERT",
        "PERMISSION_CHANGED",
        "DELETE",
        "CURSOR_ADVANCED",
    ]
    assert [change.type for change in incremental] == ["DELETE", "CURSOR_ADVANCED"]


async def test_contract_rejects_provider_mismatch() -> None:
    change = CursorAdvanced(provider=Provider.GMAIL, change_id="cursor", cursor={"position": 1})
    with pytest.raises(ContractViolationError, match="another provider"):
        await validate_change_stream(stream(change), expected_provider=Provider.GITHUB)


async def test_contract_rejects_duplicate_change_id() -> None:
    first = DeleteSource(provider=Provider.GITHUB, change_id="same", external_id="1")
    second = CursorAdvanced(provider=Provider.GITHUB, change_id="same", cursor={"position": 1})
    with pytest.raises(ContractViolationError, match="duplicate"):
        await validate_change_stream(stream(first, second), expected_provider=Provider.GITHUB)


async def test_contract_requires_terminal_cursor() -> None:
    change = DeleteSource(provider=Provider.GITHUB, change_id="delete", external_id="1")
    with pytest.raises(ContractViolationError, match="did not finish"):
        await validate_change_stream(stream(change), expected_provider=Provider.GITHUB)


async def test_contract_rejects_change_after_cursor() -> None:
    cursor = CursorAdvanced(provider=Provider.GITHUB, change_id="cursor", cursor={"position": 1})
    change = DeleteSource(provider=Provider.GITHUB, change_id="delete", external_id="1")
    with pytest.raises(ContractViolationError, match="final change"):
        await validate_change_stream(stream(cursor, change), expected_provider=Provider.GITHUB)
