"""Runtime validation at the connector/orchestrator boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator

from .errors import ContractViolationError
from .models import Change, CursorAdvanced, Provider


async def validate_change_stream(
    stream: AsyncIterator[Change], *, expected_provider: Provider
) -> list[Change]:
    """Validate provider identity, unique changes, and terminal cursor ordering."""

    validated: list[Change] = []
    change_ids: set[str] = set()
    cursor_seen = False
    async for change in stream:
        if change.provider != expected_provider:
            raise ContractViolationError("Connector emitted a change for another provider")
        if change.change_id in change_ids:
            raise ContractViolationError("Connector emitted a duplicate change_id")
        if cursor_seen:
            raise ContractViolationError("CURSOR_ADVANCED must be the final change")
        change_ids.add(change.change_id)
        validated.append(change)
        cursor_seen = isinstance(change, CursorAdvanced)
    if not cursor_seen:
        raise ContractViolationError("Connector stream did not finish with CURSOR_ADVANCED")
    return validated
